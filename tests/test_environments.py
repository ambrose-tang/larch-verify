"""The code under test runs in the PROJECT's runtime, not Larch's.

Regression tests for the `ModuleNotFoundError` family: Larch used to borrow packages
from its own environment into the project's interpreter (breaking whenever the two
Python versions differed), ignored activated/managed virtualenvs, clobbered the
user's PYTHONPATH, and missed project-root and namespace-package imports.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from larch.config import Config
from larch.engine.impl_client import ImplClient, LoadError
from larch.engine.runner import JobRunner
from larch.py.backend import PYTHON, import_layout
from larch.py.env import candidates, resolve_python
from larch.wire import Opaque, from_wire, to_wire

CLAMP = '''\
def clamp(x: int, lo: int, hi: int) -> int:
    """Clamp x into [lo, hi]."""
    return max(lo, min(x, hi))
'''


def _runtime(path: Path, **cfg):
    info = PYTHON.extract(path, "clamp")
    return info, PYTHON.runtime(info, Config(**cfg))


def _check(tmp_path: Path, path: Path, **cfg) -> dict:
    info, rt = _runtime(path, **cfg)
    return JobRunner(rt, tmp_path / "work").check()


def _fake_venv(d: Path, python: str = sys.executable) -> Path:
    """A minimal virtualenv layout pointing at a real interpreter."""
    (d / "bin").mkdir(parents=True)
    (d / "pyvenv.cfg").write_text(f"home = {Path(python).parent}\n")
    exe = d / "bin" / "python"
    exe.symlink_to(python)
    return exe


# -- wire format ---------------------------------------------------------------------------------

@pytest.mark.parametrize("v", [0, -5, 2**80, -(2**64), True, None, "héllo\n", [1, [2, 3]], (1, "a"), [(1, None)], {"a": 1}, 0.5])
def test_wire_roundtrip(v):
    assert from_wire(to_wire(v)) == v


def test_wire_opaque():
    o = from_wire({"$repr": "<Foo>", "$type": "Foo"})
    assert isinstance(o, Opaque) and repr(o) == "<Foo>"


# -- interpreter discovery -----------------------------------------------------------------------

def test_project_venv_preferred(tmp_path):
    (tmp_path / ".git").mkdir()
    exe = _fake_venv(tmp_path / ".venv")
    f = tmp_path / "pkg" / "mod.py"
    f.parent.mkdir()
    f.write_text(CLAMP)
    env = resolve_python(f)
    assert env.executable == str(exe) and "project virtualenv" in env.source


def test_venv_found_at_repo_root_above_subproject(tmp_path):
    (tmp_path / ".git").mkdir()
    exe = _fake_venv(tmp_path / ".venv")
    sub = tmp_path / "services" / "billing"
    sub.mkdir(parents=True)
    (sub / "pyproject.toml").write_text("[project]\nname = 'billing'\n")
    f = sub / "mod.py"
    f.write_text(CLAMP)
    assert resolve_python(f).executable == str(exe)


def test_active_virtualenv_used(tmp_path, monkeypatch):
    (tmp_path / "proj" / ".git").mkdir(parents=True)
    exe = _fake_venv(tmp_path / "elsewhere" / "proj-py3")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "elsewhere" / "proj-py3"))
    f = tmp_path / "proj" / "mod.py"
    f.write_text(CLAMP)
    env = resolve_python(f)
    assert env.executable == str(exe) and "VIRTUAL_ENV" in env.source


def test_configured_interpreter_wins_and_accepts_venv_dir(tmp_path):
    (tmp_path / ".git").mkdir()
    _fake_venv(tmp_path / ".venv")
    other = _fake_venv(tmp_path / "custom")
    f = tmp_path / "mod.py"
    f.write_text(CLAMP)
    assert resolve_python(f, str(tmp_path / "custom")).executable == str(other)
    assert resolve_python(f, "custom").executable == str(other)  # relative to the project root


def test_broken_configured_interpreter_is_an_error(tmp_path):
    from larch.py.env import PythonEnvError

    f = tmp_path / "mod.py"
    f.write_text(CLAMP)
    with pytest.raises(PythonEnvError):
        resolve_python(f, str(tmp_path / "nope" / "python"))


def test_larch_own_interpreter_is_last_resort(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text(CLAMP)
    assert candidates(f)[-1] == (sys.executable, "Larch's own interpreter")


# -- import layout ---------------------------------------------------------------------------------

def test_layout_regular_package(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    pkg = tmp_path / "src" / "acme" / "util"
    pkg.mkdir(parents=True)
    (tmp_path / "src" / "acme" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    paths, module, package = import_layout(pkg / "mod.py")
    assert module == "acme.util.mod" and package == "acme.util"
    assert paths[0] == str((tmp_path / "src").resolve())
    assert str(tmp_path.resolve()) in paths


def test_layout_namespace_package(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "app" / "core").mkdir(parents=True)
    paths, module, package = import_layout(tmp_path / "app" / "core" / "mod.py")
    assert module == "app.core.mod" and package == "app.core"
    assert str(tmp_path.resolve()) in paths


def test_layout_includes_pytest_and_larch_pythonpath(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\npythonpath = ["libs"]\n')
    paths, _, _ = import_layout(tmp_path / "mod.py", ["vendor"])
    assert str((tmp_path / "libs").resolve()) in paths and str((tmp_path / "vendor").resolve()) in paths


# -- loading in the project's runtime (real adapter) ---------------------------------------------------

def test_adapter_imports_project_root_namespace_package(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "app" / "core").mkdir(parents=True)
    (tmp_path / "app" / "limits.py").write_text("LO = 0\n")
    f = tmp_path / "app" / "core" / "mod.py"
    f.write_text("from app.limits import LO\nfrom ..limits import LO as LO2\n" + CLAMP)
    assert _check(tmp_path, f)["ok"]


def test_adapter_keeps_user_pythonpath(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "shared_consts.py").write_text("X = 1\n")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "lib"))
    f = tmp_path / "mod.py"
    f.write_text("import shared_consts\n" + CLAMP)
    assert _check(tmp_path, f)["ok"]


def test_adapter_does_not_expose_larch_or_its_dependencies(tmp_path):
    (tmp_path / ".git").mkdir()
    venv = tmp_path / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    f = tmp_path / "mod.py"
    f.write_text("import larch\n" + CLAMP)
    res = _check(tmp_path, f)
    assert not res["ok"] and res["missing"] == "larch"


def test_missing_dependency_is_explained(tmp_path):
    (tmp_path / ".git").mkdir()
    f = tmp_path / "mod.py"
    f.write_text("import definitely_not_installed_pkg\n" + CLAMP)
    info, rt = _runtime(f)
    res = JobRunner(rt, tmp_path / "work").check()
    assert not res["ok"] and res["missing"] == "definitely_not_installed_pkg"
    msg = PYTHON.explain_load_error(rt, info, res)
    assert "definitely_not_installed_pkg" in msg and "--python" in msg


def test_local_module_not_on_path_is_explained(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "tools" / "helpers").mkdir(parents=True)
    (tmp_path / "tools" / "helpers" / "fmt_utils.py").write_text("")
    (tmp_path / "svc").mkdir()
    f = tmp_path / "svc" / "mod.py"
    f.write_text("import fmt_utils\n" + CLAMP)
    info, rt = _runtime(f)
    res = JobRunner(rt, tmp_path / "work").check()
    msg = PYTHON.explain_load_error(rt, info, res)
    assert "pythonpath" in msg and "tools/helpers" in msg


def test_calls_print_crash_and_hang(tmp_path):
    (tmp_path / ".git").mkdir()
    f = tmp_path / "mod.py"
    f.write_text(
        "import os\n"
        "def clamp(x: int, lo: int, hi: int) -> int:\n"
        "    print('noise')\n"
        "    if x == 1:\n        os._exit(3)\n"
        "    if x == 2:\n        while True: pass\n"
        "    if x == 3:\n        raise ValueError('bad')\n"
        "    return (x, [lo, hi])\n"
    )
    info, rt = _runtime(f)
    c = ImplClient(rt.cmd, rt.env, str(tmp_path), log_path=str(tmp_path / "log"))
    with c:
        c.load(rt.load)
        assert c.call([0, 2**70, -1], 2.0) == {"status": "ok", "value": (0, [2**70, -1])}
        assert c.call([1, 0, 0], 2.0)["status"] == "exception"  # interpreter died; reported, restarted
        assert c.call([2, 0, 0], 0.2) == {"status": "timeout"}
        assert "ValueError" in c.call([3, 0, 0], 2.0)["exc"]
        assert c.call([0, 1, 2], 2.0)["status"] == "ok"  # still usable
        with pytest.raises(LoadError):
            c.load(rt.load, source="def clamp(:\n")


@pytest.mark.parametrize("version", ["3.8", "3.9", "3.10", "3.11", "3.12", "3.13", "3.14"])
def test_adapter_runs_on_other_python_versions(tmp_path, version):
    exe = shutil.which(f"python{version}")
    if exe is None and shutil.which("uv"):
        r = subprocess.run(["uv", "python", "find", "--no-python-downloads", version], capture_output=True, text=True)
        exe = r.stdout.strip() if r.returncode == 0 else None
    if not exe:
        pytest.skip(f"python{version} not available")
    f = tmp_path / "mod.py"
    f.write_text("from typing import List, Optional\n" + CLAMP)
    info, rt = _runtime(f, python=exe)
    assert rt.display == f"Python {version}" or rt.display.startswith(f"Python {version}.")
    c = ImplClient(rt.cmd, rt.env, str(tmp_path), log_path=str(tmp_path / "log"))
    with c:
        c.load(rt.load)
        assert c.call([5, 0, 3], 2.0) == {"status": "ok", "value": 3}


def test_job_files_do_not_contain_the_environment(tmp_path, monkeypatch):
    """Job files are kept as run artifacts: they must not capture credentials."""
    from larch.lean.toolchain import ToolchainError, find_toolchain

    try:
        find_toolchain()
    except ToolchainError:
        pytest.skip("pinned Lean toolchain not installed")
    from test_integration import CLAMP_OK, _cfg, fake_llm

    from larch.engine.session import verify_function

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-must-not-leak")
    (tmp_path / ".git").mkdir()
    f = tmp_path / "clamp.py"
    f.write_text(CLAMP_OK)
    cfg = _cfg(tmp_path)
    cfg.run_mutation = False
    cfg.run_proofs = False
    report = verify_function(f, "clamp", cfg, llm=fake_llm())
    assert report.drt["valid"] > 0, report.error
    jobs = list(Path(report.artifacts_dir).rglob("job*.json"))
    assert jobs and not any("sk-test-must-not-leak" in j.read_text() for j in jobs)
