"""Tests for the Phase 1 transparency changes: citable bullets and table rows,
draft-vs-research provenance, agent topic scoping, ingest metadata fixes,
the Word export, and the per-book file routes."""

import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pytest

import citation_matcher as cm


class HashEmbed:
    """Deterministic bag-of-words embedder: texts sharing words score high."""
    model_name = "hash-test"
    dimensions = 256
    batch_size = 64
    max_parallel = 1

    def embed(self, texts):
        out = []
        for t in texts:
            v = np.zeros(self.dimensions, dtype=np.float32)
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                if len(w) < 3:
                    continue
                h = int(hashlib.md5(w.encode()).hexdigest(), 16)
                v[h % self.dimensions] += 1.0
            if not v.any():
                v[0] = 1.0
            out.append(v.tolist())
        return out


STORIES = [
    {"title": "Council budget vote",
     "content": ("The city council voted seven to two on Tuesday to approve a "
                 "forty million dollar budget for the parks department. "
                 "Alderman Maria Lopez opposed the plan and said the parks "
                 "department had not explained its spending on maintenance "
                 "contracts. ") * 3},
    {"title": "School board hires superintendent",
     "content": ("The school board hired Denise Carter as superintendent after "
                 "a national search that lasted eight months. Carter previously "
                 "led a district in Ohio and promised to publish test score "
                 "data every quarter. ") * 3},
]


# ── Segmentation ───────────────────────────────────────────────────────────

def test_long_bullets_and_body_rows_are_citable_short_ones_are_not():
    md = "\n".join([
        "## Key Sources",
        "- **Maria Lopez**, alderman who opposed the parks department budget plan",
        "- Budget hearings",
        "1. Denise Carter was hired as superintendent after an eight month search",
        "",
        "| Date | Event |",
        "|---|---|",
        "| March 3 | City council votes on the parks department budget plan |",
    ])
    entries = cm._segment_markdown(md)
    by_content = {e["content"]: e for e in entries}
    assert by_content["- **Maria Lopez**, alderman who opposed the parks department budget plan"]["needs_embedding"]
    assert by_content["- **Maria Lopez**, alderman who opposed the parks department budget plan"]["embed_text"].startswith("Maria Lopez")
    assert not by_content["- Budget hearings"]["needs_embedding"]
    assert by_content["1. Denise Carter was hired as superintendent after an eight month search"]["kind"] == "list_item"
    assert not by_content["| Date | Event |"]["needs_embedding"]          # header row
    assert not by_content["|---|---|"]["needs_embedding"]                 # separator
    assert by_content["| March 3 | City council votes on the parks department budget plan |"]["needs_embedding"]
    assert not by_content["## Key Sources"]["needs_embedding"]


def test_numbered_list_marker_is_not_lost():
    # split_into_sentences would split "1." off and drop it as too short.
    entries = cm._segment_markdown("1. Denise Carter was hired as superintendent after a long search.")
    assert entries[0]["content"].startswith("1. ")


# ── Provenance ─────────────────────────────────────────────────────────────

def _entries(markdown, draft=None):
    client = HashEmbed()
    index = cm.embed_source_stories(STORIES, client)
    return cm.markdown_to_beatbook_entries(markdown, index, client, draft_markdown=draft)


def test_provenance_marks_corpus_web_and_unsupported():
    # Separate paragraphs: context-sum deliberately lends a sentence some of
    # its neighbors' signal, which would let the physics line borrow a match.
    draft = ("The city council voted seven to two to approve the parks department budget.\n\n"
             "Quantum chromodynamics describes gluon interactions inside protons.")
    final = draft + ("\n\nThe state legislature passed a transit funding bill "
                     "(Chicago Tribune, Mar 2026).")
    out = _entries(final, draft)
    claims = [e for e in out["entries"] if not e["passthrough"]]
    assert len(claims) == 3
    council, physics, transit = claims
    assert council["provenance"] == "corpus" and council["origin"] == "draft"
    assert physics["provenance"] == "unsupported"
    assert transit["provenance"] == "web" and transit["origin"] == "research"
    assert out["stats"]["claims"] == 3
    assert out["stats"]["research_added"] == 1


