"""
ingest.py
---------
Multi-format ingestion pipeline: raw bytes / URLs → normalized story dicts.

Two stages:
1. extract_text(filename, raw_bytes) → plain text/markdown
   Format dispatcher. markitdown handles docx/pdf/html/pptx/xlsx/rtf.
   stdlib handles txt/md/json. Unknown extensions get a utf-8 best-effort.

2. normalize(text, source_label, ollama_key) → list[Story]
   Single Ollama Cloud (qwen3.5:397b-cloud) call with strict tool-use schema.
   The LLM only infers metadata (title/date/author) and returns character
   offsets for each story's body — the body itself is sliced from the
   original text, never LLM-rewritten.

A wrapper, ingest_source(), runs both stages and packages the result for
the /ingest route.
"""

from __future__ import annotations

import html
import io
import ipaddress
import json
import logging
import re
import socket
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx

from ollama_client import CHAT_MODEL, chat_client

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

MAX_FILE_BYTES = 15 * 1024 * 1024            # 15 MB per file
MAX_NORMALIZE_CHARS = 120_000                # cap before single-call normalization
URL_FETCH_TIMEOUT = 15.0                     # seconds
URL_USER_AGENT = "BeatBookBuilder/1.0 (+https://github.com/clayludwig/beat-book)"
NORMALIZE_MODEL = CHAT_MODEL
NORMALIZE_MAX_TOKENS = 4096

# Extensions markitdown converts to readable markdown.
_MARKITDOWN_EXTS = {
    ".docx", ".doc",
    ".pdf",
    ".html", ".htm",
    ".pptx", ".ppt",
    ".xlsx", ".xls",
    ".csv",
    ".rtf",
    ".epub",
}
# Extensions we read directly as utf-8 text.
_TEXT_EXTS = {".txt", ".md", ".markdown", ".log"}


# ─────────────────────────────────────────────────────────────────────────────
# TYPES
# ─────────────────────────────────────────────────────────────────────────────


class IngestError(Exception):
    """Raised when a single source can't be ingested. Surfaced to the user."""


@dataclass
class Story:
    """A single normalized story. Output of stage 2."""
    title: str
    content: str
    date: str = ""
    author: str = ""
    link: str = ""
    confidence: str = "medium"   # "high" | "medium" | "low"
    reasoning: str = ""          # one-sentence justification for the preview UI

    def to_pipeline_dict(self) -> dict:
        """Shape consumed by pipeline.py / agent.py / citation_matcher.py."""
        out = {"title": self.title, "content": self.content}
        if self.date:
            out["date"] = self.date
        if self.author:
            out["author"] = self.author
        if self.link:
            out["link"] = self.link
        return out

    def to_preview_dict(self) -> dict:
        """Shape sent to the preview UI (includes metadata fields)."""
        return {
            "title": self.title,
            "content": self.content,
            "date": self.date,
            "author": self.author,
            "link": self.link,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
        }


@dataclass
class IngestedSource:
    """Result of ingesting one file or one URL."""
    source_label: str            # filename or URL
    kind: str                    # "file" | "url"
    stories: list[Story] = field(default_factory=list)
    excluded: bool = False
    skip_reason: str = ""
    extract_error: str = ""
    char_count: int = 0
    truncated: bool = False

    def to_preview_dict(self) -> dict:
        return {
            "source_label": self.source_label,
            "kind": self.kind,
            "stories": [s.to_preview_dict() for s in self.stories],
            "excluded": self.excluded,
            "skip_reason": self.skip_reason,
            "extract_error": self.extract_error,
            "char_count": self.char_count,
            "truncated": self.truncated,
        }


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 1 — EXTRACT
# ─────────────────────────────────────────────────────────────────────────────


def _ext_of(filename: str) -> str:
    return Path(filename).suffix.lower()


