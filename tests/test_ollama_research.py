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


def _chat_body(monkeypatch, env, replies):
    """Send one request through OllamaChatProvider with a scripted server."""
    import chat_provider as cp
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        status, payload = replies[min(len(sent) - 1, len(replies) - 1)]
        return httpx.Response(status, json=payload) if isinstance(payload, dict) else httpx.Response(status, text=payload)
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return cp, sent


def test_ollama_think_on_sends_think_and_keeps_the_answer_clean(monkeypatch):
    ok = (200, {"message": {"role": "assistant", "content": "City Budget Vote",
                            "thinking": "The user wants a label..."}, "done_reason": "stop"})
    cp, sent = _chat_body(monkeypatch, {"OLLAMA_THINK": "on", "OLLAMA_CHAT_HOST": "http://o.test"}, [ok])
    resp = cp.OllamaChatProvider().create(model="glm-5.3:cloud", system="", messages=[{"role": "user", "content": "x"}])
    assert sent[0]["think"] is True and resp.text == "City Budget Vote"


def test_ollama_think_falls_back_for_models_without_thinking(monkeypatch):
    rejected = (400, '{"error":"\\"llama3\\" does not support thinking"}')
    ok = (200, {"message": {"role": "assistant", "content": "City Budget Vote"}, "done_reason": "stop"})
    cp, sent = _chat_body(monkeypatch, {"OLLAMA_THINK": "on", "OLLAMA_CHAT_HOST": "http://o.test"}, [rejected, ok, ok])
    p = cp.OllamaChatProvider()
    assert p.create(model="llama3", system="", messages=[{"role": "user", "content": "x"}]).text == "City Budget Vote"
    assert [b["think"] for b in sent] == [True, False]
    p.create(model="llama3", system="", messages=[{"role": "user", "content": "x"}])
    assert sent[-1]["think"] is False            # remembered: no second rejection
    cp._OLLAMA_NO_THINKING.discard("llama3")


def test_think_is_off_by_default(monkeypatch):
    ok = (200, {"message": {"role": "assistant", "content": "x"}, "done_reason": "stop"})
    cp, sent = _chat_body(monkeypatch, {"OLLAMA_CHAT_HOST": "http://o.test"}, [ok])
    cp.OllamaChatProvider().create(model="qwen3:8b", system="", messages=[{"role": "user", "content": "x"}])
    assert sent[0]["think"] is False


def test_turn_at_the_output_limit_without_a_tool_call_gets_a_firm_nudge(tmp_path, monkeypatch):
    script = [{"content": "Let me deliberate at great length..."},       # ends at the limit
              {"content": "", "tool_calls": [_tool_call("finalize_research", {"summary": "Nothing to add."})]}]
    import research_agent as ra
    real_ask = ra._OllamaBackend.ask
    calls = {"n": 0}

    async def ask(self, *a, **kw):
        content, stop, usage = await real_ask(self, *a, **kw)
        calls["n"] += 1
        return content, ("max_tokens" if calls["n"] == 1 else stop), usage
    monkeypatch.setattr(ra._OllamaBackend, "ask", ask)
    out, trace, chat = _run(tmp_path, monkeypatch, script)
    nudge = [m for m in chat["chat"][1]["messages"] if m["role"] == "user"][-1]["content"]
    assert "without calling a tool" in nudge and trace["turns_at_output_limit"] == 1
    assert trace["finalized"]


def test_research_can_run_on_a_local_ollama_while_writing_uses_the_cloud(monkeypatch):
    import egress
    import research_agent as ra
    monkeypatch.setenv("RESEARCH_PROVIDER", "ollama")
    monkeypatch.setenv("CHAT_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_CHAT_HOST", "https://ollama.com")
    monkeypatch.setenv("RESEARCH_OLLAMA_MODEL", "gpt-oss:120b")
    monkeypatch.setenv("RESEARCH_OLLAMA_HOST", "http://localhost:11434")
    choice = ra.research_provider()
    assert choice["host"] == "http://localhost:11434" and choice["model"] == "gpt-oss:120b"
    assert ra._OllamaBackend(choice["model"], choice["host"]).chat._host == "http://localhost:11434"
    row = next(r for r in egress.egress_plan() if r["stage"] == "Add web research")
    assert row["to"]["local"] is True
    # Without the override, research uses the writing model's host.
    monkeypatch.delenv("RESEARCH_OLLAMA_HOST")
    assert ra.research_provider()["host"] == "https://ollama.com"


POINT_SCRIPT = [
    {"content": "", "tool_calls": [_tool_call("web_search", {"query": "Keith Pettigrew CHA start date"})]},
    {"content": "", "tool_calls": [_tool_call("fetch_page", {"url": PAGE_URL})]},
    {"content": "", "tool_calls": [
        _tool_call("submit_fact", {"fact": "Pettigrew began his tenure as CHA chief executive officer on April 20, 2026.",
                                   "sentences": [2], "url": PAGE_URL, "source_name": "Chicago Housing Authority",
                                   "published": "Apr 20, 2026", "section": "Key Sources & Players"}),
        _tool_call("submit_fact", {"fact": "Pettigrew previously led the Alexandria Redevelopment and Housing Authority.",
                                   "sentences": [99], "url": PAGE_URL, "source_name": "CHA",
                                   "section": "Key Sources & Players"}),
        _tool_call("finalize_research", {"summary": "Added Pettigrew's start date."})]},
]


def test_ollama_quotes_by_pointing_at_numbered_sentences(tmp_path, monkeypatch):
    out, trace, calls = _run(tmp_path, monkeypatch, POINT_SCRIPT)
    # The page reached the model with numbered sentences.
    page_msg = next(m for m in calls["chat"][2]["messages"] if m["role"] == "tool" and "BEGIN PAGE" in m["content"])
    # (Sentence 1 is the page title, which extraction puts first.)
    assert "[2] Keith Pettigrew officially began" in page_msg["content"]
    # Sentence 2 became the quote, copied exactly, and the fact went in.
    accepted = trace["facts_accepted"]
    assert len(accepted) == 1 and accepted[0]["sentences"] == [2] and accepted[0]["quote"] == QUOTE
    assert "Pettigrew began his tenure as CHA chief executive officer on April 20, 2026" in out
    # A number past the page's last sentence is rejected with the count.
    assert any("sentence 99 isn't on that page" in r["reason"] for r in trace["facts_rejected"])
    # The prompt explains pointing, and the tool asks for sentences, not a quote.
    assert "Quoting by sentence number" in calls["chat"][0]["messages"][0]["content"]
    submit = next(t for t in calls["chat"][0]["tools"] if t["function"]["name"] == "submit_fact")
    params = submit["function"]["parameters"]
    assert "sentences" in params["required"] and "quote" not in params["properties"]


def test_pointed_sentences_join_like_an_excerpt():
    import research_agent as ra
    s = ["First sentence here.", "Second one follows.", "A third is apart.", "Fourth.", "Fifth."]
    assert ra._quote_from_sentences([2, 1], s) == ("First sentence here. Second one follows.", "")
    assert ra._quote_from_sentences([1, 3], s) == ("First sentence here. … A third is apart.", "")
    assert "at most 3" in ra._quote_from_sentences([1, 2, 3, 4], s)[1]
    assert "sentence numbers" in ra._quote_from_sentences(["one"], s)[1]


def test_claude_keeps_typed_quotes():
    import research_agent as ra
    submit = next(t for t in ra.build_tools("anthropic") if t["name"] == "submit_fact")
    assert "quote" in submit["input_schema"]["required"] and "sentences" not in submit["input_schema"]["properties"]
