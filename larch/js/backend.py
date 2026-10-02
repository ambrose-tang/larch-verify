"""The JavaScript and TypeScript language backends (code under test runs on Node.js)."""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
from pathlib import Path

from ..lang import ComponentInfo, ExtractError, FunctionInfo, Language, MethodInfo, Mutant, PyParam, Runtime, RuntimeEnvError
from ..py.extract import splice_function as _splice_lines
from ..spec import lean_binder
from ..util import project_root, repo_root
from .parse import FunctionDecl, JSParseError, Module, jsdoc_types

ADAPTER = Path(__file__).resolve().with_name("adapter.mjs")
SAFE_INT = 2**53 - 1
MIN_NODE = (20, 6)  # module.register()
MIN_NODE_TS = (22, 6)  # built-in type stripping

TYPE_GUIDE = """\
JavaScript/TypeScript-to-Lean mapping (be exact; differential tests will catch any mismatch):
- A `number` holding integers ↦ Int (use Nat only for counts or indices that cannot be
  negative). Larch tests `number` parameters only with integers in ±(2^53 − 1); never
  rely on larger values. If the function returns non-integral numbers it is out of scope.
- Division: `/` is real division. Integer code writes `Math.floor(a / b)` (= `Int.fdiv a b`)
  or `Math.trunc(a / b)` (= `Int.tdiv a b`). `a % b` takes the sign of the DIVIDEND:
  write `Int.tmod a b` (not `%`, which is Euclidean in Lean). `Math.round(x)` rounds .5 up.
- `bigint` ↦ Int (`/` truncates: `Int.tdiv`; `%` is `Int.tmod`).
- boolean ↦ Bool; string ↦ String; `T[]` / `Array<T>` / `ReadonlyArray<T>` ↦ List T;
  tuple `[A, B]` ↦ A × B; `T | null`, `T | undefined` and optional parameters ↦ Option T
  (null/undefined ↦ none). For an untyped JavaScript parameter, infer the type from the
  JSDoc, the name and the body.
- Strings: JavaScript strings are UTF-16 and `s.length` counts code units; Lean's
  `String.length` counts characters. `toUpperCase`/`toLowerCase` and `\\s`/`\\w` regexes
  are Unicode-aware in places where Lean's `Char.isWhitespace`, `toUpper`, ... are ASCII-only.
  If that matters, restrict the precondition to ASCII strings.
- `throw` on some intended inputs ↦ "exceptions": true (the model returns Option T).
- Equality: `===` on arrays compares identity, not contents; model the intended value.
- The input generator is still PYTHON code using hypothesis (`st`): it produces Python
  ints, str, bool, None, lists and tuples, which Larch converts to JavaScript values.
"""

def _module(path: Path) -> Module:
    try:
        return Module(path.read_text())
    except JSParseError as e:
        raise ExtractError(f"cannot parse {path}: {e}") from e
    except UnicodeDecodeError as e:
        raise ExtractError(f"{path} is not UTF-8 text") from e


def _visible(f: FunctionDecl) -> bool:
    short = f.name.split(".")[-1]
    return not short.startswith(("_", "#")) and f.is_static and not f.is_async and not f.is_generator \
        and short != "constructor" and not any(p.rest or p.pattern for p in f.params)