def _decode_text(raw: bytes) -> str:
    """Best-effort utf-8 decode with replacement for stray bytes."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _clean_inline_html(s: str) -> str:
    """Unescape HTML entities and strip tags, preserving paragraph breaks.

    Applied to bodies pulled out of JSON/RSS records where the value commonly
    contains raw HTML. Keeps the text in a shape the LLM can read directly,
    so the markers it returns will match the extracted text.
    """
    if not s:
        return s
    # Decode entities twice in case of double-encoding (&amp;lt; → &lt; → <).
    text = html.unescape(html.unescape(s))
    # Block tags → paragraph breaks before stripping the rest.
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(
        r"(?i)</\s*(p|div|li|h[1-6]|blockquote|tr|article|section)\s*>",
        "\n\n", text,
    )
    text = re.sub(
        r"(?i)<\s*(p|div|li|h[1-6]|blockquote|tr|article|section)(\s[^>]*)?>",
        "\n\n", text,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_with_markitdown(filename: str, raw: bytes) -> str:
    """Convert binary/office formats to markdown via markitdown.

    markitdown reads from a path, so we write the bytes to a NamedTemporaryFile
    with the correct suffix and let it dispatch on extension.
    """
    from markitdown import MarkItDown

    suffix = _ext_of(filename) or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        tmp.write(raw)
        tmp.flush()
        md = MarkItDown()
        result = md.convert(tmp.name)
        return (result.text_content or "").strip()


def _extract_json(raw: bytes) -> str:
    """Render JSON as readable markdown the LLM can scan for stories.

    No privileged path — we just produce text so stage 2 can normalize it
    the same way it would any other input.
    """
    text = _decode_text(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Not valid JSON — return raw text; the LLM can still try.
        return text

    def render_record(rec: dict, idx: Optional[int] = None) -> str:
        title = (rec.get("title") or rec.get("headline") or "").strip()
        date = (rec.get("date") or rec.get("published") or rec.get("published_parsed") or "").strip() if isinstance(rec.get("date") or rec.get("published") or rec.get("published_parsed"), str) else ""
        author = (rec.get("author") or rec.get("byline") or "").strip() if isinstance(rec.get("author") or rec.get("byline"), str) else ""
        link = (rec.get("link") or rec.get("url") or "").strip() if isinstance(rec.get("link") or rec.get("url"), str) else ""
        body = rec.get("content") or rec.get("summary") or rec.get("body") or rec.get("text") or ""
        if not isinstance(body, str):
            body = json.dumps(body, ensure_ascii=False)
        # Bodies pulled from JSON/RSS commonly contain HTML — unescape and
        # strip tags so the LLM sees the same characters it will return as
        # markers. Otherwise the marker resolver can't find the body.
        body = _clean_inline_html(body)

        header_lines = []
        if title:
            header_lines.append(f"# {title}")
        meta_bits = []
        if date:
            meta_bits.append(f"Date: {date}")
        if author:
            meta_bits.append(f"Author: {author}")
        if link:
            meta_bits.append(f"Link: {link}")
        if meta_bits:
            header_lines.append(" · ".join(meta_bits))

        if not body and not header_lines:
            # No standard fields — dump the whole record as JSON so the LLM
            # at least sees the data.
            return json.dumps(rec, indent=2, ensure_ascii=False)

        parts = []
        if header_lines:
            parts.append("\n".join(header_lines))
        if body:
            parts.append(body)
        return "\n\n".join(parts)

    # Common wrappers
    if isinstance(data, dict):
        if "entries" in data and isinstance(data["entries"], list):
            data = data["entries"]
        elif "stories" in data and isinstance(data["stories"], list):
            data = data["stories"]
        elif "items" in data and isinstance(data["items"], list):
            data = data["items"]

    if isinstance(data, list):
        rendered = [render_record(r, i) for i, r in enumerate(data) if isinstance(r, dict)]
        return "\n\n---\n\n".join(rendered)
    if isinstance(data, dict):
        return render_record(data)

    # Scalars — just stringify
    return str(data)


def extract_text(filename: str, raw: bytes) -> str:
    """Stage 1. Dispatch on extension to produce clean text/markdown."""
    if len(raw) > MAX_FILE_BYTES:
        raise IngestError(
            f"{filename}: file is {len(raw) / 1_048_576:.1f} MB; the limit is "
            f"{MAX_FILE_BYTES / 1_048_576:.0f} MB."
        )

    ext = _ext_of(filename)

    if ext == ".json":
        return _extract_json(raw)

    if ext in _TEXT_EXTS:
        return _decode_text(raw).strip()

    if ext in _MARKITDOWN_EXTS:
        try:
            return _extract_with_markitdown(filename, raw)
        except Exception as e:
            raise IngestError(f"{filename}: failed to extract — {type(e).__name__}: {e}") from e

    # Unknown — try utf-8, then markitdown as a hail-mary
    decoded = _decode_text(raw).strip()
    if decoded and "\x00" not in decoded[:1000]:
        return decoded
    try:
        return _extract_with_markitdown(filename, raw)
    except Exception as e:
        raise IngestError(
            f"{filename}: unsupported file type {ext!r}; could not extract text."
        ) from e


# ─────────────────────────────────────────────────────────────────────────────
# URL FETCHING (with SSRF protection)
# ─────────────────────────────────────────────────────────────────────────────


def _is_blocked_ip(host: str) -> bool:
    """Resolve hostname; reject loopback / private / link-local / multicast."""
    try:
        # getaddrinfo returns all A/AAAA records — check every one.
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True  # unresolvable → block

    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return True
    return False


def fetch_url(url: str) -> Tuple[str, bytes]:
    """Fetch a URL, return (suggested_filename, raw_bytes).

    Refuses non-http(s) schemes and private/loopback addresses.
    Honors MAX_FILE_BYTES.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise IngestError(f"{url}: only http and https URLs are accepted.")
    if not parsed.hostname:
        raise IngestError(f"{url}: URL is missing a hostname.")
    if _is_blocked_ip(parsed.hostname):
        raise IngestError(f"{url}: URL resolves to a private or unreachable address.")

    headers = {"User-Agent": URL_USER_AGENT, "Accept": "*/*"}
    try:
        with httpx.Client(
            timeout=URL_FETCH_TIMEOUT,
            follow_redirects=True,
            headers=headers,
        ) as client:
            resp = client.get(url)
    except httpx.HTTPError as e:
        raise IngestError(f"{url}: fetch failed — {type(e).__name__}: {e}") from e

    if resp.status_code >= 400:
        raise IngestError(f"{url}: server returned HTTP {resp.status_code}.")

    body = resp.content
    if len(body) > MAX_FILE_BYTES:
        raise IngestError(
            f"{url}: response is {len(body) / 1_048_576:.1f} MB; the limit is "
            f"{MAX_FILE_BYTES / 1_048_576:.0f} MB."
        )

    # Decide a filename for extension dispatch.
    content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    type_to_ext = {
        "text/html": ".html",
        "application/xhtml+xml": ".html",
        "text/plain": ".txt",
        "text/markdown": ".md",
        "application/json": ".json",
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/rtf": ".rtf",
        "text/rtf": ".rtf",
    }
    suggested_ext = type_to_ext.get(content_type, "")
    path_part = Path(parsed.path).name or parsed.hostname
    if suggested_ext and not path_part.lower().endswith(suggested_ext):
        path_part = f"{path_part}{suggested_ext}" if path_part else f"page{suggested_ext}"
    elif not _ext_of(path_part):
        path_part = f"{path_part}.html"  # default for unknown content-type

    return path_part, body


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 2 — NORMALIZE (LLM)
# ─────────────────────────────────────────────────────────────────────────────


