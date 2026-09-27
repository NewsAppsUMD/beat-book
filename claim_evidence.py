"""
claim_evidence.py
-----------------
Two passes over the citation entries, after embedding matching:

1. Anchor evidence for factual claims. The embedding matcher compares
   meaning and misses facts that a story states in different words. A claim
   about who, what, when and where carries anchors (names, figures, dates),
   so if every anchor appears together in one short stretch of one story,
   that stretch is cited as the claim's evidence. Two anchors must be
   distinctive (a figure, a date, or a name found in a minority of the
   stories), so "Chicago" and "Bears" never count, and the stretch must
   share some of the claim's other words. The best-matching stretch across
   all stories is cited. This is weaker evidence than a close paraphrase,
   and the reader says so.

2. Sorting the claims still unsourced. A small model labels each one a
   fact (checkable specifics), analysis (interpretation, significance or
   characterization) or a suggestion (a story idea, question or advice).
   Analysis is labeled as such in the reader instead of being flagged as
   unsourced; suggestions count with reporting tips. Facts stay flagged:
   those are the claims a reporter must check.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from research_facts import _STOPWORDS, _split_dates, figures_in, key_words, normalize_for_quote

# ── Anchors ──────────────────────────────────────────────────────────────────

# At least this many distinctive anchors: figures, dates, years, or names
# found in no more than DISTINCTIVE_MAX_SHARE of the stories. Common words
# ("Chicago", "Bears", "Hammond" in a Bears corpus) never count toward it.
MIN_DISTINCTIVE = 2
DISTINCTIVE_MAX_SHARE = 0.3
# The window must also share this much of the claim's other key words, so
# names and figures appearing near each other by chance don't count.
MIN_CONTEXT_OVERLAP = 0.3
# Evidence must sit within this many consecutive passages (~100 words each).
WINDOW_PASSAGES = 2

_CAP_RE = re.compile(r"[A-Z][A-Za-z'’.-]*[A-Za-z]")
_NOT_NAMES = {
    "the", "a", "an", "in", "on", "at", "by", "for", "after", "before", "during", "since",
    "his", "her", "their", "its", "this", "that", "these", "those", "several", "many",
    "some", "most", "both", "each", "every", "any", "all", "one", "two", "three", "four",
    "five", "six", "seven", "eight", "nine", "ten", "first", "last", "next", "new",
    "democratic", "republican", "democrat", "gov", "sen", "rep", "mr", "mrs", "ms", "dr",
    "state", "city", "county", "house", "senate", "board", "committee", "department",
    "office", "mayor", "governor", "president", "chairman", "chair", "commissioner",
    "judge", "court", "council", "authority", "act", "bill", "law", "plan", "project",
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "jan", "feb", "mar", "apr", "jun", "jul", "aug",
    "sep", "sept", "oct", "nov", "dec",
    "daily", "weekly", "monthly", "quarterly", "annual", "annually", "yearly", "biweekly",
    "ongoing", "recurring", "watch", "track", "note", "key", "background", "context",
}


def names_in(claim: str) -> List[str]:
    """Capitalized words that look like names. Sentence-initial words count
    only if part of a capitalized run ("Hammond Mayor ...") or possessive
    ("Buckner's"), since most sentence starters are ordinary words."""
    text = re.sub(r"[*_`]+", "", claim or "")
    out: List[str] = []
    for sent in re.split(r"(?<=[.!?])\s+", text):
        tokens = [t.rstrip(".-") for t in re.findall(r"[A-Za-z][A-Za-z'’.-]*", sent)]
        for i, tok in enumerate(tokens):
            if not _CAP_RE.fullmatch(tok):
                continue
            base = re.sub(r"['’]s$|['’]$", "", tok).strip(".").lower()
            if len(base) < 3 or base in _NOT_NAMES or base in _STOPWORDS:
                continue
            if i == 0:
                nxt = tokens[1] if len(tokens) > 1 else ""
                if not (tok.endswith(("'s", "’s")) or _CAP_RE.fullmatch(nxt or "x")):
                    continue
            out.append(base)
    return list(dict.fromkeys(out))


def anchors_in(claim: str) -> Dict[str, List]:
    rest, dates, years = _split_dates(claim)
    return {
        "names": names_in(rest),
        "figures": [f for f in figures_in(rest)],
        "dates": dates,           # [("mar", "17")]
        "years": years,
    }


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None


_MONTH_FULL = {"jan": "january", "feb": "february", "mar": "march", "apr": "april", "may": "may",
               "jun": "june", "jul": "july", "aug": "august", "sep": "september", "oct": "october",
               "nov": "november", "dec": "december"}


def _date_in(text: str, month: str, day: str) -> bool:
    return re.search(rf"\b(?:{month}|{_MONTH_FULL.get(month, month)})\.?\s+{int(day)}(?!\d)", text) is not None


def _anchors_present(anchors: Dict[str, List], norm: str, digits: str) -> bool:
    return (all(_has_word(norm, n) for n in anchors["names"])
            and all(_has_word(digits, f) for f in anchors["figures"])
            and all(_has_word(digits, y) for y in anchors["years"])
            and all(_date_in(norm, m, d) for m, d in anchors["dates"]))


def _highlights(raw: str, base_offset: int, anchors: Dict[str, List]) -> List[Dict[str, int]]:
    """Character spans of each anchor's first occurrence in the window."""
    spans = []
    for n in anchors["names"]:
        m = re.search(rf"(?i)(?<![a-z0-9]){re.escape(n)}", raw)
        if m:
            spans.append((m.start(), m.end()))
    for f in anchors["figures"] + anchors["years"]:
        for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", raw):
            if m.group().replace(",", "").rstrip(".") == f:
                spans.append((m.start(), m.end()))
                break
    for mo, d in anchors["dates"]:
        m = re.search(rf"(?i)\b(?:{mo}|{_MONTH_FULL.get(mo, mo)})\.?\s+{int(d)}(?!\d)", raw)
        if m:
            spans.append((m.start(), m.end()))
    return [{"char_offset": base_offset + a, "char_length": b - a, "contribution": 0.0}
            for a, b in sorted(set(spans))]


