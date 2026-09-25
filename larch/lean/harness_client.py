"""Client for the JSON-lines Lean harness (`lean --run LarchHarness.lean`).

Standard library only: this module is imported inside the sandboxed worker.
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import time


class HarnessError(RuntimeError):
    pass


class HarnessTimeout(HarnessError):
    pass


class HarnessClient:
    def __init__(self, cmd: list[str], env: dict[str, str], cwd: str, *, timeout: float = 10.0, stderr_path: str | None = None):
        self.cmd = cmd
        self.env = env
        self.cwd = cwd
        self.timeout = timeout
        self.stderr_path = stderr_path
        self.proc: subprocess.Popen | None = None
        self._buf = b""
        self.restarts = 0

    def start(self) -> None:
        err = open(self.stderr_path, "ab") if self.stderr_path else subprocess.DEVNULL
        self.proc = subprocess.Popen(
            self.cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=err,
            env=self.env,
            cwd=self.cwd,
        )
        self._buf = b""
        # First request also absorbs harness elaboration time.
        resp = self.request({"op": "ping"}, timeout=max(self.timeout, 120.0))
        if not resp.get("pong"):
            raise HarnessError(f"harness did not start correctly: {resp}")

    def close(self) -> None:
        if self.proc is not None:
            try:
                self.proc.stdin.close()  # type: ignore[union-attr]
            except Exception:
                pass
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
            self.proc = None

    def restart(self) -> None:
        self.close()
        self.restarts += 1
        self.start()

    def _readline(self, timeout: float) -> bytes:
        assert self.proc is not None and self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HarnessTimeout("Lean harness did not answer in time")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                raise HarnessError("Lean harness exited unexpectedly")
            self._buf += chunk
        line, _, rest = self._buf.partition(b"\n")
        self._buf = rest
        return line

    def request(self, obj: dict, timeout: float | None = None) -> dict:
        if self.proc is None:
            self.start()
        assert self.proc is not None and self.proc.stdin is not None
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
            line = self._readline(timeout if timeout is not None else self.timeout)
        except (HarnessTimeout, HarnessError, BrokenPipeError, OSError) as e:
            # Leave the client usable for the next request.
            try:
                self.restart()
            except Exception as e2:  # pragma: no cover - surfaced to caller
                raise HarnessError(f"harness restart failed: {e2}") from e
            if isinstance(e, HarnessTimeout):
                raise
            raise HarnessError(str(e)) from e
        try:
            return json.loads(line)
        except json.JSONDecodeError as e:
            raise HarnessError(f"bad harness output: {line[:200]!r}") from e

    def __enter__(self) -> "HarnessClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
