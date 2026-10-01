"""Repository-level discovery: which functions in a codebase Larch can verify, and
which of them a change touched.

`scan` walks a repository (respecting .gitignore when it is a git checkout), extracts
every function in a supported language, and sorts them into verification candidates
and functions skipped with a reason (unsupported types, I/O, too large, ...). Candidates
are ranked so that a team can start where verification pays off most: documented,
branchy, pure functions over supported types.

`changed_functions` maps `git diff <ref>` hunks onto functions, for pull-request runs.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .lang import ExtractError, FunctionInfo, Language, language_for, supported_extensions
from .util import repo_root

SKIP_DIRS = {
    "node_modules", ".git", ".hg", ".svn", "dist", "build", "out", "coverage", ".next", ".turbo", ".venv", "venv",
    "env", ".env", "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache", "site-packages", "vendor",
    "third_party", ".larch",
}
_TEST_FILE = re.compile(r"(^test_.*\.py$|_test\.py$|^conftest\.py$|\.(test|spec)\.[cm]?[jt]s$)")
_TEST_DIR = {"tests", "test", "__tests__", "testing", "spec", "e2e", "fixtures", "__mocks__", "benchmarks", "examples"}
MAX_LINES = 120


@dataclass
class Candidate:
    file: str
    function: str
    language: str
    line: int
    end_line: int
    status: str  # ready | skipped
    reason: str = ""
    score: float = 0.0
    documented: bool = False
    annotated: bool = False

    @property
    def target(self) -> str:
        return f"{self.file}::{self.function}"

    def to_json(self) -> dict:
        d = asdict(self)
        d["target"] = self.target
        return d


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def is_test_path(path: Path) -> bool:
    return bool(_TEST_FILE.search(path.name)) or any(part in _TEST_DIR for part in path.parts[:-1])


def source_files(paths: list[Path], *, include_tests: bool = False) -> list[Path]:
    """Supported source files under `paths` (files are kept as given)."""
    exts = tuple(supported_extensions())
    out: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_file():
            if language_for(p):
                out.append(p.resolve())
            continue
        if not p.is_dir():
            continue
        listed = _git(["ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."], p)
        if listed is not None:
            files = [p / f for f in listed.split("\0") if f]
        else:
            files = []
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
                files += [Path(dirpath) / f for f in sorted(filenames)]
        for f in files:
            rel = f.relative_to(p)
            if not f.name.endswith(exts) or f.name.endswith(".d.ts") or not f.is_file():
                continue
            if any(part in SKIP_DIRS for part in rel.parts[:-1]):
                continue
            if not include_tests and is_test_path(rel):
                continue
            out.append(f.resolve())
    seen: set[Path] = set()
    return [f for f in out if not (f in seen or seen.add(f))]


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

_PY_OK = {"int", "bool", "str", "list", "List", "tuple", "Tuple", "Optional", "None", "Sequence", "Iterable",
          "typing", "Union", "Literal"}
_PY_BAD = {"float": "floats", "complex": "complex numbers", "dict": "dicts", "Dict": "dicts", "Mapping": "mappings",
           "set": "sets", "Set": "sets", "frozenset": "sets", "bytes": "bytes", "bytearray": "bytes", "Any": "Any",
           "Callable": "callables", "object": "arbitrary objects", "Decimal": "decimals", "datetime": "datetimes",
           "date": "dates", "Iterator": "iterators", "Generator": "generators", "Path": "paths"}
_TS_OK = {"number", "bigint", "string", "boolean", "null", "undefined", "Array", "ReadonlyArray", "readonly"}
_TS_BAD = {"any": "any", "unknown": "unknown", "object": "objects", "Record": "records", "Map": "maps", "Set": "sets",
           "Date": "dates", "Promise": "promises", "Function": "functions", "RegExp": "regexes", "Buffer": "buffers",
           "Uint8Array": "typed arrays", "symbol": "symbols"}
_PY_IMPURE = re.compile(r"\b(open|input)\s*\(|\b(requests|httpx|urllib|socket|subprocess|os|shutil|sqlite3|"
                        r"random|secrets|time|uuid|boto3|psycopg2?|redis)\.|datetime\.(now|today|utcnow)|\bself\.|"
                        r"\bglobal\s|\bnonlocal\s|\bawait\b")
_JS_IMPURE = re.compile(r"\b(fetch|require)\s*\(|\b(fs|process|document|window|localStorage|sessionStorage|crypto|"
                        r"axios|http|https|child_process)\.|Math\.random|Date\.now|new\s+Date\s*\(|\bthis\.|\bawait\b")
_BRANCH = re.compile(r"\b(if|elif|else|for|while|case|switch|match|try|except|catch)\b|\?\s|&&|\|\||\band\b|\bor\b")


def _type_problem(annotation: str | None, language: str) -> str | None:
    if not annotation:
        return None
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", annotation)
    if language == "python":
        for w in words:
            if w in _PY_BAD:
                return f"uses {_PY_BAD[w]} (`{annotation}`)"
            if w not in _PY_OK and w[:1].isupper():
                return f"uses the custom type `{w}`"
    else:
        if "=>" in annotation or "{" in annotation:
            return f"uses an object or function type (`{annotation}`)"
        for w in words:
            if w in _TS_BAD:
                return f"uses {_TS_BAD[w]} (`{annotation}`)"
            if w not in _TS_OK and w[:1].isupper():
                return f"uses the custom type `{w}`"
    return None


def _code_only(info: FunctionInfo) -> str:
    """The function's code with comments and string literals (docs) removed."""
    if info.language == "python":
        import io
        import textwrap
        import tokenize

        out: list[str] = []
        prev_end = None
        try:
            for t in tokenize.generate_tokens(io.StringIO(textwrap.dedent(info.source)).readline):
                if t.type in (tokenize.COMMENT, tokenize.STRING):
                    out.append(" ")
                elif t.string:
                    out.append(("" if t.start == prev_end else " ") + t.string)
                prev_end = t.end
        except (tokenize.TokenError, IndentationError, SyntaxError):
            return info.source
        return "".join(out)
    from .js.parse import JSParseError, tokenize as js_tokenize

    try:
        text = info.source
        for t in reversed(js_tokenize(text)):
            if t.kind in ("comment", "str", "tmpl"):
                text = text[: t.start] + " " + text[t.end:]
        return text
    except JSParseError:
        return info.source