class JavaScriptLanguage(Language):
    name = "javascript"
    display = "JavaScript"
    fence = "js"
    extensions = (".js", ".mjs", ".cjs")
    type_guide = TYPE_GUIDE

    # -- discovery and extraction -----------------------------------------------------------
    def list_functions(self, path: Path) -> list[str]:
        return [f.name for f in _module(Path(path)).functions if _visible(f)]

    def extract(self, path: Path, name: str) -> FunctionInfo:
        path = Path(path)
        if not path.exists():
            raise ExtractError(f"{path} does not exist")
        mod = _module(path)
        decl = next((f for f in mod.functions if f.name == name), None)
        if decl is None:
            avail = ", ".join(f.name for f in mod.functions) or "none"
            raise ExtractError(f"function {name!r} not found in {path} (available: {avail})")
        if decl.is_async:
            raise ExtractError("async functions are not supported yet")
        if decl.is_generator:
            raise ExtractError("generator functions are not supported yet")
        if not decl.is_static:
            raise ExtractError("only top-level functions and static methods are supported (methods need an instance)")
        if any(p.rest or p.pattern for p in decl.params):
            raise ExtractError("rest parameters and destructured parameters are not supported yet")
        doc_params, doc_ret = jsdoc_types(decl.jsdoc)
        params, used = [], set()
        for p in decl.params:
            ann = p.annotation or doc_params.get(p.name)
            if p.optional and ann and "undefined" not in ann:
                ann = f"{ann} | undefined"
            lean_name = lean_binder(p.name)
            while lean_name in used:
                lean_name += "'"
            used.add(lean_name)
            params.append(PyParam(p.name, ann, lean_name, has_default=p.has_default))
        returns = decl.returns or doc_ret
        first_tok = decl.start_tok
        start_char = mod.toks[first_tok].start
        if decl.jsdoc_tok is not None:
            start_char = mod.all_toks[decl.jsdoc_tok].start
        end_char = mod.toks[decl.end_tok].end
        src = mod.src
        line_start = src.rfind("\n", 0, start_char) + 1
        lineno = src.count("\n", 0, start_char) + 1
        end_lineno = src.count("\n", 0, end_char) + 1
        line_end = src.find("\n", end_char)
        source = src[line_start: len(src) if line_end == -1 else line_end]
        ps = ", ".join(p.name + (f": {p.annotation}" if p.annotation else "") for p in params)
        short = name.split(".")[-1]
        sig = f"function {short}({ps})" + (f": {returns}" if returns else "")
        info = FunctionInfo(
            path=path.resolve(), name=name, source=source, module_source=src, lineno=lineno,
            end_lineno=end_lineno, col_offset=start_char - line_start, params=params, returns=returns,
            docstring=decl.jsdoc, language=self.name, signature_text=sig,
        )
        info.context = _context(mod, decl)
        if not decl.jsdoc:
            info.warnings.append("no JSDoc comment: the intended behaviour will be inferred from the code and name")
        if not all(p.annotation for p in params) and self.name == "javascript":
            info.warnings.append("some parameters have no JSDoc @param type: types will be inferred")
        return info

    # -- mutation and fixes ------------------------------------------------------------------------
    def generate_mutants(self, info: FunctionInfo, *, max_mutants: int = 40, seed: int = 0) -> list[Mutant]:
        return generate_mutants(info, max_mutants=max_mutants, seed=seed)

    def splice_function(self, info: FunctionInfo, new_function_source: str) -> str:
        return _splice_lines(info, new_function_source)

    def extract_class(self, path: Path, name: str) -> ComponentInfo:
        path = Path(path)
        mod = _module(path)
        cd = next((c for c in mod.classes if c.name == name), None)
        if cd is None:
            raise ExtractError(f"class {name!r} not found in {path} (classes: {', '.join(c.name for c in mod.classes) or 'none'})")
        ctor = next((m for m in cd.members if m.member == "constructor"), None)
        if ctor is not None and any(p.rest or p.pattern for p in ctor.params):
            raise ExtractError("constructors with rest or destructured parameters are not supported yet")
        params = self._params(ctor) if ctor else []
        methods, warnings = [], []
        for m in cd.members:
            short = m.member or ""
            if m.is_static or short == "constructor" or short.startswith(("_", "#")) or m.kind == "setter":
                continue
            if m.is_async or m.is_generator or any(p.rest or p.pattern for p in m.params):
                warnings.append(f"method {short} is async, a generator, or has rest/destructured parameters: not modelled")
                continue
            doc_params, doc_ret = jsdoc_types(m.jsdoc)
            methods.append(MethodInfo(name=short, params=self._params(m), returns=m.returns or doc_ret, docstring=m.jsdoc,
                                      kind="property" if m.kind == "getter" else "method"))
        if not methods:
            raise ExtractError(f"class {name} has no public methods to model")
        src = mod.src
        start_char = mod.all_toks[cd.jsdoc_tok].start if cd.jsdoc_tok is not None else mod.toks[cd.start_tok].start
        end_char = mod.toks[cd.end_tok].end
        line_start = src.rfind("\n", 0, start_char) + 1
        line_end = src.find("\n", end_char)
        info = ComponentInfo(
            path=path.resolve(), name=name, source=src[line_start: len(src) if line_end == -1 else line_end], module_source=src,
            lineno=src.count("\n", 0, start_char) + 1, end_lineno=src.count("\n", 0, end_char) + 1,
            col_offset=start_char - line_start, params=params, returns=None, docstring=cd.jsdoc, language=self.name,
            methods=methods, warnings=warnings,
        )
        info.context = "\n\n".join(mod.text(st.start, st.end) for st in mod.statements if st.is_import)
        return info

    def _params(self, decl) -> list[PyParam]:
        doc_params, _ = jsdoc_types(decl.jsdoc)
        out, used = [], set()
        for p in decl.params:
            ann = p.annotation or doc_params.get(p.name)
            if p.optional and ann and "undefined" not in ann:
                ann = f"{ann} | undefined"
            lean_name = lean_binder(p.name)
            while lean_name in used:
                lean_name += "'"
            used.add(lean_name)
            out.append(PyParam(p.name, ann, lean_name, has_default=p.has_default))
        return out

    def generate_class_mutants(self, info, *, max_mutants: int = 40, seed: int = 0) -> list[Mutant]:
        return generate_class_mutants(info, max_mutants=max_mutants, seed=seed)

    # -- runtime ----------------------------------------------------------------------------------
    def runtime(self, info: FunctionInfo, cfg) -> Runtime:
        node, why = _find_node(info.path, cfg.node)
        version = _node_version(node)
        if version is None:
            raise RuntimeEnvError(f"could not run Node.js at {node} ({why})")
        need = MIN_NODE_TS if self.name == "typescript" else MIN_NODE
        vstr = ".".join(map(str, version))
        if version[:2] < need:
            what = "TypeScript (built-in type stripping)" if self.name == "typescript" else "JavaScript"
            raise RuntimeEnvError(
                f"{what} needs Node.js >= {need[0]}.{need[1]}; {node} ({why}) is {vstr}. "
                "Install a newer Node, or point Larch at one with `--node PATH` (or `node = \"PATH\"` in .larch.toml)."
            )
        flags = _node_flags(node, typescript=self.name == "typescript")
        decl_params = [p for p in info.params]
        kinds = [_arg_kind(p.annotation) for p in decl_params]
        method_kinds = {m.name: [_arg_kind(p.annotation) for p in m.params] for m in getattr(info, "methods", [])}
        every = kinds + [k for ks in method_kinds.values() for k in ks]
        bigint_only = all(k["bigint"] for k in every if k["numeric"]) and any(k["numeric"] for k in every)
        binding, _, member = info.name.partition(".")
        return Runtime(
            language=self.name,
            display=f"Node {vstr}" + (" (TypeScript)" if self.name == "typescript" else ""),
            executable=node,
            source=why,
            cmd=[node, *flags, str(ADAPTER)],
            env={},
            load={"path": str(info.path), "function": info.name, "binding": binding, "member": member or None,
                  "arg_kinds": [{"bigint": k["bigint"], "undef": k["undef"]} for k in kinds],
                  "method_kinds": {m: [{"bigint": k["bigint"], "undef": k["undef"]} for k in ks] for m, ks in method_kinds.items()}},
            int_bound=None if bigint_only else SAFE_INT,
        )

    def explain_load_error(self, rt: Runtime, info: FunctionInfo, err: dict) -> str:
        error = str(err.get("error", "")).strip()
        where = f"{rt.display} at {rt.executable} (chosen: {rt.source})"
        missing = err.get("missing")
        if missing:
            if missing.startswith(("@/", "~/", "#")) or _tsconfig_alias(info.path, missing):
                return (
                    f"{info.path.name} imports `{missing}` through a path alias (tsconfig `paths` or "
                    "package.json `imports`), which plain Node cannot resolve. Use a relative import for this "
                    "module, or verify a function whose imports are relative or installed packages."
                )
            if missing.startswith((".", "/")):
                return f"{info.path.name} imports `{missing}`, which does not exist relative to the file ({error})."
            root = project_root(info.path)
            return (
                f"{info.path.name} imports `{missing}`, which is not installed (no node_modules/{missing} is reachable "
                f"from {info.path.parent}). Install the project's dependencies (e.g. `npm ci`, `pnpm install` or "
                f"`yarn install` in {root}) and re-run."
            )
        if "SyntaxError" in error or "ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX" in error or "ERR_INVALID_TYPESCRIPT_SYNTAX" in error:
            hint = ""
            if self.name == "typescript":
                hint = ("\nNode's built-in TypeScript support strips types without type-checking; it does not support "
                        "every TypeScript feature (e.g. legacy decorators or `import x = require()`).")
            return f"{info.path.name} could not be loaded by {where}: {error}{hint}"
        return (
            f"loading {info.path.name} with {where} failed: {error}\n"
            "Larch imports the module like `import` does, so top-level code must run without services, "
            "files or environment variables it cannot reach."
        )

    def fix_constraints(self, rt: Runtime) -> str:
        lang = "TypeScript" if self.name == "typescript" else "JavaScript"
        return (
            f"The fixed code runs on {rt.display}. Write {lang} the file already uses, keep the same module "
            "syntax (import/export or require), and do not add imports of packages the file does not already use."
        )


