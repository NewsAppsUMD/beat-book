"""
embed_client.py
---------------
Thin abstraction over embedding providers (OpenAI, Ollama).
"""

from __future__ import annotations

import os
from typing import List, Protocol

import httpx
from openai import OpenAI

# Single source of truth for the Ollama embedding default — app.py's
# /api/embed-config endpoint reports this same constant so the UI's
# reported default never drifts from what get_embed_client() actually uses.
DEFAULT_OLLAMA_EMBED_MODEL = "qwen3-embedding:0.6b"


class EmbedClient(Protocol):
    model_name: str
    dimensions: int
    # Provider-appropriate ceiling on texts per embed() call. Callers should
    # take min(their own desired batch size, this) when chunking — a hosted
    # API can take a large batch in one fast round-trip, but a local/
    # CPU-bound model (Ollama with no GPU) needs a small one so no single
    # call blocks long enough to stall the progress stream it's reported on.
    batch_size: int

    def embed(self, texts: List[str]) -> List[List[float]]:
        ...


OPENAI_KNOWN_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


class OpenAIEmbedClient:
    # Hosted API, fast per-call — large batches minimize request count/RPM
    # pressure rather than risking a stall.
    batch_size = 256

    def __init__(self, api_key: str, model: str = "text-embedding-3-small"):
        self._client = OpenAI(api_key=api_key)
        self.model_name = model
        self.dimensions = OPENAI_KNOWN_DIMS.get(model, 1536)

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        cleaned = [t if t.strip() else " " for t in texts]
        resp = self._client.embeddings.create(model=self.model_name, input=cleaned)
        return [item.embedding for item in sorted(resp.data, key=lambda x: x.index)]


class OllamaEmbedClient:
    # Local, usually CPU-bound (no GPU in a Codespace) — small batches keep
    # each call short so progress/heartbeats keep flowing.
    batch_size = 20

    def __init__(self, host: str = "http://localhost:11434",
                 model: str = DEFAULT_OLLAMA_EMBED_MODEL,
                 api_key: str | None = None):
        self._host = host.rstrip("/")
        self.model_name = f"ollama/{model}"
        self._model = model
        self._api_key = api_key or os.environ.get("OLLAMA_EMBED_API_KEY", "")
        self.dimensions = self._probe_dimensions()

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def _probe_dimensions(self) -> int:
        resp = httpx.post(
            f"{self._host}/api/embed",
            headers=self._headers(),
            json={"model": self._model, "input": ["dimension probe"]},
            timeout=60.0,
        )
        resp.raise_for_status()
        vecs = resp.json()["embeddings"]
        return len(vecs[0])

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        cleaned = [t if t.strip() else " " for t in texts]
        resp = httpx.post(
            f"{self._host}/api/embed",
            headers=self._headers(),
            json={"model": self._model, "input": cleaned},
            timeout=120.0,
        )
        resp.raise_for_status()
        return resp.json()["embeddings"]


def get_embed_provider() -> str:
    return os.environ.get("EMBED_PROVIDER", "openai").lower()


def get_ollama_host() -> str:
    return os.environ.get("OLLAMA_HOST", "http://localhost:11434")


def list_ollama_models(host: str | None = None) -> list[dict]:
    host = (host or get_ollama_host()).rstrip("/")
    api_key = os.environ.get("OLLAMA_EMBED_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    resp = httpx.get(f"{host}/api/tags", headers=headers, timeout=10.0)
    resp.raise_for_status()
    models = resp.json().get("models", [])
    embed_models = []
    for m in models:
        name = m.get("name", "")
        families = (m.get("details") or {}).get("families") or []
        if "bert" in families or "nomic-bert" in families or "embed" in name.lower():
            embed_models.append({"name": name, "size": m.get("size", 0)})
    return embed_models


def get_embed_client(model_override: str | None = None) -> EmbedClient:
    provider = get_embed_provider()
    if provider == "ollama":
        host = get_ollama_host()
        model = model_override or os.environ.get("OLLAMA_EMBED_MODEL", DEFAULT_OLLAMA_EMBED_MODEL)
        return OllamaEmbedClient(host=host, model=model)  # api_key read from OLLAMA_EMBED_API_KEY
    else:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is required when EMBED_PROVIDER=openai (the default). "
                "Set EMBED_PROVIDER=ollama to use a local model instead."
            )
        model = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
        return OpenAIEmbedClient(api_key=api_key, model=model)
