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

import datetime
import json
import re
from typing import Any, Dict, List, Optional

from research_facts import (_STOPWORDS, _split_dates, dates_implied_by, figures_in, key_words,
                            normalize_for_quote)

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
    "democratic", "republican", "democrat", "gov", "sen", "sens", "rep", "reps", "mr", "mrs",
    "ms", "dr",
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
    # A dash or colon starts a new clause too: in "**Tinaglia** — Skeptical
    # of the plan", "Skeptical" opens the description.
    for sent in re.split(r"(?<=[.!?])\s+|\s+[—–]\s+|:\s+", text):
        tokens = [t.rstrip(".-") for t in re.findall(r"[A-Za-z][A-Za-z'’.-]*", sent)]
        for i, tok in enumerate(tokens):
            if not _CAP_RE.fullmatch(tok) or re.search(r"-[a-z]", tok):
                continue            # "Bears-specific", not "Giants-Jets"
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


# ── Outcomes ─────────────────────────────────────────────────────────────────
# A claim that something happened (won, lost, acquitted, approved, fired)
# needs a passage that says it happened. Embeddings can't tell "Preckwinkle
# faces off Tuesday" from "Preckwinkle turned back Reilly", so a preview
# written before the result matches the result. Each outcome the claim
# states must have a word of the same kind in the passage it's cited to.
# This checks the kind of outcome, not who it happened to: a passage with
# "won" in it passes for any win.

# (kind, words in a claim that state the outcome, words in a passage that report it)
OUTCOMES = [
    ("election",
     r"won|wins|defeated|prevailed|re-?elected|unseated|turned back|fended off"
     r"|conceded(?=[,.;]|$| (?:the (?:race|election|primary|contest)|defeat|to)\b)"
     r"|(?:was|were) elected|lost(?=[,.;]| (?:the|her|his|their|a|re-?election|reelection|to)\b)",
     r"won|wins|winning|defeat\w*|beat|beats|beating|lost|loses|losing|conced\w*"
     r"|prevailed in (?:the |a |her |his |their )?(?:primary|election|race|runoff|contest)"
     r"|re-?elected|(?:was|were|been|is|are|get|got) elected|elected (?:to|as)|unseated|victor\w*"
     r"|ousted|fended off|turned back"),
    ("acquittal", r"acquitted|found not guilty|cleared of", r"acquit\w*|not guilty|cleared"),
    ("conviction", r"convicted|found guilty|pleaded guilty|sentenced",
     r"convict\w*|guilty|sentenc\w*|plea"),
    ("approval",
     r"approved|ratified|adopted|signed into law|passed the (?:house|senate|council|board|legislature)"
     r"|(?:bill|measure|ordinance|budget|plan|resolution|contract|referendum) passed",
     r"approv\w*|ratif\w*|adopt\w*|pass(?:ed|es|age)|signed|voted|vote"),
    ("rejection", r"rejected|vetoed|voted down|struck down|overturned|denied",
     r"reject\w*|veto\w*|voted down|struck down|overturn\w*|den(?:ied|ial|ies|y)"),
    ("removal",
     r"fired|ousted|removed|dismissed|terminated|resigned|stepped down|forced out|(?:was|were) replaced",
     r"fir(?:ed|ing)|oust\w*|remov\w*|dismiss\w*|terminat\w*|resign\w*|step(?:ped|s)? down"
     r"|forced out|replac\w*|depart\w*"),
    ("appointment",
     r"appointed|hired|tapped|(?:was|were) (?:selected|chosen|named|confirmed)",
     r"appoint\w*|hir(?:ed|es|ing)|select\w*|chose|chosen|named|confirm\w*|tapped|picked"),
    ("settlement", r"settled", r"settle\w*|payout|paid"),
    ("withdrawal",
     r"withdrew|withdrawn|dropped (?:the|her|his|their|its|a) (?:lawsuit|suit|case|bid|charges?|appeal|complaint|challenge)",
     r"withdr\w*|drop\w*|dismiss\w*|abandon\w*"),
]
_OUTCOME_RES = [(kind, re.compile(rf"(?<![a-z])(?:{claim})(?![a-z])"),
                 re.compile(rf"(?<![a-z])(?:{passage})(?![a-z])"))
                for kind, claim, passage in OUTCOMES]