def assess(info: FunctionInfo) -> tuple[str, str, float]:
    """(status, reason, score) for one extracted function."""
    lang = info.language
    for p in info.params:
        prob = _type_problem(p.annotation, lang)
        if prob:
            return "skipped", f"parameter `{p.name}` {prob}", 0.0
    prob = _type_problem(info.returns, lang)
    if prob:
        return "skipped", f"return value {prob}", 0.0
    if lang == "python" and info.returns in ("None",):
        return "skipped", "returns None (side effects only)", 0.0
    if lang != "python" and info.returns in ("void", "never"):
        return "skipped", "returns nothing (side effects only)", 0.0
    if not info.params:
        return "skipped", "takes no parameters", 0.0
    body = _code_only(info)
    m = (_PY_IMPURE if lang == "python" else _JS_IMPURE).search(body)
    if m:
        return "skipped", f"looks impure or stateful (`{m.group(0).strip()}`)", 0.0
    n_lines = info.end_lineno - info.lineno + 1
    if n_lines > MAX_LINES:
        return "skipped", f"too long ({n_lines} lines) for a reliable reference model", 0.0
    score = 1.0
    if info.docstring and len(info.docstring.split()) >= 8:
        score += 2.0
    if all(p.annotation for p in info.params) and info.returns:
        score += 1.0
    score += min(3.0, len(_BRANCH.findall(body)) * 0.5)
    if n_lines < 4:
        score -= 1.0
    return "ready", "", round(score, 2)


