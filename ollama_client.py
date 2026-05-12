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


def chat_client(api_key: str | None = None) -> OpenAI:
    """OpenAI-compatible client pointed at Ollama Cloud."""
    key = api_key or os.environ.get("OLLAMA_API_KEY") or "ollama"
    base = os.environ.get("OLLAMA_CHAT_BASE_URL", DEFAULT_CHAT_BASE_URL)
    return OpenAI(api_key=key, base_url=base)
