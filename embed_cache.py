"""
embed_cache.py
--------------
A disk cache in front of any embedding client, so building another book
from the same stories doesn't embed them again.

Each vector is stored under a hash of the model name and the exact text,
so a different model or any change to the text is a miss, never a stale
hit. The cache covers every embedding call: story clustering, the source
passages, the book's sentences and the highlight windows. Vectors are kept
as float32, the precision every caller already uses, so a cached run
matches a fresh one exactly.

The cache holds vectors, not text, in `.cache/embeddings/` (one SQLite file
per model). `make clean` removes it; EMBED_CACHE=off turns it off.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

CACHE_DIR = Path(".cache") / "embeddings"
_LOOKUP_CHUNK = 500     # keys per SELECT, under SQLite's variable limit


def cache_enabled() -> bool:
    return os.environ.get("EMBED_CACHE", "on").strip().lower() not in ("0", "off", "false", "no")


class CachingEmbedClient:
    """Wraps an embed client: same attributes and embed(), with a cache."""

    def __init__(self, inner: Any, cache_dir: Path | None = None):
        self._inner = inner
        self.model_name = inner.model_name
        self.dimensions = inner.dimensions
        self.batch_size = getattr(inner, "batch_size", 64)
        self.max_parallel = getattr(inner, "max_parallel", 1)
        self.hits = 0
        self.misses = 0
        directory = Path(cache_dir or CACHE_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", self.model_name) or "model"
        self.path = directory / f"{slug}.sqlite"
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, dim INTEGER, data BLOB)")
        self._db.commit()

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model_name}\0{text}".encode("utf-8")).hexdigest()

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        keys = [self._key(t) for t in texts]
        found: Dict[str, np.ndarray] = {}
        with self._lock:
            unique = list(dict.fromkeys(keys))
            for i in range(0, len(unique), _LOOKUP_CHUNK):
                chunk = unique[i:i + _LOOKUP_CHUNK]
                rows = self._db.execute(
                    f"SELECT key, dim, data FROM vectors WHERE key IN ({','.join('?' * len(chunk))})", chunk)
                for key, dim, data in rows:
                    if dim == self.dimensions:
                        found[key] = np.frombuffer(data, dtype=np.float32)
        todo = [i for i, k in enumerate(keys) if k not in found]
        # Embed each distinct missing text once.
        first: Dict[str, int] = {}
        for i in todo:
            first.setdefault(keys[i], i)
        if first:
            fresh = self._inner.embed([texts[i] for i in first.values()])
            rows = []
            for key, vec in zip(first, fresh):
                arr = np.asarray(vec, dtype=np.float32)
                found[key] = arr
                rows.append((key, int(arr.shape[0]), arr.tobytes()))
            with self._lock:
                self._db.executemany("INSERT OR REPLACE INTO vectors (key, dim, data) VALUES (?, ?, ?)", rows)
                self._db.commit()
        self.misses += len(todo)
        self.hits += len(keys) - len(todo)
        return [found[k].tolist() for k in keys]

    def stats(self) -> Dict[str, Any]:
        return {"hits": self.hits, "misses": self.misses, "file": str(self.path)}
