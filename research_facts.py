"""
research_facts.py
-----------------
Verify and insert the facts the research agent submits.

The research agent no longer edits the beat book. For each fact it wants to
add, it submits the fact, a verbatim quote from a page it fetched, the page's
URL, and where the fact belongs. This module:

- checks the quote appears on that page, after normalizing whitespace,
  quote marks, dashes and Markdown formatting;
- checks the fact says what the quote says: every figure in the fact
  (numbers, dollar amounts, percentages, years) must appear in the quote, and
  most of the fact's key words must too;
- writes the attribution itself, from the page, not from the model;
- inserts accepted facts into the Markdown without changing any existing
  line, so no claim from the reporter's stories can be lost.

This is the same principle ingest uses for story bodies: the model points at
text, and the app does the copying.
"""

from __future__ import annotations

import re
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from citation_matcher import _is_markdown_list_item, _is_markdown_table_row, _plain_text

MIN_QUOTE_CHARS = 25
MAX_QUOTE_CHARS = 700
MIN_FACT_WORDS = 6
MAX_FACT_WORDS = 70
MIN_KEYWORD_OVERLAP = 0.5
MAX_FACTS_PER_RUN = 25

_STOPWORDS = set("""a an and are as at be been but by for from had has have he her his in into is
it its of on or she that the their them they this to was were which who will with after
before about over under more than also since while where when what would could can not
new said says""".split())


# ── Normalization ────────────────────────────────────────────────────────────

_PUNCT_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    " ": " ", " ": " ", " ": " ", "…": "...",
}


def normalize_for_quote(text: str) -> str:
    """Canonical form for verbatim comparison: Unicode-normalized, straight
    quotes and hyphens, Markdown links and emphasis removed, whitespace
    collapsed, lowercased."""
    t = unicodedata.normalize("NFKC", text or "")
    t = "".join(_PUNCT_MAP.get(ch, ch) for ch in t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)      # [text](url) → text
    t = re.sub(r"[*_`#>|]+", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip().lower()


def figures_in(text: str) -> List[str]:
    """Numbers a statement relies on, including years; single digits are
    skipped (they are usually counts written either way, "3" or "three")."""
    t = unicodedata.normalize("NFKC", text or "")
    out = []
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", t):
        n = m.group().replace(",", "").rstrip(".")
        if len(n.replace(".", "")) >= 2:
            out.append(n)
    return out


def key_words(text: str) -> List[str]:
    words = re.findall(r"[a-z][a-z'-]{2,}", normalize_for_quote(_plain_text(text)))
    return [w for w in words if w not in _STOPWORDS]


# ── Attribution ──────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def name_matches_page(name: str, url: str, title: str) -> bool:
    """Does a source name plausibly refer to this page (host or title)?"""
    generic = {"the", "of", "and", "press", "release", "news", "report", "website", "page"}
    words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in generic]
    if not words:
        return False
    host = _norm((urlparse(url).hostname or "").removeprefix("www."))
    if len("".join(words)) >= 3 and "".join(words) in host:
        return True
    hay = f" {' '.join(re.findall(r'[a-z0-9]+', (title or '').lower()))} "
    return all(f" {w} " in hay for w in words)


def _site_from_title(title: str) -> str:
    """"Board Members | Chicago Housing Authority" → "Chicago Housing Authority"."""
    parts = [p.strip() for p in re.split(r"\s[|–—-]\s", title or "") if p.strip()]
    return parts[-1] if len(parts) > 1 and len(parts[-1]) <= 60 else ""


_MONTHS = "jan feb mar apr may jun jul aug sep sept oct nov dec".split()
_PUBLISHED_RE = re.compile(
    r"^(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+)?(?:\d{1,2},?\s+)?(?:19|20)\d{2}$",
    re.I)


def attribution_for(source_name: str, published: str, url: str, title: str,
                    now: Optional[float] = None) -> Tuple[str, str]:
    """The inline attribution the app writes, and the source name it used.
    The model's name is kept only when it matches the page; otherwise the
    site name from the page title, or the host, is used. A publication date
    is used only when it looks like one; otherwise the retrieval month is
    shown and labeled as such."""
    name = (source_name or "").strip()
    if not name or not name_matches_page(name, url, title):
        name = _site_from_title(title) or (urlparse(url).hostname or "").removeprefix("www.")
    name = re.sub(r"[()]", "", name).strip()[:80]
    pub = (published or "").strip().strip(".")
    if pub and _PUBLISHED_RE.match(pub):
        when = pub
    else:
        when = "retrieved " + time.strftime("%b %Y", time.localtime(now or time.time()))
    return f"({name}, {when})", name


# ── Verification ─────────────────────────────────────────────────────────────

