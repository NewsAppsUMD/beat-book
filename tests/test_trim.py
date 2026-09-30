"""The trimming pass for drafts far over their word target, and the
section budgets in the length instruction."""

from chat_provider import ChatResponse
import agent
import trim

SECTIONS = ["Beat Overview", "Key Topics & Themes", "Key Sources & Players", "Story Ideas & Angles",
            "Background & Context", "Reporting Tips", "Calendar & Recurring Events"]


def _book(sentence, per_section, title="# Immigration Beat Book"):
    parts = [title, "", "*A guide*", ""]
    for name in SECTIONS:
        parts += [f"## {name}", "", sentence * per_section, ""]
    return "\n".join(parts)


LONG = _book("Judge Reddick heard arguments April 24 about 400 coalition members. ", 40)   # ~2,500 words
SHORT = _book("Judge Reddick heard arguments April 24. ", 14)                                  # ~700 words


class Editor:
    """A chat provider that returns a fixed edit and records the prompt."""
    write_model = "deepseek-test"

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.prompts = reply, error, []

    def create(self, **kw):
        self.prompts.append(kw["messages"][0]["content"])
        if self.error:
            raise self.error
        return ChatResponse(content=[{"type": "text", "text": self.reply}], stop_reason="end_turn")


def test_length_directive_budgets_each_section():
    text = agent._length_directive(1000)
    assert "Beat Overview about 120" in text and "Key Topics & Themes about 300" in text
    assert sum(w for _, w in agent.section_budgets(2000)) == 2000


def test_draft_within_length_is_not_sent():
    ed = Editor(reply="unused")
    out, rec = trim.trim_draft(SHORT, 1000, ed, 4096)
    assert out == SHORT and not rec["used"] and ed.prompts == []


def test_good_trim_is_used_and_lists_what_it_removed():
    ed = Editor(reply="Here is the edit:\n\n" + SHORT)       # preamble is stripped
    out, rec = trim.trim_draft(LONG, 1000, ed, 4096)
    assert rec["used"] and out.startswith("# Immigration Beat Book")
    assert rec["words_before"] > 2000 and rec["words_after"] < 1000
    assert "400" in rec["details_dropped"]                    # "400 coalition members" was cut
    assert "Word budget by section" in ed.prompts[0] and LONG in ed.prompts[0]


def test_trim_that_adds_a_figure_is_rejected():
    ed = Editor(reply=SHORT.replace("April 24.", "April 24, with 3,500 people watching."))
    out, rec = trim.trim_draft(LONG, 1000, ed, 4096)
    assert out == LONG and not rec["used"] and "3500" in rec["reason"]


def test_trim_that_drops_a_section_or_saves_nothing_is_rejected():
    no_calendar = SHORT.split("## Calendar & Recurring Events")[0]
    out, rec = trim.trim_draft(LONG, 1000, Editor(reply=no_calendar), 4096)
    assert out == LONG and "sections differ" in rec["reason"]
    out, rec = trim.trim_draft(LONG, 1000, Editor(reply=LONG), 4096)
    assert out == LONG and "saved too little" in rec["reason"]


def test_failed_call_keeps_the_draft():
    out, rec = trim.trim_draft(LONG, 1000, Editor(error=RuntimeError("model down")), 4096)
    assert out == LONG and not rec["used"] and "model down" in rec["reason"]
