"""
GEX calculation engine — pure functions, no I/O, no DB.

Formula (from spec / carried over from the existing personal tool):

    GEX per contract = reference_price² × gamma × open_interest
                       × contract_multiplier × 0.01

US callers retain the historical defaults of spot as the reference price and
a multiplier of 100. Provider adapters may supply a futures reference price
and the instrument's actual multiplier (for example, NIFTY Black-76 inputs).

Sign convention:
    CALL  →  GEX is positive  (dealer long gamma — stabilising)
    PUT   →  GEX is negative  (dealer short gamma — amplifying)

⚠️  VERIFY BEFORE GOING LIVE: this formula is implemented exactly as written
    in docs/spec/02-data-pipeline-gex-collection.md.  Cross-check it against
    your existing personal-tool implementation before running on live data.
    Pay particular attention to:
      - The contract_multiplier (100 is the backward-compatible default)
      - The 0.01 factor (makes the formula equivalent to spot² × gamma × OI)
      - Sign convention for puts (negative, not zero)
      - Whether you want GEX in notional dollars or normalised units

Input shape (one record per option contract — any expiry, any source):
    {
        "type":          "C" | "P",
        "strike":        float,
        "expiry":        datetime.date,
        "gamma":         float,
        "delta":         float,
        "theta":         float,
        "vega":          float,
        "iv":            float,   # decimal, e.g. 0.25 = 25 %
        "open_interest": int,
    }

Output shape:
    {
        "net_gex":      float,           # sum of all per-strike GEX values
        "call_wall":    float | None,    # strike with highest positive GEX
        "put_wall":     float | None,    # strike with highest negative GEX
        "gex_by_strike": [               # sorted by strike ascending
            {
                "strike":      float,
                "call_gex":    float,    # positive
                "put_gex":     float,    # negative
                "call_greeks": {delta, gamma, theta, vega, iv, open_interest},
                "put_greeks":  {delta, gamma, theta, vega, iv, open_interest},
            },
            ...
        ],
    }
"""
import logging
from collections import defaultdict
from datetime import date, datetime

log = logging.getLogger(__name__)


# ── Public API ─────────────────────────────────────────────────────────────

