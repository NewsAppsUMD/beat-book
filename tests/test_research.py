"""Tests for the quoted-facts research design: fact checks, attribution,
insertion, the agent loop (scripted client, no network), and mapping web
claims back to their quotes."""

import asyncio
import json

import pytest

import research_facts as rf
from test_transparency import HashEmbed, _FakeBlock as B, _FakeStream, _fake_http

PAGE = ("Keith Pettigrew officially began his tenure as Chief Executive Officer of the Chicago "
        "Housing Authority on April 20, 2026. A native of Washington, D.C., Pettigrew grew up in "
        "public housing at Barry Farm. He previously led the Alexandria Redevelopment and Housing "
        "Authority, where the vacancy rate stayed below 3 percent and the agency issued 4,000 "
        "vouchers in six months.")

DRAFT = """# CHA Beat Book

## Beat Overview

The Chicago Housing Authority is run by a board appointed by the mayor.

## Key Sources & Players

- **Keith Pettigrew** — CEO of the Chicago Housing Authority, appointed over the mayor's objection.
- **Matthew Brewer** — former board chair who certified the appointment.

## Calendar & Recurring Events

| When | What |
|---|---|
| Monthly | CHA board meeting at agency headquarters |
"""


# ── check_fact ─────────────────────────────────────────────────────────────

def test_fact_backed_by_verbatim_quote_is_accepted():
    quote = "Keith Pettigrew officially began his tenure as Chief Executive Officer of the Chicago Housing Authority on April 20, 2026."
    assert rf.check_fact("Pettigrew began his tenure as CHA chief executive officer on April 20, 2026.", quote, PAGE) is None


def test_quote_matching_ignores_curly_quotes_dashes_and_markdown():
    page = "The board said it was “disappointed” — and **reserved the right** to appeal the [ruling](https://x.org)."
    quote = 'The board said it was "disappointed" - and reserved the right to appeal the ruling.'
    assert rf.check_fact("The board said it was disappointed and reserved the right to appeal the ruling.", quote, page) is None


@pytest.mark.parametrize("fact,quote,reason", [
    ("Pettigrew grew up in public housing at Barry Farm in Washington.",
     "Pettigrew grew up in public housing at Barry Farm in Washington, D.C., in the 1970s.", "does not appear"),
    ("The agency issued 5,000 vouchers in six months under Pettigrew.",
     "the agency issued 4,000 vouchers in six months.", "not in the quote: 5000"),
    ("Pettigrew was fired by the board after a dispute with the mayor over his contract.",
     "Pettigrew grew up in public housing at Barry Farm.", "says more than the quote"),
    ("Pettigrew grew up in public housing (CHA press release, Apr 2026).",
     "Pettigrew grew up in public housing at Barry Farm.", "leave the attribution off"),
    ("Grew up there.", "Pettigrew grew up in public housing at Barry Farm.", "too short"),
])
def test_bad_facts_are_rejected_with_a_reason(fact, quote, reason):
    why = rf.check_fact(fact, quote, PAGE)
    assert why is not None and reason in why


# ── Attribution ────────────────────────────────────────────────────────────

def test_attribution_uses_model_name_only_when_it_matches_the_page():
    attr, name = rf.attribution_for("Chicago Housing Authority", "Apr 20, 2026",
                                    "https://www.thecha.org/news/x", "Pettigrew begins | Chicago Housing Authority")
    assert attr == "(Chicago Housing Authority, Apr 20, 2026)"
    attr, name = rf.attribution_for("Chicago Tribune", "Apr 2026",
                                    "https://www.thecha.org/news/x", "Pettigrew begins | Chicago Housing Authority")
    assert name == "Chicago Housing Authority"          # model's name didn't match the page
    attr, _ = rf.attribution_for("", "sometime last spring", "https://www.loevy.com/p", "Press release",
                                 now=1790000000)
    assert attr.startswith("(loevy.com, retrieved ")


def test_app_written_attributions_are_recognized():
    import citation_matcher as cm
    assert cm.attributed_sources("x (loevy.com, retrieved Sep 2026).") == ["loevy.com"]


# ── Insertion ──────────────────────────────────────────────────────────────

def _lines_in_order(sub, full):
    it = iter(full.split("\n"))
    return all(any(line == x for x in it) for line in sub.split("\n"))


