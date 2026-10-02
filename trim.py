"""
trim.py
-------
Cut a draft that runs far over its word target, by selection.

DeepSeek (on Ollama) wrote beat books 1.5 to 2.8 times the requested
length whatever the length instruction said, and when asked to rewrite a
draft shorter it cut almost nothing (1,493 words to 1,470). So the model
no longer rewrites. The draft is split into numbered units (paragraphs,
list items with their sub-items, table rows), and the model names the
units to remove, least important first. The app removes them itself,
stopping once enough is cut and keeping at least one unit in every
section. The title, subtitle and headings are never removed, and nothing
left is reworded, so the trimmed book can't say anything the draft didn't.

The build record gives what was removed and the names, figures and dates
that went with it. Research and citation matching run on the result.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from agent import section_budgets
from claim_evidence import _NOT_NAMES
from research_facts import _STOPWORDS, _split_dates, figures_in

# Trim when the draft is more than this many times the word target.
TRIM_TRIGGER = 1.4
# A cut that saves less than this share isn't worth making.
MIN_SAVING = 0.10
# Stop removing units once the draft is within this share of the target.
STOP_WITHIN = 0.05
# Report at most this many removed details in the build record.
MAX_DROPPED_LISTED = 60

_SELECT_TOOL = {
    "name": "remove_units",
    "description": "Name the numbered units to remove from the beat book.",
    "input_schema": {
        "type": "object",
        "properties": {
            "remove": {"type": "array", "items": {"type": "integer"},
                       "description": "Unit numbers to remove, least important first."},
        },
        "required": ["remove"],
    },
}

_SELECT_PROMPT = """\
A beat book, a reporting guide for journalists, is {words:,} words long and \
should be about {target:,} words. Cut about {cut:,} words by removing whole \
units. Each numbered unit below is a paragraph, a list item or a table row, \
with its section and word count.

Word budget by section: {budgets}.

Pick the units to remove, least important first: repetition, filler, \
generic advice and secondary detail before the names, figures, dates and \
facts a reporter needs most. Take the most from the sections furthest over \
their budget. Don't remove every unit of a section.

Call remove_units with the numbers, least important first. List a few more \
than you think you need; the app stops removing once enough is cut.

