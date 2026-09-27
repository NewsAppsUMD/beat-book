"""Tests for the Phase 1 transparency changes: citable bullets and table rows,
draft-vs-research provenance, agent topic scoping, ingest metadata fixes,
the Word export, and the per-book file routes."""

import hashlib
import json
import re

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

def test_draft_diff_counts_added_lines():
    from jobs import _draft_diff
    d = _draft_diff("# T\n\nA.\n", "# T\n\nA.\n\nB from the web.\n")
    assert d["changed"] and d["lines_added"] == 2 and d["lines_removed"] == 0
    assert "+B from the web." in d["unified_diff"]


def test_alderman_abbreviation_does_not_split_sentences():
    assert cm.split_into_sentences("Retired Ald. Walter Burnett will lead the agency. He starts Monday.") == [
        "Retired Ald. Walter Burnett will lead the agency.", "He starts Monday."]


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


# ── Shell confinement, dedupe, reads, web basis ────────────────────────────

def test_topic_scans_do_not_count_as_reads():
    from agent import _progress_report
    pr = _pipeline_result()
    text, met = _progress_report(pr, {"Parks", "Schools"}, set())
    assert not met and "does not count" in text


def test_replaced_draft_claims_are_listed():
    draft = "The board chair is Matthew Brewer, who certified the appointment.\n\nThe city council voted to approve the parks department budget."
    final = "The board chair is Jawanza Malone, as of mid-2026.\n\nThe city council voted to approve the parks department budget."
    out = _entries(final, draft)
    assert out["replaced_draft_claims"] == ["The board chair is Matthew Brewer, who certified the appointment."]
    assert out["stats"]["research_replaced"] == 1


# ── App-side page fetcher ──────────────────────────────────────────────────

def _fake_http(monkeypatch, routes):
    """Route page_fetcher's httpx client through a MockTransport."""
    import httpx
    import page_fetcher as pf
    calls = []
    def handler(request):
        calls.append(str(request.url))
        status, headers, body = routes[str(request.url)]
        return httpx.Response(status, headers=headers, content=body)
    real = httpx.Client
    monkeypatch.setattr(pf.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(pf, "_is_blocked_ip", lambda host: host.startswith("10.") or host == "internal")
    monkeypatch.setattr(pf, "_firecrawl_available", lambda: False)
    return calls


HTML = b"<html><head><title>CHA Board</title></head><body><p>Jawanza Malone chairs the board. " \
       b"See https://example.org/more for details.</p></body></html>"


def test_fetcher_caches_repeats_and_limits_urls(tmp_path, monkeypatch):
    import page_fetcher as pf
    monkeypatch.setattr(pf, "CACHE_DIR", tmp_path / "cache")
    calls = _fake_http(monkeypatch, {"https://thecha.org/board": (200, {"content-type": "text/html"}, HTML)})
    f = pf.PageFetcher(max_fetches=3, seed_text=["The board page is https://thecha.org/board"])
    first = f.fetch("https://thecha.org/board")
    assert first["ok"] and "Jawanza Malone" in first["text"] and "untrusted" in first["text"]
    assert first["record"]["title"] == "CHA Board"
    again = f.fetch("https://thecha.org/board#top")
    assert again["repeat"] and "already fetched" in again["text"]
    assert len(calls) == 1
    # Not seen in the run yet: refused. Seen on a page it read: allowed.
    assert "has not appeared" in f.fetch("https://evil.example/steal")["text"]
    assert "https://example.org/more" in f.allowed
    # A new run reuses the disk cache without a network call.
    g = pf.PageFetcher(max_fetches=3, seed_text=["https://thecha.org/board"])
    assert g.fetch("https://thecha.org/board")["record"]["cached"] and len(calls) == 1


def test_fetcher_blocks_redirects_to_private_addresses(tmp_path, monkeypatch):
    import page_fetcher as pf
    monkeypatch.setattr(pf, "CACHE_DIR", tmp_path / "cache")
    _fake_http(monkeypatch, {"https://public.org/x": (302, {"location": "http://internal/admin"}, b"")})
    f = pf.PageFetcher(max_fetches=3, seed_text=["https://public.org/x"])
    out = f.fetch("https://public.org/x")
    assert not out["ok"] and "private" in out["text"]


def test_fetcher_enforces_fetch_limit(tmp_path, monkeypatch):
    import page_fetcher as pf
    monkeypatch.setattr(pf, "CACHE_DIR", tmp_path / "cache")
    _fake_http(monkeypatch, {f"https://a.org/{i}": (200, {"content-type": "text/plain"}, b"page text") for i in range(3)})
    f = pf.PageFetcher(max_fetches=2, seed_text=[" ".join(f"https://a.org/{i}" for i in range(3))])
    assert f.fetch("https://a.org/0")["ok"] and f.fetch("https://a.org/1")["ok"]
    assert "limit" in f.fetch("https://a.org/2")["text"]


# ── Matching fixes ─────────────────────────────────────────────────────────

def test_date_fragments_are_not_source_names():
    assert cm.attributed_sources("He began (CHA press release, Apr. 20, 2026).") == ["CHA press release"]
    assert cm.attributed_sources("(WTTW News, March 17, 2026)") == ["WTTW News"]


def test_adding_an_attribution_does_not_make_a_new_claim():
    draft = "A longtime operative rather than a housing or tax policy expert, she faces a learning curve."
    final = "A longtime operative rather than a housing or tax policy expert, she faces a learning curve (WTTW News voter guide, Mar. 2026)."
    assert cm._claim_key(draft) == cm._claim_key(final)
    out = _entries(final, draft)
    claim = [e for e in out["entries"] if not e["passthrough"]][0]
    assert claim["origin"] == "draft" and out["stats"]["research_replaced"] == 0




def test_app_page_is_not_cached_and_version_changes_with_the_frontend(tmp_path, monkeypatch):
    import os, time
    from fastapi.testclient import TestClient
    import app as app_mod
    client = TestClient(app_mod.app)
    assert client.get("/").headers["cache-control"] == "no-cache"
    v1 = client.get("/api/version").json()["frontend"]
    js = app_mod.Path("static/reader.js")
    st = js.stat()
    try:
        os.utime(js, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        assert client.get("/api/version").json()["frontend"] != v1
    finally:
        os.utime(js, ns=(st.st_atime_ns, st.st_mtime_ns))