def test_insertion_never_changes_existing_lines():
    facts = [
        {"text": "He started April 20, 2026 (Chicago Housing Authority, Apr 20, 2026).",
         "section": "Key Sources & Players", "after_line": "**Keith Pettigrew** — CEO"},
        {"text": "The board has ten members (Chicago Housing Authority, Sep 2026).",
         "section": "Beat Overview", "after_line": "The Chicago Housing Authority is run"},
        {"text": "Jawanza Malone chairs the board (Chicago Housing Authority, Sep 2026).",
         "section": "Key Sources & Players", "after_line": ""},
        {"text": "Meetings are streamed online (Chicago Housing Authority, Sep 2026).",
         "section": "Calendar & Recurring Events", "after_line": "| Monthly | CHA board meeting"},
    ]
    out = rf.insert_facts(DRAFT, facts)
    assert _lines_in_order(DRAFT, out)                      # every draft line survives, in order
    lines = out.split("\n")
    i = lines.index("- **Keith Pettigrew** — CEO of the Chicago Housing Authority, appointed over the mayor's objection.")
    assert lines[i + 1] == "  - He started April 20, 2026 (Chicago Housing Authority, Apr 20, 2026)."
    assert "- Jawanza Malone chairs the board (Chicago Housing Authority, Sep 2026)." in lines
    assert lines.index("- Jawanza Malone chairs the board (Chicago Housing Authority, Sep 2026).") > \
        lines.index("- **Matthew Brewer** — former board chair who certified the appointment.")
    j = lines.index("The Chicago Housing Authority is run by a board appointed by the mayor.")
    assert lines[j + 1] == "" and lines[j + 2].startswith("The board has ten members")
    assert lines.index("Meetings are streamed online (Chicago Housing Authority, Sep 2026).") > \
        lines.index("| Monthly | CHA board meeting at agency headquarters |")


def test_placement_errors_are_explained():
    assert "no section or subsection named" in rf.find_placement(DRAFT, "Budget", "")
    assert "does not match" in rf.find_placement(DRAFT, "Beat Overview", "Nothing like this line")
    assert rf.find_placement(DRAFT, "beat overview", "") is None      # case-insensitive heading


# ── The agent loop ─────────────────────────────────────────────────────────

GOOD_QUOTE = "Keith Pettigrew officially began his tenure as Chief Executive Officer of the Chicago Housing Authority on April 20, 2026."


def _run(tmp_path, monkeypatch, script, draft=DRAFT):
    import research_agent as ra
    import page_fetcher as pf
    monkeypatch.setattr(pf, "CACHE_DIR", tmp_path / "cache")
    html = f"<html><head><title>Pettigrew begins | Chicago Housing Authority</title></head><body><p>{PAGE}</p></body></html>".encode()
    calls = _fake_http(monkeypatch, {"https://www.thecha.org/news/pettigrew": (200, {"content-type": "text/html"}, html)})
    (tmp_path / "book.md").write_text(draft)
    sent = []

    class Client:
        class messages:
            @staticmethod
            def stream(**kw):
                sent.append(kw)
                return _FakeStream(script[min(len(sent) - 1, len(script) - 1)])
    monkeypatch.setattr(ra, "Anthropic", lambda **kw: Client())
    trace = {}
    out = asyncio.run(ra.run_research_agent(tmp_path, "book.md", "key", trace=trace))
    return out, trace, sent, calls