def add_anchor_evidence(entries: dict, source_index: Dict[str, Any]) -> int:
    """Cite unsupported draft claims whose anchors all appear in one short
    stretch of a story. Returns how many claims gained a citation."""
    articles = source_index.get("articles", [])
    if not articles:
        return 0
    lowered = [normalize_for_quote(a.get("content", "")) for a in articles]
    share_cache: Dict[str, float] = {}

    def share(name: str) -> float:
        if name not in share_cache:
            share_cache[name] = sum(1 for t in lowered if _has_word(t, name)) / len(lowered)
        return share_cache[name]

    added = 0
    for e in entries.get("entries", []):
        if e.get("passthrough") or e.get("provenance") != "unsupported":
            continue
        anchors = anchors_in(e.get("content", ""))
        n_distinctive = (len(anchors["figures"]) + len(anchors["dates"]) + len(anchors["years"])
                         + sum(1 for n in anchors["names"] if share(n) <= DISTINCTIVE_MAX_SHARE))
        if n_distinctive < MIN_DISTINCTIVE:
            continue
        anchor_words = set(anchors["names"])
        context = [w for w in key_words(e.get("content", "")) if w not in anchor_words]
        best = None
        for a, low in zip(articles, lowered):
            if not all(_has_word(low, n) for n in anchors["names"]):
                continue            # quick reject on the whole story first
            content = a.get("content", "")
            ps = a.get("passages", [])
            for j in range(len(ps)):
                last = ps[min(j + WINDOW_PASSAGES - 1, len(ps) - 1)]
                start, end = ps[j]["char_offset"], last["char_offset"] + last["char_length"]
                raw = content[start:end]
                norm = normalize_for_quote(raw)
                if not _anchors_present(anchors, norm, norm.replace(",", "")):
                    continue
                words = set(key_words(raw))
                overlap = sum(1 for w in context if w in words) / len(context) if context else 1.0
                if best is None or overlap > best[0]:
                    best = (overlap, a, start, end, raw)
        if best is None or best[0] < MIN_CONTEXT_OVERLAP:
            continue
        overlap, a, start, end, raw = best
        e["supports"] = [{
            "article_id": a["article_id"], "article_title": a.get("title", ""),
            "article_date": a.get("date", ""), "article_author": a.get("author", ""),
            "passage_text": raw, "passage_offset": start, "passage_length": end - start,
            "similarity": None, "match_type": "anchors", "context_overlap": round(overlap, 3),
            "anchors": anchors["names"] + anchors["figures"] + anchors["years"]
                       + [f"{m} {d}" for m, d in anchors["dates"]],
            "highlights": _highlights(raw, start, anchors),
        }]
        e["provenance"] = "corpus"
        added += 1
    return added


