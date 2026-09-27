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

import datetime
import re
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from citation_matcher import _is_markdown_list_item, _is_markdown_table_row, _plain_text

MIN_QUOTE_CHARS = 25
# A quote may be several verbatim passages from the same page, joined with
# an ellipsis or simply as separate sentences, the way a reporter excerpts.
# Each passage must be at least this long and appear on the page.
MIN_QUOTE_PART_CHARS = 20
MAX_QUOTE_PARTS = 5
MAX_QUOTE_CHARS = 700
MIN_FACT_WORDS = 6
MAX_FACT_WORDS = 70
MIN_KEYWORD_OVERLAP = 0.5
# Lower bar for facts stated as figures and names, such as results-table
# rows ("Toni Preckwinkle 470,960 69.03%"), whose quotes lack the verbs a
# sentence uses. Applies only when the fact has figures, and every figure and
# every name in it is in the quote.
MIN_KEYWORD_OVERLAP_TABULAR = 0.25
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
    # Page extraction leaves spaces around inline markup: "the Bears ' board",
    # "( 2026 )". Drop spaces before closing punctuation and after opening.
    t = re.sub(r"\s+([,.;:!?')\]])", r"\1", t)
    t = re.sub(r"([(\[])\s+", r"\1", t)
    return t.strip().lower()


_ELLIPSIS_RE = re.compile(r"\s*(?:(?:\.\s*){3,}|\u2026|\[\s*(?:\.\s*){3,}\])\s*")
_SENTENCE_SPLIT_RE = re.compile(
    r"(?:(?<=[.!?])|(?<=[.!?][\"'\u201d\u2019]))\s+(?=[\"'\u201c\u2018]?[A-Z])")
_EDGE_RE = re.compile(r"^[\s\"'\u201c\u201d\u2018\u2019.,;:\-\u2014]+|[\s\"'\u201c\u201d\u2018\u2019.,;:\-\u2014]+$")


def _trim(passage: str) -> str:
    """Drop quote marks and punctuation at a passage's edges. Reporters
    close a truncated sentence with a period, or add quote marks the page
    places elsewhere; neither changes the words being quoted."""
    return _EDGE_RE.sub("", passage or "")


def locate_quote(quote: str, page_text: str) -> Tuple[Optional[List[str]], str]:
    """Find a quote on the page. Returns (passages, "") if every passage is
    on the page, else (None, reason).

    A quote that isn't one continuous passage is split on ellipses; any
    piece still not found is split into sentences. Each resulting passage,
    with edge punctuation and quote marks trimmed, must appear on the page
    and be at least MIN_QUOTE_PART_CHARS long. Every word is still checked
    against the page; only the joins between passages are free."""
    page = normalize_for_quote(page_text)
    found = lambda x: normalize_for_quote(_trim(x)) in page
    whole = (quote or "").strip()
    if found(whole):
        return [_trim(whole)], ""
    parts: List[str] = []
    for piece in [x for x in _ELLIPSIS_RE.split(whole) if _trim(x)]:
        if found(piece):
            parts.append(_trim(piece))
        else:
            parts.extend(_trim(x) for x in _SENTENCE_SPLIT_RE.split(piece) if _trim(x))
    if len(parts) < 2:
        return None, ("the quote does not appear on that page. Copy it exactly from the "
                      "page text you fetched, without rewording.")
    if len(parts) > MAX_QUOTE_PARTS:
        return None, f"the quote has more than {MAX_QUOTE_PARTS} separate passages; quote fewer."
    missing = [x for x in parts if not found(x)]
    if missing:
        return None, ("this part of the quote does not appear on that page: “"
                      + missing[0][:120] + "”. Copy each passage exactly from the page text you fetched.")
    if any(len(x) < MIN_QUOTE_PART_CHARS for x in parts):
        return None, (f"each passage of a quote must be at least {MIN_QUOTE_PART_CHARS} characters "
                      "so it can be checked; lengthen or drop the short one.")
    return parts, ""


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


