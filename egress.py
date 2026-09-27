"""
egress.py
---------
Describe, from the current configuration, what each pipeline stage sends off
this machine and where. The create screen shows this table before the
reporter starts a generation, and each book's manifest keeps a copy, so a
reporter can tell which parts of their material left the machine.

This is derived from configuration and from what the code sends at each call
site. It is a disclosure, not a network monitor.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List
from urllib.parse import urlparse

from embed_client import get_embed_provider, get_ollama_host

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}


def _host_of(url: str) -> str:
    host = urlparse(url if "://" in url else f"http://{url}").hostname or url
    return host


def _destination(host: str, service: str) -> Dict[str, Any]:
    local = host in _LOCAL_HOSTS or host.endswith(".localhost")
    return {"host": host, "service": service, "local": local}


def _chat_destination() -> Dict[str, Any]:
    if os.environ.get("CHAT_PROVIDER", "anthropic").strip().lower() == "ollama":
        host = _host_of(os.environ.get("OLLAMA_CHAT_HOST", "https://ollama.com"))
        return _destination(host, "Ollama")
    return _destination("api.anthropic.com", "Anthropic")


def _embed_destination() -> Dict[str, Any]:
    if get_embed_provider() == "ollama":
        return _destination(_host_of(get_ollama_host()), "Ollama")
    return _destination("api.openai.com", "OpenAI")


def egress_plan() -> List[Dict[str, Any]]:
    """One row per stage: what is sent, where, and when it applies."""
    chat = _chat_destination()
    embed = _embed_destination()
    anthropic = _destination("api.anthropic.com", "Anthropic")
    firecrawl = bool(os.environ.get("FIRECRAWL_API_KEY", "").strip())

    rows: List[Dict[str, Any]] = []
    if firecrawl:
        rows.append({
            "stage": "Parse PDFs and URLs",
            "phase": "ingest",
            "sends": "The raw PDF file, or the URL to scrape",
            "content": "full_text",
            "to": _destination("api.firecrawl.dev", "Firecrawl"),
        })
    else:
        rows.append({
            "stage": "Read scanned PDFs",
            "phase": "ingest",
            "sends": "Page images of scanned PDFs, only when a PDF has no text layer",
            "content": "full_text",
            "to": anthropic,
        })
    rows += [
        {
            "stage": "Find stories in documents",
            "phase": "ingest",
            "sends": "Full extracted text of each document, up to 100,000 characters per call. "
                     "Skipped for story-shaped JSON and RSS files, which are mapped without a model",
            "content": "full_text",
            "to": chat,
        },
        {
            "stage": "Group stories into topics",
            "phase": "analyze",
            "sends": "Title and the first 400 words of every story, as embedding input",
            "content": "excerpt",
            "to": embed,
        },
        {
            "stage": "Name the topics",
            "phase": "analyze",
            "sends": "Titles and about 30 words from up to 8 stories per topic",
            "content": "excerpt",
            "to": chat,
        },
        {
            "stage": "Write the beat book",
            "phase": "generate",
            "sends": "Story text up to 4,000 characters for stories it reads in full, "
                     "2,000-character excerpts when it scans a topic, and story metadata. "
                     "Only stories in the selected topics",
            "content": "full_text",
            "to": chat,
        },
        {
            "stage": "Add web research",
            "phase": "generate",
            "sends": "The full draft beat book, not the source stories, plus the text of "
                     "web pages it reads. Searches run at Anthropic; pages are fetched "
                     + ("by Firecrawl" if firecrawl else "from this machine"),
            "content": "derived",
            "to": anthropic,
        },
        {
            "stage": "Match citations",
            "phase": "generate",
            "sends": "Every 100-word passage of every story and every beat-book sentence, "
                     "as embedding input",
            "content": "full_text",
            "to": embed,
        },
    ]
    return rows


def egress_summary() -> Dict[str, Any]:
    rows = egress_plan()
    off_machine_full_text = sorted({
        r["to"]["host"] for r in rows
        if r["content"] == "full_text" and not r["to"]["local"]
    })
    return {"rows": rows, "full_text_leaves_machine_to": off_machine_full_text}
