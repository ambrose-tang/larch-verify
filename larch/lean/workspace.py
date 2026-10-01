"""A per-run Lean workspace: writes generated modules, compiles them, checks files."""
from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .diagnostics import Message, errors, parse_messages
from .toolchain import LeanToolchain


@dataclass
class CompileResult:
    ok: bool
    messages: list[Message] = field(default_factory=list)
    elapsed: float = 0.0
    timed_out: bool = False
    raw: str = ""

    @property
    def errors(self) -> list[Message]:
        return errors(self.messages)

    def error_text(self, line_offset: int = 0, limit: int = 6000) -> str:
        if self.timed_out:
            return "Lean timed out (the proof or definition takes too long to check)."
        errs = self.errors or self.messages
        text = "\n\n".join(m.render(line_offset) for m in errs)
        if not text.strip():
            text = self.raw.strip()
        return text[:limit]


class LeanWorkspace:
    MODEL = "LarchModel"
    HARNESS = "LarchHarness"

    def __init__(self, root: Path, toolchain: LeanToolchain):
        self.root = Path(root).resolve()
        self.tc = toolchain
        self.build = root / "build"
        self.root.mkdir(parents=True, exist_ok=True)
        self.build.mkdir(parents=True, exist_ok=True)
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    @property
    def lean_path(self) -> list[Path]:
        return [self.build]

    def path(self, module: str) -> Path:
        return self.root / f"{module}.lean"

    def write(self, module: str, text: str) -> Path:
        p = self.path(module)
        p.write_text(text)
        return p

    def compile_module(self, module: str, text: str, timeout: float = 180.0) -> CompileResult:
        """Write and compile a module to .olean so later files can import it."""
        p = self.write(module, text)
        olean = self.build / f"{module}.olean"
        if olean.exists():
            olean.unlink()
        r = self.tc.run_lean(p, cwd=self.root, lean_path=self.lean_path, olean=olean, timeout=timeout)
        msgs = parse_messages(r.stdout + "\n" + r.stderr)
        ok = r.returncode == 0 and not errors(msgs) and olean.exists()
        return CompileResult(ok, msgs, r.elapsed, r.timed_out, (r.stdout + r.stderr)[-8000:])

    def check_text(self, text: str, stem: str = "Attempt", timeout: float = 120.0) -> CompileResult:
        """Elaborate a scratch file (not importable afterwards)."""
        with self._lock:
            n = next(self._counter)
        p = self.write(f"{stem}_{n}", text)
        r = self.tc.run_lean(p, cwd=self.root, lean_path=self.lean_path, timeout=timeout)
        msgs = parse_messages(r.stdout + "\n" + r.stderr)
        ok = r.returncode == 0 and not errors(msgs)
        return CompileResult(ok, msgs, r.elapsed, r.timed_out, (r.stdout + r.stderr)[-8000:])

    def harness_cmd(self) -> list[str]:
        return [str(self.tc.lean), "--run", str(self.path(self.HARNESS))]

    def harness_env(self) -> dict[str, str]:
        """Environment overrides for the harness (the rest is inherited)."""
        env = {k: v for k, v in self.tc.env(self.lean_path).items() if k in ("LEAN_SYSROOT", "LEAN_PATH")}
        # Specs may index out of bounds on *wrong* outputs (e.g. perturbed or buggy
        # results); `xs[i]!` then panics (returning `default`, exactly as the logic
        # says). Symbolicated backtraces make each panic ~20ms, so turn them off.
        env["LEAN_BACKTRACE"] = "0"
        return env