_NORMALIZE_TOOL = {
    "type": "function",
    "function": {
        "name": "register_stories",
        "description": (
            "Register the news stories you found in the document. "
            "Return is_news_content=false when the document is not news content "
            "(e.g., reporter notes, invoices, transcripts of meetings) — in that "
            "case return an empty stories list and explain in skip_reason."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "is_news_content": {
                    "type": "boolean",
                    "description": "True if the document contains at least one news article or substantive story-like piece of journalism.",
                },
                "skip_reason": {
                    "type": "string",
                    "description": "If is_news_content is false, one short sentence explaining what the document looks like instead. Empty otherwise.",
                },
                "stories": {
                    "type": "array",
                    "description": "One entry per distinct news story in the document. Split multi-story documents (e.g., a notebook with several articles). Leave empty if is_news_content is false.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "The story's headline. If no headline is present, write a 6-10 word descriptive title.",
                            },
                            "date": {
                                "type": "string",
                                "description": "Publication date as YYYY-MM-DD. Empty string if not present in the document.",
                            },
                            "author": {
                                "type": "string",
                                "description": "Byline author. Empty string if not present. Do not put the publication name here.",
                            },
                            "link": {
                                "type": "string",
                                "description": "Source URL if present in the document. Empty string otherwise.",
                            },
                            "body_starts_with": {
                                "type": "string",
                                "description": "The first 30-80 characters of this story's body — COPIED VERBATIM from the document. This must appear exactly in the document so the server can locate the body's beginning. Start with the first sentence of the article body itself, not the title or byline.",
                            },
                            "body_ends_with": {
                                "type": "string",
                                "description": "The last 30-80 characters of this story's body — COPIED VERBATIM from the document. This must appear exactly in the document so the server can locate the body's end.",
                            },
                            "confidence": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                                "description": "high = metadata is explicit in the text. medium = metadata is reasonably inferred. low = metadata is a guess.",
                            },
                            "reasoning": {
                                "type": "string",
                                "description": "One sentence explaining how you identified this as a story and where the metadata came from.",
                            },
                        },
                        "required": [
                            "title", "date", "author", "link",
                            "body_starts_with", "body_ends_with",
                            "confidence", "reasoning",
                        ],
                    },
                },
            },
            "required": ["is_news_content", "skip_reason", "stories"],
        },
    },
}