def _all_function_names(lang: Language, path: Path) -> list[str]:
    """Every function the backend can see (including ones it would refuse), so the
    scan can say why something is skipped."""
    if lang.name == "python":
        import ast

        from .py.extract import _functions, _is_static

        funcs = _functions(ast.parse(path.read_text(), filename=str(path)))
        return [n for n, node in funcs.items()
                if not n.split(".")[-1].startswith("_") and ("." not in n or _is_static(node))]
    from .js.parse import Module

    return [f.name for f in Module(path.read_text()).functions
            if not f.name.split(".")[-1].startswith(("_", "#")) and f.is_static]


def scan(paths: list[Path], *, include_tests: bool = False, root: Path | None = None) -> list[Candidate]:
    files = source_files(paths, include_tests=include_tests)
    base = root or (repo_root(paths[0]) if paths else Path.cwd())
    out: list[Candidate] = []
    for f in files:
        lang = language_for(f)
        if lang is None:
            continue
        rel = os.path.relpath(f, base)
        try:
            names = _all_function_names(lang, f)
        except Exception as e:  # noqa: BLE001 - unparsable file
            out.append(Candidate(rel, "*", lang.name, 0, 0, "skipped", f"cannot parse file: {str(e)[:120]}"))
            continue
        for name in names:
            try:
                info = lang.extract(f, name)
            except ExtractError as e:
                out.append(Candidate(rel, name, lang.name, 0, 0, "skipped", str(e)))
                continue
            except Exception as e:  # noqa: BLE001
                out.append(Candidate(rel, name, lang.name, 0, 0, "skipped", f"cannot analyse: {str(e)[:120]}"))
                continue
            status, reason, score = assess(info)
            out.append(Candidate(
                rel, name, lang.name, info.lineno, info.end_lineno, status, reason, score,
                documented=bool(info.docstring), annotated=all(p.annotation for p in info.params),
            ))
    out.sort(key=lambda c: (c.status != "ready", -c.score, c.file, c.line))
    return out


# ---------------------------------------------------------------------------
# Changes
# ---------------------------------------------------------------------------

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(root: Path, ref: str) -> dict[Path, set[int]]:
    """Lines of the working tree that differ from `ref` (new files: every line)."""
    root = repo_root(root)
    if _git(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], root) is None:
        raise ValueError(f"unknown git revision {ref!r}")
    diff = _git(["diff", "--no-color", "--no-ext-diff", "-U0", ref, "--"], root) or ""
    out: dict[Path, set[int]] = {}
    cur: Path | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            name = line[4:].strip()
            cur = None if name == "/dev/null" else (root / name[2:] if name.startswith("b/") else root / name).resolve()
            continue
        m = _HUNK.match(line)
        if m and cur is not None:
            start, count = int(m.group(1)), int(m.group(2) or "1")
            lines = out.setdefault(cur, set())
            if count == 0:  # pure deletion after line `start`
                lines.update({start, start + 1})
            else:
                lines.update(range(start, start + count))
    untracked = _git(["ls-files", "-z", "--others", "--exclude-standard"], root) or ""
    for f in untracked.split("\0"):
        if f:
            p = (root / f).resolve()
            try:
                out[p] = set(range(1, p.read_text(errors="replace").count("\n") + 2))
            except OSError:
                continue
    return out


def default_base_ref(root: Path) -> str:
    """The merge base with the default branch, for `--changed` without a ref."""
    root = repo_root(root)
    for upstream in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        base = _git(["merge-base", "HEAD", upstream], root)
        if base and base.strip():
            return base.strip()
    return "HEAD"


def changed_functions(cands: list[Candidate], root: Path, ref: str) -> list[Candidate]:
    changes = changed_lines(root, ref)
    base = repo_root(root)
    out = []
    for c in cands:
        f = (base / c.file).resolve()
        touched = changes.get(f)
        if touched and c.line and any(c.line <= ln <= c.end_line for ln in touched):
            out.append(c)
    return out
