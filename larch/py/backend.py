"""The Python language backend."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

from ..lang import FunctionInfo, Language, Mutant, Runtime, RuntimeEnvError
from ..util import project_root, repo_root
from . import extract as _extract
from . import mutate as _mutate
from .env import PythonEnvError, find_module_elsewhere, resolve_python

ADAPTER = Path(__file__).resolve().with_name("adapter.py")


def _pytest_pythonpath(root: Path) -> list[str]:
    """`pythonpath` from pytest's configuration: the project's own statement of what
    must be importable when its code runs under test."""
    out: list[str] = []
    pp = root / "pyproject.toml"
    if pp.exists():
        try:
            ini = tomllib.loads(pp.read_text()).get("tool", {}).get("pytest", {}).get("ini_options", {})
        except (tomllib.TOMLDecodeError, OSError):
            ini = {}
        v = ini.get("pythonpath")
        if isinstance(v, str):
            out += v.split()
        elif isinstance(v, list):
            out += [str(x) for x in v]
    for name in ("pytest.ini", "setup.cfg", "tox.ini"):
        f = root / name
        if not f.exists():
            continue
        import configparser

        cp = configparser.ConfigParser()
        try:
            cp.read(f)
        except configparser.Error:
            continue
        for section in ("pytest", "tool:pytest"):
            if cp.has_option(section, "pythonpath"):
                out += cp.get(section, "pythonpath").split()
    return out


def import_layout(path: Path, extra: list[str] | None = None) -> tuple[list[str], str, str]:
    """(sys.path entries, module name, package) for importing `path` the way the
    project's own tooling would."""
    p = Path(path).resolve()
    root = project_root(p)
    pkg_parts: list[str] = []
    d = p.parent
    while (d / "__init__.py").exists():
        pkg_parts.insert(0, d.name)
        d = d.parent
    pkg_root = d
    if not pkg_parts:
        # Implicit namespace packages (PEP 420): app/core/mod.py imported as app.core.mod
        # from the project root (or its src/ directory).
        base = root / "src" if (root / "src") in p.parents else root
        if p.parent != base and p.parent.is_relative_to(base):
            rel = p.parent.relative_to(base).parts
            if rel and all(part.isidentifier() for part in rel):
                pkg_parts = list(rel)
                pkg_root = base
    entries = [pkg_root, p.parent]
    for e in list(extra or []) + _pytest_pythonpath(root):
        q = Path(e).expanduser()
        entries.append(q if q.is_absolute() else root / q)
    entries.append(root)
    if (root / "src").is_dir():
        entries.append(root / "src")
    top = repo_root(p)
    if top != root:
        entries.append(top)
    seen: set[str] = set()
    paths = []
    for e in entries:
        s = str(Path(e).resolve())
        if s not in seen:
            seen.add(s)
            paths.append(s)
    module = ".".join(pkg_parts + [p.stem])
    return paths, module, ".".join(pkg_parts)


class PythonLanguage(Language):
    name = "python"
    display = "Python"
    fence = "python"
    extensions = (".py",)

    @property
    def type_guide(self) -> str:  # type: ignore[override]
        from ..prompts import PYTHON_TYPE_GUIDE

        return PYTHON_TYPE_GUIDE

    def list_functions(self, path: Path) -> list[str]:
        return _extract.list_functions(path)

    def extract(self, path: Path, name: str) -> FunctionInfo:
        return _extract.extract(path, name)

    def generate_mutants(self, info: FunctionInfo, *, max_mutants: int = 40, seed: int = 0) -> list[Mutant]:
        return _mutate.generate_mutants(info, max_mutants=max_mutants, seed=seed)

    def splice_function(self, info: FunctionInfo, new_function_source: str) -> str:
        return _extract.splice_function(info, new_function_source)

    def extract_class(self, path: Path, name: str):
        return _extract.extract_class(path, name)

    def generate_class_mutants(self, info, *, max_mutants: int = 40, seed: int = 0) -> list[Mutant]:
        return _mutate.generate_mutants(info, max_mutants=max_mutants, seed=seed)

    def runtime(self, info: FunctionInfo, cfg) -> Runtime:
        try:
            env = resolve_python(info.path, cfg.python)
        except PythonEnvError as e:
            raise RuntimeEnvError(str(e)) from e
        extra = cfg.pythonpath.split(os.pathsep) if isinstance(cfg.pythonpath, str) else list(cfg.pythonpath or [])
        sys_path, module, package = import_layout(info.path, [e for e in extra if e])
        penv = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "PYTHONIOENCODING": "utf-8"}
        return Runtime(
            language=self.name,
            display=f"Python {env.version_str}",
            executable=env.executable,
            source=env.source,
            cmd=[env.executable, str(ADAPTER)],
            env=penv,
            load={"path": str(info.path), "function": info.name, "module": module, "package": package, "sys_path": sys_path},
            warnings=list(env.warnings),
        )

    def explain_load_error(self, rt: Runtime, info: FunctionInfo, err: dict) -> str:
        etype = err.get("error_type", "")
        error = str(err.get("error", "")).strip()
        where = f"{rt.display} at {rt.executable} (chosen: {rt.source})"
        missing = err.get("missing")
        if etype == "ModuleNotFoundError" and missing:
            top = missing.split(".")[0]
            local = _local_module(top, info.path)
            if local is not None:
                return (
                    f"{info.path.name} imports `{missing}`, which is in your repository ({local}) but not on the "
                    f"import path Larch derived. Add its parent directory with `pythonpath = [\"{_rel(local.parent, info.path)}\"]` "
                    f"under [tool.larch] in pyproject.toml (or .larch.toml)."
                )
            elsewhere = find_module_elsewhere(top, info.path, exclude=rt.executable)
            msg = f"{info.path.name} imports `{missing}`, which is not installed for {where}."
            if elsewhere:
                exe, why = elsewhere[0]
                msg += f"\nIt is importable with {exe} ({why}): re-run with `--python {exe}`."
            else:
                msg += (
                    "\nInstall the project's dependencies into that environment, or point Larch at the "
                    "interpreter your project uses with `--python PATH` (or `python = \"PATH\"` under [tool.larch])."
                )
            return msg
        if etype in ("SyntaxError", "IndentationError", "TabError"):
            return (
                f"{info.path.name} does not compile under {where}: {error}\n"
                "If the project targets a different Python version, pass `--python` with that interpreter."
            )
        if etype == "crash":
            return f"importing {info.path.name} crashed or hung the interpreter ({where})."
        return (
            f"importing {info.path.name} raised {error} ({where}).\n"
            "Larch imports the module exactly like `import` does, so module-level code must run "
            "without services, files or environment variables it cannot reach."
        )

    def fix_constraints(self, rt: Runtime) -> str:
        return (
            f"The fixed code runs on {rt.display}. Use only the standard library and modules this file "
            "already imports: do not add imports of other third-party packages, and do not use syntax or "
            f"APIs newer than {rt.display}."
        )


def _local_module(top: str, path: Path) -> Path | None:
    root = repo_root(path)
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache", "site-packages", "build", "dist"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        if top in dirnames:
            return Path(dirpath) / top
        if f"{top}.py" in filenames:
            return Path(dirpath) / f"{top}.py"
        if dirpath.count(os.sep) - str(root).count(os.sep) > 6:
            dirnames[:] = []
    return None


def _rel(p: Path, target: Path) -> str:
    root = project_root(target)
    try:
        return str(p.relative_to(root)) or "."
    except ValueError:
        return str(p)


PYTHON = PythonLanguage()