class TypeScriptLanguage(JavaScriptLanguage):
    name = "typescript"
    display = "TypeScript"
    fence = "ts"
    extensions = (".ts", ".mts", ".cts")


# ---------------------------------------------------------------------------
# Runtime helpers
# ---------------------------------------------------------------------------

def _find_node(path: Path, configured: str | None) -> tuple[str, str]:
    if configured:
        p = Path(configured).expanduser()
        exe = str(p) if p.exists() else shutil.which(configured)
        if not exe:
            raise RuntimeEnvError(f"the configured Node.js {configured!r} was not found")
        return exe, "configured"
    env = os.environ.get("LARCH_NODE")
    if env:
        return env, "$LARCH_NODE"
    # Project-pinned Node via a version manager's shim on PATH is honoured implicitly.
    exe = shutil.which("node")
    if exe:
        return exe, "`node` on PATH"
    raise RuntimeEnvError(
        "Node.js was not found on PATH. Install Node.js (>= 22.6 for TypeScript), or point Larch at it "
        "with `--node PATH` or `node = \"PATH\"` in .larch.toml."
    )


_version_cache: dict[str, tuple[int, int, int] | None] = {}
_flags_cache: dict[tuple[str, bool], list[str]] = {}


def _node_version(node: str) -> tuple[int, int, int] | None:
    if node not in _version_cache:
        try:
            r = subprocess.run([node, "-p", "process.versions.node"], capture_output=True, text=True, timeout=30,
                               stdin=subprocess.DEVNULL)
            parts = r.stdout.strip().split(".") if r.returncode == 0 else []
            _version_cache[node] = tuple(int(x) for x in parts[:3]) if len(parts) >= 2 else None  # type: ignore[assignment]
        except (OSError, subprocess.TimeoutExpired, ValueError):
            _version_cache[node] = None
    return _version_cache[node]