# "if she wins", "would have won", "hopes to be elected": not a stated outcome.
_HYPOTHETICAL_RE = re.compile(
    r"\b(?:if|whether|would|will|could|might|may|should|can|to|seeks?|seeking|hopes?|looks?"
    r"|wants?|aims?|trying|expected|plans?|likely)\s+(?:\w+\s+)?$")
_QUOTED_RE = re.compile(r"\"[^\"]*\"")
# How far past the cited passage to look, in characters (about 60 words).
OUTCOME_MARGIN = 400


def outcomes_in(claim: str) -> List[tuple]:
    """(kind, words as written) for each outcome the claim states as having
    happened. Words inside quotation marks and hypotheticals don't count."""
    text = _QUOTED_RE.sub(" ", normalize_for_quote(re.sub(r"[*_`]+", "", claim or "")))
    found = []
    for kind, claim_re, _ in _OUTCOME_RES:
        for m in claim_re.finditer(text):
            if not _HYPOTHETICAL_RE.search(text[:m.start()]):
                found.append((kind, m.group(0)))
                break
    return found


# In a passage, an outcome word after these ("Whoever wins", "if she is
# elected") reports nothing yet.
_CONDITIONAL_RE = re.compile(r"\b(?:if|whether|whoever|whichever|unless|should)\b")
_YEAR_RE = re.compile(r"\b(?:19|20)\d\d\b")
# A year that dates the event ("in 2022", "since March 2019"), not one that
# describes a thing ("a 2023 lawsuit").
_EVENT_YEAR_RE = re.compile(
    r"\b(?:in|during|since|from|by|of)\s+(?:(?:early|late|mid|the)\s+)?"
    r"(?:[a-z]+\.?\s+(?:\d{1,2},?\s+)?)?((?:19|20)\d\d)\b")


_SENTENCE_RE = re.compile(r"[^.!?\n]+(?:[.!?]+[\"'”’)]*|\n|$)")
# A period after these doesn't end a sentence ("Larry Rogers Jr., won ...").
_ABBREV_END = re.compile(
    r"(?:\b(?:Jr|Sr|Mr|Mrs|Ms|Dr|St|Gov|Sen|Sens|Rep|Reps|Ald|Gen|Lt|Col|Capt|Sgt|Supt|Atty|Dept|"
    r"Inc|Co|Corp|Ave|Blvd|No|Vol|Rev|Prof|vs|etc|Jan|Feb|Mar|Apr|Aug|Sept|Sep|Oct|Nov|Dec)|"
    r"\b[A-Z]|\b[A-Z]\.[A-Z]|\b[ap]|\b[ap]\.m)\.$")


class _Span:
    """A sentence's text and position, shaped like a regex match."""
    def __init__(self, text: str, start: int, end: int):
        self._t, self._s, self._e = text, start, end

    def group(self, _i: int = 0) -> str:
        return self._t[self._s:self._e]

    def start(self) -> int:
        return self._s

    def end(self) -> int:
        return self._e


def _sentences(text: str) -> List[_Span]:
    """Sentences of `text` with their offsets, not split after a title or
    initial ("Gov. JB Pritzker", "Larry Rogers Jr., won")."""
    spans: List[List[int]] = []
    for m in _SENTENCE_RE.finditer(text or ""):
        if spans and _ABBREV_END.search(text[spans[-1][0]:spans[-1][1]].rstrip()) \
                and not text[spans[-1][1] - 1:spans[-1][1]] == "\n":
            spans[-1][1] = m.end()
        else:
            spans.append([m.start(), m.end()])
    return [_Span(text, a, b) for a, b in spans]


