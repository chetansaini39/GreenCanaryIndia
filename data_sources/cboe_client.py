"""
CBOE Delayed Quotes API client.

No authentication required (public endpoint).
Data is ~15–20 minutes behind real-time.

URL pattern:
  Indices (SPX, NDX):  _<SYMBOL>.json  (fallback: <SYMBOL>.json)
  ETFs/stocks (QQQ, SPY, …):  <SYMBOL>.json only

Option symbol format (OCC-derived):
  "SPX  240419C05430"
  └──┘  └────┘└┘└───┘
  ticker expiry type strike
  ticker is padded with spaces; expiry is YYMMDD; strike is integer dollars
  (full 8-digit OCC form: thousandths — e.g. 05430000 → 5430.000)
"""
import re
import logging
from datetime import date, datetime

import requests

from data_sources.retry import with_retry

log = logging.getLogger(__name__)

_BASE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options"
_TIMEOUT = 15  # seconds

# CBOE uses an underscore-prefixed path only for these index symbols.
_UNDERSCORE_PREFIX_SYMBOLS = frozenset({"SPX", "NDX"})

# Spaced OCC form: "SPX  240419C05430"
_SYMBOL_RE = re.compile(
    r"^(?P<ticker>[A-Z^.\d]+)\s+(?P<expiry>\d{6})(?P<type>[CP])(?P<strike>\d+)$"
)
# Compact form: "SPX260618C00200000" (SPX, QQQ, SPY, …)
_COMPACT_SYMBOL_RE = re.compile(
    r"^(?P<ticker>[A-Z^.\d]+)(?P<expiry>\d{6})(?P<type>[CP])(?P<strike>\d+)$"
)


# ── Public API ─────────────────────────────────────────────────────────────

def get_option_chain(symbol: str) -> dict:
    """Fetch and normalise the CBOE delayed option chain for a symbol.

    SPX and NDX use the underscore-prefixed URL first; all other symbols
    (QQQ, SPY, …) use the plain ticker path.

    Args:
        symbol: underlying ticker, with or without a leading ``$``
                (e.g. "SPX", "$SPX", "QQQ").

    Returns:
        {"options": [<normalised records>], "spot_price": float}
    """
    raw = _fetch_with_fallback(symbol)
    return _normalise_chain(raw, _normalize_symbol(symbol))


# ── Fetch helpers ──────────────────────────────────────────────────────────

def _normalize_symbol(symbol: str) -> str:
    return symbol.lstrip("$").upper()


def _cboe_urls(symbol: str) -> list[str]:
    sym = _normalize_symbol(symbol)
    plain = f"{_BASE_URL}/{sym}.json"
    if sym in _UNDERSCORE_PREFIX_SYMBOLS:
        return [f"{_BASE_URL}/_{sym}.json", plain]
    return [plain]


def _fetch_json(url: str) -> dict:
    def _get():
        resp = requests.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    return with_retry(_get, label=f"cboe.fetch({url})")


def _fetch_with_fallback(symbol: str) -> dict:
    urls = _cboe_urls(symbol)
    last_exc: Exception | None = None

    for i, url in enumerate(urls):
        try:
            raw = _fetch_json(url)
            # Validate we got usable data before declaring success
            _spot_from_raw(raw)  # raises KeyError/ValueError if malformed
            return raw
        except Exception as exc:
            last_exc = exc
            if i < len(urls) - 1:
                log.warning(
                    "CBOE URL failed for %s (%s). Trying fallback …", symbol, exc
                )

    assert last_exc is not None
    raise last_exc


# ── Normalisation ──────────────────────────────────────────────────────────

def _normalise_chain(raw: dict, symbol: str) -> dict:
    """Parse CBOE JSON into the common normalised option record shape.

    TODO: verify field names against live CBOE response. The documented
    shape is raw["data"]["options"] = [{option, bid, ask, iv, delta,
    gamma, theta, vega, open_interest, volume, ...}].
    """
    data = raw.get("data", raw)  # some CBOE endpoints nest under "data"
    spot_price = _spot_from_raw(raw)
    records: list[dict] = []

    for entry in data.get("options", []):
        opt_symbol = entry.get("option", "")
        try:
            parsed = _parse_option_symbol(opt_symbol)
        except ValueError as exc:
            log.debug("Skipping unparseable CBOE symbol %r: %s", opt_symbol, exc)
            continue

        try:
            records.append({
                "type": parsed["type"],
                "strike": parsed["strike"],
                "expiry": parsed["expiry"],
                # TODO: confirm exact field names from live CBOE response
                "gamma": float(entry.get("gamma", 0.0)),
                "delta": float(entry.get("delta", 0.0)),
                "theta": float(entry.get("theta", 0.0)),
                "vega": float(entry.get("vega", 0.0)),
                "iv": float(entry.get("iv", 0.0)),
                "open_interest": int(entry.get("open_interest", 0)),
            })
        except (TypeError, ValueError) as exc:
            log.debug("Skipping malformed CBOE option record: %s", exc)

    return {"options": records, "spot_price": spot_price}


def _spot_from_raw(raw: dict) -> float:
    """Extract spot price from the CBOE response.

    TODO: verify the exact field name against live CBOE response.
    Common candidates: raw["data"]["current_price"], raw["data"]["close"],
    raw["timestamp"]["close"].
    """
    data = raw.get("data", raw)
    for key in ("current_price", "close", "last", "price"):
        val = data.get(key)
        if val is not None:
            return float(val)
    raise KeyError(f"Cannot find spot price in CBOE response keys: {list(data.keys())}")


def _parse_option_symbol(sym: str) -> dict:
    """Parse an OCC-style option symbol into type, strike, and expiry.

    Handles:
      - Compact CBOE form: "SPX260618C00200000", "QQQ260618C00174780"
      - Spaced form:       "SPX  240419C05430"
      - Full OCC form:     "SPX   240419C05430000" (8-digit strike = thousandths)
    """
    text = sym.strip()
    m = _SYMBOL_RE.match(text) or _COMPACT_SYMBOL_RE.match(text)
    if not m:
        raise ValueError(f"Unrecognisable option symbol: {sym!r}")

    expiry = datetime.strptime(m.group("expiry"), "%y%m%d").date()

    raw_strike = m.group("strike")
    if len(raw_strike) == 8:
        strike = int(raw_strike) / 1000.0
    else:
        strike = float(raw_strike)

    return {"type": m.group("type"), "strike": strike, "expiry": expiry}