def compute(
    options: list[dict],
    spot_price: float,
    *,
    reference_price: float | None = None,
) -> dict:
    """Compute GEX from a normalised list of option contracts.

    Contracts across all expiries are aggregated by strike.  Greeks are
    OI-weighted averages; open_interest is summed across expiries.

    Returns the output shape documented at the top of this module.
    """
    empty = {
        "net_gex": 0.0,
        "call_wall": None,
        "put_wall": None,
        "gex_by_strike": [],
        "gex_by_expiration": [],
        "gex_by_expiry_strike": [],
        "options_slice": [],
    }
    if not options:
        return empty

    calculation_price = float(reference_price or spot_price)
    if calculation_price <= 0:
        raise ValueError("GEX calculation price must be positive")

    # Accumulate per-strike, per-type data
    # Structure: strike → {"C": {...}, "P": {...}}
    buckets: dict[float, dict] = defaultdict(lambda: {
        "C": {"gex": 0.0, "oi": 0, "gamma": 0.0, "delta": 0.0,
              "theta": 0.0, "vega": 0.0, "iv": 0.0, "_weighted_sum": 0},
        "P": {"gex": 0.0, "oi": 0, "gamma": 0.0, "delta": 0.0,
              "theta": 0.0, "vega": 0.0, "iv": 0.0, "_weighted_sum": 0},
    })
    # expiry → strike → {"C": gex, "P": gex}
    expiry_buckets: dict = defaultdict(lambda: defaultdict(lambda: {"C": 0.0, "P": 0.0}))
    # expiry → {"C": gex, "P": gex}
    expiry_totals: dict = defaultdict(lambda: {"C": 0.0, "P": 0.0})
    options_slice: list[dict] = []

    for opt in options:
        opt_type = opt.get("type")
        if opt_type not in ("C", "P"):
            continue

        strike = float(opt.get("strike", 0.0))
        oi = int(opt.get("open_interest", 0))
        gamma_value = opt.get("gamma")
        if gamma_value is None and oi > 0:
            raise ValueError(
                f"Missing gamma for non-zero-OI contract at strike {strike:g}"
            )
        gamma = float(gamma_value or 0.0)
        contract_multiplier = float(opt.get("contract_multiplier", 100))
        contract_reference_price = float(
            opt.get("reference_price") or calculation_price
        )
        expiry = _normalise_expiry(opt.get("expiry"))

        gex = _per_contract_gex(
            contract_reference_price, gamma, oi, contract_multiplier
        )
        if opt_type == "P":
            gex = -abs(gex)  # puts are always negative

        b = buckets[strike][opt_type]
        b["gex"] += gex
        b["oi"] += oi

        if expiry is not None:
            expiry_buckets[expiry][strike][opt_type] += gex
            expiry_totals[expiry][opt_type] += gex

        slice_row = {
            "type": opt_type,
            "strike": strike,
            "expiry": expiry.isoformat() if expiry else None,
            "gex": gex,
            "gamma": gamma,
            "delta": float(opt.get("delta", 0.0)),
            "theta": float(opt.get("theta", 0.0)),
            "vega": float(opt.get("vega", 0.0)),
            "iv": float(opt.get("iv", 0.0)),
            "open_interest": oi,
            "volume": int(opt.get("volume", 0)),
        }
        # Preserve the exact legacy US options_slice shape. Provider-native
        # contracts opt in to the extended audit fields by supplying a
        # reference price or multiplier (the Zerodha adapter supplies both).
        if "reference_price" in opt or "contract_multiplier" in opt:
            slice_row.update({
                "rho": float(opt.get("rho", 0.0)),
                "contract_multiplier": contract_multiplier,
                "reference_price": contract_reference_price,
                "exchange": opt.get("exchange"),
                "tradingsymbol": opt.get("tradingsymbol"),
                "instrument_token": opt.get("instrument_token"),
                "market_price": opt.get("market_price"),
                "market_price_source": opt.get("market_price_source"),
                "parity_residual": opt.get("parity_residual"),
                "parity_residual_pct": opt.get("parity_residual_pct"),
                "atm_iv": opt.get("atm_iv"),
                "quote_timestamp": opt.get("quote_timestamp"),
            })
        options_slice.append(slice_row)

        # Accumulate for OI-weighted greek averages
        weight = oi or 1  # avoid zero-weight division later
        b["_weighted_sum"] += weight
        for greek in ("gamma", "delta", "theta", "vega", "iv"):
            b[greek] += float(opt.get(greek) or 0.0) * weight

    # Build sorted gex_by_strike list
    gex_by_strike = []
    for strike in sorted(buckets):
        call_b = buckets[strike]["C"]
        put_b = buckets[strike]["P"]
        gex_by_strike.append({
            "strike": strike,
            "call_gex": call_b["gex"],
            "put_gex": put_b["gex"],
            "call_greeks": _finalise_greeks(call_b),
            "put_greeks": _finalise_greeks(put_b),
        })

    net_gex = sum(r["call_gex"] + r["put_gex"] for r in gex_by_strike)

    # Call wall: strike with the largest positive call GEX
    call_wall = max(gex_by_strike, key=lambda r: r["call_gex"])["strike"] if gex_by_strike else None
    # Put wall: strike with the largest-magnitude negative put GEX
    put_wall = min(gex_by_strike, key=lambda r: r["put_gex"])["strike"] if gex_by_strike else None

    gex_by_expiration = []
    for expiry in sorted(expiry_totals):
        call_gex = expiry_totals[expiry]["C"]
        put_gex = expiry_totals[expiry]["P"]
        gex_by_expiration.append({
            "expiry": expiry.isoformat(),
            "call_gex": call_gex,
            "put_gex": put_gex,
            "net_gex": call_gex + put_gex,
        })

    gex_by_expiry_strike = []
    for expiry in sorted(expiry_buckets):
        for strike in sorted(expiry_buckets[expiry]):
            call_gex = expiry_buckets[expiry][strike]["C"]
            put_gex = expiry_buckets[expiry][strike]["P"]
            gex_by_expiry_strike.append({
                "expiry": expiry.isoformat(),
                "strike": strike,
                "gex": call_gex + put_gex,
            })

    return {
        "net_gex": net_gex,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "gex_by_strike": gex_by_strike,
        "gex_by_expiration": gex_by_expiration,
        "gex_by_expiry_strike": gex_by_expiry_strike,
        "options_slice": options_slice,
    }


# ── Internal helpers ───────────────────────────────────────────────────────

def _per_contract_gex(
    reference_price: float,
    gamma: float,
    open_interest: int,
    contract_multiplier: float = 100,
) -> float:
    """Single-contract GEX (unsigned — caller applies sign convention).

    Formula: reference² × gamma × OI × contract_multiplier × 0.01

    ⚠️  Verify this against your existing personal tool before going live.
    """
    return (
        reference_price
        * gamma
        * open_interest
        * contract_multiplier
        * reference_price
        * 0.01
    )


def _normalise_expiry(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _finalise_greeks(bucket: dict) -> dict:
    """Produce the final per-strike greeks dict from an accumulated bucket."""
    ws = bucket["_weighted_sum"] or 1  # avoid div-by-zero
    return {
        "delta": bucket["delta"] / ws,
        "gamma": bucket["gamma"] / ws,
        "theta": bucket["theta"] / ws,
        "vega": bucket["vega"] / ws,
        "iv": bucket["iv"] / ws,
        "open_interest": bucket["oi"],
    }
