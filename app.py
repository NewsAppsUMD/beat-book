"""
app.py
------
FastAPI web app.

- POST /ingest             → upload files and/or URLs, run multi-format
                              extraction + LLM normalization, return a
                              preview of detected stories.
- POST /process            → run the embedding/clustering pipeline on a
                              confirmed (and optionally edited) story list.
                              Streams SSE progress; ends with a session_id.
- POST /books              → enqueue a beat book for background generation.
- GET  /books              → list saved beat books (sidebar + library).
- WS   /ws/books/{id}      → reconnectable progress stream for a generation.
- GET  /                   → serves the frontend.
"""

import asyncio
import contextlib
import io
import json
import os
import queue
import re
import shutil
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Load .env (a shell variable wins; differences are recorded, see env_settings).
from env_settings import ENV_OVERRIDES, is_secret as _is_secret, load_env
load_env(Path(__file__).parent / ".env")


def _check_existing_books() -> int:
    """Run the damaged-draft check once on ready books built before it
    existed (records with no "warning" field). Returns how many it flagged."""
    from draft_check import check_draft
    flagged = 0
    for rec in store.list_books():
        if rec.get("status") != "ready" or "warning" in rec:
            continue
        md = OUTPUT_DIR / f"{rec.get('stem', '')}.md"
        if not md.exists():
            continue
        verdict = check_draft(md.read_text(encoding="utf-8"), int(rec.get("target_words") or 2000))
        store.update_book(rec["id"], warning=" ".join(verdict["problems"]))
        flagged += 0 if verdict["ok"] else 1
    return flagged


def log_model_settings() -> None:
    """Print the models this server will use, and any .env setting a shell
    variable is overriding (values shown only for non-secrets)."""
    try:
        chat = get_chat_provider()
        print(f"[startup] chat: {type(chat).__name__} (writing {chat.agent_model}, exploring "
              f"{chat.explore_model}, labels {chat.label_model})", flush=True)
    except Exception as e:
        print(f"[startup] chat provider not configured: {e}", flush=True)
    try:
        emb = get_embed_client()
        print(f"[startup] embeddings: {get_embed_provider()} {getattr(emb, 'model_name', '')}", flush=True)
    except Exception as e:
        print(f"[startup] embeddings not configured: {e}", flush=True)
    try:
        from research_agent import MODEL as _research_model
        print(f"[startup] research: anthropic {_research_model}", flush=True)
    except Exception:
        pass
    for name in ENV_OVERRIDES:
        shown = "" if _is_secret(name) else f" ({os.environ.get(name)!r})"
        print(f"[startup] WARNING: {name} is set in the shell{shown} and overrides the "
              f"different value in .env. Unset it in this shell to use .env.", flush=True)

from pipeline import run_pipeline, PipelineResult
from agent import _derive_filename, LENGTH_PRESETS, DEFAULT_TARGET_WORDS
from ingest import ingest_file, ingest_url
from embed_client import (
    DEFAULT_OLLAMA_EMBED_MODEL,
    get_embed_client,
    get_embed_provider,
    list_ollama_models,
)
from chat_provider import ChatProvider, get_chat_provider
from egress import egress_summary
import store
from jobs import BookJob, generation_worker

# ─────────────────────────────────────────────────────────────────────────────

# ── Background generation queue (single worker, one book at a time) ──────────
# Decoupled from any browser tab so generation survives a tab refresh/close.
# The queue + registry live in process memory, so the app MUST run as a single
# Uvicorn process (no --reload / --workers).
job_queue: "asyncio.Queue[str] | None" = None
book_jobs: Dict[str, BookJob] = {}
_worker_task: "asyncio.Task | None" = None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global job_queue, _worker_task
    n = store.reconcile_on_startup()
    if n:
        print(f"[startup] marked {n} interrupted book(s) as failed", flush=True)
    adopted = store.adopt_orphan_files()
    if adopted:
        print(f"[startup] adopted {adopted} pre-existing beat book(s) into library", flush=True)
    log_model_settings()
    flagged = _check_existing_books()
    if flagged:
        print(f"[startup] {flagged} existing beat book(s) look damaged; they are marked in the library", flush=True)
    job_queue = asyncio.Queue()
    _worker_task = asyncio.create_task(generation_worker(job_queue, book_jobs))
    print("[startup] generation worker started (single process — do not use "
          "--reload/--workers)", flush=True)
    try:
        yield
    finally:
        if _worker_task:
            _worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await _worker_task


