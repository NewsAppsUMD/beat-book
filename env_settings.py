"""
env_settings.py
---------------
Load .env into the environment, and remember where the shell overrode it.

A variable already set in the shell wins, as it always has, but each one
whose value differs from .env is recorded in ENV_OVERRIDES. A model name left
exported from a test (OLLAMA_CHAT_MODEL=glm-5.3:cloud) once silently
overrode the .env choice for a whole book; the startup log and each book's
build record now name such variables.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

ENV_OVERRIDES: List[str] = []


def load_env(path: Path) -> List[str]:
    if not path.exists():
        return ENV_OVERRIDES
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k in os.environ and os.environ[k] != v and k not in ENV_OVERRIDES:
                ENV_OVERRIDES.append(k)
            os.environ.setdefault(k, v)
    return ENV_OVERRIDES


def is_secret(name: str) -> bool:
    return any(w in name.upper() for w in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
