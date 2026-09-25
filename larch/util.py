"""Small shared helpers (hashing, paths, subprocess with timeouts)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def sha256(*parts: str | bytes) -> str:
    h = hashlib.sha256()
    for p in parts:
        if isinstance(p, str):
            p = p.encode()
        h.update(p)
        h.update(b"\x00")
    return h.hexdigest()


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def cache_root() -> Path:
    base = os.environ.get("LARCH_CACHE_DIR")
    if base:
        return Path(base).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "larch"


def slug(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_")
    return (s or "x")[:maxlen]


@dataclass
class ProcResult:
    returncode: int | None  # None => timed out
    stdout: str
    stderr: str
    elapsed: float

    @property
    def timed_out(self) -> bool:
        return self.returncode is None


def run_proc(
    cmd: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    stdin: str | None = None,
) -> ProcResult:
    """Run a process, capturing output; never raises on timeout (returncode=None)."""
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return ProcResult(p.returncode, p.stdout, p.stderr, time.monotonic() - t0)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return ProcResult(None, out, err, time.monotonic() - t0)


def extract_code_block(text: str, lang: str = "lean") -> str | None:
    """Return the last fenced code block of the given language (or any fence)."""
    pattern = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\n(.*?)```", re.DOTALL)
    blocks = [(m.group(1).lower(), m.group(2)) for m in pattern.finditer(text)]
    if not blocks:
        return None
    tagged = [b for tag, b in blocks if tag == lang]
    chosen = tagged[-1] if tagged else blocks[-1][1]
    return chosen.strip("\n")


def indent(text: str, n: int = 2) -> str:
    pad = " " * n
    return "\n".join(pad + line if line.strip() else line for line in text.splitlines())


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit * 2 // 3]
    tail = text[-limit // 3 :]
    return f"{head}\n… [{len(text) - limit} characters truncated] …\n{tail}"
