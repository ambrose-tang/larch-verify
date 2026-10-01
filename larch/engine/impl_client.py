"""Driver-side client for a language adapter (the process that runs the user's code).

The adapter is restarted transparently when user code kills it (a segfault, os._exit,
a hang inside C code that ignores the in-process timeout): the call is reported as a
crash or timeout and the function is reloaded for the next call.
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import time

from ..wire import from_wire, to_wire


USER_PYTHONPATH = "LARCH_USER_PYTHONPATH"  # the user's PYTHONPATH, saved while the driver runs
_UNSET = "<larch:unset>"


def child_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a process that runs on the user's behalf (an adapter, the Lean
    harness): this process's environment with the user's own PYTHONPATH restored, plus
    `overrides`. Jobs carry only the overrides, so no credentials are written to disk."""
    env = dict(os.environ)
    if USER_PYTHONPATH in env:
        saved = env.pop(USER_PYTHONPATH)
        if saved == _UNSET:
            env.pop("PYTHONPATH", None)
        else:
            env["PYTHONPATH"] = saved
    env.update(overrides or {})
    return env


class AdapterError(RuntimeError):
    """The adapter could not be started or answered nonsense (an environment problem)."""


class LoadError(RuntimeError):
    """The module under test could not be loaded (import error, syntax error, ...)."""

    def __init__(self, info: dict):
        super().__init__(info.get("error") or "could not load the code under test")
        self.info = info

    @property
    def missing(self) -> str | None:
        return self.info.get("missing")


class ImplClient:
    def __init__(self, cmd: list[str], env: dict[str, str], cwd: str, log_path: str | None = None,
                 start_timeout: float = 60.0):
        """`env` holds overrides on top of the inherited environment (see child_env)."""
        self.cmd = cmd
        self.env = child_env(env)
        self.cwd = cwd
        self.log_path = log_path
        self.start_timeout = start_timeout
        self.proc: subprocess.Popen | None = None
        self._buf = b""
        self._load_req: dict | None = None
        self.restarts = 0
        self.info: dict = {}

    # -- process management ---------------------------------------------------------------
    def start(self) -> dict:
        err = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        try:
            self.proc = subprocess.Popen(
                self.cmd + ([self.log_path] if self.log_path else []),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, env=self.env, cwd=self.cwd,
            )
        except OSError as e:
            raise AdapterError(f"could not start {self.cmd[0]}: {e}") from e
        finally:
            if err is not subprocess.DEVNULL:
                err.close()
        self._buf = b""
        try:
            self.info = self._rpc({"op": "ping"}, self.start_timeout)
        except (TimeoutError, EOFError) as e:
            raise AdapterError(f"{self.cmd[0]} did not start correctly ({e}){self._log_tail()}") from e
        if not self.info.get("ok"):
            raise AdapterError(f"{self.cmd[0]} did not start correctly: {self.info}")
        return self.info

    def close(self) -> None:
        if self.proc is not None:
            try:
                self.proc.stdin.close()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            self.proc = None

    def _restart(self) -> None:
        self.close()
        self.restarts += 1
        self.start()
        if self._load_req is not None:
            resp = self._rpc(self._load_req, self.start_timeout)
            if not resp.get("ok"):
                raise LoadError(resp)

    def _log_tail(self) -> str:
        if not self.log_path or not os.path.exists(self.log_path):
            return ""
        with open(self.log_path, errors="replace") as f:
            tail = f.read()[-1500:].strip()
        return f"\n{tail}" if tail else ""

    # -- protocol ------------------------------------------------------------------------
    def _readline(self, timeout: float) -> bytes:
        assert self.proc is not None and self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no answer in time")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                raise EOFError(f"adapter exited (code {self.proc.poll()})")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    def _rpc(self, req: dict, timeout: float) -> dict:
        if self.proc is None:
            raise EOFError("adapter not running")
        try:
            self.proc.stdin.write((json.dumps(req) + "\n").encode())  # type: ignore[union-attr]
            self.proc.stdin.flush()  # type: ignore[union-attr]
        except (BrokenPipeError, OSError) as e:
            raise EOFError(str(e)) from e
        while True:
            line = self._readline(timeout)
            if line.lstrip().startswith(b"{"):
                return json.loads(line)

    # -- API -------------------------------------------------------------------------------
    def load(self, load_req: dict, source: str | None = None) -> None:
        """Load the function under test (optionally from replacement module source).
        Raises LoadError with the adapter's diagnosis if it cannot be imported."""
        req = dict(load_req, op="load")
        if source is not None:
            req["source"] = source
        if self.proc is None:
            self.start()
        try:
            resp = self._rpc(req, self.start_timeout)
        except (TimeoutError, EOFError):
            self._load_req = None
            self.close()
            self.start()
            raise LoadError({"error": "loading the module crashed or hung the interpreter", "error_type": "crash"})
        if not resp.get("ok"):
            self._load_req = None
            raise LoadError(resp)
        self._load_req = req

    def call(self, args: list, timeout: float) -> dict:
        """Returns {"status": "ok", "value": <python value>} | {"status": "exception", "exc"}
        | {"status": "timeout"}."""
        req = {"op": "call", "args": [to_wire(a) for a in args], "timeout": timeout}
        try:
            resp = self._rpc(req, timeout + 5.0)
        except TimeoutError:
            # Hung where the in-process timer cannot interrupt it (C code, a blocked signal).
            self._restart()
            return {"status": "timeout"}
        except EOFError:
            code = self.proc.poll() if self.proc else None
            self._restart()
            return {"status": "exception", "exc": f"ProcessCrash: the interpreter exited (code {code}) during the call"}
        if resp.get("status") == "ok":
            resp["value"] = from_wire(resp.get("value"))
        return resp

    def __enter__(self) -> "ImplClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
