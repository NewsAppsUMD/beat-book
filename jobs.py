"""
jobs.py
-------
Server-side beat-book generation, decoupled from any browser tab.

A single background worker pulls one book at a time off an asyncio queue and
runs the full generation pipeline (agent draft ∥ research, then citations),
driven by an ``emit(event)`` callback that buffers and broadcasts progress —
so generation is decoupled from any one browser tab.

``emit`` does two things under one lock: appends the event to a per-job replay
buffer AND fans it out to every connected subscriber queue. A tab that connects
(or reconnects after a refresh) gets the buffered events replayed, then the live
stream — with no gap or duplicate at the boundary because the WS handler's
"snapshot the buffer + register my queue" happens under the same lock.

CRITICAL: ``emit`` is async and must only be awaited on the event loop. Progress
that originates in executor threads (the citation matcher) is funnelled through a
plain ``queue.Queue`` that the loop drains and re-emits — never call ``emit``
from a worker thread.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue as _queue
import re
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional
from urllib.parse import quote

import store
from agent import run_agent
from research_agent import run_research_agent
from citation_matcher import (
    embed_source_stories,
    markdown_to_beatbook_entries,
    build_sources_file,
)

OUTPUT_DIR = Path("output")
SANDBOX_ROOT = OUTPUT_DIR / "sandboxes"
OUTPUT_DIR.mkdir(exist_ok=True)
SANDBOX_ROOT.mkdir(exist_ok=True)

# Heartbeats are keepalives — broadcast them but never buffer (meaningless and
# noisy on replay). The buffer is capped so a long run can't grow unbounded; the
# terminal event is also kept in ``final_event`` so a late joiner always learns
# the outcome even if buffer trimming ever reached it.
_NO_BUFFER_TYPES = {"heartbeat"}
_MAX_BUFFER = 4000


@dataclass
class BookJob:
    book_id: str
    pipeline_result: Any = None              # PipelineResult; nulled after run
    selected_topics: List[str] = field(default_factory=list)
    status: str = "queued"
    events: List[dict] = field(default_factory=list)
    subscribers: set = field(default_factory=set)        # set[asyncio.Queue]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    final_event: Optional[dict] = None


def make_emit(job: BookJob) -> Callable[[dict], Awaitable[None]]:
    """Build the ``emit(event)`` coroutine bound to one job: buffer + broadcast
    under the job lock."""

    async def emit(event: dict) -> None:
        async with job.lock:
            etype = event.get("type")
            if etype not in _NO_BUFFER_TYPES:
                job.events.append(event)
                if len(job.events) > _MAX_BUFFER:
                    job.events = job.events[-_MAX_BUFFER:]
            if etype in ("beat_book", "error"):
                job.final_event = event
            dead = []
            for q in job.subscribers:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    dead.append(q)  # slow/stalled client — drop; it can reconnect
            for q in dead:
                job.subscribers.discard(q)

    return emit


def _title_from_markdown(md: str) -> Optional[str]:
    """The real document title is the first H1 of the generated markdown."""
    for line in md.splitlines():
        m = re.match(r"^#\s+(.+?)\s*$", line)
        if m:
            return m.group(1).strip()
    return None


async def run_generation(
    book_id: str,
    pipeline_result: Any,
    selected_topics: List[str],
    emit: Callable[[dict], Awaitable[None]],
    anthropic_key: str,
    openai_key: str,
) -> None:
    """Run one beat book end to end. Never raises — terminal state is recorded
    in the store and emitted as a ``beat_book`` or ``error`` event."""
    book = store.get_book(book_id)
    # Stem is reserved+frozen at create_book time; force all output paths onto it
    # so two corpora with the same top topic can never overwrite each other.
    stem = book["stem"] if book else f"beat_book_{book_id}"
    filename = f"{stem}.md"

    if not anthropic_key:
        store.update_book(book_id, status="failed", error="ANTHROPIC_API_KEY not configured.")
        await emit({"type": "error", "text": "ANTHROPIC_API_KEY not configured."})
        await emit({"type": "status", "status": "failed"})
        return

    store.update_book(book_id, status="generating")
    await emit({"type": "status", "status": "generating"})

    loop = asyncio.get_event_loop()
    sandbox_dir = SANDBOX_ROOT / book_id
    sandbox_dir.mkdir(parents=True, exist_ok=True)

    # ── Agent callbacks (ported from the old agent_ws; ws.send_json -> emit) ──
    async def on_message(text: str):
        await emit({"type": "message", "text": text})

    async def on_heartbeat():
        await emit({"type": "heartbeat"})

    async def on_tool_status(tool_name: str, tool_desc: str, detail: str):
        await emit({"type": "tool_status", "tool_name": tool_name, "tool": tool_desc, "detail": detail})

    async def on_agent_progress(pct: float, label: str):
        await emit({"type": "agent_progress", "pct": pct, "label": label})

    _research_task: asyncio.Task | None = None
    _exploration_context: str | None = None
    book_written = False

    async def on_exploration_done(context_doc: str):
        """Start the Opus research agent as soon as exploration finishes, in
        parallel with the draft write."""
        nonlocal _research_task, _exploration_context
        _exploration_context = context_doc
        await emit({"type": "research_started", "filename": filename})

        async def on_research_progress(stage, detail):
            await emit({"type": "research_progress", "stage": stage, "detail": detail})

        async def on_research_tool_status(tool_name, desc, detail):
            await emit({"type": "research_tool_status", "tool_name": tool_name, "tool": desc, "detail": detail})

        async def on_research_text(text):
            await emit({"type": "research_message", "text": text})

        async def _run():
            try:
                return await run_research_agent(
                    sandbox_dir=sandbox_dir,
                    markdown_filename=filename,
                    anthropic_api_key=anthropic_key,
                    on_progress=on_research_progress,
                    on_tool_status=on_research_tool_status,
                    on_text=on_research_text,
                    initial_content=context_doc,
                )
            except Exception:
                traceback.print_exc()
                return None  # fall back to the draft

        _research_task = asyncio.create_task(_run())

    async def on_beat_book(_agent_filename: str, markdown: str):
        """Merge draft + research, write outputs (onto the reserved stem), run
        the citation matcher, emit the final beat_book event."""
        nonlocal book_written

        # 1. Persist the raw draft.
        (OUTPUT_DIR / f"{stem}.draft.md").write_text(markdown, encoding="utf-8")

        # 2. Await research (parallel path), or run it now (sequential fallback).
        research_result: str | None = None
        if _research_task is not None:
            (sandbox_dir / filename).write_text(markdown, encoding="utf-8")
            research_result = await _research_task
        else:
            await emit({"type": "research_started", "filename": filename})
            (sandbox_dir / filename).write_text(markdown, encoding="utf-8")

            async def on_research_progress(stage, detail):
                await emit({"type": "research_progress", "stage": stage, "detail": detail})

            async def on_research_tool_status(tool_name, desc, detail):
                await emit({"type": "research_tool_status", "tool_name": tool_name, "tool": desc, "detail": detail})

            async def on_research_text(text):
                await emit({"type": "research_message", "text": text})

            try:
                research_result = await run_research_agent(
                    sandbox_dir=sandbox_dir,
                    markdown_filename=filename,
                    anthropic_api_key=anthropic_key,
                    on_progress=on_research_progress,
                    on_tool_status=on_research_tool_status,
                    on_text=on_research_text,
                )
            except Exception as e:
                traceback.print_exc()
                await emit({"type": "error",
                            "text": f"Research agent failed ({type(e).__name__}: {e}). Using draft."})

        # 3. Merge: draft is the base; append only what research added beyond the
        #    exploration context it started from (same logic as the old handler).
        if research_result and _exploration_context:
            stripped = research_result
            if stripped.startswith(_exploration_context):
                stripped = stripped[len(_exploration_context):]
            elif _exploration_context.strip() in stripped:
                idx = stripped.index(_exploration_context.strip())
                stripped = stripped[:idx] + stripped[idx + len(_exploration_context.strip()):]
            stripped = stripped.strip()
            revised_markdown = markdown + "\n\n" + stripped if stripped else markdown
        elif research_result:
            revised_markdown = research_result
        else:
            revised_markdown = markdown

        await emit({"type": "research_complete"})

        # 4. Canonical markdown.
        (OUTPUT_DIR / filename).write_text(revised_markdown, encoding="utf-8")
        await emit({"type": "beat_book_markdown_saved", "filename": filename})

        final_title = _title_from_markdown(revised_markdown) or (book["title"] if book else stem)

        def _finish_ready():
            """Mark ready + emit the terminal beat_book event (shared by the
            success and citation-skipped/failed-but-markdown-exists paths)."""
            store.update_book(book_id, status="ready", title=final_title)

        async def _emit_beat_book():
            await emit({
                "type": "beat_book",
                "filename": filename,
                "markdown_path": f"/output/{quote(filename)}",
                "stem": stem,
            })

        # 5. Citation matching (OpenAI embeddings). If unavailable, the book is
        #    still usable as raw markdown — mark ready and deliver it.
        if not openai_key:
            await emit({"type": "error",
                        "text": "OPENAI_API_KEY not configured; skipping citation matching."})
            _finish_ready()
            await _emit_beat_book()
            book_written = True
            return

        stories = pipeline_result.stories
        cpq: _queue.Queue = _queue.Queue()

        def on_matcher_progress(stage, fraction, detail):
            cpq.put({"stage": stage, "fraction": fraction, "detail": detail})

        def run_matcher():
            source_embeddings = embed_source_stories(stories, openai_key, on_matcher_progress)
            entries = markdown_to_beatbook_entries(revised_markdown, source_embeddings, openai_key, on_matcher_progress)
            sources = build_sources_file(stories, source_embeddings)
            return entries, sources

        await emit({"type": "citation_progress", "stage": "starting",
                    "fraction": 0.0, "detail": "Embedding source passages…"})

        future = loop.run_in_executor(None, run_matcher)
        while not future.done():
            try:
                msg = cpq.get_nowait()
                await emit({"type": "citation_progress", **msg})
            except _queue.Empty:
                await asyncio.sleep(0.15)
        while not cpq.empty():
            msg = cpq.get_nowait()
            await emit({"type": "citation_progress", **msg})

        try:
            entries, sources = future.result()
        except Exception as e:
            await emit({"type": "error",
                        "text": f"Citation matching failed: {e}. The raw Markdown is still available."})
            _finish_ready()
            await _emit_beat_book()
            book_written = True
            return

        (OUTPUT_DIR / f"{stem}.json").write_text(
            json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
        (OUTPUT_DIR / f"{stem}_sources.json").write_text(
            json.dumps(sources, indent=2, ensure_ascii=False), encoding="utf-8")

        _finish_ready()
        await _emit_beat_book()
        book_written = True

    # ── Run the agent loop ───────────────────────────────────────────────────
    try:
        await run_agent(
            pipeline_result=pipeline_result,
            anthropic_key=anthropic_key,
            on_message=on_message,
            on_beat_book=on_beat_book,
            on_tool_status=on_tool_status,
            on_heartbeat=on_heartbeat,
            on_agent_progress=on_agent_progress,
            on_exploration_done=on_exploration_done,
            selected_topics=selected_topics,
        )
    except Exception as e:
        traceback.print_exc()
        store.update_book(book_id, status="failed", error=f"{type(e).__name__}: {e}")
        await emit({"type": "error", "text": f"Agent error ({type(e).__name__}): {e}"})
        await emit({"type": "status", "status": "failed"})
        return

    # The agent can exit without producing a beat book (e.g. persistent rate
    # limits). Treat that as a failure so the dot doesn't hang on "generating".
    if not book_written:
        store.update_book(book_id, status="failed",
                          error="agent finished without producing a beat book")
        await emit({"type": "error",
                    "text": "The agent finished without producing a beat book. Please try again."})
        await emit({"type": "status", "status": "failed"})
    else:
        await emit({"type": "status", "status": "ready"})


async def generation_worker(job_queue: asyncio.Queue, book_jobs: dict) -> None:
    """Single consumer: process one book at a time, forever. One worker makes
    'one generation at a time' structural — no extra mutex needed."""
    print("[jobs] generation worker running", flush=True)
    while True:
        book_id = await job_queue.get()
        try:
            job = book_jobs.get(book_id)
            if job is None:
                continue
            emit = make_emit(job)
            job.status = "generating"
            anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
            openai_key = os.environ.get("OPENAI_API_KEY", "")
            try:
                await run_generation(
                    book_id, job.pipeline_result, job.selected_topics,
                    emit, anthropic_key, openai_key,
                )
            except Exception:
                # run_generation already handles its own errors; this is a backstop
                # so one bad job can never kill the worker.
                traceback.print_exc()
                store.update_book(book_id, status="failed", error="internal error")
                try:
                    await emit({"type": "error", "text": "Internal error during generation."})
                except Exception:
                    pass
            finally:
                rec = store.get_book(book_id)
                job.status = rec.get("status", "failed") if rec else "failed"
                job.pipeline_result = None  # free the corpus; outputs are on disk now
                job.done.set()
        finally:
            job_queue.task_done()