app = FastAPI(title="Beat Book Builder", lifespan=lifespan)

# Files-in-flight per /ingest request. Serial so a multi-file upload
# doesn't multiply concurrent Claude calls against Anthropic's per-tier
# concurrent-request limit (ingest.py itself runs chunks serially too).
_INGEST_CONCURRENCY = 4

@dataclass
class SessionData:
    pipeline_result: PipelineResult
    embed_model: Optional[str] = None

# In-memory handoff between /process and POST /books: session_id → SessionData.
# Bounded so heavy corpora don't leak — once POST /books runs, the BookJob owns
# the corpus, so eviction here is safe.
_SESSIONS_CAP = 16
sessions: "OrderedDict[str, SessionData]" = OrderedDict()


def _remember_session(session_id: str, result: PipelineResult, embed_model: Optional[str] = None) -> None:
    sessions[session_id] = SessionData(pipeline_result=result, embed_model=embed_model)
    sessions.move_to_end(session_id)
    while len(sessions) > _SESSIONS_CAP:
        sessions.popitem(last=False)


@dataclass
class IngestJob:
    job_id: str
    msg_queue: queue.Queue = field(default_factory=queue.Queue)
    done: bool = False
    result: Optional[dict] = None
    error: str = ""


ingest_jobs: Dict[str, IngestJob] = {}


