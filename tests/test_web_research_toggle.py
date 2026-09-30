"""Building a beat book with web research turned off."""

import asyncio

from chat_provider import ChatResponse
from test_transparency import HashEmbed, _pipeline_result

DRAFT = ("# Parks Beat Book\n\n*A guide*\n\n## Beat Overview\n\n"
         "The city council voted to approve the parks department budget in March.\n\n"
         "## Key Sources & Players\n\n- **Jane Doe** — parks director since 2020.\n")


class LocalWriter:
    """Stands in for an Ollama chat provider: no Anthropic key involved."""
    explore_model = agent_model = write_model = label_model = normalize_model = "local-test"

    def create(self, **kw):
        return ChatResponse(content=[{"type": "text", "text": "{}"}], stop_reason="end_turn")


def _isolate(tmp_path, monkeypatch):
    import jobs
    import store
    monkeypatch.setattr(store, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(store, "LIBRARY_PATH", tmp_path / "library.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(jobs, "SANDBOX_ROOT", tmp_path / "sandboxes")

    async def replay(pipeline_result, provider, on_message, on_beat_book, *a, trace=None, **kw):
        if trace is not None:
            trace.update({"stories_read": [], "tool_calls": [], "model_calls": []})
        await on_beat_book("draft.md", DRAFT)
    monkeypatch.setattr(jobs, "run_agent", replay)
    return jobs, store


def test_book_builds_without_research_or_an_anthropic_key(tmp_path, monkeypatch):
    jobs, store = _isolate(tmp_path, monkeypatch)

    async def must_not_run(**kw):
        raise AssertionError("web research ran although it was turned off")
    monkeypatch.setattr(jobs, "run_research_agent", must_not_run)

    rec = store.create_book(title="Parks", desired_stem="parks_beat_book", num_stories=4,
                            num_topics=2, selected_topics=["Parks", "Schools"], style="narrative")
    events = []

    async def emit(ev):
        events.append(ev)

    asyncio.run(jobs.run_generation(rec["id"], _pipeline_result(), ["Parks", "Schools"], emit,
                                    anthropic_key="", embed_client=HashEmbed(), target_words=1000,
                                    chat_provider=LocalWriter(), web_research=False))
    book = store.get_book(rec["id"])
    assert book["status"] == "ready" and book["web_research"] is False
    types = [e["type"] for e in events]
    assert "research_skipped" in types and "research_started" not in types
    import json
    manifest = json.loads((tmp_path / f"{book['stem']}.manifest.json").read_text())
    assert manifest["web_research"] is False and manifest["research"] == {"skipped": True}
    assert manifest["providers"]["research"]["skipped"] is True
    assert (tmp_path / f"{book['stem']}.md").read_text() == DRAFT      # the draft, unchanged


def test_research_still_needs_the_key(tmp_path, monkeypatch):
    jobs, store = _isolate(tmp_path, monkeypatch)
    rec = store.create_book(title="Parks", desired_stem="parks_beat_book", num_stories=4,
                            num_topics=2, selected_topics=["Parks"], style="narrative")

    async def emit(ev):
        pass

    asyncio.run(jobs.run_generation(rec["id"], _pipeline_result(), ["Parks"], emit, anthropic_key="",
                                    embed_client=HashEmbed(), chat_provider=LocalWriter(), web_research=True))
    book = store.get_book(rec["id"])
    assert book["status"] == "failed" and "web research needs it" in book["error"]


def test_egress_plan_leaves_out_research_when_off():
    from egress import egress_plan
    assert "Add web research" in [r["stage"] for r in egress_plan()]
    assert "Add web research" not in [r["stage"] for r in egress_plan(web_research=False)]