def test_loop_inserts_only_verified_facts(tmp_path, monkeypatch):
    import research_agent as ra
    submit = lambda i, **kw: B(type="tool_use", id=f"f{i}", name=ra.SUBMIT_TOOL_NAME, input=kw)
    script = [
        B(stop_reason="tool_use", usage=None, content=[
            B(type="server_tool_use", id="s1", name="web_search", input={"query": "Pettigrew CHA"}),
            B(type="web_search_tool_result", tool_use_id="s1", content=[
                B(url="https://www.thecha.org/news/pettigrew", title="Pettigrew begins", page_age=""),
                B(url="https://www.wbez.org/snippet-only", title="Snippet", page_age="")]),
            B(type="tool_use", id="t1", name=ra.FETCH_TOOL_NAME, input={"url": "https://www.thecha.org/news/pettigrew"}),
        ]),
        B(stop_reason="tool_use", usage=None, content=[
            submit(1, fact="Pettigrew began his tenure as CHA chief executive officer on April 20, 2026.",
                   quote=GOOD_QUOTE, url="https://www.thecha.org/news/pettigrew",
                   source_name="Chicago Housing Authority", published="Apr 20, 2026",
                   section="Key Sources & Players", after_line="**Keith Pettigrew** — CEO"),
            submit(2, fact="Pettigrew grew up in public housing in Chicago's Cabrini-Green.",
                   quote="Pettigrew grew up in public housing at Cabrini-Green.",
                   url="https://www.thecha.org/news/pettigrew", source_name="CHA", section="Key Sources & Players"),
            submit(3, fact="The CHA board voted 6 to 4 to approve Pettigrew's contract terms.",
                   quote="The CHA board voted 6 to 4 to approve the contract.",
                   url="https://www.wbez.org/snippet-only", source_name="WBEZ", section="Beat Overview"),
            B(type="tool_use", id="fin", name=ra.FINALIZE_TOOL_NAME, input={"summary": "Added Pettigrew's start date."}),
        ]),
    ]
    out, trace, sent, calls = _run(tmp_path, monkeypatch, script)
    assert _lines_in_order(DRAFT, out)
    assert "  - Pettigrew began his tenure as CHA chief executive officer on April 20, 2026 (Chicago Housing Authority, Apr 20, 2026)." in out
    assert "Cabrini" not in out and "6 to 4" not in out
    assert [f["id"] for f in trace["facts_accepted"]] == [1]
    reasons = [r["reason"] for r in trace["facts_rejected"]]
    assert any("does not appear" in r for r in reasons) and any("not been fetched" in r for r in reasons)
    assert trace["finalized"] and trace["summary"] == "Added Pettigrew's start date."
    assert json.loads((tmp_path / "facts.json").read_text())[0]["quote"] == GOOD_QUOTE
    assert (tmp_path / "book.md").read_text() == out
    names = [t["name"] for t in sent[0]["tools"]]
    assert names == ["web_search", "fetch_page", "submit_fact", "finalize_research"]
    assert "BEGIN BEAT BOOK" in sent[0]["messages"][0]["content"][0]["text"]


def test_loop_records_summary_when_model_never_finalizes(tmp_path, monkeypatch):
    import research_agent as ra
    script = [
        B(stop_reason="end_turn", usage=None, content=[B(type="text", text="Nothing to add.")]),
        B(stop_reason="tool_use", usage=None, content=[
            B(type="tool_use", id="fin", name=ra.FINALIZE_TOOL_NAME, input={"summary": "No gaps found."})]),
    ]
    out, trace, sent, _ = _run(tmp_path, monkeypatch, script)
    assert out == DRAFT and trace["finalize_turn"] and trace["summary"] == "No gaps found."
    assert sent[-1]["tool_choice"] == {"type": "tool", "name": ra.FINALIZE_TOOL_NAME}


def test_repeated_search_blocks_are_counted_once():
    import research_agent as ra
    trace = {"web_searches": [], "web_results": []}
    turn = [B(type="server_tool_use", id="s1", name="web_search", input={"query": "q"}),
            B(type="web_search_tool_result", tool_use_id="s1", content=[B(url="https://a.org", title="A", page_age="")])]
    ra._record_web_activity(turn, trace)
    ra._record_web_activity(turn, trace)
    assert trace["web_searches"] == ["q"] and len(trace["web_results"]) == 1


def test_wrap_up_note_goes_on_trailing_user_message_only():
    import research_agent as ra
    msgs = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}]
    assert ra._append_user_note(msgs, "note") and msgs[-1]["content"][-1]["text"] == "note"
    assert not ra._append_user_note([{"role": "assistant", "content": []}], "note")


# ── Back to the reader: every web claim maps to a quote ────────────────────