{units}"""

_LIST_ITEM = re.compile(r"^(?:[-*+]|\d+[.)])\s+")


def _words(text: str) -> int:
    return len((text or "").split())


# ── Units ────────────────────────────────────────────────────────────────────

def split_units(markdown: str) -> List[Dict[str, Any]]:
    """The draft as an ordered list of pieces, each {"lines", "unit",
    "section", "gap"}: `unit` is True for removable ones (paragraphs, list
    items with their indented sub-items, table data rows). `gap` marks a
    blank line before the piece."""
    pieces: List[Dict[str, Any]] = []
    section = ""
    seen_section = False
    table = ""          # "" outside a table, then "header", then "rows"
    gap = False
    for line in (markdown or "").split("\n"):
        if not line.strip():
            gap = True
            table = ""
            continue
        stripped = line.lstrip()
        if not stripped.startswith("|"):
            table = ""
        new = None
        if re.match(r"#{1,6}\s", stripped):
            if stripped.startswith("## ") or stripped.startswith("##\t"):
                section = stripped[2:].strip()
                seen_section = True
            new = {"lines": [line], "unit": False}
        elif not seen_section:
            new = {"lines": [line], "unit": False}           # title and subtitle
        elif stripped.startswith("|"):
            if table == "header" and re.match(r"^\|?\s*:?-{2,}", stripped):
                table = "rows"                               # the header separator
                new = {"lines": [line], "unit": False}
            elif table == "rows":
                new = {"lines": [line], "unit": True}        # a data row
            else:
                table = "header"
                new = {"lines": [line], "unit": False}
        elif _LIST_ITEM.match(stripped) and line == stripped:
            new = {"lines": [line], "unit": True}
        elif line != stripped and pieces and not gap and pieces[-1]["unit"]:
            pieces[-1]["lines"].append(line)                 # sub-item or continuation
            continue
        elif pieces and not gap and pieces[-1]["unit"] and not _LIST_ITEM.match(pieces[-1]["lines"][0].lstrip()) \
                and not pieces[-1]["lines"][0].lstrip().startswith("|"):
            pieces[-1]["lines"].append(line)                 # same paragraph
            continue
        else:
            new = {"lines": [line], "unit": True}
        new["section"] = section
        new["gap"] = gap
        gap = False
        pieces.append(new)
    return pieces


def join_units(pieces: List[Dict[str, Any]]) -> str:
    out: List[str] = []
    for p in pieces:
        if out and p["gap"]:
            out.append("")
        out.extend(p["lines"])
    return "\n".join(out).strip() + "\n"


def _numbered(pieces: List[Dict[str, Any]]) -> Tuple[str, Dict[int, int]]:
    """The units as the model sees them, and unit number → piece index."""
    lines, index = [], {}
    n = 0
    for i, p in enumerate(pieces):
        if not p["unit"]:
            continue
        n += 1
        index[n] = i
        text = "\n".join(p["lines"])
        lines.append(f"[{n}] ({p['section']}, {_words(text)} words)\n{text}")
    return "\n\n".join(lines), index


# ── Details, for the build record ────────────────────────────────────────────

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
    return {"figures": set(figures_in(rest)) | set(years),
            "dates": {f"{m} {d}" for m, d in dates}, "names": set(_names(text))}


# ── The pass ─────────────────────────────────────────────────────────────────

def needs_trim(markdown: str, target_words: int) -> bool:
    return bool(target_words) and _words(markdown) > target_words * TRIM_TRIGGER


def _parse_selection(resp: Any) -> Optional[List[int]]:
    from chat_provider import _json_in_text
    data = None
    for block in getattr(resp, "content", []) or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            data = block.get("input")
            break
    if data is None:
        data = _json_in_text(getattr(resp, "text", "") or "", ["remove"])
    if isinstance(data, dict):
        data = data.get("remove")
    if not isinstance(data, list):
        return None
    out = []
    for x in data:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(out))


def trim_draft(markdown: str, target_words: int, provider: Any,
               max_tokens: int) -> Tuple[str, Dict[str, Any]]:
    """Return (the draft to use, a record for the build manifest)."""
    before = _words(markdown)
    record: Dict[str, Any] = {"words_before": before, "target_words": target_words,
                              "trigger": TRIM_TRIGGER, "used": False, "method": "selection"}
    if not needs_trim(markdown, target_words):
        record["reason"] = "within length"
        return markdown, record
    pieces = split_units(markdown)
    units_text, index = _numbered(pieces)
    record["units"] = len(index)
    if not index:
        record["reason"] = "Kept the draft: it has no paragraphs or list items to remove."
        return markdown, record
    record["model"] = getattr(provider, "write_model", "") or getattr(provider, "agent_model", "")
    budgets = "; ".join(f"{name} about {words:,}" for name, words in section_budgets(target_words))
    prompt = _SELECT_PROMPT.format(words=before, target=target_words, cut=before - target_words,
                                   budgets=budgets, units=units_text)
    try:
        resp = provider.create(model=record["model"], system="",
                               messages=[{"role": "user", "content": prompt}],
                               tools=[_SELECT_TOOL], tool_choice={"type": "tool", "name": "remove_units"},
                               max_tokens=min(max_tokens, 2048))
    except Exception as ex:
        record["reason"] = f"Kept the draft: the trimming call failed ({type(ex).__name__}: {ex})"[:300]
        return markdown, record
    record["usage"] = dict(getattr(resp, "usage", None) or {})
    chosen = _parse_selection(resp)
    if chosen is None:
        record["reason"] = "Kept the draft: the model's reply named no units to remove."
        return markdown, record
    chosen = [n for n in chosen if n in index]
    record["selected"] = chosen

    # Remove in the model's order until close enough to the target, never
    # emptying a section.
    left_in_section: Dict[str, int] = {}
    for n, i in index.items():
        left_in_section[pieces[i]["section"]] = left_in_section.get(pieces[i]["section"], 0) + 1
    removed: List[int] = []
    words_now = before
    stop_at = target_words * (1 + STOP_WITHIN)
    for n in chosen:
        if words_now <= stop_at:
            break
        p = pieces[index[n]]
        if left_in_section[p["section"]] <= 1:
            continue
        left_in_section[p["section"]] -= 1
        removed.append(n)
        words_now -= _words("\n".join(p["lines"]))
    drop = {index[n] for n in removed}
    kept = [p for i, p in enumerate(pieces) if i not in drop]
    # A table left with no data rows loses its header too.
    kept = _drop_empty_tables(kept)
    trimmed = join_units(kept)
    after = _words(trimmed)
    record["words_after"] = after
    record["removed"] = [{"section": pieces[index[n]]["section"],
                          "text": "\n".join(pieces[index[n]]["lines"])[:600]} for n in removed]
    if after > before * (1 - MIN_SAVING):
        record["reason"] = (f"Kept the draft: removing the units the model chose would save too little "
                            f"({before:,} to {after:,} words).")
        return markdown, record

    old, new = _details(markdown), _details(trimmed)
    dropped = sorted((old["names"] - new["names"]) | (old["figures"] - new["figures"])
                     | (old["dates"] - new["dates"]))
    record.update(used=True, reason=f"Trimmed from {before:,} to {after:,} words by removing "
                                    f"{len(removed)} of {len(index)} paragraphs, list items and table rows.",
                  details_dropped=dropped[:MAX_DROPPED_LISTED], details_dropped_count=len(dropped))
    return trimmed, record


def _drop_empty_tables(pieces: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove a table's header and separator when none of its rows are left."""
    out: List[Dict[str, Any]] = []
    i = 0
    while i < len(pieces):
        p = pieces[i]
        is_row = p["lines"][0].lstrip().startswith("|")
        if is_row and not p["unit"]:
            # Collect this table's pieces.
            j = i
            while j < len(pieces) and pieces[j]["lines"][0].lstrip().startswith("|") and (j == i or not pieces[j]["gap"]):
                j += 1
            table = pieces[i:j]
            if any(t["unit"] for t in table):
                out.extend(table)
            i = j
            continue
        out.append(p)
        i += 1
    return out