class StoryIn(BaseModel):
    """Content entry payload accepted by /process. The pipeline only requires
    title + content; the rest are passed through if non-empty."""
    title: str
    content: str
    date: str = ""
    author: str = ""
    organization: str = ""
    language: str = ""
    link: str = ""
    content_type: str = "article"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ProcessRequest(BaseModel):
    stories: List[StoryIn] = Field(default_factory=list)
    embed_model: Optional[str] = None

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)
SANDBOX_ROOT = OUTPUT_DIR / "sandboxes"
SANDBOX_ROOT.mkdir(exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    # Never cached: the page names the script versions to load, so a reload
    # must always get the current one.
    return FileResponse("static/index.html", headers={"Cache-Control": "no-cache"})


def _frontend_version() -> str:
    """A fingerprint of the frontend files. An open tab compares it with the
    one it loaded, so it can tell the user to reload after an update (a
    single-page app keeps running its old scripts until it's reloaded)."""
    import hashlib
    h = hashlib.sha256()
    for f in sorted(Path("static").glob("*.*")):
        if f.suffix in (".js", ".css", ".html"):
            h.update(f.name.encode())
            h.update(str(f.stat().st_mtime_ns).encode())
    return h.hexdigest()[:12]


@app.get("/api/version")
async def frontend_version():
    return JSONResponse({"frontend": _frontend_version()}, headers={"Cache-Control": "no-store"})


@app.get("/api/embed-config")
async def embed_config():
    provider = get_embed_provider()
    result: dict = {"provider": provider}
    if provider == "ollama":
        try:
            result["models"] = list_ollama_models()
        except Exception:
            result["models"] = []
        result["default_model"] = os.environ.get("OLLAMA_EMBED_MODEL", DEFAULT_OLLAMA_EMBED_MODEL)
    else:
        model = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
        result["models"] = [{"name": model}]
        result["default_model"] = model
    return JSONResponse(result)


@app.get("/api/egress-plan")
async def egress_plan_endpoint():
    """What each stage sends off this machine, and where, under the current
    configuration. Shown on the create screen before generation starts."""
    return JSONResponse(egress_summary())


async def _run_ingest_job(
    job: IngestJob,
    buffered_files: List[tuple[str, bytes]],
    url_list: List[str],
    *,
    anthropic_key: str,
    provider: Optional[ChatProvider] = None,
) -> None:
    loop = asyncio.get_event_loop()
    semaphore = asyncio.Semaphore(_INGEST_CONCURRENCY)
    total_sources = len(buffered_files) + len(url_list)

    job.msg_queue.put({"type": "job_started", "total_sources": total_sources})

    async def run_file(name: str, raw: bytes):
        async with semaphore:
            job.msg_queue.put({"type": "source_started", "source_label": name})

            def on_progress(payload: dict):
                job.msg_queue.put({
                    "type": "source_progress",
                    "source_label": name,
                    **payload,
                })

            result = await loop.run_in_executor(
                None,
                lambda: ingest_file(
                    name,
                    raw,
                    anthropic_key,
                    provider=provider,
                    on_progress=on_progress,
                ),
            )
            job.msg_queue.put({
                "type": "source_done",
                "source_label": name,
                "num_entries": len(result.stories),
                "excluded": result.excluded,
            })
            return result

    async def run_url(url: str):
        async with semaphore:
            job.msg_queue.put({"type": "source_started", "source_label": url})

            def on_progress(payload: dict):
                job.msg_queue.put({
                    "type": "source_progress",
                    "source_label": url,
                    **payload,
                })

            result = await loop.run_in_executor(
                None,
                lambda: ingest_url(
                    url,
                    anthropic_key,
                    provider=provider,
                    on_progress=on_progress,
                ),
            )
            job.msg_queue.put({
                "type": "source_done",
                "source_label": url,
                "num_entries": len(result.stories),
                "excluded": result.excluded,
            })
            return result

    tasks = [run_file(name, raw) for name, raw in buffered_files]
    tasks += [run_url(u) for u in url_list]

    try:
        results = await asyncio.gather(*tasks)
    except Exception as e:
        import traceback
        traceback.print_exc()
        job.error = f"Ingestion failed: {type(e).__name__}: {e}"
        job.msg_queue.put({"type": "error", "error": job.error})
        job.done = True
        return

    sources = [r.to_preview_dict() for r in results]
    total_stories = sum(len(r.stories) for r in results)
    job.result = {
        "sources": sources,
        "total_stories": total_stories,
        "total_sources": len(results),
    }
    job.msg_queue.put({"type": "done", **job.result})
    job.done = True


@app.post("/ingest/start")
async def ingest_start(
    files: List[UploadFile] = File(default_factory=list),
    urls: str = Form(""),
):
    """Start ingest in the background and return a job id."""
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    chat_provider_name = os.environ.get("CHAT_PROVIDER", "anthropic").strip().lower()
    if not anthropic_key and chat_provider_name == "anthropic":
        return JSONResponse(
            {"error": "ANTHROPIC_API_KEY not configured."}, status_code=500
        )
    ingest_provider = get_chat_provider(api_key=anthropic_key or None)

    url_list = [u.strip() for u in urls.splitlines() if u.strip()]

    if not files and not url_list:
        return JSONResponse(
            {"error": "No files or URLs provided."}, status_code=400
        )

    buffered_files: List[tuple[str, bytes]] = []
    for f in files:
        raw = await f.read()
        buffered_files.append((f.filename or "upload.bin", raw))

    job_id = str(uuid.uuid4())[:10]
    job = IngestJob(job_id=job_id)
    ingest_jobs[job_id] = job

    asyncio.create_task(
        _run_ingest_job(
            job,
            buffered_files,
            url_list,
            anthropic_key=anthropic_key,
            provider=ingest_provider,
        )
    )

    return JSONResponse({"job_id": job_id})


@app.get("/ingest/stream/{job_id}")
async def ingest_stream(job_id: str):
    job = ingest_jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Invalid ingest job."}, status_code=404)

    async def event_stream():
        while not job.done or not job.msg_queue.empty():
            try:
                msg = job.msg_queue.get_nowait()
                yield f"data: {json.dumps(msg)}\n\n"
            except queue.Empty:
                await asyncio.sleep(0.1)
        if job.error and job.result is None:
            yield f"data: {json.dumps({'type': 'error', 'error': job.error})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/process")
