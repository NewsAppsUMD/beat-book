"""Research on an Ollama model: search runs through Ollama's search service,
pages through the app's fetcher, and every fact faces the same quote checks
as on Anthropic. The chat, search and page requests are faked over HTTP."""

import asyncio
import json

import httpx

PAGE_URL = "https://www.thecha.org/news/pettigrew"
PAGE = ("Keith Pettigrew officially began his tenure as Chief Executive Officer of the Chicago "
        "Housing Authority on April 20, 2026. He previously led the Alexandria Redevelopment and "
        "Housing Authority. ") * 3
DRAFT = ("# CHA Beat Book\n\n## Beat Overview\n\nThe Chicago Housing Authority is run by a board.\n\n"
         "## Key Sources & Players\n\n- **Keith Pettigrew** — CEO of the Chicago Housing Authority, appointed over objections.\n")
QUOTE = "Keith Pettigrew officially began his tenure as Chief Executive Officer of the Chicago Housing Authority on April 20, 2026."


def _tool_call(name, args):
    return {"function": {"name": name, "arguments": args}}


def _run(tmp_path, monkeypatch, chat_script, env=None, page=PAGE):
    import page_fetcher as pf
    import research_agent as ra
    env = {"RESEARCH_PROVIDER": "ollama", "OLLAMA_CHAT_HOST": "http://ollama.test",
           "OLLAMA_CHAT_MODEL": "glm-5.3:cloud", "OLLAMA_API_KEY": "test-key",
           "OLLAMA_SEARCH_HOST": "http://search.test", **(env or {})}
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    monkeypatch.setattr(pf, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(pf, "_is_blocked_ip", lambda host: False)
    monkeypatch.setattr(pf, "_firecrawl_available", lambda: False)
    calls = {"chat": [], "search": [], "page": 0}

    def handler(request):
        url = str(request.url)
        if url.endswith("/api/chat"):
            body = json.loads(request.content)
            calls["chat"].append(body)
            reply = chat_script[min(len(calls["chat"]) - 1, len(chat_script) - 1)]
            return httpx.Response(200, json={"message": {"role": "assistant", **reply}, "done_reason": "stop"})
        if url.endswith("/api/web_search"):
            calls["search"].append((json.loads(request.content), request.headers.get("authorization")))
            return httpx.Response(200, json={"results": [
                {"title": "Pettigrew begins", "url": PAGE_URL, "content": "Pettigrew began April 20."}]})
        if url == PAGE_URL:
            calls["page"] += 1
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  content=f"<html><head><title>Pettigrew begins | Chicago Housing Authority</title></head><body><p>{page}</p></body></html>".encode())
        return httpx.Response(404)

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    (tmp_path / "book.md").write_text(DRAFT)
    trace = {}
    out = asyncio.run(ra.run_research_agent(tmp_path, "book.md", "", trace=trace))
    return out, trace, calls


SCRIPT = [
    {"content": "Let me look for Pettigrew's start date.",
     "tool_calls": [_tool_call("web_search", {"query": "Keith Pettigrew CHA start date"})]},
    {"content": "", "tool_calls": [_tool_call("fetch_page", {"url": PAGE_URL})]},
    {"content": "", "tool_calls": [
        _tool_call("submit_fact", {"fact": "Pettigrew began his tenure as CHA chief executive officer on April 20, 2026.",
                                   "quote": QUOTE, "url": PAGE_URL, "source_name": "Chicago Housing Authority",
                                   "published": "Apr 20, 2026", "section": "Key Sources & Players",
                                   "after_line": "**Keith Pettigrew** — CEO"}),
        _tool_call("submit_fact", {"fact": "Pettigrew was fired by the board in May after a dispute with the mayor.",
                                   "quote": "Pettigrew was fired by the board in May after a dispute.",
                                   "url": PAGE_URL, "source_name": "CHA", "section": "Beat Overview"}),
        _tool_call("finalize_research", {"summary": "Added Pettigrew's start date."})]},
]


def test_ollama_research_searches_fetches_and_checks_quotes(tmp_path, monkeypatch):
    out, trace, calls = _run(tmp_path, monkeypatch, SCRIPT)
    assert trace["provider"] == "ollama" and trace["model"] == "glm-5.3:cloud" and trace["search"] == "ollama"
    # Search went to Ollama's service with the key; its URL became fetchable.
    assert calls["search"][0][0]["query"] == "Keith Pettigrew CHA start date"
    assert calls["search"][0][1] == "Bearer test-key"
    assert trace["web_searches"] == ["Keith Pettigrew CHA start date"] and calls["page"] == 1
    # Only the fact backed by its quote reached the book; the made-up one didn't.
    assert "  - Pettigrew began his tenure as CHA chief executive officer on April 20, 2026 (Chicago Housing Authority, Apr 20, 2026)." in out
    assert "fired" not in out
    assert any("does not appear" in r["reason"] for r in trace["facts_rejected"])
    assert trace["finalized"] and trace["summary"] == "Added Pettigrew's start date."
    # The chat request carried the app-run search tool, not Anthropic's server tool.
    tool_names = [t["function"]["name"] for t in calls["chat"][0]["tools"]]
    assert tool_names[0] == "web_search" and "finalize_research" in tool_names
    assert calls["chat"][0]["model"] == "glm-5.3:cloud"


def test_ollama_pages_are_shown_in_smaller_pieces(tmp_path, monkeypatch):
    import research_agent as ra
    out, trace, calls = _run(tmp_path, monkeypatch, SCRIPT, page=PAGE * 400)
    tool_msgs = [m for m in calls["chat"][2]["messages"] if m["role"] == "tool"]
    assert tool_msgs and len(tool_msgs[-1]["content"]) <= ra.OLLAMA_MAX_SHOWN_CHARS + 1000


def test_without_a_search_key_research_runs_without_search(tmp_path, monkeypatch):
    import research_agent as ra
    monkeypatch.setenv("RESEARCH_PROVIDER", "ollama")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_SEARCH_API_KEY", raising=False)
    assert ra.research_provider()["search"] is None
    assert [t["name"] for t in ra.build_tools("ollama")] == ["fetch_page", "submit_fact", "finalize_research"]


def test_anthropic_stays_the_default(monkeypatch):
    import research_agent as ra
    monkeypatch.delenv("RESEARCH_PROVIDER", raising=False)
    assert ra.research_provider() == {"provider": "anthropic", "model": ra.MODEL, "search": "anthropic"}
    assert ra.build_tools()[0]["type"] == "web_search_20260209"


def test_forced_finalize_turn_works_on_ollama(tmp_path, monkeypatch):
    # The model ends its turn without finalizing; the forced turn uses a JSON
    # schema on Ollama and records the summary.
    script = [{"content": "I think the book is fine as is."},
              {"content": json.dumps({"summary": "No gaps found."})}]
    out, trace, calls = _run(tmp_path, monkeypatch, script)
    assert out == DRAFT and trace["finalize_turn"] and trace["summary"] == "No gaps found."
    assert calls["chat"][-1]["format"]["required"] == ["summary"]


def test_model_prose_is_not_sent_back_to_ollama(tmp_path, monkeypatch):
    out, trace, calls = _run(tmp_path, monkeypatch, SCRIPT)
    later = calls["chat"][1]["messages"]
    assistant = [m for m in later if m["role"] == "assistant"]
    assert assistant and all(m["content"] == "" for m in assistant)       # reasoning dropped
    assert assistant[0]["tool_calls"][0]["function"]["name"] == "web_search"  # tool call kept