_NORMALIZE_SYSTEM = (
    "You are a document parser for the Beat Book Builder, a tool that helps "
    "journalists turn a collection of source articles into a reporting guide. "
    "You receive a single document — extracted from any format (Word, PDF, "
    "HTML, plain text, markdown, JSON, scraped web pages) — and you identify "
    "the discrete news stories inside it.\n\n"
    "Rules:\n"
    "- One document may contain ZERO, ONE, or MANY stories. Split multi-story "
    "documents using their natural boundaries (headings, separator lines, "
    "byline blocks).\n"
    "- For each story, extract title, date (YYYY-MM-DD or empty), author "
    "(byline only, not publication name), and a source URL if present.\n"
    "- body_starts_with: a VERBATIM 30-80 character snippet from the document "
    "marking where this story's body begins. It must be the literal opening "
    "text of the article body (the first sentence) — NOT the title, byline, "
    "or any separator line above it.\n"
    "- body_ends_with: a VERBATIM 30-80 character snippet from the document "
    "marking where this story's body ends. It must be the last text of this "
    "article before the next article (or end of document).\n"
    "- The snippets must appear exactly in the document — character-for-"
    "character. The server will locate them with a substring search, so "
    "any paraphrasing or whitespace difference breaks the match.\n"
    "- If the document is not news content (meeting notes, an invoice, raw "
    "data, an empty file), set is_news_content=false, leave stories empty, "
    "and put a short explanation in skip_reason.\n"
    "- Do NOT rewrite or summarize content. The server uses your snippets "
    "to slice the original text verbatim.\n"
    "- You MUST call the register_stories tool. Do not respond with prose."
)


def _normalize_for_match(s: str) -> str:
    """Collapse whitespace so fuzzy substring searches survive small reformats."""
    return re.sub(r"\s+", " ", s).strip()