async def process(body: ProcessRequest):
    """Run the embedding + clustering pipeline on a confirmed list of stories.
    Streams SSE progress events, terminates with a session_id the frontend can
    open over WebSocket for the agent conversation."""
    stories = [
        {k: v for k, v in s.model_dump().items() if v or k in ("title", "content")}
        for s in body.stories
    ]
    if not stories:
        return JSONResponse({"error": "No stories provided."}, status_code=400)

    try:
        embed_clt = get_embed_client(model_override=body.embed_model)
    except Exception as e:
        return JSONResponse({"error": f"Embedding provider not configured: {e}"}, status_code=500)
    try:
        chat_pvd = get_chat_provider()
    except Exception as e:
        return JSONResponse({"error": f"Chat provider not configured: {e}"}, status_code=500)

    progress_queue: queue.Queue = queue.Queue()

    def on_progress(step: str, fraction: float, detail: str):
        progress_queue.put({"step": step, "fraction": fraction, "detail": detail})

    async def event_stream():
        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(
            None, run_pipeline, stories, embed_clt, chat_pvd, on_progress
        )

        last_sent = loop.time()
        while not future.done():
            try:
                msg = progress_queue.get_nowait()
                yield f"data: {json.dumps({'type': 'progress', **msg})}\n\n"
                last_sent = loop.time()
            except queue.Empty:
                # A slow embedding/labeling call (e.g. a local CPU-bound
                # Ollama model) can leave the queue empty for a long stretch.
                # Without some bytes flowing, GitHub Codespaces' port-forward
                # proxy (and many reverse proxies) will reset an idle-looking
                # connection, which the browser surfaces as an opaque stream
                # error rather than a clean HTTP status. An SSE comment line
                # is invisible to the frontend's event parser but keeps the
                # connection demonstrably alive.
                if loop.time() - last_sent > 10:
                    yield ": heartbeat\n\n"
                    last_sent = loop.time()
            await asyncio.sleep(0.15)

        while not progress_queue.empty():
            msg = progress_queue.get_nowait()
            yield f"data: {json.dumps({'type': 'progress', **msg})}\n\n"

        try:
            result = future.result()
        except Exception as e:
            import traceback
            traceback.print_exc()
            yield f"data: {json.dumps({'type': 'error', 'error': f'{type(e).__name__}: {e}'})}\n\n"
            return

        session_id = str(uuid.uuid4())[:8]
        _remember_session(session_id, result, embed_model=body.embed_model)

        yield (
            "data: " + json.dumps({
                "type": "done",
                "session_id": session_id,
                "num_stories": len(stories),
                "num_topics": len(result.topics),
                "broad_topics": {k: len(v) for k, v in result.broad_topics.items()},
                "specific_topics": {k: len(v) for k, v in result.specific_topics.items()},
            }) + "\n\n"
        )

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ─────────────────────────────────────────────────────────────────────────────
# BOOKS — library index + background generation
# ─────────────────────────────────────────────────────────────────────────────