def test_bullet_gets_a_citation():
    md = "- Denise Carter was hired as superintendent by the school board after a national search"
    out = _entries(md, md)
    entry = out["entries"][0]
    assert entry["kind"] == "list_item"
    assert entry["supports"], "expected the bullet to match the school board story"
    assert entry["supports"][0]["article_id"] == "story-1"
    assert out["stats"]["list_items_cited"] == 1


def test_no_draft_means_no_origin_claim():
    out = _entries("The city council voted to approve the parks department budget.")
    claim = [e for e in out["entries"] if not e["passthrough"]][0]
    assert claim["origin"] is None
    assert claim["provenance"] == "corpus"


def test_sources_file_keeps_ingest_metadata():
    stories = [dict(STORIES[0], organization="Chicago Sun-Times", language="English",
                    metadata={"tags": ["Budget"]})]
    index = cm.embed_source_stories(stories, HashEmbed())
    src = cm.build_sources_file(stories, index)[0]
    assert src["organization"] == "Chicago Sun-Times"
    assert src["metadata"] == {"tags": ["Budget"]}


# ── Agent scoping ──────────────────────────────────────────────────────────

def _pipeline_result():
    from pipeline import PipelineResult
    stories = [{"title": f"Story {i}", "content": f"text about topic {i} council"} for i in range(4)]
    topics = {"Parks": [0, 1], "Schools": [2, 3]}
    return PipelineResult(
        stories=stories, topics=topics,
        story_topics=[["Parks"], ["Parks"], ["Schools"], ["Schools"]],
        broad_topics=dict(topics), specific_topics={},
    )


def test_scoped_result_hides_deselected_topics_and_stories():
    from dataclasses import replace
    from agent import execute_local_tool
    pr = _pipeline_result()
    scoped = replace(pr, topics={"Parks": [0, 1]}, broad_topics={"Parks": [0, 1]},
                     allowed_indices=frozenset({0, 1}))
    assert "Schools" not in execute_local_tool("view_topics", {}, scoped)
    assert "outside the topics" in execute_local_tool("read_story", {"index": 2}, scoped)
    assert json.loads(execute_local_tool("read_story", {"index": 0}, scoped))["title"] == "Story 0"
    hits = json.loads(execute_local_tool("search_stories", {"query": "council"}, scoped))
    assert {h["index"] for h in hits} == {0, 1}


def test_progress_text_matches_target_rule():
    from agent import _progress_report, _target_for_topic
    pr = _pipeline_result()
    text, _ = _progress_report(pr, {"Parks"}, set())
    assert "fewer than 8" in text and "max 10" in text
    assert _target_for_topic(7) == 7 and _target_for_topic(30) == 10


# ── Ingest metadata ────────────────────────────────────────────────────────

def test_numeric_wordpress_author_is_dropped_and_tags_kept():
    from ingest import _map_json_item
    wp = {"title": {"rendered": "Budget passes"},
          "content": {"rendered": "<p>" + "The council passed the budget. " * 5 + "</p>"},
          "author": 23, "tags": [3261, 887], "link": "https://streetcarsuburbs.news/x"}
    story = _map_json_item(wp, "")
    assert story.author == ""
    assert "tags" not in story.metadata

    rss = {"title": "Budget passes", "summary": "The council passed the budget. " * 5,
           "author": "Tina Sfondeles", "tags": [{"term": "Politics"}, {"term": "Budget"}]}
    story = _map_json_item(rss, "")
    assert story.author == "Tina Sfondeles"
    assert story.metadata["tags"] == ["Politics", "Budget"]


# ── Word export ────────────────────────────────────────────────────────────

