from pymongo import ASCENDING
from app.utils.time import now_ct


COLLECTION = "symbols_config"

NIFTY_CONFIG = {
    "symbol": "NIFTY",
    "tier": "paid",
    "asset_type": "index",
    "weekly_expiry": False,
    "provider": "zerodha",
    "market": "nse",
    "market_timezone": "Asia/Kolkata",
    "spot_instrument": "NSE:NIFTY 50",
    "spot_exchange": "NSE",
    "derivatives_exchange": "NFO",
    "underlying_name": "NIFTY",
    "pricing_model": "black76",
    "currency": "INR",
    "display_unit": "crore",
    "risk_free_rate": 0.055,
}


def ensure_indexes(db):
    col = db[COLLECTION]
    col.create_index("symbol", unique=True)
    col.create_index("tier")
    col.create_index("active")


# ── Write ──────────────────────────────────────────────────────────────────

def upsert(db, doc: dict):
    """Insert or update the config entry for a symbol."""
    doc.setdefault("added_at", now_ct())
    doc.setdefault("active", True)
    key = {"symbol": doc["symbol"]}
    db[COLLECTION].replace_one(key, doc, upsert=True)


def set_active(db, symbol: str, active: bool):
    db[COLLECTION].update_one({"symbol": symbol}, {"$set": {"active": active}})


# ── Read ───────────────────────────────────────────────────────────────────

def find_by_symbol(db, symbol: str) -> dict | None:
    return db[COLLECTION].find_one({"symbol": symbol})


def find_all_active(db) -> list[dict]:
    return list(db[COLLECTION].find({"active": True}).sort("symbol", ASCENDING))


def find_by_tier(db, tier: str, active_only: bool = True) -> list[dict]:
    query: dict = {"tier": tier}
    if active_only:
        query["active"] = True
    return list(db[COLLECTION].find(query).sort("symbol", ASCENDING))


def find_all(db) -> list[dict]:
    """All symbol config entries (active and inactive) for admin management."""
    return list(db[COLLECTION].find().sort("symbol", ASCENDING))


def delete_by_symbol(db, symbol: str):
    db[COLLECTION].delete_one({"symbol": symbol})