def _prettify_stem(stem: str) -> str:
    """Human title from a stem (de-underscore, drop the _beat_book suffix)."""
    s = stem.replace("_beat_book", "").replace("_", " ").replace("-", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return s.title() if s else "Beat Book"


# ─────────────────────────────────────────────────────────────────────────────
# WORD (.docx) EXPORT
# ─────────────────────────────────────────────────────────────────────────────
# Convert the canonical beat-book Markdown to a .docx on demand. Handles the
# elements the writing agent actually emits: headings, paragraphs, bullet/
# numbered lists, blockquotes, and inline bold/italic/links. Markdown tables
# become plain pipe-joined lines (beat books are overwhelmingly prose).

# One regex over the inline span types, matched left-to-right.
_INLINE_MD_RE = re.compile(
    r"(\[([^\]]+)\]\((https?://[^)\s]+)\))"   # 1 link, 2 text, 3 url
    r"|(\*\*(.+?)\*\*)"                        # 4/5 bold  **x**
    r"|(__(.+?)__)"                            # 6/7 bold  __x__
    r"|(\*(.+?)\*)"                            # 8/9 italic *x*
    r"|(?<!\w)(_(.+?)_)(?!\w)"                 # 10/11 italic _x_ (word-boundary)
)


def _docx_add_hyperlink(paragraph, text: str, url: str) -> None:
    """Append a real clickable hyperlink run to a python-docx paragraph."""
    from docx.oxml.shared import OxmlElement, qn
    part = paragraph.part
    r_id = part.relate_to(
        url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    color = OxmlElement("w:color"); color.set(qn("w:val"), "0563C1"); rPr.append(color)
    underline = OxmlElement("w:u"); underline.set(qn("w:val"), "single"); rPr.append(underline)
    run.append(rPr)
    t = OxmlElement("w:t"); t.text = text; run.append(t)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def _docx_add_inline(paragraph, text: str) -> None:
    """Add text to a paragraph, rendering inline **bold**, *italic*, and links."""
    pos = 0
    for m in _INLINE_MD_RE.finditer(text):
        if m.start() > pos:
            paragraph.add_run(text[pos:m.start()])
        if m.group(1):                          # link
            _docx_add_hyperlink(paragraph, m.group(2), m.group(3))
        elif m.group(4) or m.group(6):          # bold
            paragraph.add_run(m.group(5) or m.group(7)).bold = True
        elif m.group(8) or m.group(10):         # italic
            paragraph.add_run(m.group(9) or m.group(11)).italic = True
        pos = m.end()
    if pos < len(text):
        paragraph.add_run(text[pos:])


def _citation_source_key(passage: Dict[str, Any]) -> str:
    return f"{passage.get('article_id')}::{passage.get('passage_offset', 'x')}::{passage.get('passage_length', 'x')}"


def _citation_numbering(entries: List[Dict[str, Any]]) -> tuple[Dict[int, int], "OrderedDict[str, Dict[str, Any]]"]:
    """Mirror static/reader.js's citation pipeline (Pass 1-3): pick each
    sentence's best (first) support, collapse consecutive runs that cite the
    same passage down to one visible marker, and assign sequential numbers.
    Returns (number_by_entry_index, sources_by_key) with sources in first-seen
    order, matching what the web Reader's footnote panel shows."""
    primary_by_idx: List[Optional[Dict[str, Any]]] = []
    for entry in entries:
        content = entry.get("content", "")
        # Older books never cited table rows; their rows have no `kind`.
        if content.lstrip().startswith("|") and entry.get("kind") != "table_row":
            primary_by_idx.append(None)
            continue
        primary = None
        if not entry.get("passthrough") and entry.get("supports"):
            primary = entry["supports"][0]
        primary_by_idx.append(primary)

    show_cite_at: set = set()
    run_key: Optional[str] = None
    run_last_idx = -1

    def flush():
        nonlocal run_key, run_last_idx
        if run_last_idx >= 0:
            show_cite_at.add(run_last_idx)
        run_key = None
        run_last_idx = -1

    for i, primary in enumerate(primary_by_idx):
        if primary:
            key = _citation_source_key(primary)
            if run_key is not None and key != run_key:
                flush()
            run_key = key
            run_last_idx = i
        elif entries[i].get("content", "").strip():
            flush()
    flush()

    number_by_idx: Dict[int, int] = {}
    sources_by_key: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    next_number = 1
    for i in sorted(show_cite_at):
        primary = primary_by_idx[i]
        key = _citation_source_key(primary)
        number_by_idx[i] = next_number
        next_number += 1
        bucket = sources_by_key.setdefault(key, {"primary": primary, "numbers": []})
        bucket["numbers"].append(number_by_idx[i])

    return number_by_idx, sources_by_key


def _docx_add_citation_marker(paragraph, number: int) -> None:
    run = paragraph.add_run(str(number))
    run.font.superscript = True


def _docx_add_sources_section(doc, sources_by_key: Dict[str, Dict[str, Any]]) -> None:
    if not sources_by_key:
        return
    doc.add_heading("Sources", level=1)
    for info in sources_by_key.values():
        primary = info["primary"]
        label = ", ".join(str(n) for n in info["numbers"])
        title = primary.get("article_title") or "Untitled"
        meta_bits = [b for b in (primary.get("article_author"), primary.get("article_date")) if b]
        meta = f" ({', '.join(meta_bits)})" if meta_bits else ""
        p = doc.add_paragraph(style="List Number")
        p.add_run(f"[{label}] ").bold = True
        p.add_run(f"{title}{meta}")
        if primary.get("passage_text"):
            doc.add_paragraph(primary["passage_text"], style="Intense Quote")


def _docx_add_sourcing_section(doc, entries: List[Dict[str, Any]], stats: Dict[str, Any]) -> None:
    """"About the sourcing": the book's counts, why some facts have no
    source, and each unsourced factual claim with its reason. A printed or
    shared copy can't show the reader's hover notes, so it says it here."""
    from claim_evidence import UNSOURCED_REASONS
    claims = [e for e in entries if not e.get("passthrough")]
    if not claims:
        return
    unsourced = [e for e in claims if e.get("provenance") == "unsupported"]
    count = lambda prov: sum(1 for e in claims if e.get("provenance") == prov)
    doc.add_heading("About the sourcing", level=1)
    doc.add_paragraph(
        f"{count('corpus')} of {len(claims)} claims match a passage in the stories this book was "
        f"built from; the superscript numbers point to them. {count('web')} were added from web "
        f"pages and quote those pages. {count('analysis')} are analysis or interpretation, which no "
        f"record could confirm. {count('guidance')} are tips and story ideas. {len(unsourced)} "
        "factual claims have no matching source.")
    if not unsourced:
        return
    doc.add_paragraph(
        "Why some facts have no source: the writing model drafted this book from the stories, and "
        "along the way it sometimes joins details from different stories, rewords them, or adds "
        "background from its own training, which isn't tied to any source. Treat the claims below "
        "as leads to verify, not as reported facts.")
    reasons = stats.get("unsourced_reasons") or {}
    if reasons:
        bits = []
        if reasons.get("outside_stories"):
            bits.append(f"{reasons['outside_stories']} mention details found in none of the stories")
        if reasons.get("in_stories"):
            bits.append(f"{reasons['in_stories']} use details from the stories that no single passage states together")
        if reasons.get("outcome_not_stated"):
            bits.append(f"{reasons['outcome_not_stated']} state an outcome that no matching passage reports")
        if reasons.get("no_details"):
            bits.append(f"{reasons['no_details']} name nothing specific to look up")
        doc.add_paragraph("Of these, " + "; ".join(bits) + ".")
    doc.add_heading("Claims to check", level=2)
    for e in unsourced:
        p = doc.add_paragraph(style="List Bullet")
        _docx_add_inline(p, re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", e.get("content", "")).strip())
        why = UNSOURCED_REASONS.get(e.get("unsourced_reason", ""), "No passage in the stories matches it.")
        missing = e.get("details_not_in_stories") or []
        note = why.replace("your stories", "the stories") + (f" Not in any story: {', '.join(missing)}." if missing else "")
        if e.get("unsourced_reason") == "outcome_not_stated" and e.get("outcome_not_stated"):
            note += " Outcome it states: " + ", ".join(f"“{w}”" for w in e["outcome_not_stated"]) + "."
        run = p.add_run(f" — {note}")
        run.italic = True


def _markdown_to_docx(markdown_text: str, entries: Optional[List[Dict[str, Any]]] = None,
                      stats: Optional[Dict[str, Any]] = None) -> bytes:
    """Render beat-book Markdown to .docx bytes. When `entries` (the
    citation-matcher's per-sentence entry list, from `{stem}.json`) is given,
    cited sentences get a superscript marker and a "Sources" section is
    appended — mirroring the web Reader's footnotes. Without it, renders the
    same as plain Markdown-to-docx always has."""
    from docx import Document

    if entries is None:
        entries = [{"content": line, "passthrough": True, "supports": []}
                   for line in markdown_text.split("\n")]

    number_by_idx, sources_by_key = _citation_numbering(entries)

    doc = Document()
    in_code = False
    current_p = None   # open paragraph accumulating consecutive prose sentences

    for i, entry in enumerate(entries):
        line = entry.get("content", "").rstrip()
        stripped = line.strip()
        is_passthrough = entry.get("passthrough", True)
        marker = number_by_idx.get(i)

        # Cited bullets and table rows are non-passthrough but must still
        # render as bullets and rows, with the marker at the end.
        if not is_passthrough and entry.get("kind", "sentence") == "sentence":
            if current_p is None:
                current_p = doc.add_paragraph()
            else:
                current_p.add_run(" ")
            _docx_add_inline(current_p, stripped)
            if i in number_by_idx:
                _docx_add_citation_marker(current_p, number_by_idx[i])
            continue

        current_p = None   # any passthrough line ends the open prose paragraph

        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            doc.add_paragraph(line, style="No Spacing")
            continue
        if not stripped:
            continue
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):   # horizontal rule
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            level = min(len(heading.group(1)), 4)
            p = doc.add_heading(level=level)
            _docx_add_inline(p, heading.group(2))
            continue

        if stripped.startswith(">"):
            p = doc.add_paragraph(style="Intense Quote")
            _docx_add_inline(p, stripped.lstrip("> ").rstrip())
            continue

        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        if bullet:
            p = doc.add_paragraph(style="List Bullet")
            _docx_add_inline(p, bullet.group(1))
            if marker:
                _docx_add_citation_marker(p, marker)
            continue

        numbered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if numbered:
            p = doc.add_paragraph(style="List Number")
            _docx_add_inline(p, numbered.group(1))
            if marker:
                _docx_add_citation_marker(p, marker)
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c or "-") for c in cells):
                continue   # table separator row
            p = doc.add_paragraph()
            _docx_add_inline(p, "  |  ".join(cells))
            if marker:
                _docx_add_citation_marker(p, marker)
            continue

        p = doc.add_paragraph()
        _docx_add_inline(p, stripped)

    if stats is not None:
        _docx_add_sourcing_section(doc, entries, stats)
    _docx_add_sources_section(doc, sources_by_key)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