def _node_accepts(node: str, flags: list[str]) -> bool:
    try:
        r = subprocess.run([node, *flags, "-e", "0"], capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _node_flags(node: str, *, typescript: bool) -> list[str]:
    key = (node, typescript)
    if key in _flags_cache:
        return _flags_cache[key]
    flags: list[str] = []
    for f in ("--disable-warning=ExperimentalWarning", "--disable-warning=MODULE_TYPELESS_PACKAGE_JSON"):
        if _node_accepts(node, [f]):
            flags.append(f)
    if typescript:
        for f in ("--experimental-transform-types", "--experimental-strip-types"):
            if _node_accepts(node, [f]):
                flags.append(f)
                break
    _flags_cache[key] = flags
    return flags


def _arg_kind(annotation: str | None) -> dict:
    a = annotation or ""
    return {
        "bigint": "bigint" in a and "number" not in a,
        "undef": ("undefined" in a or a.endswith("?")) and "null" not in a,
        "numeric": "bigint" in a or "number" in a or not a,
    }


def _tsconfig_alias(path: Path, spec: str) -> bool:
    d = path.resolve().parent
    top = repo_root(path)
    for cand in [d, *d.parents]:
        cfg = cand / "tsconfig.json"
        if cfg.exists():
            text = re.sub(r"//[^\n]*|/\*.*?\*/", "", cfg.read_text(errors="replace"), flags=re.S)
            try:
                paths = json.loads(re.sub(r",(\s*[}\]])", r"\1", text)).get("compilerOptions", {}).get("paths", {})
            except json.JSONDecodeError:
                return False
            return any(spec.startswith(k.rstrip("*")) for k in paths)
        if cand == top:
            break
    return False


# ---------------------------------------------------------------------------
# Context for the formalizer
# ---------------------------------------------------------------------------

def _context(mod: Module, decl: FunctionDecl) -> str:
    """Imports, and top-level declarations the function (transitively) refers to."""
    by_name = {}
    for st in mod.statements:
        for n in st.names:
            by_name.setdefault(n, st)
    own = next((st for st in mod.statements if st.start <= decl.start_tok <= st.end), None)
    wanted: list = []
    seen = {id(own)}
    frontier = [(decl.start_tok, decl.end_tok)]
    depth = 0
    while frontier and depth < 3:
        nxt = []
        for a, b in frontier:
            for t in mod.toks[a:b + 1]:
                if t.kind == "id" and t.text in by_name:
                    st = by_name[t.text]
                    if id(st) not in seen:
                        seen.add(id(st))
                        wanted.append(st)
                        nxt.append((st.start, st.end))
        frontier = nxt
        depth += 1
    parts = [mod.text(st.start, st.end) for st in mod.statements if st.is_import]
    for st in sorted(wanted, key=lambda s: s.start):
        seg = mod.text(st.start, st.end)
        parts.append(seg if len(seg) <= 4000 else seg[:4000] + "\n// … (truncated)")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Mutation operators (token level, body only)
# ---------------------------------------------------------------------------

_ROR = {"<": ["<=", ">"], "<=": ["<", ">="], ">": [">=", "<"], ">=": [">", "<="],
        "===": ["!=="], "!==": ["==="], "==": ["!="], "!=": ["=="]}
_AOR = {"+": ["-"], "-": ["+"], "*": ["+", "/"], "/": ["*"], "%": ["/"], "**": ["*"],
        "&": ["|"], "|": ["&"], "^": ["|"], "<<": [">>"], ">>": ["<<"], ">>>": [">>"]}
_LCR = {"&&": ["||"], "||": ["&&"], "??": ["||"]}
_ASG = {"+=": ["-="], "-=": ["+="], "++": ["--"], "--": ["++"], "*=": ["+="]}
_CALL = {"floor": "ceil", "ceil": "floor", "min": "max", "max": "min", "trunc": "round", "round": "trunc"}


_TYPE_WORDS = {"number", "string", "boolean", "bigint", "null", "undefined", "void", "never", "unknown", "any", "object"}


def _typeish(prev, nxt) -> bool:
    """`|`/`&` between type names is a union/intersection type, not a bitwise operator."""
    return any(t is not None and t.kind == "id" and (t.text in _TYPE_WORDS or t.text[:1].isupper()) for t in (prev, nxt))


def _sites(info: FunctionInfo, mod: Module, decl: FunctionDecl) -> list[tuple[str, str, int, int, int, str]]:
    """(operator, description, line, start, end, replacement) over the function body."""
    out = []
    src = mod.src
    toks = mod.toks
    for i in range(decl.body_start_tok, decl.end_tok + 1):
        t = toks[i]
        prev = toks[i - 1] if i > 0 else None
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if t.kind == "punct":
            spaced = t.start > 0 and src[t.start - 1] in " \t" and t.end < len(src) and src[t.end] in " \t\n"
            if t.text in _ROR and (t.text not in ("<", ">") or spaced):
                for r in _ROR[t.text]:
                    out.append(("ROR", f"`{t.text}` → `{r}`", t.line, t.start, t.end, r))
            elif t.text in _AOR and spaced and not (t.text in ("|", "&") and _typeish(prev, nxt)):
                for r in _AOR[t.text]:
                    out.append(("AOR", f"`{t.text}` → `{r}`", t.line, t.start, t.end, r))
            elif t.text in _LCR:
                for r in _LCR[t.text]:
                    out.append(("LCR", f"`{t.text}` → `{r}`", t.line, t.start, t.end, r))
            elif t.text in _ASG:
                for r in _ASG[t.text]:
                    out.append(("ASR", f"`{t.text}` → `{r}`", t.line, t.start, t.end, r))
            elif t.text == "!" and nxt is not None and nxt.text not in ("=", "=="):
                if prev is None or prev.kind == "punct" and prev.text not in (")", "]"):
                    out.append(("UOI", "remove `!`", t.line, t.start, t.end, ""))
            elif t.text == "-" and not spaced and prev is not None and prev.kind == "punct" and prev.text not in (")", "]"):
                out.append(("UOI", "remove unary `-`", t.line, t.start, t.end, ""))
        elif t.kind == "num" and not t.text.startswith("0x") and re.fullmatch(r"\d+n?", t.text):
            big = t.text.endswith("n")
            v = int(t.text.rstrip("n"))
            for r in sorted({v + 1, v - 1 if v > 0 else 1} - {v}):
                rep = f"{r}{'n' if big else ''}"
                out.append(("CRP", f"constant {t.text} → {rep}", t.line, t.start, t.end, rep))
        elif t.kind == "id":
            if t.text in ("true", "false"):
                r = "false" if t.text == "true" else "true"
                out.append(("CRP", f"`{t.text}` → `{r}`", t.line, t.start, t.end, r))
            elif t.text in _CALL and prev is not None and prev.text == "." and i >= 2 and toks[i - 2].text == "Math":
                r = _CALL[t.text]
                out.append(("CALL", f"`Math.{t.text}` → `Math.{r}`", t.line, t.start, t.end, r))
            elif t.text == "length" and prev is not None and prev.text == "." and (nxt is None or nxt.text != "="):
                out.append(("OBO", "`.length` → `.length - 1`", t.line, t.start, t.end, "length - 1"))
    return out


def generate_mutants(info: FunctionInfo, *, max_mutants: int = 40, seed: int = 0) -> list[Mutant]:
    mod = Module(info.module_source)
    decl = next((f for f in mod.functions if f.name == info.name), None)
    if decl is None:
        return []
    return _pick(mod, _sites(info, mod, decl), decl.start_tok, decl.end_tok, decl.jsdoc_tok, max_mutants, seed)


def generate_class_mutants(info: ComponentInfo, *, max_mutants: int = 40, seed: int = 0) -> list[Mutant]:
    """Mutants of every method body of a class (signatures and types are left alone)."""
    mod = Module(info.module_source)
    cd = next((c for c in mod.classes if c.name == info.name), None)
    if cd is None:
        return []
    sites = []
    for m in cd.members:
        if not m.is_static:
            sites += _sites(info, mod, m)
    return _pick(mod, sites, cd.start_tok, cd.end_tok, cd.jsdoc_tok, max_mutants, seed)


def _pick(mod: Module, sites: list, start_tok: int, end_tok: int, jsdoc_tok, max_mutants: int, seed: int) -> list[Mutant]:
    src = mod.src
    fn_start = src.rfind("\n", 0, mod.toks[start_tok].start) + 1
    if jsdoc_tok is not None:
        fn_start = src.rfind("\n", 0, mod.all_toks[jsdoc_tok].start) + 1
    fn_end = src.find("\n", mod.toks[end_tok].end)
    fn_end = len(src) if fn_end == -1 else fn_end
    by_op: dict[str, list[Mutant]] = {}
    seen: set[str] = set()
    for n, (op, desc, line, a, b, rep) in enumerate(sites):
        new_src = src[:a] + rep + src[b:]
        if new_src in seen:
            continue
        seen.add(new_src)
        delta = len(rep) - (b - a)
        m = Mutant(
            id=f"m{n + 1}", operator=op, description=f"line {line}: {desc}", lineno=line,
            function_source=new_src[fn_start:fn_end + delta], module_source=new_src,
        )
        by_op.setdefault(op, []).append(m)
    rng = random.Random(seed)
    pools = {k: v[:] for k, v in sorted(by_op.items())}
    for v in pools.values():
        rng.shuffle(v)
    chosen: list[Mutant] = []
    while len(chosen) < max_mutants and any(pools.values()):
        for k in list(pools):
            if pools[k] and len(chosen) < max_mutants:
                chosen.append(pools[k].pop())
    chosen.sort(key=lambda m: (m.lineno, m.id))
    return chosen


JS = JavaScriptLanguage()
TS = TypeScriptLanguage()