def test_docx_keeps_cited_bullets_as_bullets():
    pytest.importorskip("docx")
    import io
    from docx import Document
    from app import _markdown_to_docx
    support = {"article_id": "story-1", "article_title": "School board hires superintendent",
               "passage_offset": 0, "passage_length": 10, "passage_text": "x"}
    entries = [
        {"content": "## Key Sources", "passthrough": True, "kind": "other", "supports": []},
        {"content": "- Denise Carter, superintendent hired after a national search",
         "passthrough": False, "kind": "list_item", "supports": [support]},
    ]
    data = _markdown_to_docx("", entries)
    doc = Document(io.BytesIO(data))
    bullets = [p for p in doc.paragraphs if p.style.name == "List Bullet"]
    assert len(bullets) == 1
    assert bullets[0].text.startswith("Denise Carter")
    assert bullets[0].runs[-1].font.superscript


# ── Egress ────────────────────────────────────────────────────────────────

def test_egress_plan_reflects_local_providers(monkeypatch):
    import egress
    monkeypatch.setenv("CHAT_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_CHAT_HOST", "http://localhost:11434")
    monkeypatch.setenv("EMBED_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    summary = egress.egress_summary()
    # With local chat and embeddings, only the OCR fallback and research
    # agent still reach Anthropic, and only OCR sends source material.
    assert summary["full_text_leaves_machine_to"] == ["api.anthropic.com"]
    research = [r for r in summary["rows"] if r["stage"] == "Add web research"][0]
    assert research["content"] == "derived"


# ── File routes ────────────────────────────────────────────────────────────

def test_book_files_served_by_id_and_output_dir_not_mounted(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app as app_mod
    import store
    monkeypatch.setattr(app_mod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(store, "LIBRARY_PATH", tmp_path / "library.json", raising=False)
    monkeypatch.setattr(store, "OUTPUT_DIR", tmp_path, raising=False)
    rec = {"id": "abc12345", "stem": "parks_beat_book", "title": "Parks", "status": "ready"}
    monkeypatch.setattr(store, "get_book", lambda bid: rec if bid == rec["id"] else None)
    (tmp_path / "parks_beat_book.md").write_text("# Parks\n")
    (tmp_path / "parks_beat_book.manifest.json").write_text("{}")

    client = TestClient(app_mod.app)
    assert client.get("/books/abc12345/files/markdown").text == "# Parks\n"
    assert client.get("/books/abc12345/files/manifest").json() == {}
    assert client.get("/books/abc12345/files/sources").status_code == 404
    assert client.get("/books/abc12345/files/library").status_code == 404
    assert client.get("/books/nope/files/markdown").status_code == 404
    assert client.get("/output/library.json").status_code == 404


# ── Research agent trace and shell ─────────────────────────────────────────

def test_research_web_activity_is_recorded():
    import research_agent as ra
    trace = {"web_searches": [], "web_results": [], "web_fetches": [], "cited_sources": []}
    content = [
        {"type": "server_tool_use", "name": "web_search", "input": {"query": "cps budget 2026"}},
        {"type": "web_search_tool_result", "content": [
            {"url": "https://cps.edu/budget", "title": "CPS Budget", "page_age": "2 days"},
            {"url": "https://cps.edu/budget", "title": "dupe"}]},
        {"type": "server_tool_use", "name": "web_fetch", "input": {"url": "https://cps.edu/budget"}},
        {"type": "text", "text": "x", "citations": [
            {"url": "https://cps.edu/budget", "title": "CPS Budget", "cited_text": "$9.9 billion"}]},
        {"type": "web_search_tool_result", "content": {"type": "web_search_tool_result_error"}},
    ]
    statuses = ra._record_web_activity(content, trace)
    assert trace["web_searches"] == ["cps budget 2026"]
    assert [r["url"] for r in trace["web_results"]] == ["https://cps.edu/budget"]
    assert trace["web_fetches"] == ["https://cps.edu/budget"]
    assert trace["cited_sources"][0]["cited_text"] == "$9.9 billion"
    assert [s[0] for s in statuses] == ["web_search", "web_fetch"]


def test_research_shell_does_not_inherit_api_keys(tmp_path, monkeypatch):
    import research_agent as ra
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    out = ra._run_bash("env", False, tmp_path)
    assert "secret" not in out
    assert f"HOME={tmp_path.resolve()}" in out


def test_draft_diff_counts_added_lines():
    from jobs import _draft_diff
    d = _draft_diff("# T\n\nA.\n", "# T\n\nA.\n\nB from the web.\n")
    assert d["changed"] and d["lines_added"] == 2 and d["lines_removed"] == 0
    assert "+B from the web." in d["unified_diff"]


def test_alderman_abbreviation_does_not_split_sentences():
    assert cm.split_into_sentences("Retired Ald. Walter Burnett will lead the agency. He starts Monday.") == [
        "Retired Ald. Walter Burnett will lead the agency.", "He starts Monday."]


def test_wrap_up_note_goes_on_trailing_user_message_only():
    import research_agent as ra
    msgs = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}]
    assert ra._append_user_note(msgs, "note")
    assert msgs[-1]["content"][-1] == {"type": "text", "text": "note"}
    assert msgs[-1]["content"][0]["type"] == "tool_result"
    paused = [{"role": "assistant", "content": []}]
    assert not ra._append_user_note(paused, "note")
    assert "LAST turn" in ra._wrap_up_note(1)
    assert ra.MAX_TURNS > ra.WRAP_UP_TURNS_LEFT


# ── Labels, guidance, and the finalize-only turn ───────────────────────────

def test_label_lines_are_not_claims():
    md = "\n".join([
        "## Key Sources & Players",
        "**At the Chicago Housing Authority:**",
        "Key agencies:",
        "**On the CHA beat:** The agency resists public records requests and delays FOIA replies.",
    ])
    entries = cm._segment_markdown(md)
    claims = [e for e in entries if e["needs_embedding"]]
    assert [e["content"] for e in claims] == [
        "**On the CHA beat:** The agency resists public records requests and delays FOIA replies."]
    assert all(e["section"] == "Key Sources & Players" for e in entries[1:])


def test_unmatched_reporting_tips_are_guidance_not_unsourced():
    md = ("## Reporting Tips\n\nFile records requests early and expect long delays from agencies.\n\n"
          "## Background & Context\n\nQuantum chromodynamics describes gluon interactions inside protons.")
    out = _entries(md, md)
    claims = [e for e in out["entries"] if not e["passthrough"]]
    assert [c["provenance"] for c in claims] == ["guidance", "unsupported"]
    assert claims[0]["section"] == "Reporting Tips"
    assert out["stats"]["guidance"] == 1 and out["stats"]["unsupported"] == 1


class _FakeBlock:
    def __init__(self, **kw): self.__dict__.update(kw)


class _FakeStream:
    def __init__(self, message): self.message = message
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __iter__(self): return iter([])
    def get_final_message(self): return self.message


class _FakeClient:
    def __init__(self, message):
        self.requests = []
        outer = self
        class Messages:
            def stream(self, **kw):
                outer.requests.append(kw)
                return _FakeStream(message)
        self.messages = Messages()


def test_finalize_only_turn_records_summary(tmp_path):
    import asyncio
    import research_agent as ra
    (tmp_path / "book.md").write_text("# Book\n")
    msg = _FakeBlock(stop_reason="tool_use", usage=None, content=[
        _FakeBlock(type="tool_use", name=ra.FINALIZE_TOOL_NAME,
                   input={"filename": "book.md", "summary": "Added the 2026 CHA budget."})])
    client = _FakeClient(msg)
    trace = {"model_calls": []}
    messages = [{"role": "user", "content": "start"}, {"role": "assistant", "content": []}]
    path, summary = asyncio.run(ra._finalize_only_turn(
        client, "system", [], messages, "cont-1", tmp_path, "book.md", trace))
    assert path == (tmp_path / "book.md").resolve() and summary == "Added the 2026 CHA budget."
    assert trace["finalized"] and trace["finalize_turn"]
    req = client.requests[0]
    assert req["tool_choice"] == {"type": "tool", "name": ra.FINALIZE_TOOL_NAME}
    assert req["container"] == "cont-1"
    assert req["messages"][-1]["role"] == "user"      # note added after the assistant turn
    assert len(messages) == 2                          # caller's transcript untouched


def test_finalize_only_turn_failure_is_harmless(tmp_path):
    import asyncio
    import research_agent as ra

    class Boom:
        class messages:
            @staticmethod
            def stream(**kw): raise RuntimeError("api down")
    trace = {"model_calls": []}
    path, summary = asyncio.run(ra._finalize_only_turn(
        Boom(), "system", [], [{"role": "user", "content": "x"}], None, tmp_path, "book.md", trace))
    assert path is None and summary == "" and "api down" in trace["finalize_turn_error"]


# ── Shell confinement, dedupe, reads, web basis ────────────────────────────

def test_research_shell_cannot_write_outside_its_folder(tmp_path):
    import research_agent as ra
    import shell_sandbox
    if shell_sandbox.detect()["kind"] is None:
        pytest.skip("no OS sandbox on this machine; the shell is disabled instead")
    box = tmp_path / "box"
    box.mkdir()
    outside = tmp_path / "escaped.txt"
    out = ra._run_bash(f"echo hi > inside.txt; echo x > {outside}; echo y > /tmp/beatbook_escape_probe; "
                       f"python3 -c \"open('{outside}.py','w')\"", False, box)
    assert (box / "inside.txt").read_text().strip() == "hi"
    assert not outside.exists() and not (tmp_path / "escaped.txt.py").exists()
    assert not Path("/tmp/beatbook_escape_probe").exists()
    assert "Operation not permitted" in out or "Read-only" in out or "Permission denied" in out


def test_shell_fails_closed_without_a_sandbox(tmp_path, monkeypatch):
    import research_agent as ra
    import shell_sandbox
    monkeypatch.setattr(shell_sandbox, "_cached", {"kind": None, "reason": "test"})
    assert ra._run_bash("echo hi > x.txt", False, tmp_path).startswith("Error: the shell is unavailable")
    assert not (tmp_path / "x.txt").exists()
    assert not ra.shell_status()["enabled"]
    assert all(t.get("name") != "bash" for t in ra.build_tools())



def test_repeated_blocks_are_counted_once():
    import research_agent as ra
    trace = {"web_searches": [], "web_results": [], "web_fetches": [], "cited_sources": []}
    turn = [
        {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "q"}},
        {"type": "server_tool_use", "id": "f1", "name": "web_fetch", "input": {"url": "https://a.org/x"}},
        {"type": "web_fetch_tool_result", "tool_use_id": "f1",
         "content": {"url": "https://a.org/x", "content": {"title": "A page"}}},
    ]
    ra._record_web_activity(turn, trace)
    ra._record_web_activity(turn, trace)          # resumed response repeats blocks
    assert trace["web_searches"] == ["q"] and trace["web_fetches"] == ["https://a.org/x"]
    assert trace["pages_read"] == [{"url": "https://a.org/x", "title": "A page"}]