VALID_STYLES = ("narrative", "scannable", "briefing")


class CreateBookRequest(BaseModel):
    session_id: str
    selected_topics: List[str] = Field(default_factory=list)
    title: Optional[str] = None
    style: str = "narrative"
    length: str = "standard"        # brief | standard | indepth (see LENGTH_PRESETS)


class PatchBookRequest(BaseModel):
    title: Optional[str] = None
    opened: Optional[bool] = None


@app.get("/books")
async def list_books_endpoint():
    books = store.list_books()
    for b in books:
        j = book_jobs.get(b["id"])
        b["is_active"] = bool(j and not j.done.is_set())
    return JSONResponse(books)


@app.get("/books/{book_id}")
async def get_book_endpoint(book_id: str):
    rec = store.get_book(book_id)
    if not rec:
        return JSONResponse({"error": "Beat book not found."}, status_code=404)
    return JSONResponse(rec)


# Files a book's reader may load, by name. Everything else under output/
# (other books' files, library.json, research sandboxes) stays unserved.
_BOOK_FILES = {
    "markdown": (".md", "text/markdown; charset=utf-8"),
    "draft": (".draft.md", "text/markdown; charset=utf-8"),
    "entries": (".json", "application/json"),
    "sources": ("_sources.json", "application/json"),
    "manifest": (".manifest.json", "application/json"),
}
BOOK_FILE_SUFFIXES = tuple(v[0] for v in _BOOK_FILES.values())


