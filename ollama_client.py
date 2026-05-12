"""
ollama_client.py
----------------
Shared Ollama Cloud configuration for the chat-model slots (ingest
normalization, cluster labeling, the interview agent). Ollama Cloud has
no embedding models, so embeddings continue to use OpenAI's API — that
client is constructed at the call sites that need it.

Env vars:
- OLLAMA_API_KEY        (required)
- OLLAMA_CHAT_BASE_URL  (optional, default https://ollama.com/v1)
"""

import os
from openai import OpenAI

CHAT_MODEL = "qwen3.5:397b-cloud"
DEFAULT_CHAT_BASE_URL = "https://ollama.com/v1"


# Per-request timeout for the OpenAI-compatible client. The SDK default is
# 10 minutes, which lets a stuck model hang the UI silently. 180s leaves
# headroom for cold-start on Qwen 3.5 397B (often 60–90s to first token)
# while keeping real failures visible to the user inside ~3 minutes.
CHAT_TIMEOUT_SECONDS = 180.0

# The OpenAI SDK retries timeouts twice by default — that turns a single
# 180s timeout into a silent 9-minute wait. Timeouts here mean the model
# is overloaded, not transient network blips, so retries don't help.
CHAT_MAX_RETRIES = 0


def chat_client(api_key: str | None = None) -> OpenAI:
    """OpenAI-compatible client pointed at Ollama Cloud."""
    key = api_key or os.environ.get("OLLAMA_API_KEY") or "ollama"
    base = os.environ.get("OLLAMA_CHAT_BASE_URL", DEFAULT_CHAT_BASE_URL)
    return OpenAI(
        api_key=key,
        base_url=base,
        timeout=CHAT_TIMEOUT_SECONDS,
        max_retries=CHAT_MAX_RETRIES,
    )


def thinking_enabled() -> bool:
    """Whether reasoning/thinking should be enabled for the chat models.

    Controlled by the ENABLE_THINKING env var. Default is off — thinking
    models (Opus 4.7 adaptive, Qwen 3.5) are higher quality but materially
    slower. Set ENABLE_THINKING=true to re-enable.
    """
    return (os.environ.get("ENABLE_THINKING") or "").strip().lower() in {"1", "true", "yes", "on"}


def prepare_thinking(messages: list[dict]) -> tuple[list[dict], dict]:
    """Configure messages and extra_body to honor the ENABLE_THINKING flag.

    Returns (messages, extra_body) ready to splat into chat.completions.create.
    When thinking is enabled, returns the inputs unchanged. When disabled,
    applies a belt-and-suspenders approach for Qwen 3.5 on Ollama Cloud:

      1. `/no_think` chat-template directive prepended to the system
         message — the most reliable disable mechanism for Qwen3.x.
      2. `think: false` body flag — Ollama's native parameter, honored
         on the OpenAI-compatible endpoint when the model respects it.

    The body flag alone is unreliable on the Ollama Cloud OpenAI-compatible
    endpoint for Qwen 3.5; the chat-template directive is what actually
    suppresses reasoning tokens in practice.
    """
    if thinking_enabled():
        return messages, {}

    new_messages = list(messages)
    if new_messages and new_messages[0].get("role") == "system":
        new_messages[0] = {
            **new_messages[0],
            "content": "/no_think\n" + new_messages[0].get("content", ""),
        }
    else:
        new_messages.insert(0, {"role": "system", "content": "/no_think"})
    return new_messages, {"think": False}