def check_fact(fact: str, quote: str, page_text: str) -> Optional[str]:
    """Return None if the fact is backed by the quote and the quote is on the
    page, else a reason the model can act on."""
    fact = (fact or "").strip()
    quote = (quote or "").strip()
    n_words = len(fact.split())
    if n_words < MIN_FACT_WORDS:
        return f"the fact is too short ({n_words} words); state a complete fact."
    if n_words > MAX_FACT_WORDS:
        return f"the fact is too long ({n_words} words); keep each fact to one or two sentences."
    if "\n" in fact:
        return "the fact must be a single paragraph with no line breaks."
    if re.search(r"\((?:[^()]*\b(?:19|20)\d{2})\)\s*[.!?]?$", fact):
        return "leave the attribution off the fact; the application adds it from the page."
    if len(quote) < MIN_QUOTE_CHARS:
        return f"the quote is too short; copy at least {MIN_QUOTE_CHARS} characters from the page."
    if len(quote) > MAX_QUOTE_CHARS:
        return f"the quote is too long; copy the one to three sentences that state the fact (at most {MAX_QUOTE_CHARS} characters)."
    if normalize_for_quote(quote) not in normalize_for_quote(page_text):
        return ("the quote does not appear on that page. Copy it exactly from the "
                "page text you fetched, without rewording or joining separate passages.")
    missing = [f for f in figures_in(fact) if f not in figures_in(quote)]
    if missing:
        return (f"these figures in the fact are not in the quote: {', '.join(missing)}. "
                "Quote the passage that states them, or remove them from the fact.")
    words = key_words(fact)
    if words:
        qwords = set(key_words(quote))
        overlap = sum(1 for w in words if w in qwords) / len(words)
        if overlap < MIN_KEYWORD_OVERLAP:
            return ("the fact says more than the quote does. Restate only what the "
                    "quote says, or quote the passage that supports the rest.")
    return None


# ── Insertion ────────────────────────────────────────────────────────────────

def _norm_heading(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (h or "").lower()).strip()


def sections(markdown: str) -> List[str]:
    return [re.sub(r"^##\s+", "", l.strip()).strip()
            for l in markdown.split("\n") if re.match(r"^##\s+", l.strip())]


def _section_bounds(lines: List[str], section: str) -> Optional[Tuple[int, int]]:
    """(index of the H2 line, index one past the section's last line)."""
    want = _norm_heading(section)
    start = None
    for i, l in enumerate(lines):
        if re.match(r"^##\s+", l.strip()):
            if start is not None:
                return start, i
            if _norm_heading(re.sub(r"^##\s+", "", l.strip())) == want:
                start = i
    return (start, len(lines)) if start is not None else None


def find_placement(markdown: str, section: str, after_line: str) -> Optional[str]:
    """Validate a placement; return None if usable, else a reason."""
    lines = markdown.split("\n")
    bounds = _section_bounds(lines, section)
    if bounds is None:
        return f"there is no section named “{section}”. Use one of: " + "; ".join(sections(markdown))
    if after_line and _target_line(lines, bounds, after_line) is None:
        return ("`after_line` does not match the start of any line in that section. "
                "Copy the first words of an existing paragraph or bullet exactly, or leave it empty.")
    return None


def _target_line(lines: List[str], bounds: Tuple[int, int], after_line: str) -> Optional[int]:
    want = normalize_for_quote(after_line)
    if len(want) < 12:
        return None
    for i in range(bounds[0] + 1, bounds[1]):
        text = normalize_for_quote(re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", lines[i]))
        if text.startswith(want) or (len(want) >= 30 and want in text):
            return i
    return None


def _indent_for_child(line: str) -> str:
    m = re.match(r"^(\s*)((?:[-*+]|\d+[.)])\s+)", line)
    return (m.group(1) + " " * len(m.group(2))) if m else "  "


def insert_facts(markdown: str, facts: List[Dict[str, Any]]) -> str:
    """Insert accepted facts. Each fact has "text" (fact plus attribution),
    "section" and optional "after_line". Existing lines are never changed.

    - After a bullet: an indented sub-bullet under it (after any existing
      sub-bullets).
    - After a paragraph: a new paragraph right after it; several facts for
      the same paragraph share one new paragraph.
    - After a table row: a paragraph after the table.
    - No `after_line`: at the end of the section, as a bullet if the
      section ends with a bullet, otherwise as a paragraph."""
    lines = markdown.split("\n")
    # anchor index → list of (kind, text, indent)
    plan: Dict[int, List[Tuple[str, str, str]]] = {}
    for f in facts:
        bounds = _section_bounds(lines, f["section"])
        if bounds is None:
            continue
        start, end = bounds
        target = _target_line(lines, bounds, f.get("after_line") or "")
        if target is None:
            last = end - 1
            while last > start and not lines[last].strip():
                last -= 1
            if last > start and _is_markdown_list_item(lines[last]):
                indent = re.match(r"^(\s*)", lines[last]).group(1)
                # Stay at the top level of the list, after its last line.
                plan.setdefault(last, []).append(("bullet", f["text"], indent))
            else:
                plan.setdefault(last, []).append(("para", f["text"], ""))
            continue
        line = lines[target]
        if _is_markdown_list_item(line):
            indent = _indent_for_child(line)
            anchor = target
            own = len(re.match(r"^(\s*)", line).group(1))
            while anchor + 1 < end and lines[anchor + 1].strip() and \
                    len(re.match(r"^(\s*)", lines[anchor + 1]).group(1)) > own:
                anchor += 1
            plan.setdefault(anchor, []).append(("bullet", f["text"], indent))
        elif _is_markdown_table_row(line):
            anchor = target
            while anchor + 1 < end and _is_markdown_table_row(lines[anchor + 1]):
                anchor += 1
            plan.setdefault(anchor, []).append(("para", f["text"], ""))
        else:
            plan.setdefault(target, []).append(("para", f["text"], ""))

    out: List[str] = []
    for i, line in enumerate(lines):
        out.append(line)
        items = plan.get(i)
        if not items:
            continue
        for indent in dict.fromkeys(ind for k, _, ind in items if k == "bullet"):
            out.extend(f"{indent}- {t}" for k, t, ind in items if k == "bullet" and ind == indent)
        paras = [t for k, t, _ in items if k == "para"]
        if paras:
            out.extend(["", " ".join(paras)])
            if i + 1 < len(lines) and lines[i + 1].strip():
                out.append("")
    return "\n".join(out)
