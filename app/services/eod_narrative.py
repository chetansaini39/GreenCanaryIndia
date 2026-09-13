"""Bounded LM Studio prose generation for deterministic EOD X threads."""
from __future__ import annotations

import json
import logging
import math
import os
import re

import requests

from app.services.lm_studio import chat_completions_url


log = logging.getLogger(__name__)

FIELDS = (
    "regime_explanation",
    "hot_zone_explanation",
    "bull_behavior",
    "bear_behavior",
    "wildcard_commentary",
    "verdict",
)

FIELD_LIMITS = {
    "regime_explanation": 75,
    "hot_zone_explanation": 15,
    "bull_behavior": 30,
    "bear_behavior": 30,
    "wildcard_commentary": 15,
    "verdict": 95,
}

_NUMBER_RE = re.compile(r"\d")

_SYSTEM_PROMPT = """You write concise SPX gamma-exposure commentary for professional traders.
Return strict JSON and nothing else. Calculations, numeric levels, post structure, and hashtags
are owned by the application. Never output a digit, price, strike, dollar amount, hashtag,
bracketed token, bullet, or newline inside a field. Do not give personalized investment advice."""


def _validated_metrics(analysis: dict) -> dict:
    required_numeric = (
        "net_gex_b",
        "gamma_flip",
        "call_wall",
        "put_wall",
        "hot_zone",
    )
    for key in required_numeric:
        value = analysis.get(key)
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"validated EOD analysis is missing {key}")
    regime = analysis.get("regime")
    position = analysis.get("dealer_position")
    if regime not in ("POSITIVE", "NEGATIVE") or position not in ("LONG", "SHORT"):
        raise ValueError("validated EOD analysis has an invalid regime")
    return {
        "regime": regime,
        "dealer_position": position,
        "net_gex_billions": round(float(analysis["net_gex_b"]), 4),
        "allowed_levels": {
            "gamma_flip": round(float(analysis["gamma_flip"]), 2),
            "call_wall": round(float(analysis["call_wall"]), 2),
            "put_wall": round(float(analysis["put_wall"]), 2),
            "hot_zone": round(float(analysis["hot_zone"]), 2),
        },
        "meaning": (
            "Dealers dampen moves and favor range-bound action"
            if regime == "POSITIVE"
            else "Dealers chase direction and can amplify moves"
        ),
    }


def validate_narrative(value) -> dict[str, str]:
    """Validate the model contract and reject any model-authored number."""
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise ValueError(f"response must contain exactly these fields: {', '.join(FIELDS)}")

    cleaned: dict[str, str] = {}
    for key in FIELDS:
        text = value.get(key)
        if not isinstance(text, str):
            raise ValueError(f"{key} must be a string")
        text = text.strip()
        if not text:
            raise ValueError(f"{key} cannot be empty")
        if "\n" in text or "\r" in text:
            raise ValueError(f"{key} must be one line")
        if len(text) > FIELD_LIMITS[key]:
            raise ValueError(f"{key} exceeds {FIELD_LIMITS[key]} characters")
        if _NUMBER_RE.search(text):
            raise ValueError(f"{key} contains an invented numeric value")
        if "[" in text or "]" in text or "#" in text:
            raise ValueError(f"{key} contains forbidden formatting")
        cleaned[key] = text
    return cleaned


def _response_text(response) -> str:
    if not response.ok:
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        raise RuntimeError(f"LM Studio {response.status_code}: {detail}")
    try:
        choices = response.json().get("choices") or []
        content = choices[0].get("message", {}).get("content") if choices else ""
    except Exception as exc:
        raise RuntimeError("LM Studio returned an invalid response envelope") from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("LM Studio returned an empty narrative")
    return content.strip()


def generate_eod_narrative(analysis: dict, *, request_post=None) -> tuple[dict[str, str], str]:
    """Return validated prose and the LM Studio model name.

    Exactly one repair request is made after invalid JSON or contract validation.
    Network/model failures surface immediately so no draft can be marked ready.
    """
    metrics = _validated_metrics(analysis)
    base_url = os.environ.get("LM_STUDIO_BASE_URL", "http://localhost:1234")
    model = os.environ.get("LM_STUDIO_TEXT_MODEL", "google/gemma-4-e4b")
    try:
        timeout = int(os.environ.get("LM_STUDIO_TIMEOUT", "240"))
    except ValueError as exc:
        raise RuntimeError("LM_STUDIO_TIMEOUT must be an integer") from exc
    post = request_post or requests.post

    schema = {key: f"one line, maximum {FIELD_LIMITS[key]} characters" for key in FIELDS}
    user_prompt = (
        "Write the six bounded prose fragments requested by this JSON schema. "
        "Use only the validated context for meaning; repeat none of its numbers. "
        "The hot-zone line must be especially short.\n\n"
        f"SCHEMA: {json.dumps(schema, sort_keys=True)}\n"
        f"VALIDATED_CONTEXT: {json.dumps(metrics, sort_keys=True)}"
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    last_error = ""
    last_raw = ""
    for attempt in range(2):
        if attempt:
            messages.extend([
                {"role": "assistant", "content": last_raw},
                {
                    "role": "user",
                    "content": (
                        f"Repair the response. Validation error: {last_error}. "
                        "Return one strict JSON object only, with all six required fields."
                    ),
                },
            ])
        try:
            response = post(
                chat_completions_url(base_url),
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.2,
                    "stream": False,
                    "max_tokens": 400,
                    "response_format": {"type": "json_object"},
                },
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"LM Studio request failed: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"LM Studio request failed: {exc}") from exc

        last_raw = _response_text(response)
        try:
            parsed = json.loads(last_raw)
            return validate_narrative(parsed), model
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc)
            log.warning("Invalid EOD narrative attempt %d: %s", attempt + 1, last_error)

    raise RuntimeError(f"LM Studio returned invalid EOD narrative after repair: {last_error}")