# ── Sorting the rest: fact, analysis or suggestion ───────────────────────────

CLASSIFY_BATCH = 50

_CLASSIFY_TOOL = {
    "name": "label_claims",
    "description": "Label each numbered sentence from a beat book.",
    "input_schema": {
        "type": "object",
        "properties": {
            "labels": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "kind": {"type": "string", "enum": ["fact", "analysis", "suggestion"]},
                    },
                    "required": ["id", "kind"],
                },
            }
        },
        "required": ["labels"],
    },
}

_CLASSIFY_PROMPT = """\
Each numbered line below is a sentence from a beat book, a reporting guide \
for journalists. Label each one:

- fact: states something checkable about who, what, when, where or how, such \
as a person's role, an action taken, a vote, a figure, a date, a place, or a \
rule. Label it fact even if it is wrong or unsourced; checking comes later.
- analysis: interpretation, significance or characterization that no record \
could confirm, such as "one of the most consequential disputes in a \
generation", "the beat sits at the intersection of finance and politics", or \
"that finding gutted the bill's political cover".
- suggestion: a story idea, question, or advice to the reporter.

A sentence that mixes a checkable fact with a characterization is a fact. \
Call label_claims once with a label for every id.

{lines}"""


def classify_claims(entries: dict, provider: Any) -> Dict[str, Any]:
    """Label unsupported draft claims; analysis → provenance "analysis",
    suggestion → "guidance". Facts stay "unsupported". Never raises: on any
    failure, claims keep their current labels and the error is returned."""
    items = [e for e in entries.get("entries", [])
             if not e.get("passthrough") and e.get("provenance") == "unsupported"]
    info: Dict[str, Any] = {"model": getattr(provider, "label_model", ""), "checked": len(items),
                            "fact": 0, "analysis": 0, "suggestion": 0, "errors": []}
    if provider is None or not items:
        return info
    for start in range(0, len(items), CLASSIFY_BATCH):
        batch = items[start:start + CLASSIFY_BATCH]
        lines = "\n".join(f"{i}. {re.sub(r'[*_`]+', '', e['content']).strip()}"
                          for i, e in enumerate(batch))
        try:
            resp = provider.create(
                model=provider.label_model, system="",
                messages=[{"role": "user", "content": _CLASSIFY_PROMPT.format(lines=lines)}],
                tools=[_CLASSIFY_TOOL], tool_choice={"type": "tool", "name": "label_claims"},
                max_tokens=min(4096, 200 + 24 * len(batch)),
            )
            labels = _parse_labels(resp)
        except Exception as ex:   # classification is best-effort
            info["errors"].append(f"{type(ex).__name__}: {ex}"[:300])
            continue
        if len(labels) < len(batch):
            info["errors"].append(f"{len(batch) - len(labels)} of {len(batch)} claims came back unlabeled")
        for i, e in enumerate(batch):
            kind = labels.get(i)
            if kind == "analysis":
                e["provenance"] = "analysis"
            elif kind == "suggestion":
                e["provenance"] = "guidance"
            if kind in ("fact", "analysis", "suggestion"):
                e["claim_kind"] = kind
                info[kind] += 1
    return info