_MONTH_RE = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE_RE = re.compile(rf"\b({_MONTH_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+((?:19|20)\d{{2}}))?\b", re.I)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _month_key(m: str) -> str:
    return m.lower().rstrip(".")[:3]


def _dates_on(text: str) -> set:
    """{("mar", "17")} for every "March 17" / "Mar. 17, 2026" in the text."""
    t = unicodedata.normalize("NFKC", text or "")
    return {(_month_key(m.group(1)), str(int(m.group(2)))) for m in _DATE_RE.finditer(t)}


def _split_dates(text: str) -> Tuple[str, List[Tuple[str, str]], List[str]]:
    """Remove dates and years from a statement. Returns (rest, [(month,
    day)], [years]) so they can be checked against the whole page, not
    just the quote: writers routinely date a fact from the page's dateline."""
    t = unicodedata.normalize("NFKC", text or "")
    dates, years = [], []
    def take_date(m):
        dates.append((_month_key(m.group(1)), str(int(m.group(2)))))
        if m.group(3):
            years.append(m.group(3))
        return " "
    t = _DATE_RE.sub(take_date, t)
    def take_year(m):
        years.append(m.group())
        return " "
    t = _YEAR_RE.sub(take_year, t)
    return t, dates, years


def proper_names(text: str) -> List[str]:
    """Capitalized words that are likely names ("Preckwinkle", "Reilly"),
    skipping each sentence's first word and common capitalized words."""
    out = []
    for sent in re.split(r"(?<=[.!?;:])\s+", _plain_text(text or "")):
        words = re.findall(r"[A-Za-z][A-Za-z'-]*", sent)
        for w in words[1:]:
            if w[0].isupper() and w.lower() not in _STOPWORDS and len(w) > 2:
                out.append(w.lower().removesuffix("'s"))
    return out


def key_words(text: str) -> List[str]:
    words = re.findall(r"[a-z][a-z'-]{2,}", normalize_for_quote(_plain_text(text)))
    words = [re.sub(r"'s$|'$", "", w) for w in words]      # Reilly's → reilly
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

_URL_DATE_RE = re.compile(r"/((?:19|20)\d{2})[/-](\d{1,2})[/-](\d{1,2})(?:/|$|-)")


def _url_dates(url: str) -> Tuple[set, set]:
    """Dates in a URL path like /2026/03/17/, which news sites use for the
    publication date. Returns ({("mar", "17")}, {"2026"})."""
    m = _URL_DATE_RE.search(urlparse(url or "").path)
    if not m or not 1 <= int(m.group(2)) <= 12:
        return set(), set()
    return {(_MONTHS[int(m.group(2)) - 1], str(int(m.group(3))))}, {m.group(1)}


_MONTH_NUM = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_PUBLISHED_ON_RE = re.compile(
    rf"\b(?:published|posted|updated|last updated)\b[^0-9a-z]{{0,20}}(?:on\s+)?({_MONTH_RE})\s+(\d{{1,2}}),?\s+((?:19|20)\d{{2}})",
    re.I)


# A date shortly after a byline ("by Hannah Meisel ... May 21, 2026"), the
# layout many news sites use. Only a byline-anchored date counts: site
# headers often carry today's date, which is not the story's.
_BYLINE_DATE_RE = re.compile(
    rf"\bby\s+[A-Z][^\n]{{1,60}}(?:\s+(?:and|&)\s+[A-Z][^\n]{{1,60}})?[\s|·,]{{1,40}}"
    rf"(?i:(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+)?({_MONTH_RE}))\s+(\d{{1,2}}),?\s+((?:19|20)\d{{2}})")


def publication_date(url: str, page_text: str) -> Optional[datetime.date]:
    """The page's publication date: from its URL (/2026/03/17/), a
    "Published June 5, 2026" dateline, or a date right after the byline."""
    m = _URL_DATE_RE.search(urlparse(url or "").path)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    text = unicodedata.normalize("NFKC", page_text or "")
    m = _PUBLISHED_ON_RE.search(text) or _BYLINE_DATE_RE.search(text)
    if m:
        try:
            return datetime.date(int(m.group(3)), _MONTH_NUM[_month_key(m.group(1))], int(m.group(2)))
        except (KeyError, ValueError):
            pass
    return None


