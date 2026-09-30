"""
trim.py
-------
One editing pass for a draft far over its word target.

DeepSeek (on Ollama) wrote beat books 1.5 to 2.8 times the requested
length, in one pass, whatever the length instruction said. When a draft
runs more than TRIM_TRIGGER times the target, the writing model is asked
once to cut it to the target. The cut is kept only if it still looks like
the same book: same title and sections, clearly shorter, no damage signs,
and no figure or date the draft didn't have. Otherwise the draft stands.
Either way the build record says what happened, and lists the names,
figures and dates the cut removed.

Research and citation matching run on the result, so every claim in a
trimmed book is still checked against the stories.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from agent import section_budgets, strip_preamble
from claim_evidence import _NOT_NAMES
from research_facts import _STOPWORDS
from draft_check import check_draft
from research_facts import _split_dates, figures_in

# Trim when the draft is more than this many times the word target.
TRIM_TRIGGER = 1.4
# A cut that saves less than this share isn't worth the risk.
MIN_SAVING = 0.10
# Report at most this many removed details in the build record.
MAX_DROPPED_LISTED = 60

_TRIM_PROMPT = """\
Below is a beat book, a reporting guide for journalists, that is {words:,} \
words long. It should be about {target:,} words. Edit it down to about \
{target:,} words.

Word budget by section: {budgets}.

Rules:
- Keep the title, the subtitle line and every ## section, in the same order, \
with the same headings.
- Cut repetition, filler, generic advice and secondary detail first. Keep \
the names, figures, dates and facts a reporter needs most.
- Do not add anything: no new facts, names, figures, dates or claims. Only \
shorten and cut what is there.
- Keep the Markdown formatting: bold, bullets and tables stay as they are.

Reply with ONLY the edited Markdown document, starting with the title line \
(`# ...`). No preamble, no notes about what you changed.

---

{draft}"""


def _words(text: str) -> int:
    return len((text or "").split())


def _sections(markdown: str) -> List[str]:
    return [m.group(1).strip().lower() for m in re.finditer(r"(?m)^##[ \t]+(?!#)(.+)$", markdown or "")]


def _names(text: str) -> List[str]:
    """Capitalized words inside sentences, as written: "Gainer", "McCaskill".
    A word that opens a sentence, line or clause is skipped, since most are
    ordinary words ("Meanwhile", "Approval")."""
    out = []
    for m in re.finditer(r"(?<![A-Za-z'’])([A-Z][a-z]*[A-Z]?[a-z]+)(?:['’]s)?\b", text):
        before = text[:m.start()].rstrip(" \t\"“(")
        if not before or before[-1] in ".!?:—–-\n•":
            continue
        word = m.group(1)
        if word.lower() not in _NOT_NAMES and word.lower() not in _STOPWORDS:
            out.append(word)
    return out


def _details(markdown: str) -> Dict[str, set]:
    """Figures, years and dates in a document, plus capitalized names."""
    text = re.sub(r"[*_`|]+", " ", markdown or "")
    rest, dates, years = _split_dates(text)
    names = set(_names(text))
    return {"figures": set(figures_in(rest)) | set(years),
            "dates": {f"{m} {d}" for m, d in dates}, "names": names}


def needs_trim(markdown: str, target_words: int) -> bool:
    return bool(target_words) and _words(markdown) > target_words * TRIM_TRIGGER


def trim_draft(markdown: str, target_words: int, provider: Any,
               max_tokens: int) -> Tuple[str, Dict[str, Any]]:
    """Return (the draft to use, a record for the build manifest)."""
    before = _words(markdown)
    record: Dict[str, Any] = {"words_before": before, "target_words": target_words,
                              "trigger": TRIM_TRIGGER, "used": False}
    if not needs_trim(markdown, target_words):
        record["reason"] = "within length"
        return markdown, record
    record["model"] = getattr(provider, "write_model", "") or getattr(provider, "agent_model", "")
    budgets = "; ".join(f"{name} about {words:,}" for name, words in section_budgets(target_words))
    prompt = _TRIM_PROMPT.format(words=before, target=target_words, budgets=budgets, draft=markdown)
    try:
        resp = provider.create(model=record["model"], system="",
                               messages=[{"role": "user", "content": prompt}], max_tokens=max_tokens)
    except Exception as ex:
        record["reason"] = f"the trimming call failed ({type(ex).__name__}: {ex})"[:300]
        return markdown, record
    record["usage"] = dict(getattr(resp, "usage", None) or {})
    trimmed, _ = strip_preamble(getattr(resp, "text", "") or "")
    trimmed = trimmed.strip() + "\n"
    after = _words(trimmed)
    record["words_after"] = after

    old, new = _details(markdown), _details(trimmed)
    added = sorted((new["figures"] - old["figures"]) | (new["dates"] - old["dates"]))
    problems = []
    if not re.match(r"#[ \t]+\S", trimmed):
        problems.append("it has no title")
    if set(_sections(trimmed)) != set(_sections(markdown)):
        problems.append("its sections differ from the draft's")
    if after > before * (1 - MIN_SAVING):
        problems.append(f"it saved too little ({before:,} to {after:,} words)")
    if added:
        problems.append("it added figures or dates the draft didn't have: " + ", ".join(added[:10]))
    if not check_draft(trimmed, target_words, continuations=0)["ok"]:
        problems.append("it failed the damaged-draft check")
    if problems:
        record["reason"] = "Kept the draft: the trimmed version was rejected because " + "; ".join(problems) + "."
        return markdown, record

    dropped = sorted((old["names"] - new["names"]) | (old["figures"] - new["figures"])
                     | (old["dates"] - new["dates"]))
    record.update(used=True, reason=f"Trimmed from {before:,} to {after:,} words.",
                  details_dropped=dropped[:MAX_DROPPED_LISTED], details_dropped_count=len(dropped))
    return trimmed, record
