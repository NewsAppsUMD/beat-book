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


class EmbedClient(Protocol):
    model_name: str
    dimensions: int

    def embed(self, texts: List[str]) -> List[List[float]]:
        ...


OPENAI_KNOWN_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


class OpenAIEmbedClient:
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
    def __init__(self, host: str = "http://localhost:11434",
                 model: str = "qwen3-embedding:4b"):
        self._host = host.rstrip("/")
        self.model_name = f"ollama/{model}"
        self._model = model
        self.dimensions = self._probe_dimensions()

    def _probe_dimensions(self) -> int:
        resp = httpx.post(
            f"{self._host}/api/embed",
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
    resp = httpx.get(f"{host}/api/tags", timeout=10.0)
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
        model = model_override or os.environ.get("OLLAMA_EMBED_MODEL", "qwen3-embedding:4b")
        return OllamaEmbedClient(host=host, model=model)
    else:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is required when EMBED_PROVIDER=openai (the default). "
                "Set EMBED_PROVIDER=ollama to use a local model instead."
            )
        model = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
        return OpenAIEmbedClient(api_key=api_key, model=model)