def _sentence_kinds(kinds: set, sent: str, claim_years: set, names: Optional[List[str]] = None) -> set:
    """Which of `kinds` one sentence reports. A sentence dated to a year the
    claim doesn't name is about another event ("Preckwinkle last won ...
    in 2022"), so it doesn't count."""
    norm = normalize_for_quote(sent)
    if any(y not in claim_years for y in _EVENT_YEAR_RE.findall(norm)):
        return set()
    # A story about a race usually covers several candidates: an election
    # word counts only in a sentence naming someone the claim names.
    if names and "election" in kinds and not any(_has_word(norm, n) for n in names):
        kinds = kinds - {"election"}
    stated = set()
    for kind, _, passage_re in _OUTCOME_RES:
        if kind not in kinds:
            continue
        for m in passage_re.finditer(norm):
            before = norm[:m.start()]
            if not (_CONDITIONAL_RE.search(before) or _HYPOTHETICAL_RE.search(before)):
                stated.add(kind)
                break
    return stated


def _kinds_stated(outcomes: List[tuple], passage: str, claim: str) -> set:
    """Which of the claim's outcome kinds some sentence of the passage reports."""
    kinds = {kind for kind, _ in outcomes}
    claim_years = set(_YEAR_RE.findall(claim or ""))
    names = _claim_names(claim)
    stated = set()
    for m in _sentences(passage or ""):
        stated |= _sentence_kinds(kinds - stated, m.group(0), claim_years, names)
    return stated


_INSTITUTION_WORDS = {
    "review", "appeals", "education", "trustees", "commissioners", "supervisors", "aldermen",
    "primary", "election", "elections", "general", "district", "ward", "precinct", "side",
    "north", "south", "east", "west", "northern", "southern", "eastern", "western", "central",
    "public", "housing", "schools", "school", "agency", "police", "fire", "health", "national",
    "federal", "united", "states", "american", "department", "division", "bureau", "story",
}


def _claim_names(claim: str) -> List[str]:
    """Any capitalized word in the claim that isn't a common one or part of
    an institution's name, including a name that opens the sentence
    ("Steele lost ..."). Lowercased, in order."""
    return [t.lower() for t in re.findall(r"\b[A-Z][a-z'’-]{2,}", re.sub(r"[*_`]+", "", claim or ""))
            if t.lower() not in _NOT_NAMES and t.lower() not in _STOPWORDS
            and t.lower() not in _INSTITUTION_WORDS]