def test_topic_scans_do_not_count_as_reads():
    from agent import _progress_report
    pr = _pipeline_result()
    text, met = _progress_report(pr, {"Parks", "Schools"}, set())
    assert not met and "does not count" in text


def test_web_basis_distinguishes_read_pages_from_snippets():
    import jobs
    trace = {"pages_read": [{"url": "https://www.thecha.org/x", "title": "Keith Pettigrew | CHA"}],
             "web_results": [{"url": "https://washingtoncitypaper.com/a", "title": "DCHA audit"}]}
    entries = {"entries": [
        {"provenance": "web", "content": "He began April 20 (CHA press release, Apr. 2026)."},
        {"provenance": "web", "content": "The audit found 19 weaknesses (Washington City Paper, 2024)."},
        {"provenance": "web", "content": "The budget is $1.4 billion (The Real Deal, Apr. 2026)."},
        {"provenance": "web", "content": "The agency is large (and old)."},
    ]}
    counts = jobs.tag_web_basis(entries, trace)   # no page text: falls back to names
    assert [e["web_basis"] for e in entries["entries"]] == ["read", "snippet", "unconfirmed", "unattributed"]
    assert counts == {"read": 1, "snippet": 1, "unconfirmed": 1, "unattributed": 1, "method": "names"}