@app.get("/books/{book_id}/files/{kind}")
async def get_book_file(book_id: str, kind: str):
    """Serve one of a book's output files by book id. Replaces the old static
    mount of the whole output/ directory, which let any client that could
    reach the server list-guess stems and read full source text, library.json,
    and the research agent's sandbox. The app still has no authentication, so
    keep it bound to 127.0.0.1."""
    rec = store.get_book(book_id)
    if not rec:
        return JSONResponse({"error": "Beat book not found."}, status_code=404)
    spec = _BOOK_FILES.get(kind)
    if spec is None:
        return JSONResponse({"error": f"Unknown file '{kind}'."}, status_code=404)
    path = OUTPUT_DIR / f"{rec['stem']}{spec[0]}"
    if not path.is_file():
        return JSONResponse({"error": "Not available for this beat book."}, status_code=404)
    return FileResponse(path, media_type=spec[1], headers={"Cache-Control": "no-store"})


@app.get("/books/{book_id}/docx")
async def download_book_docx(book_id: str):
    """Convert the canonical beat-book Markdown to a Word document on demand."""
    rec = store.get_book(book_id)
    if not rec:
        return JSONResponse({"error": "Beat book not found."}, status_code=404)
    stem = rec.get("stem", "")
    md_path = OUTPUT_DIR / f"{stem}.md"
    if not md_path.exists():
        return JSONResponse(
            {"error": "This beat book isn't ready to download yet."}, status_code=409)
    entries = None
    stats = None
    citations_path = OUTPUT_DIR / f"{stem}.json"
    if citations_path.exists():
        try:
            payload = json.loads(citations_path.read_text(encoding="utf-8"))
            entries, stats = payload.get("entries"), payload.get("stats") or {}
        except Exception:
            entries, stats = None, None
    try:
        data = _markdown_to_docx(md_path.read_text(encoding="utf-8"), entries, stats)
    except Exception as e:
        return JSONResponse(
            {"error": f"Could not build the Word document: {type(e).__name__}: {e}"},
            status_code=500)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{stem}.docx"'},
    )


