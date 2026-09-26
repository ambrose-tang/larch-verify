"""Locate (and if needed install) the pinned Lean 4 toolchain, and run Lean.

Larch pins one Lean version. Prompts, the tactic portfolio, the harness template
and the proof checker are all tested against it; Lean's core library and tactic
set change quickly between releases.
"""
from __future__ import annotations

import os
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from ..util import ProcResult, run_proc

PINNED_TOOLCHAIN = "leanprover/lean4:v4.34.1"

# Cap concurrent Lean elaborations process-wide: proof portfolios for several specs
# (and several functions) otherwise start dozens of Lean processes, thrash, and turn
# into spurious timeouts. Waiting for a slot does not count toward a check's timeout.
_LEAN_SLOTS = threading.BoundedSemaphore(int(os.environ.get("LARCH_LEAN_JOBS", str(max(2, os.cpu_count() or 4)))))


class ToolchainError(RuntimeError):
    pass


def _elan_home() -> Path:
    return Path(os.environ.get("ELAN_HOME", Path.home() / ".elan"))


def _toolchain_dirname(spec: str) -> str:
    # elan stores "leanprover/lean4:v4.34.1" as "leanprover--lean4---v4.34.1"
    return spec.replace("/", "--").replace(":", "---")


@dataclass
class LeanToolchain:
    prefix: Path
    spec: str

    @property
    def lean(self) -> Path:
        return self.prefix / "bin" / "lean"

    @property
    def lake(self) -> Path:
        return self.prefix / "bin" / "lake"

    @property
    def leanchecker(self) -> Path:
        return self.prefix / "bin" / "leanchecker"

    @property
    def version(self) -> str:
        return self.spec.split(":")[-1]

    def env(self, lean_path: list[Path] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        # LEAN_SYSROOT avoids a `lean --print-prefix` round trip through the elan
        # proxy (seconds of latency per invocation).
        env["LEAN_SYSROOT"] = str(self.prefix)
        if lean_path:
            env["LEAN_PATH"] = os.pathsep.join(str(p) for p in lean_path)
        else:
            env.pop("LEAN_PATH", None)
        return env

    def run_lean(
        self,
        file: Path,
        *,
        cwd: Path,
        lean_path: list[Path] | None = None,
        olean: Path | None = None,
        timeout: float = 120.0,
    ) -> ProcResult:
        cmd = [str(self.lean)]
        if olean is not None:
            olean.parent.mkdir(parents=True, exist_ok=True)
            cmd += ["-o", str(olean), "-i", str(olean.with_suffix(".ilean"))]
        cmd.append(str(file))
        with _LEAN_SLOTS:
            return run_proc(cmd, cwd=cwd, env=self.env(lean_path), timeout=timeout)


_lock = threading.Lock()
_cached: dict[str, LeanToolchain] = {}


def find_toolchain(spec: str | None = None, *, install: bool = False) -> LeanToolchain:
    """Find the pinned toolchain. With install=True, ask elan to install it if missing."""
    spec = spec or os.environ.get("LARCH_LEAN_TOOLCHAIN") or PINNED_TOOLCHAIN
    with _lock:
        if spec in _cached:
            return _cached[spec]
        explicit = os.environ.get("LARCH_LEAN_PREFIX")
        if explicit:
            tc = LeanToolchain(Path(explicit).expanduser(), spec)
            if not tc.lean.exists():
                raise ToolchainError(f"LARCH_LEAN_PREFIX={explicit} has no bin/lean")
            _cached[spec] = tc
            return tc
        prefix = _elan_home() / "toolchains" / _toolchain_dirname(spec)
        if not (prefix / "bin" / "lean").exists():
            if not install:
                raise ToolchainError(
                    f"Lean toolchain {spec} is not installed. Run `larch doctor --install` "
                    f"(uses elan), or `elan toolchain install {spec}`."
                )
            elan = shutil.which("elan") or str(_elan_home() / "bin" / "elan")
            if not Path(elan).exists():
                raise ToolchainError(
                    "elan (the Lean version manager) was not found. Install it from "
                    "https://github.com/leanprover/elan, then run `larch doctor --install`."
                )
            r = run_proc([elan, "toolchain", "install", spec], timeout=1800)
            if r.returncode != 0 or not (prefix / "bin" / "lean").exists():
                raise ToolchainError(f"elan failed to install {spec}:\n{r.stderr[-2000:]}")
        tc = LeanToolchain(prefix, spec)
        _cached[spec] = tc
        return tc