def _resolve_marker_offset(text: str, marker: str, *, after: int = 0) -> int:
    """Find `marker` in `text` starting at `after`, with whitespace-tolerant fallback.

    Returns -1 if not found.
    """
    marker = (marker or "").strip()
    if not marker:
        return -1

    # 1. Exact substring match.
    pos = text.find(marker, after)
    if pos >= 0:
        return pos

    # 2. Whitespace-normalized search — collapse runs of whitespace on both
    # sides and find the marker in the normalized text, then map the index
    # back to the original.
    norm_text = _normalize_for_match(text[after:])
    norm_marker = _normalize_for_match(marker)
    norm_pos = norm_text.find(norm_marker)
    if norm_pos >= 0:
        # Walk the original text counting normalized chars until we reach
        # norm_pos, then return the matching offset in the original.
        i, j = after, 0
        prev_space = False
        while i < len(text) and j < norm_pos:
            ch = text[i]
            if ch.isspace():
                if not prev_space:
                    j += 1
                    prev_space = True
            else:
                j += 1
                prev_space = False
            i += 1
        return i

    # 3. First-five-words fallback — handles minor LLM paraphrasing.
    first_words = " ".join(marker.split()[:5])
    if first_words and first_words != marker:
        pos = text.find(first_words, after)
        if pos >= 0:
            return pos

    return -1


def _slice_body(text: str, body_starts_with: str, body_ends_with: str) -> str:
    """Locate the body inside `text` using the LLM's start/end snippets."""
    start = _resolve_marker_offset(text, body_starts_with)
    if start < 0:
        return ""

    end_marker = (body_ends_with or "").strip()
    end_pos = _resolve_marker_offset(text, end_marker, after=start)
    if end_pos < 0:
        end = len(text)
    else:
        end = end_pos + len(end_marker)
        end = min(end, len(text))

    return text[start:end].strip()


def normalize(
    text: str,
    source_label: str,
    ollama_key: str,
    *,
    link_hint: str = "",
) -> Tuple[list[Story], bool, str]:
    """Stage 2. Run extracted text through Ollama Cloud (qwen3.5:397b-cloud)
    to produce structured stories.

    Returns (stories, is_news_content, skip_reason).
    """
    if not text or not text.strip():
        return [], False, "The document appears to be empty."

    truncated = False
    if len(text) > MAX_NORMALIZE_CHARS:
        text = text[:MAX_NORMALIZE_CHARS]
        truncated = True

    client = chat_client(ollama_key)

    user_prefix = f"Source label: {source_label}\n"
    if link_hint:
        user_prefix += f"Source URL: {link_hint}\n"
    if truncated:
        user_prefix += (
            f"Note: the document was truncated to {MAX_NORMALIZE_CHARS} "
            "characters for parsing. Identify stories within this window only.\n"
        )
    user_prefix += f"Document length (chars): {len(text)}\n\n"
    user_prefix += "----- BEGIN DOCUMENT -----\n"
    user_suffix = "\n----- END DOCUMENT -----"

    try:
        resp = client.chat.completions.create(
            model=NORMALIZE_MODEL,
            max_tokens=NORMALIZE_MAX_TOKENS,
            messages=[
                {"role": "system", "content": _NORMALIZE_SYSTEM},
                {"role": "user", "content": user_prefix + text + user_suffix},
            ],
            tools=[_NORMALIZE_TOOL],
            tool_choice={"type": "function", "function": {"name": "register_stories"}},
        )
    except Exception as e:
        raise IngestError(
            f"{source_label}: LLM normalization failed — {type(e).__name__}: {e}"
        ) from e

    msg = resp.choices[0].message
    tool_calls = msg.tool_calls or []
    tool_call = next(
        (tc for tc in tool_calls if tc.function and tc.function.name == "register_stories"),
        None,
    )
    if tool_call is None:
        raise IngestError(f"{source_label}: LLM did not return structured stories.")

    try:
        payload = json.loads(tool_call.function.arguments or "{}")
    except json.JSONDecodeError as e:
        raise IngestError(
            f"{source_label}: LLM returned invalid tool-call JSON — {e}"
        ) from e
    is_news = bool(payload.get("is_news_content", False))
    skip_reason = (payload.get("skip_reason") or "").strip()
    raw_stories = payload.get("stories") or []

    if not is_news or not raw_stories:
        return [], False, skip_reason or "No news stories found in this document."

    stories: list[Story] = []
    single_story_total = len(raw_stories) == 1
    for raw in raw_stories:
        body_start = raw.get("body_starts_with") or ""
        body_end = raw.get("body_ends_with") or ""

        content = _slice_body(text, body_start, body_end)

        # Only fall back to the full document when marker lookup totally
        # failed AND there is only one story in this document — for
        # multi-story files, full-doc fallback would duplicate content
        # across stories.
        if len(content) < 20 and single_story_total:
            logger.warning(
                "Marker lookup for %s failed (got %d chars); falling back to full text.",
                source_label, len(content),
            )
            content = text.strip()

        if not content:
            logger.warning("Dropping story with empty body from %s.", source_label)
            continue

        title = (raw.get("title") or "").strip()
        if not title:
            first_line = next((ln for ln in content.splitlines() if ln.strip()), "")
            title = first_line[:80] if first_line else source_label

        stories.append(Story(
            title=title,
            content=content,
            date=(raw.get("date") or "").strip(),
            author=(raw.get("author") or "").strip(),
            link=(raw.get("link") or link_hint or "").strip(),
            confidence=(raw.get("confidence") or "medium").strip(),
            reasoning=(raw.get("reasoning") or "").strip(),
        ))

    if not stories:
        # All stories were dropped — treat the whole document as non-news so
        # the user gets a clear reason in the preview.
        return [], False, skip_reason or "No story bodies could be extracted from this document."

    return stories, True, skip_reason


