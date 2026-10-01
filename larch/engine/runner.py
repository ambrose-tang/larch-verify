"""Parent-side control of test jobs.

Each job runs in a driver subprocess on LARCH'S OWN interpreter (sys.executable),
which owns input generation (hypothesis), the Lean harness and shrinking. The driver
talks to an adapter process in the PROJECT's runtime that loads and calls the code
under test. Nothing from Larch's environment is ever injected into the project's
interpreter, and nothing from the project's environment is needed by the driver.
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from ..lang import Runtime
from .impl_client import _UNSET, USER_PYTHONPATH, AdapterError, ImplClient, LoadError

_LARCH_PARENT = str(Path(__file__).resolve().parent.parent.parent)


class WorkerError(RuntimeError):
    pass


class JobRunner:
    def __init__(self, runtime: Runtime, workdir: Path):
        self.runtime = runtime
        self.workdir = Path(workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    @property
    def python(self) -> str:  # backwards compatibility: the interpreter running user code
        return self.runtime.executable

    def _driver_env(self) -> dict[str, str]:
        env = dict(os.environ)
        # Make `larch` importable in the driver even from a source checkout. This only
        # affects Larch's own interpreter, never the project's.
        pp = env.get("PYTHONPATH")
        env[USER_PYTHONPATH] = pp if pp is not None else _UNSET  # restored for the user's processes
        env["PYTHONPATH"] = _LARCH_PARENT + (os.pathsep + pp if pp else "")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONHASHSEED"] = "0"
        env["HYPOTHESIS_STORAGE_DIRECTORY"] = str(self.workdir / ".hypothesis")
        return env

    def impl_spec(self) -> dict:
        rt = self.runtime
        return {"cmd": rt.cmd, "env": rt.env, "cwd": str(self.workdir), "load": rt.load}

    def check(self, source: str | None = None, timeout: float = 120.0) -> dict:
        """Start the project's runtime and load the function under test, without any
        Lean or LLM work. Returns {"ok": True, "info": ping} or {"ok": False, ...} with
        the adapter's diagnosis (error, error_type, missing, traceback)."""
        with self._lock:
            n = next(self._counter)
        log = str(self.workdir / f"check{n:03d}.log")
        client = ImplClient(self.runtime.cmd, self.runtime.env, str(self.workdir), log_path=log, start_timeout=timeout)
        try:
            info = client.start()
            client.load(self.runtime.load, source)
            return {"ok": True, "info": info}
        except LoadError as e:
            return dict(e.info, ok=False)
        except AdapterError as e:
            return {"ok": False, "error_type": "adapter", "error": str(e)}
        finally:
            client.close()

    def run(self, job: dict, timeout: float = 900.0) -> dict:
        with self._lock:
            n = next(self._counter)
        job_path = self.workdir / f"job{n:03d}_{job['kind']}.json"
        result_path = self.workdir / f"job{n:03d}_{job['kind']}.result.json"
        job = dict(job)
        job.setdefault("log_path", str(self.workdir / f"job{n:03d}_{job['kind']}.log"))
        if job["kind"] != "props":
            job.setdefault("impl", self.impl_spec())
        if self.runtime.int_bound is not None:
            job.setdefault("int_bound", self.runtime.int_bound)
        job_path.write_text(json.dumps(job, default=repr))
        cmd = [sys.executable, "-m", "larch.engine.driver", str(job_path), str(result_path)]
        try:
            proc = subprocess.run(cmd, cwd=self.workdir, env=self._driver_env(), timeout=timeout, capture_output=True, text=True)
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
        """Run mutants, restarting the driver if it dies part-way."""
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
                # The mutant being processed when the driver died is treated as killed.
                nxt = next((m for m in pending if m["id"] not in results), None)
                if nxt is not None:
                    results[nxt["id"]] = {"id": nxt["id"], "killed": True, "by": "worker_crash", "specs": [], "counterexample": None}
        ordered = [results[m["id"]] for m in mutants if m["id"] in results]
        return {"ok": True, "mutants": ordered, "valid_inputs": valid_inputs, "strategy_note": note}


PythonRunner = JobRunner  # backwards-compatible name