def _parse_labels(resp: Any) -> Dict[int, str]:
    for block in getattr(resp, "content", []) or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            data = block.get("input") or {}
            if isinstance(data, str):
                data = json.loads(data)
            return {int(x["id"]): str(x["kind"]) for x in data.get("labels", [])
                    if isinstance(x, dict) and "id" in x and "kind" in x}
    text = getattr(resp, "text", "") or ""
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        data = json.loads(m.group(0))
        return {int(x["id"]): str(x["kind"]) for x in data.get("labels", [])}
    raise ValueError("no labels in the model's reply")


def recount(entries: dict) -> None:
    """Refresh the claim counts in entries["stats"] after these passes."""
    st = entries.setdefault("stats", {})
    claims = [e for e in entries.get("entries", []) if not e.get("passthrough")]
    for k in ("cited", "unsupported", "guidance", "analysis"):
        st[k] = 0
    for e in claims:
        prov = e.get("provenance")
        if prov == "corpus":
            st["cited"] += 1
        elif prov in ("unsupported", "guidance", "analysis"):
            st[prov] += 1
    st["cited_by_anchors"] = sum(
        1 for e in claims if e.get("provenance") == "corpus"
        and (e.get("supports") or [{}])[0].get("match_type") == "anchors")
    st["list_items_cited"] = sum(1 for e in claims if e.get("provenance") == "corpus" and e.get("kind") == "list_item")
    st["table_rows_cited"] = sum(1 for e in claims if e.get("provenance") == "corpus" and e.get("kind") == "table_row")


# ── Why a factual claim has no source ────────────────────────────────────────
# The one strong signal is absence: a name, figure or date that appears in
# none of the reporter's stories didn't come from them. Presence proves
# little (in a corpus about one subject nearly every name turns up
# somewhere), so that case is worded as "check it", never as "supported".

UNSOURCED_REASONS = {
    "outside_stories": ("Some of its details appear in none of your stories, so they most "
                        "likely came from the writing model's own general knowledge."),
    "in_stories": ("Its names, figures and dates appear in your stories, but no passage says "
                   "what this sentence says. It may combine details from several stories or "
                   "restate them too loosely to match."),
    "no_details": ("It names no people, figures or dates that could be looked up in your "
                   "stories."),
}


def _as_written(claim: str, label: str) -> str:
    """The claim's own spelling of a detail ("Giants-Jets", "$750")."""
    m = re.search(re.escape(label), re.sub(r"[*_`]+", "", claim), re.I)
    return m.group(0) if m else label


def explain_unsourced(entries: dict, source_index: Dict[str, Any]) -> Dict[str, int]:
    """Give each factual claim still unsupported an `unsourced_reason`
    ("outside_stories", "in_stories" or "no_details") and, for
    "outside_stories", the details found in no story (`details_not_in_stories`).
    Returns counts per reason."""
    corpus = "\n".join(normalize_for_quote(a.get("content", "")) for a in source_index.get("articles", []))
    digits = corpus.replace(",", "")
    counts = {k: 0 for k in UNSOURCED_REASONS}
    for e in entries.get("entries", []):
        if e.get("passthrough") or e.get("provenance") != "unsupported":
            continue
        anchors = anchors_in(e.get("content", ""))
        checks = ([(n, _has_word(corpus, n)) for n in anchors["names"]]
                  + [(f, _has_word(digits, f)) for f in anchors["figures"] + anchors["years"]]
                  + [(f"{_MONTH_FULL.get(m, m).title()} {int(d)}", _date_in(corpus, m, d))
                     for m, d in anchors["dates"]])
        missing = [_as_written(e.get("content", ""), label) for label, found in checks if not found]
        if not checks:
            reason = "no_details"
        elif missing:
            reason = "outside_stories"
            e["details_not_in_stories"] = missing
        else:
            reason = "in_stories"
        e["unsourced_reason"] = reason
        counts[reason] += 1
    return counts