def _outcome_support(kinds: set, article: Dict[str, Any], claim: str, min_names: int = 1) -> Any:
    """A sentence of the story that names someone in the claim (at least
    `min_names` of the claim's names) and reports an outcome the matched
    passages left out, as a support."""
    names = _claim_names(claim)
    # A claim that names no one ("She dropped her lawsuit", a sub-bullet
    # under the person's name) can use a cited story, if the sentence
    # shares at least two of the claim's key words.
    if not names and min_names > 1:
        return None
    claim_words = set(key_words(claim)) if not names else set()
    claim_years = set(_YEAR_RE.findall(claim or ""))
    content = article.get("content", "")
    for m in _sentences(content):
        sent = m.group(0)
        norm = normalize_for_quote(sent)
        if names and sum(1 for n in dict.fromkeys(names) if _has_word(norm, n)) < min(min_names, len(set(names))):
            continue
        if not names and len(claim_words & set(key_words(sent))) < 2:
            continue
        if _sentence_kinds(kinds, sent, claim_years) != kinds:
            continue
        a, b = m.start(), m.end()
        while a < b and content[a].isspace():
            a += 1
        start, end = max(0, a - OUTCOME_MARGIN // 2), min(len(content), b + OUTCOME_MARGIN // 2)
        return {
            "article_id": article["article_id"], "article_title": article.get("title", ""),
            "article_date": article.get("date", ""), "article_author": article.get("author", ""),
            "passage_text": content[start:end], "passage_offset": start, "passage_length": end - start,
            "similarity": None, "match_type": "outcome",
            "highlights": [{"char_offset": a, "char_length": b - a, "contribution": 0.0}],
        }
    return None


def _support_window(sup: Dict[str, Any], contents: Dict[str, str]) -> str:
    content = contents.get(sup.get("article_id", ""))
    start, length = sup.get("passage_offset"), sup.get("passage_length")
    if content is None or start is None or length is None:
        return sup.get("passage_text", "")
    return content[max(0, start - OUTCOME_MARGIN):start + length + OUTCOME_MARGIN]


def check_outcomes(entries: dict, source_index: Dict[str, Any]) -> int:
    """Make sure a claim's citations report the outcomes it states.

    Citations whose passage reports none of them are dropped. If those left
    miss an outcome, the stories are searched for a sentence that names
    people in the claim and reports it (one name in a story already cited,
    two in any other); each such sentence is cited first. Failing that, the claim becomes unsourced, marked with
    `outcome_not_stated` (its words for the outcomes nothing reports).
    Returns how many claims became unsourced."""
    articles = {a["article_id"]: a for a in source_index.get("articles", [])}
    contents = {k: a.get("content", "") for k, a in articles.items()}
    dropped = 0
    for e in entries.get("entries", []):
        if e.get("passthrough") or e.get("provenance") != "corpus" or not e.get("supports"):
            continue
        claim = e.get("content", "")
        outcomes = outcomes_in(claim)
        if not outcomes:
            continue
        kept, covered = [], set()
        for s in e["supports"]:
            stated = _kinds_stated(outcomes, _support_window(s, contents), claim)
            if stated:
                kept.append(s)
                covered |= stated
        missing = {kind for kind, _ in outcomes} - covered
        cited = [articles[aid] for aid in dict.fromkeys(s.get("article_id") for s in e["supports"])
                 if aid in articles]
        others = [a for aid, a in articles.items() if aid not in {c["article_id"] for c in cited}]
        for kind in sorted(missing):
            # A cited story needs one of the claim's names in the sentence;
            # any other story needs two, since it wasn't matched at all.
            found = next((f for f in (_outcome_support({kind}, a, claim) for a in cited) if f), None) \
                or next((f for f in (_outcome_support({kind}, a, claim, 2) for a in others) if f), None)
            if found:
                kept.insert(0, found)
                missing.discard(kind)
        if not missing:
            e["supports"] = kept
            continue
        e["supports"] = []
        e["provenance"] = "unsupported"
        e["outcome_not_stated"] = [words for kind, words in outcomes if kind in missing]
        dropped += 1
    return dropped


# ── Contradictions ───────────────────────────────────────────────────────────
# A Qwen book said "Steele won the primary"; the stories say "Steele lost
# reelection ... to Liz Nicholson". The outcome check only asks whether the
# cited passage reports an outcome of the same kind, and "lost" is one. Here
# each outcome is tied to the person it happens to, with a direction: a claim
# that pins one direction on a name, where a story pins the opposite on the
# same name and no story agrees, is contradicted.

# family → {direction: words}. Intransitive words are about the name before
# them ("Steele lost"); transitive ones are a win for the name before and a
# loss for the name after ("Nicholson defeated Steele").
_POLAR = {
    "election": {
        "win": r"won|wins|prevailed|(?:was|were|been) (?:re-?)?elected|re-?elected|clinched",
        "loss": r"lost|loses|conceded|(?:was|were) (?:defeated|unseated|ousted|beaten)|fell short",
    },
    "verdict": {
        "win": r"(?:was|were)? ?acquitted|found not guilty|cleared of",
        "loss": r"(?:was|were)? ?convicted|found guilty|pleaded guilty",
    },
}
_TRANSITIVE_WIN = r"defeated|beat|unseated|ousted|turned back|fended off"
# How many words may sit between a name and its outcome word.
_POLAR_GAP = 4
_POLAR_RES = {fam: {d: re.compile(rf"(?<![a-z])(?:{w})(?![a-z])") for d, w in dirs.items()}
              for fam, dirs in _POLAR.items()}
_TRANSITIVE_RE = re.compile(rf"(?<![a-z])(?:{_TRANSITIVE_WIN})(?![a-z])")


def _directed_outcomes(sentence: str, names: List[str]) -> set:
    """{(name, family, "win" | "loss")} that the sentence pins on each of
    `names`, as a subject a few words before the outcome word, or as the
    object of a transitive win ("defeated Steele" is a loss for Steele)."""
    norm = normalize_for_quote(sentence)
    if _CONDITIONAL_RE.search(norm):
        return set()
    words = list(re.finditer(r"[a-z0-9'’-]+", norm))
    out = set()
    for n in names:
        for i, w in enumerate(words):
            if re.sub(r"['’]s$", "", w.group(0)) != n:
                continue
            after = norm[w.end():words[min(i + _POLAR_GAP, len(words) - 1)].end()] if i + 1 < len(words) else ""
            before = norm[words[max(0, i - 3)].start():w.start()] if i else ""
            for fam, dirs in _POLAR_RES.items():
                for d, rx in dirs.items():
                    m = rx.search(after)
                    if m and not _HYPOTHETICAL_RE.search(after[:m.start()]):
                        out.add((n, fam, d))
            t = _TRANSITIVE_RE.search(after)
            if t and not _HYPOTHETICAL_RE.search(after[:t.start()]):
                out.add((n, "election", "win"))
            if _TRANSITIVE_RE.search(before):
                out.add((n, "election", "loss"))
    return out


def check_contradictions(entries: dict, source_index: Dict[str, Any]) -> int:
    """Mark claims a story contradicts: provenance "unsupported", citations
    removed, `contradicted_by` = the story sentence. Returns how many."""
    articles = source_index.get("articles", [])
    sentences = [(a, m.group(0)) for a in articles for m in _sentences(a.get("content", ""))]
    found = 0
    for e in entries.get("entries", []):
        if e.get("passthrough") or e.get("provenance") not in ("corpus", "unsupported"):
            continue
        claim = e.get("content", "")
        names = list(dict.fromkeys(_claim_names(claim)))
        if not names:
            continue
        stated = _directed_outcomes(claim, names)
        if not stated:
            continue
        claim_years = set(_YEAR_RE.findall(claim))
        agree, against = False, None
        for a, sent in sentences:
            if any(y not in claim_years for y in _EVENT_YEAR_RE.findall(normalize_for_quote(sent))):
                continue
            seen = _directed_outcomes(sent, names)
            if not seen:
                continue
            for n, fam, d in stated:
                if (n, fam, d) in seen:
                    agree = True
                elif against is None and (n, fam, "loss" if d == "win" else "win") in seen:
                    against = (a, sent.strip())
            if agree:
                break
        if agree or against is None:
            continue
        a, sent = against
        e["contradicted_by"] = {"article_id": a.get("article_id"), "article_title": a.get("title", ""),
                                "article_date": a.get("date", ""), "sentence": sent[:400]}
        e["supports"] = []
        e["provenance"] = "unsupported"
        e["claim_kind"] = "fact"
        found += 1
    return found


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
        outcomes = outcomes_in(e.get("content", ""))
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
                if outcomes and len(_kinds_stated(outcomes, raw, e.get("content", ""))) < len(outcomes):
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
        e.pop("outcome_not_stated", None)
        added += 1
    return added


# ── Near matches confirmed by a detail ──────────────────────────────────────
# In a corpus about one subject, passages just below the cutoff are often
# the right source, but as often they are only about the same subject. A
# shared distinctive detail (a name, figure, year or date found in few
# stories) separates the two about as well as the cutoff does above it: a
# figure or date on its own, or at least two names. One name alone ("HUD",
# "Parker") turns up in too many passages about the same subject.

def add_near_evidence(entries: dict, source_index: Dict[str, Any]) -> int:
    """Cite an unsupported factual claim to a near-cutoff passage (the
    matcher's `near_supports`) that contains a distinctive figure or date
    from the claim, or two of its distinctive names. Run it after
    classify_claims, so questions and tips stay uncited. Removes
    `near_supports` from every entry. Returns how many claims gained a
    citation."""
    articles = source_index.get("articles", [])
    lowered = [normalize_for_quote(a.get("content", "")) for a in articles]
    share_cache: Dict[str, float] = {}

    def share(key: str, test) -> float:
        if key not in share_cache:
            share_cache[key] = sum(1 for t in lowered if test(t)) / len(lowered) if lowered else 1.0
        return share_cache[key]

    added = 0
    for e in entries.get("entries", []):
        near = e.pop("near_supports", None)
        if not near or e.get("passthrough") or e.get("provenance") != "unsupported":
            continue
        anchors = anchors_in(e.get("content", ""))
        details = (
            [("name", n) for n in anchors["names"] if share("n:" + n, lambda t, n=n: _has_word(t, n)) <= DISTINCTIVE_MAX_SHARE]
            + [("figure", f) for f in anchors["figures"] + anchors["years"]
               if share("f:" + f, lambda t, f=f: _has_word(t.replace(",", ""), f)) <= DISTINCTIVE_MAX_SHARE]
            + [("date", (m, d)) for m, d in anchors["dates"]
               if share(f"d:{m}{d}", lambda t, m=m, d=d: _date_in(t, m, d)) <= DISTINCTIVE_MAX_SHARE])
        if not details:
            continue
        kept = []
        for sup in near:
            norm = normalize_for_quote(sup.get("passage_text", ""))
            found = [v for kind, v in details
                     if (kind == "name" and _has_word(norm, v))
                     or (kind == "figure" and _has_word(norm.replace(",", ""), v))
                     or (kind == "date" and _date_in(norm, *v))]
            if len(found) >= 2 or any(kind != "name" for kind, v in details if v in found):
                one = {"names": [v for v in found if isinstance(v, str) and not v.isdigit()],
                       "figures": [], "years": [v for v in found if isinstance(v, str) and v.isdigit()],
                       "dates": [v for v in found if isinstance(v, tuple)]}
                sup = {**sup, "match_type": "near",
                       "anchors": [v if isinstance(v, str) else f"{v[0]} {v[1]}" for v in found],
                       "highlights": _highlights(sup.get("passage_text", ""), sup.get("passage_offset", 0), one)}
                kept.append(sup)
        if kept:
            e["supports"] = kept
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
            info["errors"].append(f"{len(batch) - len(labels)} of {len(batch)} claims came back unlabeled"
                                  f" (reply began: {_reply_sample(resp)!r})")
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


_KINDS = ("fact", "analysis", "suggestion")


def _label_items(data: Any) -> Dict[int, str]:
    """Labels from the shapes models return: {"labels": [{"id", "kind"}]},
    another key for the kind ("label", "type", "category"), a list of kinds
    in id order, or a mapping of id to kind."""
    if isinstance(data, dict):
        inner = next((data[k] for k in ("labels", "claims", "items", "results") if k in data), None)
        if inner is None:
            # {"0": "fact", "1": "analysis"}
            return {int(k): str(v).lower() for k, v in data.items()
                    if str(k).strip().isdigit() and str(v).lower() in _KINDS}
        data = inner
    out: Dict[int, str] = {}
    if isinstance(data, dict):
        return _label_items(data)
    for i, x in enumerate(data if isinstance(data, list) else []):
        if isinstance(x, str) and x.lower() in _KINDS:
            out[i] = x.lower()
        elif isinstance(x, dict):
            kind = next((str(x[k]).lower() for k in ("kind", "label", "type", "category", "class")
                         if k in x), "")
            key = next((x[k] for k in ("id", "index", "number", "n") if k in x), i)
            if kind in _KINDS and str(key).strip().isdigit():
                out[int(key)] = kind
    return out


def _parse_labels(resp: Any) -> Dict[int, str]:
    for block in getattr(resp, "content", []) or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            data = block.get("input") or {}
            if isinstance(data, str):
                data = json.loads(data)
            return _label_items(data)
    text = getattr(resp, "text", "") or ""
    m = re.search(r"[\[{].*[\]}]", text, re.S)
    if m:
        try:
            return _label_items(json.loads(m.group(0)))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"no labels in the model's reply: {text[:200]!r}")


