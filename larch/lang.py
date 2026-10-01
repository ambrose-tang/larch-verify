"""Language backends: everything Larch needs to know about the language of the code
under test. The rest of the pipeline (formalization, Lean, proofs, the test driver)
is language-neutral.

A backend provides
  * discovery and extraction of functions (signature, docs, surrounding context),
  * mutation operators and source splicing (mutation analysis, fixes),
  * the runtime: which interpreter runs the project's code, and the command of the
    small adapter that loads and calls one function there (see larch/wire.py for
    how values cross the boundary),
  * the type-mapping guidance the formalizer needs (language types -> Lean types).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class ExtractError(ValueError):
    pass


class RuntimeEnvError(RuntimeError):
    """The project's runtime could not be found or cannot load the code under test.
    The message is meant for the user and says how to fix it."""


@dataclass
class PyParam:
    name: str
    annotation: str | None
    lean_name: str
    has_default: bool = False


SourceParam = PyParam


@dataclass
class FunctionInfo:
    path: Path
    name: str  # "func" or "Class.method"
    source: str
    module_source: str
    lineno: int  # first line of the definition, including decorators (1-based)
    end_lineno: int
    col_offset: int
    params: list[PyParam]
    returns: str | None
    docstring: str | None
    context: str = ""
    warnings: list[str] = field(default_factory=list)
    language: str = "python"
    signature_text: str = ""  # language-specific rendering, if not Python

    @property
    def signature(self) -> str:
        if self.signature_text:
            return self.signature_text
        ps = ", ".join(p.name + (f": {p.annotation}" if p.annotation else "") for p in self.params)
        ret = f" -> {self.returns}" if self.returns else ""
        return f"def {self.name.split('.')[-1]}({ps}){ret}"


@dataclass
class Mutant:
    id: str
    operator: str
    description: str
    lineno: int
    function_source: str
    module_source: str


@dataclass
class Runtime:
    """How the code under test is run."""

    language: str
    display: str  # "Python 3.11.4", "Node 22.3.0 (TypeScript)"
    executable: str
    source: str  # why this runtime was chosen
    cmd: list[str]  # adapter command; the log path is appended
    env: dict[str, str]
    load: dict  # the adapter's load request (path, function, ...)
    int_bound: int | None = None  # largest integer magnitude the runtime handles exactly
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return f"{self.display} at {self.executable} ({self.source})"


class Language:
    name = "language"
    display = "Language"
    fence = ""
    extensions: tuple[str, ...] = ()
    type_guide = ""  # language -> Lean type mapping, for the formalization prompt

    def list_functions(self, path: Path) -> list[str]:
        raise NotImplementedError

    def extract(self, path: Path, name: str) -> FunctionInfo:
        raise NotImplementedError

    def generate_mutants(self, info: FunctionInfo, *, max_mutants: int = 40, seed: int = 0) -> list[Mutant]:
        raise NotImplementedError

    def splice_function(self, info: FunctionInfo, new_function_source: str) -> str:
        raise NotImplementedError

    def runtime(self, info: FunctionInfo, cfg) -> Runtime:
        """Resolve the project's runtime for `info` (cfg: larch.config.Config). Raises
        RuntimeEnvError with a user-facing message if there is none."""
        raise NotImplementedError

    def explain_load_error(self, rt: Runtime, info: FunctionInfo, err: dict) -> str:
        """A user-facing explanation of why the module could not be loaded."""
        return str(err.get("error") or "the module could not be loaded")

    def fix_constraints(self, rt: Runtime) -> str:
        """What a proposed fix may rely on in this runtime (for the fix prompt)."""
        return ""


_REGISTRY: list[Language] = []


def _registry() -> list[Language]:
    if not _REGISTRY:
        from .py.backend import PYTHON

        from .js.backend import JS, TS

        _REGISTRY.extend([PYTHON, TS, JS])
    return _REGISTRY


def language_for(path: Path | str) -> Language | None:
    suffix = Path(path).suffix.lower()
    for lang in _registry():
        if suffix in lang.extensions:
            return lang
    return None


def get_language(name: str) -> Language:
    for lang in _registry():
        if lang.name == name:
            return lang
    raise KeyError(name)


def supported_extensions() -> list[str]:
    return [e for lang in _registry() for e in lang.extensions]
