"""Shared helpers for LM Studio's OpenAI-compatible HTTP API."""


def chat_completions_url(base_url: str) -> str:
    """Return one canonical chat-completions URL for a host root or /v1 URL."""
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        raise ValueError("LM_STUDIO_BASE_URL cannot be empty")
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"