def test_every_web_claim_maps_to_its_quote_and_no_story_claim_is_lost():
    import citation_matcher as cm
    import jobs
    fact = {"id": 1, "url": "https://www.thecha.org/news/pettigrew", "final_url": "https://www.thecha.org/news/pettigrew",
            "title": "Pettigrew begins", "source_name": "Chicago Housing Authority", "quote": GOOD_QUOTE,
            "text": "Pettigrew began his tenure as CHA chief executive officer on April 20, 2026 (Chicago Housing Authority, Apr 20, 2026).",
            "section": "Key Sources & Players", "after_line": "**Keith Pettigrew** — CEO"}
    final = rf.insert_facts(DRAFT, [fact])
    client = HashEmbed()
    index = cm.embed_source_stories([{"title": "t", "content": "The housing authority board met. " * 40}], client)
    entries = cm.markdown_to_beatbook_entries(final, index, client, draft_markdown=DRAFT)
    counts = jobs.tag_web_facts(entries, {"facts_accepted": [fact]})
    assert counts == {"quoted": 1, "unverified": 0}
    web = [e for e in entries["entries"] if e.get("provenance") == "web"]
    assert web[0]["web_support"]["quote"] == GOOD_QUOTE
    assert entries["stats"]["research_replaced"] == 0


def test_multi_sentence_fact_placed_as_a_bullet_maps_to_its_quote():
    import citation_matcher as cm
    import jobs
    fact = {"id": 1, "url": "https://chicago.suntimes.com/x", "final_url": "https://chicago.suntimes.com/x",
            "title": "t", "source_name": "Chicago Sun-Times", "quote": "q" * 30,
            "text": "No Republican filed for Cook County Board President this year. She has said it will be her last term (Chicago Sun-Times, Mar 17, 2026).",
            "section": "Key Sources & Players", "after_line": "**Keith Pettigrew** — CEO"}
    final = rf.insert_facts(DRAFT, [fact])
    client = HashEmbed()
    index = cm.embed_source_stories([{"title": "t", "content": "The housing authority board met. " * 40}], client)
    entries = cm.markdown_to_beatbook_entries(final, index, client, draft_markdown=DRAFT)
    assert jobs.tag_web_facts(entries, {"facts_accepted": [fact]}) == {"quoted": 1, "unverified": 0}


# ── Relaxed rules: dates from the page, figure-and-name facts ──────────────

RESULTS_PAGE = ("Posted March 18, 2026. Cook County Board President, Democratic primary, certified results. "
                "Toni Preckwinkle 470,960 69.03% Brendan Reilly 211,278 30.97% Precincts reporting 100%.")


def test_table_row_quote_supports_a_figures_and_names_fact():
    quote = "Toni Preckwinkle 470,960 69.03% Brendan Reilly 211,278 30.97%"
    fact = "In the certified primary results, Preckwinkle received 470,960 votes (69.03%) to Reilly's 211,278 (30.97%)."
    assert rf.check_fact(fact, quote, RESULTS_PAGE) is None


def test_table_rule_still_needs_every_name_and_figure():
    quote = "Toni Preckwinkle 470,960 69.03% Brendan Reilly 211,278 30.97%"
    # A name the quote doesn't have: back to the strict bar.
    assert "says more" in rf.check_fact(
        "In the certified primary results, Preckwinkle beat Reilly and Kaegi with 470,960 votes (69.03%).", quote, RESULTS_PAGE)
    # A figure the quote doesn't have.
    assert "not in the quote: 480960" in rf.check_fact(
        "Preckwinkle received 480,960 votes (69.03%) to Reilly's 211,278 (30.97%).", quote, RESULTS_PAGE)
    # Narrative facts without figures keep the strict bar.
    assert "says more" in rf.check_fact(
        "Preckwinkle and Reilly traded accusations about property tax delays all spring.", quote, RESULTS_PAGE)


def test_dates_may_come_from_elsewhere_on_the_page():
    quote = "Toni Preckwinkle 470,960 69.03% Brendan Reilly 211,278 30.97%"
    fact = "As of March 18, 2026, certified results showed Preckwinkle with 470,960 votes (69.03%) to Reilly's 211,278 (30.97%)."
    assert rf.check_fact(fact, quote, RESULTS_PAGE) is None
    bad = "As of April 2, 2026, certified results showed Preckwinkle with 470,960 votes (69.03%) to Reilly's 211,278 (30.97%)."
    assert "not on the page: apr 2" in rf.check_fact(bad, quote, RESULTS_PAGE)
    assert "not on the page: 2025" in rf.check_fact(
        "In 2025, certified results showed Preckwinkle with 470,960 votes (69.03%) to Reilly's 211,278 (30.97%).", quote, RESULTS_PAGE)


