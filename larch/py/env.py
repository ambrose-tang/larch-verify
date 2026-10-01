"""Find the Python interpreter (and import paths) the project's own code runs with.

Larch itself is usually installed in an isolated tool environment (uv tool, pipx),
whose interpreter has none of the project's dependencies. Running the code under
test there is the classic source of `ModuleNotFoundError`. So Larch looks for the
project's interpreter the way the developer's own tooling would, in this order:

  1. explicit: --python, LARCH_PYTHON, `python = ...` in [tool.larch] / .larch.toml
  2. an in-project virtualenv: .venv, venv, .env, env (any directory with pyvenv.cfg),
     or $UV_PROJECT_ENVIRONMENT, searched from the file up to the project root
  3. the active environment: $VIRTUAL_ENV, then $CONDA_PREFIX
  4. the project's environment manager: poetry, pipenv, pdm, hatch
  5. `python3` / `python` on PATH
  6. Larch's own interpreter (last resort, with a warning)

Every candidate is executed once to confirm it runs and to read its version.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..util import project_root, repo_root

VENV_NAMES = (".venv", "venv", ".env", "env", ".virtualenv")
MIN_VERSION = (3, 7)


class PythonEnvError(RuntimeError):
    pass


@dataclass
class PythonEnv:
    executable: str
    source: str  # why this interpreter was chosen
    version: tuple[int, int, int] = (0, 0, 0)
    prefix: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def version_str(self) -> str:
        return ".".join(map(str, self.version))

    def describe(self) -> str:
        return f"Python {self.version_str} at {self.executable} ({self.source})"


def _venv_python(d: Path) -> Path | None:
    for rel in ("bin/python", "bin/python3", "Scripts/python.exe"):
        p = d / rel
        if p.exists():
            return p
    return None


def _is_own_env(prefix: str) -> bool:
    try:
        return Path(prefix).resolve() == Path(sys.prefix).resolve()
    except OSError:
        return False


_probe_cache: dict[str, tuple | None] = {}


def probe(executable: str, timeout: float = 30.0) -> tuple[tuple[int, int, int], str, str] | None:
    """(version, sys.executable, sys.prefix) of an interpreter, or None if it does not run."""
    if executable in _probe_cache:
        return _probe_cache[executable]
    code = "import sys, json; print(json.dumps([list(sys.version_info[:3]), sys.executable, sys.prefix]))"
    out = None
    try:
        r = subprocess.run([executable, "-c", code], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        if r.returncode == 0:
            v, exe, prefix = json.loads(r.stdout.strip().splitlines()[-1])
            out = (tuple(v), exe or executable, prefix)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, IndexError):
        out = None
    _probe_cache[executable] = out
    return out


def _tool_output(cmd: list[str], cwd: Path) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout.strip().splitlines()
    return out[-1].strip() if r.returncode == 0 and out else None


def _manager_candidates(root: Path) -> list[tuple[str, str]]:
    """Interpreters owned by the project's environment manager (only queried when the
    project actually uses that manager)."""
    out: list[tuple[str, str]] = []
    pp = root / "pyproject.toml"
    pyproject = pp.read_text(errors="replace") if pp.exists() else ""
    if (root / "poetry.lock").exists() or "[tool.poetry" in pyproject:
        exe = _tool_output(["poetry", "env", "info", "--executable"], root)
        if exe:
            out.append((exe, "poetry environment"))
    if (root / "Pipfile").exists():
        exe = _tool_output(["pipenv", "--py"], root)
        if exe:
            out.append((exe, "pipenv environment"))
    if (root / "pdm.lock").exists() or "[tool.pdm" in pyproject:
        exe = _tool_output(["pdm", "info", "--python"], root)
        if exe:
            out.append((exe, "pdm environment"))
    if (root / "hatch.toml").exists() or "[tool.hatch.envs" in pyproject:
        d = _tool_output(["hatch", "env", "find", "default"], root)
        if d:
            py = _venv_python(Path(d))
            if py:
                out.append((str(py), "hatch environment"))
    return out


def candidates(target: Path, configured: str | None = None) -> list[tuple[str, str]]:
    """All plausible interpreters for `target`, best first, as (path, reason)."""
    target = Path(target).resolve()
    root = project_root(target)
    top = repo_root(target)
    out: list[tuple[str, str]] = []
    if configured:
        p = Path(configured).expanduser()
        if not p.is_absolute() and (root / p).exists():
            p = root / p
        if p.is_dir():
            vp = _venv_python(p)
            p = vp if vp else p
        exe = str(p) if p.exists() else (shutil.which(configured) or configured)
        out.append((exe, "configured"))
        return out
    uv_env = os.environ.get("UV_PROJECT_ENVIRONMENT")
    if uv_env:
        d = Path(uv_env) if Path(uv_env).is_absolute() else root / uv_env
        vp = _venv_python(d)
        if vp:
            out.append((str(vp), "$UV_PROJECT_ENVIRONMENT"))
    d = target.parent if target.is_file() or target.suffix else target
    while True:
        for name in VENV_NAMES:
            cand = d / name
            if (cand / "pyvenv.cfg").exists() or (name in (".venv", "venv") and cand.is_dir()):
                vp = _venv_python(cand)
                if vp:
                    rel = cand.relative_to(top) if cand.is_relative_to(top) else cand
                    out.append((str(vp), f"project virtualenv {rel}"))
        if d == top or d.parent == d:
            break
        d = d.parent
    venv = os.environ.get("VIRTUAL_ENV")
    if venv and not _is_own_env(venv):
        vp = _venv_python(Path(venv))
        if vp:
            out.append((str(vp), "active virtualenv $VIRTUAL_ENV"))
    conda = os.environ.get("CONDA_PREFIX")
    if conda and not _is_own_env(conda):
        cp = Path(conda) / ("python.exe" if os.name == "nt" else "bin/python")
        if cp.exists():
            out.append((str(cp), f"active conda env {os.environ.get('CONDA_DEFAULT_ENV', conda)}"))
    out += _manager_candidates(root)
    for name in ("python3", "python"):
        w = shutil.which(name)
        if w and not _is_own_env(str(Path(w).parent.parent)):
            out.append((w, f"`{name}` on PATH"))
    out.append((sys.executable, "Larch's own interpreter"))
    seen: set[str] = set()
    dedup = []
    for exe, why in out:
        if exe not in seen:
            seen.add(exe)
            dedup.append((exe, why))
    return dedup


def resolve_python(target: Path, configured: str | None = None) -> PythonEnv:
    """The interpreter to run `target` with. Raises PythonEnvError if an explicitly
    configured interpreter does not work."""
    tried: list[str] = []
    for exe, why in candidates(target, configured):
        info = probe(exe)
        if info is None:
            if why == "configured":
                raise PythonEnvError(f"the configured Python interpreter {exe!r} could not be run")
            tried.append(f"{exe} ({why}): does not run")
            continue
        version, real_exe, prefix = info
        if version[:2] < MIN_VERSION:
            msg = f"{exe} ({why}) is Python {'.'.join(map(str, version))}; Larch needs >= {'.'.join(map(str, MIN_VERSION))}"
            if why == "configured":
                raise PythonEnvError(msg)
            tried.append(msg)
            continue
        env = PythonEnv(executable=exe, source=why, version=version, prefix=prefix)
        if why == "Larch's own interpreter":
            env.warnings.append(
                "no project environment found; running your code with Larch's own interpreter. "
                "If it imports third-party packages, pass --python (or set `python` under [tool.larch])."
            )
        env.warnings += [f"skipped {t}" for t in tried]
        return env
    raise PythonEnvError("no working Python interpreter found:\n  " + "\n  ".join(tried))


def find_module_elsewhere(module: str, target: Path, exclude: str) -> list[tuple[str, str]]:
    """Other candidate interpreters in which `module` is importable (side-effect free:
    uses importlib.util.find_spec)."""
    top = module.split(".")[0]
    code = f"import importlib.util, sys; sys.exit(0 if importlib.util.find_spec({top!r}) else 1)"
    found = []
    for exe, why in candidates(target):
        if exe == exclude:
            continue
        try:
            r = subprocess.run([exe, "-c", code], capture_output=True, timeout=20, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0:
            found.append((exe, why))
    return found
