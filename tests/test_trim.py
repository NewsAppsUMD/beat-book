"""The selection-based trim for drafts far over their word target, and the
section budgets in the length instruction."""

from chat_provider import ChatResponse
import agent
import trim

SECTIONS = ["Beat Overview", "Key Topics & Themes", "Key Sources & Players", "Story Ideas & Angles",
            "Background & Context", "Reporting Tips", "Calendar & Recurring Events"]


def _book():
    """Seven sections, each with four 60-word paragraphs (about 1,700 words)."""
    parts = ["# Immigration Beat Book", "", "*A guide*", ""]
    n = 0
    for name in SECTIONS:
        parts += [f"## {name}", ""]
        for _ in range(4):
            n += 1
            parts += [f"Paragraph {n} names Judge Reddick{n} and {n * 100} coalition members. "
                      + "More words here about the case. " * 9, ""]
    parts += ["- **Locke Bowman** — attorney for the coalition.", "  - Sub-point about Bowman.", ""]
    return "\n".join(parts)


BOOK = _book()


class Selector:
    """A chat provider that answers the selection with fixed unit numbers."""
    write_model = "deepseek-test"

    def __init__(self, remove=None, text=None, error=None):
        self.remove, self.text, self.error, self.prompts = remove, text, error, []

    def create(self, **kw):
        self.prompts.append(kw["messages"][0]["content"])
        if self.error:
            raise self.error
        if self.text is not None:
            return ChatResponse(content=[{"type": "text", "text": self.text}], stop_reason="end_turn")
        return ChatResponse(content=[{"type": "tool_use", "id": "t", "name": "remove_units",
                                      "input": {"remove": self.remove}}], stop_reason="tool_use")


def test_length_directive_budgets_each_section():
    text = agent._length_directive(1000)
    assert "Beat Overview about 120" in text and "Key Topics & Themes about 300" in text
    assert sum(w for _, w in agent.section_budgets(2000)) == 2000


def test_units_round_trip_and_headings_are_fixed():
    pieces = trim.split_units(BOOK)
    assert trim.join_units(pieces) == BOOK.strip() + "\n"
    units = [p for p in pieces if p["unit"]]
    assert len(units) == 29                                    # 28 paragraphs + 1 bullet with its sub-point
    assert units[-1]["lines"] == ["- **Locke Bowman** — attorney for the coalition.", "  - Sub-point about Bowman."]
    assert not any(p["unit"] for p in pieces if p["lines"][0].startswith("#"))


def test_draft_within_length_is_not_sent():
    sel = Selector(remove=[1])
    out, rec = trim.trim_draft(BOOK, 1500, sel, 4096)
    assert out == BOOK and not rec["used"] and sel.prompts == []


def test_selected_units_are_removed_verbatim_until_close_to_target():
    sel = Selector(remove=list(range(1, 29)))                  # names far more than needed
    out, rec = trim.trim_draft(BOOK, 1000, sel, 4096)
    assert rec["used"] and 900 <= rec["words_after"] <= 1100
    kept = [p for p in trim.split_units(out) if p["unit"]]
    original = {"\n".join(p["lines"]) for p in trim.split_units(BOOK) if p["unit"]}
    assert all("\n".join(p["lines"]) in original for p in kept)   # nothing reworded
    for name in SECTIONS:
        assert f"## {name}" in out                             # every section stays, with content
    assert "100" in rec["details_dropped"]                     # paragraph 1's "100 coalition members"
    assert rec["removed"] and len(rec["removed"]) < len(rec["selected"])   # stopped once close enough
    assert "[29] (Calendar & Recurring Events" in sel.prompts[0]


def test_a_section_is_never_emptied():
    # Units 1-4 are the whole Beat Overview.
    out, rec = trim.trim_draft(BOOK, 1000, Selector(remove=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]), 4096)
    overview = out.split("## Beat Overview")[1].split("## Key Topics")[0]
    assert "Paragraph 4 " in overview and "Paragraph 1 " not in overview


def test_selection_written_in_prose_is_read():
    out, rec = trim.trim_draft(BOOK, 1000, Selector(text='I will remove these.\n```json\n{"remove": [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]}\n```'), 4096)
    assert rec["used"] and rec["words_after"] < rec["words_before"]


def test_too_small_a_cut_or_no_selection_keeps_the_draft():
    out, rec = trim.trim_draft(BOOK, 1000, Selector(remove=[5]), 4096)
    assert out == BOOK and "save too little" in rec["reason"]
    out, rec = trim.trim_draft(BOOK, 1000, Selector(text="Sorry, I can't."), 4096)
    assert out == BOOK and "named no units" in rec["reason"]


def test_failed_call_keeps_the_draft():
    out, rec = trim.trim_draft(BOOK, 1000, Selector(error=RuntimeError("model down")), 4096)
    assert out == BOOK and not rec["used"] and "model down" in rec["reason"]