# ── Excerpted quotes, extraction spacing, subsections, URL dates ───────────

ARTICLE = ("Nicholson had won 62% of the vote to Steele's 38%, with 96% of precincts reporting. "
           "Steele did not reply to messages seeking comment. Nicholson was a longtime adviser to "
           "former Illinois Senate President John Cullerton who successfully capitalized on "
           "Steele's notoriety. The Bears ' board of directors met Thursday and decided to move "
           "forward. The ruling will affect 1.3 million people who rely on TPS to live and work in "
           "the United States legally, and advocates said they fear it.")


@pytest.mark.parametrize("quote", [
    # Two passages joined by an ellipsis, then by nothing at all.
    "Nicholson had won 62% of the vote to Steele's 38%, with 96% of precincts reporting. ... "
    "Nicholson was a longtime adviser to former Illinois Senate President John Cullerton.",
    "Nicholson had won 62% of the vote to Steele's 38%, with 96% of precincts reporting. "
    "Nicholson was a longtime adviser to former Illinois Senate President John Cullerton.",
    # A four-dot ellipsis and quote marks the page doesn't have.
    '"Nicholson had won 62% of the vote to Steele\'s 38%, with 96% of precincts reporting.... '
    'Nicholson was a longtime adviser to former Illinois Senate President John Cullerton."',
])
def test_excerpted_quotes_are_accepted(quote):
    parts, why = rf.locate_quote(quote, ARTICLE)
    assert parts is not None and len(parts) == 2, why
    fact = "Nicholson won 62% to Steele's 38% and was a longtime adviser to Senate President John Cullerton."
    assert rf.check_fact(fact, quote, ARTICLE) is None


def test_excerpts_still_need_every_word_on_the_page():
    quote = ("Nicholson had won 62% of the vote to Steele's 38%. ... "
             "Nicholson was a longtime adviser to Mayor Richard M. Daley.")
    parts, why = rf.locate_quote(quote, ARTICLE)
    assert parts is None and "Mayor Richard M" in why
    parts, why = rf.locate_quote("Nicholson had won 62%. ... Steele did not.", ARTICLE)
    assert parts is None and "at least" in why


def test_truncated_sentence_and_extraction_spacing_match():
    assert rf.locate_quote("The ruling will affect 1.3 million people who rely on TPS to live and "
                           "work in the United States legally.", ARTICLE)[0]
    assert rf.locate_quote("The Bears' board of directors met Thursday and decided to move forward.", ARTICLE)[0]


def test_facts_can_go_under_a_subsection():
    draft = ("# Book\n\n## Key Topics & Themes\n\n### Board of Review Elections\n\n"
             "Steele lost her primary.\n\n### Stadium Fight\n\nThe Bears want a new stadium.\n\n"
             "## Calendar\n\n- Monthly meetings of the county board.\n")
    assert rf.find_placement(draft, "Board of Review Elections", "") is None
    out = rf.insert_facts(draft, [{"text": "Nicholson won 62% (WBEZ, Mar 17, 2026).",
                                   "section": "Board of Review Elections", "after_line": ""}])
    lines = out.split("\n")
    assert lines.index("Nicholson won 62% (WBEZ, Mar 17, 2026).") < lines.index("### Stadium Fight")
    assert lines.index("Nicholson won 62% (WBEZ, Mar 17, 2026).") > lines.index("Steele lost her primary.")
    why = rf.find_placement(draft, "Budget", "")
    assert "Subsections: Board of Review Elections; Stadium Fight" in why


def test_a_date_in_the_url_counts_as_on_the_page():
    quote = "Toni Preckwinkle 470,960 69.03% Brendan Reilly 211,278 30.97%"
    page = "Toni Preckwinkle 470,960 69.03% Brendan Reilly 211,278 30.97%"
    fact = "In the March 17 primary, Preckwinkle received 470,960 votes (69.03%) to Reilly's 211,278 (30.97%)."
    assert "mar 17" in rf.check_fact(fact, quote, page)
    assert rf.check_fact(fact, quote, page, "https://chicago.suntimes.com/elections/2026/03/17/cook-county") is None