def test_web_claims_checked_against_page_text():
    import jobs
    page = ("The Cook County Board of Review voted to set the Arlington Park valuation at "
            "$124.7 million, below the school districts' request, and the Bears said they "
            "were disappointed with the board's decision on the property. ") * 3
    trace = {"pages_read": [{"url": "https://chicago.suntimes.com/bears/x", "title": "Bears disappointed"}],
             "web_results": []}
    entries = {"calibration": {"threshold": 0.5}, "entries": [
        # Supported by the page, and names no source: page text still backs it.
        {"provenance": "web", "kind": "sentence", "passthrough": False,
         "content": "The Board of Review set the Arlington Park valuation at $124.7 million."},
        # Same topic, but the figure is not on the page.
        {"provenance": "web", "kind": "sentence", "passthrough": False,
         "content": "The Board of Review set the Arlington Park valuation at $138 million."},
    ]}
    counts = jobs.tag_web_basis(entries, trace, {"https://chicago.suntimes.com/bears/x": page}, HashEmbed())
    a, b = entries["entries"]
    assert a["web_basis"] == "read" and a["web_support"]["url"].endswith("/bears/x")
    assert b["web_basis"] == "unattributed"
    assert counts["method"] == "page_text" and counts["read"] == 1


def test_paragraph_attribution_covers_its_sentences():
    import jobs
    items = [
        {"kind": "sentence", "passthrough": False, "content": "Steele dissented."},
        {"kind": "sentence", "passthrough": False, "content": "The Bears were disappointed (Chicago Sun-Times, Feb 2024)."},
        {"kind": "other", "passthrough": True, "content": ""},
        {"kind": "sentence", "passthrough": False, "content": "Unrelated next paragraph."},
    ]
    names = jobs._paragraph_names(items)
    assert names[0] == ["Chicago Sun-Times"] and names[3] == []


