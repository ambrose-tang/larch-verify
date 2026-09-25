"""Parent-side control of the sandboxed Python worker."""
from __future__ import annotations

import importlib.util
import itertools
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from ..util import cache_root, sha256

_PKGS = ["hypothesis", "sortedcontainers", "attr", "attrs", "exceptiongroup", "_hypothesis_pytestplugin", "_hypothesis_globals", "_hypothesis_ftz_detector"]


def _worker_pythonpath() -> Path:
    """A directory containing only the packages the worker needs (larch + hypothesis
    and its pure-Python deps), so they never shadow the user's own packages."""
    larch_dir = Path(__file__).resolve().parent.parent
    sources: dict[str, Path] = {"larch": larch_dir}
    for name in _PKGS:
        spec = importlib.util.find_spec(name)
        if spec is None or spec.origin is None:
            continue
        origin = Path(spec.origin)
        sources[name] = origin.parent if origin.name == "__init__.py" else origin
    key = sha256(*[f"{k}={v}" for k, v in sorted(sources.items())])[:12]
    d = cache_root() / "pypath" / key
    if not d.exists():
        tmp = d.with_name(d.name + f".tmp{os.getpid()}")
        tmp.mkdir(parents=True, exist_ok=True)
        for name, src in sources.items():
            link = tmp / src.name
            if not link.exists():
                os.symlink(src, link)
        try:
            os.replace(tmp, d)
        except OSError:
            pass  # another process won the race
    return d


def default_python(target_file: Path) -> str:
    """Prefer the project's virtualenv so the user's imports resolve."""
    env = os.environ.get("LARCH_PYTHON")
    if env:
        return env
    d = target_file.resolve().parent
    for _ in range(8):
        for name in (".venv", "venv", ".env"):
            cand = d / name / "bin" / "python"
            if cand.exists():
                return str(cand)
        if (d / ".git").exists() or d.parent == d:
            break
        d = d.parent
    return sys.executable


class WorkerError(RuntimeError):
    pass


class PythonRunner:
    def __init__(self, python: str, workdir: Path):
        self.python = python
        self.workdir = Path(workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._counter = itertools.count(1)
        self._lock = threading.Lock()
        self.pythonpath = _worker_pythonpath()

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.pythonpath)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONHASHSEED"] = "0"
        env["HYPOTHESIS_STORAGE_DIRECTORY"] = str(self.workdir / ".hypothesis")
        return env

    def run(self, job: dict, timeout: float = 900.0) -> dict:
        with self._lock:
            n = next(self._counter)
        job_path = self.workdir / f"job{n:03d}_{job['kind']}.json"
        result_path = self.workdir / f"job{n:03d}_{job['kind']}.result.json"
        job = dict(job)
        job.setdefault("log_path", str(self.workdir / f"job{n:03d}_{job['kind']}.log"))
        job_path.write_text(json.dumps(job, default=repr))
        cmd = [self.python, "-m", "larch.py.worker", str(job_path), str(result_path)]
        try:
            proc = subprocess.run(cmd, cwd=self.workdir, env=self._env(), timeout=timeout, capture_output=True, text=True)
            rc = proc.returncode
            err = proc.stderr
        except subprocess.TimeoutExpired:
            rc, err = None, "worker timed out"
        if result_path.exists():
            return json.loads(result_path.read_text())
        partial = Path(str(result_path) + ".partial")
        if job["kind"] == "mutants" and partial.exists():
            done = [json.loads(line) for line in partial.read_text().splitlines() if line.strip()]
            return {"ok": False, "partial": done, "error": f"worker exited ({rc}): {err[-500:] if err else ''}"}
        log = Path(job["log_path"])
        tail = log.read_text(errors="replace")[-1500:] if log.exists() else ""
        raise WorkerError(f"worker failed (exit {rc}): {(err or '')[-1000:]}\n{tail}")

    def run_mutants(self, job: dict, timeout_per_mutant: float = 60.0) -> dict:
        """Run mutants, restarting the worker if a mutant kills it (e.g. a hang in C code)."""
        mutants = job["mutants"]
        results: dict[str, dict] = {}
        attempts = 0
        valid_inputs = None
        note = None
        while len(results) < len(mutants) and attempts < 4:
            attempts += 1
            pending = [m for m in mutants if m["id"] not in results]
            sub = dict(job, mutants=pending)
            try:
                res = self.run(sub, timeout=60 + timeout_per_mutant * len(pending))
            except WorkerError as e:
                res = {"ok": False, "partial": [], "error": str(e)}
            for r in res.get("mutants", []) + res.get("partial", []):
                results[r["id"]] = r
            valid_inputs = res.get("valid_inputs", valid_inputs)
            note = res.get("strategy_note", note)
            if not res.get("ok"):
                # The mutant being processed when the worker died is treated as killed
                # (it hung or crashed the interpreter).
                nxt = next((m for m in pending if m["id"] not in results), None)
                if nxt is not None:
                    results[nxt["id"]] = {"id": nxt["id"], "killed": True, "by": "worker_crash", "specs": [], "counterexample": None}
        ordered = [results[m["id"]] for m in mutants if m["id"] in results]
        return {"ok": True, "mutants": ordered, "valid_inputs": valid_inputs, "strategy_note": note}