_RELATIVE_RE = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tonight|yesterday)\b", re.I)
_WINDOW_WORDS = 4


def _stem(w: str) -> str:
    return w[:5]


def dates_implied_by(quote: str, published: Optional[datetime.date], fact: str = "") -> set:
    """Dates a quote pins down relative to the publication date: "Thursday"
    in a story published Friday, June 5 is Thursday, June 4; "yesterday" is
    the day before; "today" or "tonight" is the day itself. Returns
    {(month, day, year)}.

    When the quote names several days ("Friday's announcement ... Thursday's
    vote"), only the one whose nearby words best match the fact counts, so
    a fact about the vote can't borrow the announcement's date."""
    if published is None:
        return set()
    q = quote or ""
    words = re.findall(r"[A-Za-z']+", q.lower())
    hits = []
    for i, w in enumerate(words):
        m = _RELATIVE_RE.fullmatch(re.sub(r"'s$", "", w))
        if not m:
            continue
        name = m.group(1).lower()
        if name in ("today", "tonight"):
            d = published
        elif name == "yesterday":
            d = published - datetime.timedelta(days=1)
        else:
            back = (published.weekday() - _WEEKDAYS.index(name)) % 7   # 0 = publication day
            d = published - datetime.timedelta(days=back)
        window = words[max(0, i - _WINDOW_WORDS):i] + words[i + 1:i + 1 + _WINDOW_WORDS]
        hits.append((d, {_stem(x) for x in window if x not in _STOPWORDS}))
    if not hits:
        return set()
    days = {d for d, _ in hits}
    if len(days) > 1 and fact:
        fact_stems = {_stem(w) for w in key_words(fact)}
        scored = [(len(ctx & fact_stems), d) for d, ctx in hits]
        best = max(score for score, _ in scored)
        days = {d for score, d in scored if score == best} if best > 0 else set()
    return {(_MONTHS_SHORT[d.month - 1], str(d.day), str(d.year)) for d in days}


_MONTHS_SHORT = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def check_fact(fact: str, quote: str, page_text: str, url: str = "") -> Optional[str]:
    """Return None if the fact is backed by the quote and the quote is on the
    page, else a reason the model can act on. `url` lets a date in the page's
    URL count as a date on the page."""
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
    parts, why = locate_quote(quote, page_text)
    if parts is None:
        return why
    # A date in the fact must be in the quote, or be pinned down by it: a
    # weekday, "today" or "yesterday" in the quote, counted from the page's
    # publication date. A date merely appearing somewhere on the page is not
    # enough: a Friday story's dateline is not the date of Thursday's vote.
    rest, dates, years = _split_dates(fact)
    quote_dates = _dates_on(quote)
    quote_years = set(_YEAR_RE.findall(quote or ""))
    published = publication_date(url, page_text)
    implied = dates_implied_by(quote, published, rest)
    implied_md = {(m, d) for m, d, _ in implied}
    # "As of <publication date>" states something at the time the page was
    # published, which the page itself establishes.
    as_of_md, as_of_years = set(), set()
    if published is not None:
        pub_m = _MONTHS_SHORT[published.month - 1]
        for m in re.finditer(rf"\bas of\s+({_MONTH_RE})(?:\s+(\d{{1,2}})(?!\d))?,?(?:\s+((?:19|20)\d{{2}}))?", fact, re.I):
            day_ok = m.group(2) is None or int(m.group(2)) == published.day
            year_ok = m.group(3) is None or int(m.group(3)) == published.year
            if _month_key(m.group(1)) == pub_m and day_ok and year_ok:
                if m.group(2):
                    as_of_md.add((pub_m, str(published.day)))
                as_of_years.add(str(published.year))
    bad = [f"{m} {d}" for m, d in dates
           if (m, d) not in quote_dates and (m, d) not in implied_md and (m, d) not in as_of_md]
    if bad:
        hint = ""
        if published is not None and implied:
            said = ", ".join(f"{m.title()} {d}" for m, d, _ in sorted(implied))
            hint = (f" The page was published {published.strftime('%b %-d, %Y')}; the weekday "
                    f"or relative day in the quote points to {said}.")
        return (f"the date {', '.join(bad)} is not in the quote.{hint} Quote the passage that "
                "gives the date, or use only the date the quote supports.")
    # Years: in the quote, or the year of a date the quote pins down, or the
    # publication year when the fact dates something the quote describes.
    ok_years = quote_years | {y for _, _, y in implied} | as_of_years
    if published is not None and (dates or implied):
        ok_years.add(str(published.year))
    bad_years = [y for y in years if y not in ok_years]
    if bad_years:
        return (f"the year {', '.join(bad_years)} is not in the quote. Quote the passage that "
                "gives it, or leave it out.")
    quote_figures = figures_in(_split_dates(quote)[0]) + figures_in(quote)
    figures = figures_in(rest)
    missing = [f for f in figures if f not in quote_figures]
    if missing:
        return (f"these figures in the fact are not in the quote: {', '.join(missing)}. "
                "Quote the passage that states them, or remove them from the fact.")
    words = key_words(fact)
    if words:
        qwords = set(key_words(quote))
        overlap = sum(1 for w in words if w in qwords) / len(words)
        bar = MIN_KEYWORD_OVERLAP
        names = proper_names(rest)       # dates removed, so "March" isn't a name
        if figures and all(n in qwords for n in names):
            bar = MIN_KEYWORD_OVERLAP_TABULAR
        if overlap < bar:
            return ("the fact says more than the quote does. Restate only what the "
                    "quote says, or quote the passage that supports the rest.")
    return None