def test_research_is_warned_about_unattributed_additions():
    import research_agent as ra
    before = "## Key Sources\n\n- **Jane Doe** — director of the housing agency since 2020.\n"
    after = before + ("- **Jawanza Malone** — chairman of the housing authority board as of mid-2026.\n"
                      "- **Matt Topic** — lawyer for the plaintiffs in the open meetings case (Loevy press release, Apr 2026).\n")
    missing = ra._unattributed_additions(before, after)
    assert missing == ["- **Jawanza Malone** — chairman of the housing authority board as of mid-2026."]
    trace = {}
    note = ra._attribution_note(before, after, trace)
    assert "name no source" in note and trace["attribution_warnings"] == 1
    tips = "## Reporting Tips\n\nFile records requests early and expect long delays from the agency.\n"
    assert ra._unattributed_additions("", tips) == []


def test_fetched_page_text_is_kept_in_memory():
    import research_agent as ra
    trace = {"web_searches": [], "web_results": [], "web_fetches": [], "cited_sources": []}
    ra._record_web_activity([{"type": "web_fetch_tool_result", "tool_use_id": "f1", "content": {
        "url": "https://a.org/x", "content": {"title": "A", "source": {"type": "text", "data": "Board roster text"}}}}], trace)
    assert trace["_page_texts"] == {"https://a.org/x": "Board roster text"}


def test_replaced_draft_claims_are_listed():
    draft = "The board chair is Matthew Brewer, who certified the appointment.\n\nThe city council voted to approve the parks department budget."
    final = "The board chair is Jawanza Malone, as of mid-2026.\n\nThe city council voted to approve the parks department budget."
    out = _entries(final, draft)
    assert out["replaced_draft_claims"] == ["The board chair is Matthew Brewer, who certified the appointment."]
    assert out["stats"]["research_replaced"] == 1
