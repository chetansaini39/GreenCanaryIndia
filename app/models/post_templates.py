from pymongo import ASCENDING
from bson import ObjectId
from app.utils.time import now_ct

COLLECTION = "post_templates"

# Delimiter between tweets in a thread template
TWEET_SEP = "\n---\n"

TEMPLATE_VERSIONS = {"premarket": 2, "eod": 2, "eow": 2}

_DEFAULTS: dict[str, str] = {
    "premarket": (
        "SPX GEX Levels — [DATE] 🗺️\n"
        "\n"
        "Regime: [REGIME] Gamma\n"
        "Spot: [SPOT] | Net GEX: [NET_GEX]B\n"
        "\n"
        "📍 Gamma flip: [GAMMA_FLIP]\n"
        "🟢 Call wall: [CALL_WALL]\n"
        "🔴 Put wall: [PUT_WALL]\n"
        "⚠️ Hot zone: [HOT_ZONE]\n"
        "\n"
        "Full EOD breakdown: [SITE_URL]/today\n"
        "#SPX #GEX #0DTE #OptionsFlow"
    ),
    # Version 2 mirrors Market Research.xlsx / EOD-Twitter Post / B1:B60.
    "eod": TWEET_SEP.join([
        (
            "SPX GEX EOD Report — [DATE] 🧠\n"
            "\n"
            "Dealers are net [DEALER_POSITION] gamma today.\n"
            "\n"
            "What that means for tomorrow 👇"
        ),
        (
            "📊 Today's Gamma Regime: [REGIME]\n"
            "\n"
            "Net GEX: [NET_GEX_B]\n"
            "\n"
            "→ [REGIME_EXPLANATION]\n"
            "\n"
            "The line in the sand: [GAMMA_FLIP]\n"
            "Above it = calm. Below it = watch out."
        ),
        (
            "🎯 Key Strikes to Watch:\n"
            "\n"
            "🟢 Call Wall (ceiling): [CALL_WALL] → +$[CALL_WALL_GEX_B] GEX\n"
            "   Dealers SELL into any rally here. Hard resistance.\n"
            "\n"
            "🔴 Put Wall (floor): [PUT_WALL] → -$[PUT_WALL_GEX_B] GEX\n"
            "   Dealers BUY into any drop here. Strong support.\n"
            "\n"
            "⚠️ Hot Zone: [HOT_ZONE] → -$[HOT_ZONE_GEX_B] GEX\n"
            "   [HOT_ZONE_EXPLANATION]"
        ),
        (
            "Tomorrow's Playbook:\n"
            "\n"
            "🟢 BULL CASE: SPX holds above [BULL_TRIGGER]\n"
            "→ [BULL_BEHAVIOR]\n"
            "→ Upside target: [UPSIDE_TARGET]\n"
            "\n"
            "🔴 BEAR CASE: SPX breaks below [BEAR_TRIGGER]\n"
            "→ [BEAR_BEHAVIOR]\n"
            "→ Downside target: [DOWNSIDE_TARGET]\n"
            "\n"
            "⚡ WILDCARD: Watch [WILDCARD_LEVEL] closely at open. [WILDCARD_COMMENTARY]"
        ),
        (
            "Bottom Line:\n"
            "\n"
            "[VERDICT]\n"
            "\n"
            "📌 Save this. Check back tomorrow EOD for the update.\n"
            "\n"
            "Follow for daily GEX maps 🗺️\n"
            "#SPX #0DTE #GEX #OptionsFlow #MarketStructure"
        ),
    ]),
    "eow": TWEET_SEP.join([
        (
            "SPX Weekly GEX Wrap — Week of [WEEK_OF] 📊\n"
            "\n"
            "Spot: [SPOT] | Weekly Net GEX: [NET_GEX]B\n"
            "Regime: [REGIME]\n"
            "\n"
            "Weekly chart attached 👇"
        ),
        (
            "Key Levels for Next Week:\n"
            "\n"
            "📍 Gamma flip: [GAMMA_FLIP]\n"
            "🟢 Call wall: [CALL_WALL]\n"
            "🔴 Put wall: [PUT_WALL]\n"
            "⚠️ Hot zone: [HOT_ZONE]"
        ),
        (
            "Dealer Positioning:\n"
            "\n"
            "[REGIME_LONG_TEXT]"
        ),
        (
            "Next-Week Setup:\n"
            "\n"
            "Above [GAMMA_FLIP] = dealers dampen moves.\n"
            "Below [GAMMA_FLIP] = dealers amplify moves.\n"
            "\n"
            "Resistance: [CALL_WALL]\n"
            "Support: [PUT_WALL]"
        ),
        (
            "The map in one line:\n"
            "\n"
            "Net GEX: [NET_GEX]B\n"
            "Spot: [SPOT]\n"
            "Flip: [GAMMA_FLIP]\n"
            "\n"
            "Full data: [SITE_URL]/snapshot/SPX/[WEEK_OF]"
        ),
        (
            "Save the map. Next EOD report: Monday after close.\n"
            "\n"
            "Follow to never trade without the map 🗺️\n"
            "#SPX #GEX #OptionsFlow #WeeklyOptions #MarketStructure"
        ),
    ]),
}


def ensure_indexes(db) -> None:
    col = db[COLLECTION]
    col.create_index([("post_type", ASCENDING), ("platform", ASCENDING)], unique=True)
    # Seed defaults — $setOnInsert is a no-op if the document already exists
    now = now_ct()
    for post_type, text in _DEFAULTS.items():
        col.update_one(
            {"post_type": post_type, "platform": "twitter"},
            {"$setOnInsert": {
                "post_type": post_type,
                "platform": "twitter",
                "template_text": text,
                "template_version": TEMPLATE_VERSIONS[post_type],
                "updated_by": None,
                "updated_at": now,
            }},
            upsert=True,
        )

    # One-time migrations replace only missing/older defaults. Once a template
    # reaches its current version, later staff/admin edits are preserved.
    for post_type, version in TEMPLATE_VERSIONS.items():
        col.update_one(
            {
                "post_type": post_type,
                "platform": "twitter",
                "$or": [
                    {"template_version": {"$exists": False}},
                    {"template_version": {"$lt": version}},
                ],
            },
            {"$set": {
                "template_text": _DEFAULTS[post_type],
                "template_version": version,
                "updated_by": None,
                "updated_at": now,
            }},
        )


# ── Read ───────────────────────────────────────────────────────────────────

def get_template(db, post_type: str) -> dict | None:
    return db[COLLECTION].find_one({"post_type": post_type, "platform": "twitter"})


def find_all(db) -> list[dict]:
    return list(db[COLLECTION].find({"platform": "twitter"}))


# ── Write ──────────────────────────────────────────────────────────────────

def update_template(db, post_type: str, template_text: str, updated_by) -> None:
    db[COLLECTION].update_one(
        {"post_type": post_type, "platform": "twitter"},
        {"$set": {
            "template_text": template_text,
            "template_version": TEMPLATE_VERSIONS.get(post_type, 1),
            "updated_by": ObjectId(str(updated_by)) if updated_by else None,
            "updated_at": now_ct(),
        }},
        upsert=True,
    )
