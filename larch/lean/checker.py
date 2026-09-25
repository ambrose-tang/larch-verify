"""Authoritative, out-of-process proof acceptance.

A proof is accepted only if, in the *compiled* environment:
  1. the theorem exists and is a `theorem`,
  2. its type is exactly the approved spec constant (no weakened statement),
  3. it depends on no axioms beyond propext, Classical.choice and Quot.sound
     (so no sorry, admit, native_decide, or new axioms),
  4. (paranoid mode) `leanchecker` replays the module's declarations through the kernel.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..util import cache_root, run_proc, sha256
from .toolchain import LeanToolchain

STANDARD_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})

_ASSET = Path(__file__).parent / "assets" / "Checker.lean"


@dataclass
class TheoremCheck:
    theorem: str
    spec: str
    found: bool = False
    kind: str = ""
    type_ok: bool = False
    axioms: list[str] = field(default_factory=list)
    replay_ok: bool | None = None
    error: str | None = None

    @property
    def bad_axioms(self) -> list[str]:
        return sorted(a for a in self.axioms if a not in STANDARD_AXIOMS)

    @property
    def uses_sorry(self) -> bool:
        return "sorryAx" in self.axioms

    @property
    def accepted(self) -> bool:
        return (
            self.error is None
            and self.found
            and self.kind == "theorem"
            and self.type_ok
            and not self.bad_axioms
            and self.replay_ok is not False
        )

    def reason(self) -> str:
        if self.error:
            return self.error
        if not self.found:
            return "theorem missing from compiled environment"
        if self.kind != "theorem":
            return f"declaration is a {self.kind}, not a theorem"
        if not self.type_ok:
            return "statement differs from the approved spec"
        if self.uses_sorry:
            return "proof uses sorry"
        if self.bad_axioms:
            return "proof uses non-standard axioms: " + ", ".join(self.bad_axioms)
        if self.replay_ok is False:
            return "kernel replay (leanchecker) failed"
        return "ok"


class Checker:
    def __init__(self, toolchain: LeanToolchain):
        self.tc = toolchain
        self._binary: Path | None = None
        self._build_failed = False

    # -- building the native checker -------------------------------------------------
    def _build_dir(self) -> Path:
        src_hash = sha256(_ASSET.read_text(), self.tc.spec)[:12]
        return cache_root() / "checker" / f"{self.tc.version}-{src_hash}"

    def binary(self, build: bool = True) -> Path | None:
        if self._binary is not None:
            return self._binary
        d = self._build_dir()
        exe = d / ".lake" / "build" / "bin" / "larch-checker"
        if exe.exists():
            self._binary = exe
            return exe
        if not build or self._build_failed:
            return None
        d.mkdir(parents=True, exist_ok=True)
        import fcntl

        with open(d / ".build.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # concurrent first runs: build once
            if exe.exists():
                self._binary = exe
                return exe
            return self._build(d, exe)

    def _build(self, d: Path, exe: Path) -> Path | None:
        (d / "Main.lean").write_text(_ASSET.read_text())
        (d / "lakefile.toml").write_text(
            'name = "larchchecker"\ndefaultTargets = ["larch-checker"]\n\n'
            '[[lean_exe]]\nname = "larch-checker"\nroot = "Main"\nsupportInterpreter = true\n'
        )
        (d / "lean-toolchain").write_text(self.tc.spec + "\n")
        r = run_proc([str(self.tc.lake), "build"], cwd=d, env=self.tc.env(), timeout=900)
        if r.returncode == 0 and exe.exists():
            self._binary = exe
            return exe
        self._build_failed = True
        shutil.rmtree(d / ".lake", ignore_errors=True)
        return None

    # -- running -------------------------------------------------------------------------
    def check(
        self,
        module: str,
        pairs: list[tuple[str, str]],
        *,
        lean_path: list[Path],
        cwd: Path,
        replay: bool = True,
        timeout: float = 300.0,
    ) -> list[TheoremCheck]:
        results = {thm: TheoremCheck(theorem=thm, spec=spec) for thm, spec in pairs}
        args = [module] + [f"{thm}={spec}" for thm, spec in pairs]
        exe = self.binary()
        if exe is not None:
            cmd = [str(exe), *args]
        else:  # interpreted fallback (slower, same logic)
            cmd = [str(self.tc.lean), "--run", str(_ASSET), *args]
        r = run_proc(cmd, cwd=cwd, env=self.tc.env(lean_path), timeout=timeout)
        if r.returncode != 0:
            err = "checker timed out" if r.timed_out else f"checker failed: {(r.stderr or r.stdout)[-1500:]}"
            for tc in results.values():
                tc.error = err
            return list(results.values())
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            tc = results.get(d.get("theorem", ""))
            if tc is None:
                continue
            tc.found = bool(d.get("found"))
            tc.kind = d.get("kind", "")
            tc.type_ok = bool(d.get("type_ok"))
            tc.axioms = list(d.get("axioms", []))
        for tc in results.values():
            if not tc.found and tc.error is None and not tc.kind:
                tc.found = False
        if replay and self.tc.leanchecker.exists():
            rr = run_proc(
                [str(self.tc.leanchecker), module],
                cwd=cwd,
                env=self.tc.env(lean_path),
                timeout=timeout,
            )
            ok = rr.returncode == 0
            for tc in results.values():
                tc.replay_ok = ok
        return list(results.values())
