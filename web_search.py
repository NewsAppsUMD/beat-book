"""
web_search.py
-------------
Web search for research runs whose model has no search of its own.

On Anthropic, search is a server-side tool that Anthropic runs. Ollama models
can't search by themselves: Ollama offers a search service
(POST https://ollama.com/api/web_search, authorized with OLLAMA_API_KEY)
that the application calls when the model asks, the same way the app runs
its page fetcher. Results feed the fetch allowlist; their snippets can't be
quoted, only the pages they point to.
"""

from __future__ import annotations

import os
from typing import Dict, List

import httpx

SEARCH_TIMEOUT = 30.0
MAX_RESULTS = 5


def ollama_search_host() -> str:
    return os.environ.get("OLLAMA_SEARCH_HOST", "https://ollama.com").rstrip("/")


def ollama_search_key() -> str:
    return (os.environ.get("OLLAMA_SEARCH_API_KEY") or os.environ.get("OLLAMA_API_KEY") or "").strip()


def ollama_search_available() -> bool:
    return bool(ollama_search_key())


class SearchError(Exception):
    pass


def ollama_web_search(query: str, max_results: int = MAX_RESULTS) -> List[Dict[str, str]]:
    """[{"title", "url", "content"}] from Ollama's search service."""
    key = ollama_search_key()
    if not key:
        raise SearchError("OLLAMA_API_KEY is not set, so Ollama web search is unavailable.")
    try:
        with httpx.Client(timeout=SEARCH_TIMEOUT) as client:
            resp = client.post(
                f"{ollama_search_host()}/api/web_search",
                headers={"Authorization": f"Bearer {key}"},
                json={"query": query, "max_results": max(1, min(10, max_results))},
            )
    except httpx.HTTPError as e:
        raise SearchError(f"search request failed: {type(e).__name__}: {e}") from e
    if resp.status_code == 429:
        raise SearchError("Ollama search rate limit reached.")
    if resp.status_code >= 400:
        raise SearchError(f"Ollama search returned HTTP {resp.status_code}: {resp.text[:200]}")
    results = (resp.json() or {}).get("results") or []
    return [{"title": str(r.get("title") or ""), "url": str(r.get("url") or ""),
             "content": str(r.get("content") or "")}
            for r in results if isinstance(r, dict) and r.get("url")]