# ─────────────────────────────────────────────────────────────────────────────
# COMBINED API
# ─────────────────────────────────────────────────────────────────────────────


def ingest_file(filename: str, raw: bytes, ollama_key: str) -> IngestedSource:
    """Run both stages on an uploaded file. Never raises; failure is reported
    via excluded/skip_reason/extract_error on the returned IngestedSource."""
    source = IngestedSource(source_label=filename, kind="file")

    try:
        text = extract_text(filename, raw)
    except IngestError as e:
        source.excluded = True
        source.extract_error = str(e)
        source.skip_reason = "Failed to extract text from this file."
        return source

    source.char_count = len(text)
    source.truncated = len(text) > MAX_NORMALIZE_CHARS

    if not text.strip():
        source.excluded = True
        source.skip_reason = "The file appears to be empty or contains no readable text."
        return source

    try:
        stories, is_news, skip_reason = normalize(text, filename, ollama_key)
    except IngestError as e:
        source.excluded = True
        source.extract_error = str(e)
        source.skip_reason = "Normalization failed."
        return source

    source.stories = stories
    if not is_news:
        source.excluded = True
        source.skip_reason = skip_reason or "This document does not appear to contain news stories."
    return source


def ingest_url(url: str, ollama_key: str) -> IngestedSource:
    """Fetch a URL, then run both stages. Same failure semantics as ingest_file."""
    source = IngestedSource(source_label=url, kind="url")

    try:
        filename, raw = fetch_url(url)
    except IngestError as e:
        source.excluded = True
        source.extract_error = str(e)
        source.skip_reason = "Failed to fetch this URL."
        return source

    try:
        text = extract_text(filename, raw)
    except IngestError as e:
        source.excluded = True
        source.extract_error = str(e)
        source.skip_reason = "Failed to extract text from the fetched URL."
        return source

    source.char_count = len(text)
    source.truncated = len(text) > MAX_NORMALIZE_CHARS

    if not text.strip():
        source.excluded = True
        source.skip_reason = "The fetched page contains no readable text."
        return source

    try:
        stories, is_news, skip_reason = normalize(
            text, url, ollama_key, link_hint=url,
        )
    except IngestError as e:
        source.excluded = True
        source.extract_error = str(e)
        source.skip_reason = "Normalization failed."
        return source

    source.stories = stories
    if not is_news:
        source.excluded = True
        source.skip_reason = skip_reason or "This page does not appear to contain news stories."
    return source
