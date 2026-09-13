"""
Vision analysis service — Module 10 weekly chart post.

Sends two chart images (week-start and week-end) to a multimodal LLM via
LM Studio's OpenAI-compatible vision endpoint and returns tweet-ready text.

Config (all via .env):
  LM_STUDIO_BASE_URL       default: http://localhost:1234
  LM_STUDIO_VISION_MODEL   default: google/gemma-4-e4b
  LM_STUDIO_TIMEOUT        default: 240
"""
import base64
import logging
import os
from pathlib import Path

import requests

from app.services.lm_studio import chat_completions_url

log = logging.getLogger(__name__)

_TWEET_SEP = "\n---\n"

_SYSTEM_PROMPT = (
    "You are a financial content creator specialising in SPX options gamma exposure (GEX) "
    "analysis for a FinTwit audience. You write clear, data-driven posts — no hype, no "
    "emojis overload, no generic filler. Every claim must be grounded in what the charts show."
)

_USER_PROMPT = (
    "I'm giving you two SPX weekly GEX charts: the first is from the START of the week, "
    "the second is from the END of the week.\n\n"
    "Analyze the key changes in gamma exposure, dealer positioning, and notable level shifts "
    "between the two. Then write a Twitter thread of 2–3 tweets summarising the week's GEX "
    "story. Rules:\n"
    "• Each tweet must be under 280 characters.\n"
    "• Separate tweets with exactly this delimiter on its own line: ---\n"
    "• Start tweet 1 with '$SPX Weekly GEX Wrap'\n"
    "• Be specific: name actual levels (call wall, put wall, net GEX direction, hot zone) "
    "if visible in the charts.\n"
    "• Do NOT add a sign-off or 'follow me' line — that gets appended automatically.\n"
    "Output ONLY the tweets, nothing else."
)


def _encode_image(path: str) -> str:
    """Return base64-encoded image content."""
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _mime(path: str) -> str:
    ext = Path(path).suffix.lower()
    return "image/png" if ext == ".png" else "image/jpeg"


def analyze_weekly_charts(start_chart_path: str, end_chart_path: str) -> list[str]:
    """
    Send two chart images to the vision LLM and return a list of tweet strings.
    Raises RuntimeError on API failure or empty response.
    """
    base_url = os.environ.get("LM_STUDIO_BASE_URL", "http://localhost:1234").rstrip("/")
    model    = os.environ.get("LM_STUDIO_VISION_MODEL", "google/gemma-4-e4b")
    timeout  = int(os.environ.get("LM_STUDIO_TIMEOUT", "240"))

    start_b64 = _encode_image(start_chart_path)
    end_b64   = _encode_image(end_chart_path)

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _USER_PROMPT},
                {"type": "image_url", "image_url": {
                    "url": f"data:{_mime(start_chart_path)};base64,{start_b64}"
                }},
                {"type": "image_url", "image_url": {
                    "url": f"data:{_mime(end_chart_path)};base64,{end_b64}"
                }},
            ],
        },
    ]

    log.info("Vision analysis: model=%s base_url=%s", model, base_url)
    resp = requests.post(
        chat_completions_url(base_url),
        json={"model": model, "messages": messages, "stream": False},
        timeout=timeout,
    )

    if not resp.ok:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        raise RuntimeError(f"LM Studio {resp.status_code}: {body}")

    choices = resp.json().get("choices") or []
    raw = (choices[0].get("message", {}).get("content") if choices else "").strip()
    if not raw:
        raise RuntimeError("Empty response from vision model")

    log.info("Vision response: %d chars", len(raw))
    tweets = [t.strip() for t in raw.split("---") if t.strip()]
    if not tweets:
        raise RuntimeError("Vision model returned text but no parseable tweets")
    return tweets