# ── Insertion ────────────────────────────────────────────────────────────────

def _norm_heading(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (h or "").lower()).strip()


def _heading(line: str) -> Tuple[int, str]:
    m = re.match(r"^(#{2,3})\s+(.*)$", line.strip())
    return (len(m.group(1)), m.group(2).strip()) if m else (0, "")


def sections(markdown: str) -> List[str]:
    """Section (##) headings."""
    return [h for lvl, h in map(_heading, markdown.split("\n")) if lvl == 2]


def subsections(markdown: str) -> List[str]:
    """Subsection (###) headings; facts can be placed under these too."""
    return [h for lvl, h in map(_heading, markdown.split("\n")) if lvl == 3]


def _section_bounds(lines: List[str], section: str) -> Optional[Tuple[int, int]]:
    """(index of the heading line, index one past its last line) for a ##
    section or, if no section has that name, a ### subsection. A section
    ends at the next ##; a subsection at the next ## or ###."""
    want = _norm_heading(section)
    for level in (2, 3):
        start = None
        for i, l in enumerate(lines):
            lvl, text = _heading(l)
            if not lvl:
                continue
            if start is not None and lvl <= level:
                return start, i
            if start is None and lvl == level and _norm_heading(text) == want:
                start = i
        if start is not None:
            return start, len(lines)
    return None


def find_placement(markdown: str, section: str, after_line: str) -> Optional[str]:
    """Validate a placement; return None if usable, else a reason."""
    lines = markdown.split("\n")
    bounds = _section_bounds(lines, section)
    if bounds is None:
        subs = subsections(markdown)
        return (f"there is no section or subsection named “{section}”. Sections: "
                + "; ".join(sections(markdown))
                + (". Subsections: " + "; ".join(subs) if subs else "") + ".")
    return None


def placement_note(markdown: str, section: str, after_line: str) -> str:
    """A note when `after_line` matches no line in the section; the fact
    then goes at the end of the section instead of being rejected."""
    if not after_line:
        return ""
    lines = markdown.split("\n")
    bounds = _section_bounds(lines, section)
    if bounds is not None and _target_line(lines, bounds, after_line) is None:
        return (" `after_line` didn't match the start of any line in that section, so it "
                "was placed at the end of the section.")
    return ""


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
