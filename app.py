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

# Load .env
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from pipeline import run_pipeline, PipelineResult
from agent import _derive_filename
from ingest import ingest_file, ingest_url
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

# In-memory handoff between /process and POST /books: session_id → PipelineResult.
# Bounded so heavy corpora don't leak — once POST /books runs, the BookJob owns
# the corpus, so eviction here is safe.
_SESSIONS_CAP = 16
sessions: "OrderedDict[str, PipelineResult]" = OrderedDict()


def _remember_session(session_id: str, result: PipelineResult) -> None:
    sessions[session_id] = result
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
    link: str = ""
    content_type: str = "article"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ProcessRequest(BaseModel):
    stories: List[StoryIn] = Field(default_factory=list)

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)
SANDBOX_ROOT = OUTPUT_DIR / "sandboxes"
SANDBOX_ROOT.mkdir(exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse("static/index.html")


async def _run_ingest_job(
    job: IngestJob,
    buffered_files: List[tuple[str, bytes]],
    url_list: List[str],
    *,
    anthropic_key: str,
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
    if not anthropic_key:
        return JSONResponse(
            {"error": "ANTHROPIC_API_KEY not configured."}, status_code=500
        )

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

    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if not openai_key:
        return JSONResponse({"error": "OPENAI_API_KEY not configured (used for embeddings)."}, status_code=500)
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_key:
        return JSONResponse({"error": "ANTHROPIC_API_KEY not configured (used for cluster labeling)."}, status_code=500)

    progress_queue: queue.Queue = queue.Queue()

    def on_progress(step: str, fraction: float, detail: str):
        progress_queue.put({"step": step, "fraction": fraction, "detail": detail})

    async def event_stream():
        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(
            None, run_pipeline, stories, openai_key, anthropic_key, on_progress
        )

        while not future.done():
            try:
                msg = progress_queue.get_nowait()
                yield f"data: {json.dumps({'type': 'progress', **msg})}\n\n"
            except queue.Empty:
                pass
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
        _remember_session(session_id, result)

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


VALID_STYLES = ("narrative", "scannable", "briefing")


class CreateBookRequest(BaseModel):
    session_id: str
    selected_topics: List[str] = Field(default_factory=list)
    title: Optional[str] = None
    style: str = "narrative"


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


@app.post("/books")
async def create_book_endpoint(body: CreateBookRequest):
    """Enqueue a beat book for background generation. Returns immediately with a
    book_id; progress streams over WS /ws/books/{book_id}."""
    pr = sessions.get(body.session_id)
    if pr is None:
        return JSONResponse(
            {"error": "Invalid or expired session. Please re-run the pipeline."},
            status_code=404,
        )
    style = body.style if body.style in VALID_STYLES else "narrative"

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

    job = BookJob(book_id=rec["id"], pipeline_result=pr, selected_topics=selected, style=style)
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
        for suffix in (".draft.md", ".md", ".json", "_sources.json"):
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
                "markdown_path": f"/output/{quote(filename)}",
                "stem": stem,
            })
        elif rec["status"] == "failed":
            await ws.send_json({"type": "error", "text": rec.get("error") or "Generation failed."})
        await ws.close()


# ─────────────────────────────────────────────────────────────────────────────
# STATIC FILES (must be last so it doesn't shadow routes)
# ─────────────────────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/output", StaticFiles(directory="output"), name="output")
