"""
store.py
--------
Persistent metadata index for generated beat books.

A single JSON file (``output/library.json``) holds one record per beat book.
It is the source of truth for the sidebar list and status dots; the heavy,
in-flight generation state lives in memory in ``jobs.py`` only while a job runs.

Design notes:
- Single-user / local scale, so a flat JSON list guarded by a process-wide
  ``threading.Lock`` is plenty — no database needed.
- The file is the source of truth. Every mutation does a full
  load-modify-write **inside the lock** and writes atomically (tmp + os.replace)
  so a crash mid-write can't leave a torn index.
- Stems must be unique because output files live at ``output/<stem>.*``.
  Uniqueness is resolved *inside* ``create_book`` (under the lock) so two
  concurrent creates can't reserve the same stem.

Status lifecycle: ``queued`` -> ``generating`` -> ``ready`` | ``failed``.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)
LIBRARY_PATH = OUTPUT_DIR / "library.json"

_LOCK = threading.Lock()

VALID_STATUSES = {"queued", "generating", "ready", "failed"}


def _now() -> float:
    return time.time()


def _load() -> list[dict]:
    """Load the index, tolerating a missing / empty / corrupt file."""
    if not LIBRARY_PATH.exists():
        return []
    try:
        raw = LIBRARY_PATH.read_text(encoding="utf-8")
    except OSError:
        return []
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Never crash the whole app over a corrupt index.
        print("[store] WARNING: library.json is unreadable; ignoring it.", flush=True)
        return []
    return data if isinstance(data, list) else []


def _save(records: list[dict]) -> None:
    """Atomically replace the index file (tmp write + os.replace)."""
    tmp = LIBRARY_PATH.with_name(LIBRARY_PATH.name + ".tmp")
    tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, LIBRARY_PATH)


def _slugify_stem(desired: str) -> str:
    """Normalize a desired stem to lowercase snake_case (defensive — the agent's
    ``_derive_filename`` already produces this shape)."""
    s = re.sub(r"[^a-z0-9]+", "_", (desired or "").lower()).strip("_")
    return s or "beat_book"


def _unique_stem_locked(records: list[dict], desired: str) -> str:
    """Return a stem used by no record and present on no disk file. Lock held."""
    base = _slugify_stem(desired)
    existing = {r.get("stem") for r in records}

    def free(stem: str) -> bool:
        if stem in existing:
            return False
        # Belt-and-suspenders: don't reuse a stem with orphaned files on disk.
        if (OUTPUT_DIR / f"{stem}.json").exists() or (OUTPUT_DIR / f"{stem}.md").exists():
            return False
        return True

    if free(base):
        return base
    i = 2
    while True:
        candidate = f"{base}-{i}"
        if free(candidate):
            return candidate
        i += 1


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def list_books() -> list[dict]:
    """All books, newest first."""
    with _LOCK:
        records = _load()
    return sorted(records, key=lambda r: r.get("created_at", 0), reverse=True)


def get_book(book_id: str) -> Optional[dict]:
    with _LOCK:
        for r in _load():
            if r.get("id") == book_id:
                return dict(r)
    return None


def create_book(
    *,
    title: str,
    desired_stem: str,
    num_stories: int,
    num_topics: int,
    selected_topics: list[str],
) -> dict:
    """Create a ``queued`` book record with a unique stem. Returns the record
    (with its generated ``id`` and resolved ``stem``)."""
    with _LOCK:
        records = _load()
        stem = _unique_stem_locked(records, desired_stem)
        now = _now()
        rec = {
            "id": uuid.uuid4().hex[:8],
            "title": title,
            "stem": stem,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "error": "",
            "num_stories": num_stories,
            "num_topics": num_topics,
            "selected_topics": selected_topics,
            "opened_at": None,
        }
        records.append(rec)
        _save(records)
        return dict(rec)


def update_book(book_id: str, **fields) -> Optional[dict]:
    """Apply ``fields`` to a book, bump ``updated_at``, persist. Returns the
    updated record, or None if no such book."""
    with _LOCK:
        records = _load()
        for r in records:
            if r.get("id") == book_id:
                r.update(fields)
                r["updated_at"] = _now()
                _save(records)
                return dict(r)
    return None


def delete_book(book_id: str) -> Optional[dict]:
    """Remove a book from the index and return the removed record (so the
    caller can clean up its output files — do that *outside* the lock)."""
    with _LOCK:
        records = _load()
        for i, r in enumerate(records):
            if r.get("id") == book_id:
                removed = records.pop(i)
                _save(records)
                return dict(removed)
    return None


def mark_opened(book_id: str) -> Optional[dict]:
    """Record that the reader opened this book (clears the 'unread' dot)."""
    return update_book(book_id, opened_at=_now())


def reconcile_on_startup() -> int:
    """Mark any book left ``queued``/``generating`` (from a prior crash/restart)
    as ``failed`` — its in-memory job state is gone and can't resume. Returns
    the number of records changed."""
    with _LOCK:
        records = _load()
        n = 0
        for r in records:
            if r.get("status") in ("queued", "generating"):
                r["status"] = "failed"
                r["error"] = "interrupted by server restart"
                r["updated_at"] = _now()
                n += 1
        if n:
            _save(records)
    return n


def adopt_orphan_files() -> int:
    """Discover finished ``.md`` beat books in ``output/`` that have no
    library.json record and create ``ready`` entries for them. Ignores
    ``.draft.md`` files (intermediate artifacts). Returns the count of
    newly adopted books."""
    with _LOCK:
        records = _load()
        known_stems = {r.get("stem") for r in records}
        adopted = 0
        for md in sorted(OUTPUT_DIR.glob("*.md")):
            if md.name.endswith(".draft.md"):
                continue
            stem = md.stem
            if stem in known_stems:
                continue
            title = stem.replace("_", " ").replace("-", " ").title()
            stat = md.stat()
            rec = {
                "id": uuid.uuid4().hex[:8],
                "title": title,
                "stem": stem,
                "status": "ready",
                "created_at": stat.st_mtime,
                "updated_at": stat.st_mtime,
                "error": "",
                "num_stories": 0,
                "num_topics": 0,
                "selected_topics": [],
                "opened_at": None,
            }
            records.append(rec)
            known_stems.add(stem)
            adopted += 1
        if adopted:
            _save(records)
    return adopted