@app.post("/books")
async def create_book_endpoint(body: CreateBookRequest):
    """Enqueue a beat book for background generation. Returns immediately with a
    book_id; progress streams over WS /ws/books/{book_id}."""
    sess = sessions.get(body.session_id)
    if sess is None:
        return JSONResponse(
            {"error": "Invalid or expired session. Please re-run the pipeline."},
            status_code=404,
        )
    pr = sess.pipeline_result
    style = body.style if body.style in VALID_STYLES else "narrative"
    target_words = LENGTH_PRESETS.get(body.length, DEFAULT_TARGET_WORDS)

    valid = set(pr.topics.keys())
    selected = [t for t in body.selected_topics if t in valid]
    if not selected:
        selected = list(pr.topics.keys())

    desired = _derive_filename(pr)
    if desired.endswith(".md"):
        desired = desired[:-3]
    provisional_title = (body.title or "").strip() or _prettify_stem(desired)

    rec = store.create_book(
        title=provisional_title,
        desired_stem=desired,
        num_stories=len(pr.stories),
        num_topics=len(selected),
        selected_topics=selected,
        style=style,
    )

    job = BookJob(book_id=rec["id"], pipeline_result=pr, selected_topics=selected, style=style, target_words=target_words, embed_model=sess.embed_model)
    book_jobs[rec["id"]] = job
    if job_queue is not None:
        await job_queue.put(rec["id"])

    return JSONResponse({"book_id": rec["id"], "stem": rec["stem"], "status": "queued"})


@app.patch("/books/{book_id}")
async def patch_book_endpoint(book_id: str, body: PatchBookRequest):
    rec = store.get_book(book_id)
    if not rec:
        return JSONResponse({"error": "Beat book not found."}, status_code=404)
    if body.title is not None:
        store.update_book(book_id, title=body.title.strip() or rec["title"])
    if body.opened:
        store.mark_opened(book_id)
    return JSONResponse(store.get_book(book_id))


@app.delete("/books/{book_id}")
async def delete_book_endpoint(book_id: str):
    rec = store.get_book(book_id)
    if not rec:
        return JSONResponse({"error": "Beat book not found."}, status_code=404)
    job = book_jobs.get(book_id)
    if rec.get("status") == "generating" and job and not job.done.is_set():
        return JSONResponse(
            {"error": "Cannot delete a beat book while it is generating."},
            status_code=409,
        )
    removed = store.delete_book(book_id)
    if removed:
        stem = removed.get("stem", "")
        for suffix in BOOK_FILE_SUFFIXES:
            try:
                (OUTPUT_DIR / f"{stem}{suffix}").unlink(missing_ok=True)
            except OSError:
                pass
        shutil.rmtree(SANDBOX_ROOT / book_id, ignore_errors=True)
    book_jobs.pop(book_id, None)
    return JSONResponse({"ok": True})


@app.websocket("/ws/books/{book_id}")
async def book_ws(ws: WebSocket, book_id: str):
    """Reconnectable progress for a generating book. On connect: status snapshot
    + replayed buffer, then live events. Falls back to the durable store record
    when no live job exists (e.g. refresh after the job finished, or a restart)."""
    await ws.accept()
    job = book_jobs.get(book_id)

    if job is not None:
        sub: asyncio.Queue = asyncio.Queue(maxsize=1000)
        # Snapshot the buffer AND register under the same lock so the live
        # stream picks up exactly where the replay ends — no gap, no dupe.
        async with job.lock:
            snapshot = list(job.events)
            job.subscribers.add(sub)
        try:
            await ws.send_json({"type": "status", "status": job.status})
            for ev in snapshot:
                await ws.send_json(ev)
            while not (job.done.is_set() and sub.empty()):
                try:
                    ev = await asyncio.wait_for(sub.get(), timeout=1.0)
                    await ws.send_json(ev)
                except asyncio.TimeoutError:
                    continue
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            async with job.lock:
                job.subscribers.discard(sub)
        with contextlib.suppress(Exception):
            await ws.close()
        return

    # No live job — synthesize a terminal message from the durable record.
    rec = store.get_book(book_id)
    if not rec:
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "error", "text": "Unknown beat book."})
            await ws.close()
        return
    with contextlib.suppress(Exception):
        await ws.send_json({"type": "status", "status": rec["status"]})
        if rec["status"] == "ready":
            stem = rec["stem"]
            filename = f"{stem}.md"
            await ws.send_json({
                "type": "beat_book",
                "filename": filename,
                "markdown_path": f"/books/{quote(book_id)}/files/markdown",
                "stem": stem,
            })
        elif rec["status"] == "failed":
            await ws.send_json({"type": "error", "text": rec.get("error") or "Generation failed."})
        await ws.close()


# ─────────────────────────────────────────────────────────────────────────────
# STATIC FILES (must be last so it doesn't shadow routes)
# ─────────────────────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")