def _reply_sample(resp: Any) -> str:
    """The start of a reply, for the build record when labels can't be read."""
    for block in getattr(resp, "content", []) or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return json.dumps(block.get("input"))[:200]
    return (getattr(resp, "text", "") or "")[:200]


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
    st["cited_near_cutoff"] = sum(
        1 for e in claims if e.get("provenance") == "corpus"
        and (e.get("supports") or [{}])[0].get("match_type") == "near")
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
    "contradicted": ("A story says otherwise. The writing model may have reversed an outcome or "
                     "mixed up two people."),
    "outcome_not_stated": ("Your stories cover it, but none of the passages that match it says "
                           "this happened. They may have been written before the outcome was "
                           "known."),
    "no_details": ("It names no people, figures or dates that could be looked up in your "
                   "stories."),
}


def _as_written(claim: str, label: str) -> str:
    """The claim's own spelling of a detail ("Giants-Jets", "$750")."""
    m = re.search(re.escape(label), re.sub(r"[*_`]+", "", claim), re.I)
    return m.group(0) if m else label


def explain_unsourced(entries: dict, source_index: Dict[str, Any]) -> Dict[str, int]:
    """Give each factual claim still unsupported an `unsourced_reason`
    ("contradicted", "outside_stories", "outcome_not_stated", "in_stories" or "no_details") and, for
    "outside_stories", the details found in no story (`details_not_in_stories`).
    Returns counts per reason."""
    articles = source_index.get("articles", [])
    corpus = "\n".join(normalize_for_quote(a.get("content", "")) for a in articles)
    digits = corpus.replace(",", "")
    counts = {k: 0 for k in UNSOURCED_REASONS}

    def published(a: Dict[str, Any]) -> Any:
        try:
            return datetime.date.fromisoformat(str(a.get("date", ""))[:10])
        except ValueError:
            return None

    def date_found(claim: str, month: str, day: str) -> bool:
        """Written out in a story, or pinned down by one: "Thursday" in a
        story published Friday, June 5 is June 4."""
        if _date_in(corpus, month, day):
            return True
        return any((month, str(int(day))) == (m, d)
                   for a in articles
                   for m, d, _ in dates_implied_by(a.get("content", ""), published(a), claim))
    for e in entries.get("entries", []):
        if e.get("passthrough") or e.get("provenance") != "unsupported":
            continue
        anchors = anchors_in(e.get("content", ""))
        checks = ([(n, _has_word(corpus, n)) for n in anchors["names"]]
                  + [(f, _has_word(digits, f)) for f in anchors["figures"] + anchors["years"]]
                  + [(f"{_MONTH_FULL.get(m, m).title()} {int(d)}", date_found(e.get("content", ""), m, d))
                     for m, d in anchors["dates"]])
        missing = [_as_written(e.get("content", ""), label) for label, found in checks if not found]
        if e.get("contradicted_by"):
            reason = "contradicted"
        elif not checks:
            reason = "no_details"
        elif missing:
            reason = "outside_stories"
            e["details_not_in_stories"] = missing
        elif e.get("outcome_not_stated"):
            reason = "outcome_not_stated"
        else:
            reason = "in_stories"
        e["unsourced_reason"] = reason
        counts[reason] += 1
    return counts
