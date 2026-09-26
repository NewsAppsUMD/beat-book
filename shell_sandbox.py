"""
shell_sandbox.py
----------------
Run the research agent's shell commands so they cannot write outside the
book's sandbox folder.

The research agent reads untrusted web pages and can run shell commands, so
its commands run under an operating-system sandbox:

- macOS: ``sandbox-exec`` with a profile that denies every file write except
  inside the sandbox folder (plus /dev/null, /dev/tty and /dev/fd).
- Linux: ``bwrap`` (bubblewrap) with the whole filesystem mounted read-only,
  the sandbox folder bound read-write, and a throwaway /tmp.

Reads and network access stay allowed, so scrapers still work. The rule is
checked once per process with a probe that writes inside and outside a test
folder. If no sandbox tool exists, or the probe shows writes escaping, the
shell is unavailable and the research agent runs without it: this fails
closed, never open.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

_PROBE_TIMEOUT = 10
_cached: Optional[Dict[str, Optional[str]]] = None


def _sbpl_string(path: str) -> str:
    """Quote a path as an SBPL string literal."""
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def macos_profile(sandbox_dir: Path) -> str:
    root = str(sandbox_dir.resolve())
    return (
        "(version 1)"
        "(allow default)"
        "(deny file-write*)"
        f"(allow file-write* (subpath {_sbpl_string(root)})"
        ' (literal "/dev/null") (literal "/dev/tty") (literal "/dev/zero")'
        ' (regex #"^/dev/fd/"))'
    )


def _argv_for(kind: str, sandbox_dir: Path, command: str) -> List[str]:
    root = str(sandbox_dir.resolve())
    if kind == "sandbox-exec":
        return ["sandbox-exec", "-p", macos_profile(sandbox_dir), "/bin/bash", "-c", command]
    if kind == "bwrap":
        return [
            "bwrap",
            "--ro-bind", "/", "/",
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
            "--bind", root, root,
            "--chdir", root,
            "--unshare-pid",
            "--die-with-parent",
            "--new-session",
            "/bin/bash", "-c", command,
        ]
    raise ValueError(f"unknown sandbox kind {kind!r}")


def _candidates() -> List[str]:
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return ["sandbox-exec"]
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return ["bwrap"]
    return []


def _probe(kind: str) -> Optional[str]:
    """Return None if `kind` confines writes correctly, else a reason."""
    base = Path(tempfile.mkdtemp(prefix="beatbook-sandbox-probe-")).resolve()
    inside = base / "inside"
    inside.mkdir()
    outside = base / "outside.txt"
    cmd = f"echo ok > inside.txt; echo escaped > {outside}"
    try:
        subprocess.run(_argv_for(kind, inside, cmd), cwd=str(inside), capture_output=True,
                       timeout=_PROBE_TIMEOUT, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
        if outside.exists():
            return "a write outside the sandbox succeeded"
        if not (inside / "inside.txt").exists():
            return "a write inside the sandbox failed"
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def detect(force: bool = False) -> Dict[str, Optional[str]]:
    """Which sandbox confines the shell here. Returns {"kind": str or None,
    "reason": why none is usable}. Cached for the process."""
    global _cached
    if _cached is not None and not force:
        return _cached
    candidates = _candidates()
    if not candidates:
        need = "sandbox-exec" if sys.platform == "darwin" else "bubblewrap (bwrap)"
        _cached = {"kind": None, "reason": f"{need} is not installed"}
        return _cached
    reasons = []
    for kind in candidates:
        why = _probe(kind)
        if why is None:
            _cached = {"kind": kind, "reason": None}
            return _cached
        reasons.append(f"{kind}: {why}")
    _cached = {"kind": None, "reason": "; ".join(reasons)}
    return _cached


def sandboxed_argv(sandbox_dir: Path, command: str) -> Optional[List[str]]:
    """The argv that runs `command` confined to `sandbox_dir`, or None when no
    working sandbox is available (the caller must then refuse to run it)."""
    kind = detect()["kind"]
    if kind is None:
        return None
    return _argv_for(kind, sandbox_dir, command)
