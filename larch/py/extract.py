"""Find a Python function and the context the model writer needs to understand it."""
from __future__ import annotations

import ast
from pathlib import Path

from ..lang import ComponentInfo, ExtractError, FunctionInfo, MethodInfo, PyParam
from ..spec import lean_binder


__all__ = ["ExtractError", "FunctionInfo", "PyParam", "extract", "list_functions", "splice_function"]


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    out: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{node.name}.{sub.name}"] = sub
    return out


def list_functions(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names = []
    for name, node in _functions(tree).items():
        if isinstance(node, ast.AsyncFunctionDef) or name.split(".")[-1].startswith("_"):
            continue
        if "." in name and not _is_static(node):
            continue
        names.append(name)
    return names


def _is_static(node: ast.AST) -> bool:
    return any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in getattr(node, "decorator_list", []))


def _segment(src_lines: list[str], node: ast.AST) -> str:
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return "\n".join(src_lines[start - 1 : node.end_lineno])


def extract(path: Path, name: str) -> FunctionInfo:
    path = Path(path)
    if not path.exists():
        raise ExtractError(f"{path} does not exist")
    source = path.read_text()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        raise ExtractError(f"cannot parse {path}: {e}") from e
    funcs = _functions(tree)
    node = funcs.get(name)
    if node is None:
        avail = ", ".join(sorted(funcs)) or "none"
        raise ExtractError(f"function {name!r} not found in {path} (available: {avail})")
    if isinstance(node, ast.AsyncFunctionDef):
        raise ExtractError("async functions are not supported yet")
    if "." in name and not _is_static(node):
        raise ExtractError("only module-level functions and @staticmethods are supported (methods need an instance)")
    if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(node)):
        raise ExtractError("generator functions are not supported yet")
    a = node.args
    if a.vararg or a.kwarg or a.kwonlyargs:
        raise ExtractError("functions with *args, **kwargs or keyword-only parameters are not supported yet")
    lines = source.splitlines()
    positional = list(a.posonlyargs) + list(a.args)
    n_defaults = len(a.defaults)
    params = []
    used: set[str] = set()
    for i, arg in enumerate(positional):
        ann = ast.unparse(arg.annotation) if arg.annotation is not None else None
        lean_name = lean_binder(arg.arg)
        while lean_name in used:
            lean_name += "'"
        used.add(lean_name)
        params.append(PyParam(arg.arg, ann, lean_name, has_default=i >= len(positional) - n_defaults))
    info = FunctionInfo(
        path=path.resolve(),
        name=name,
        source=_segment(lines, node),
        module_source=source,
        lineno=min([node.lineno] + [d.lineno for d in node.decorator_list]),
        end_lineno=node.end_lineno or node.lineno,
        col_offset=node.col_offset,
        params=params,
        returns=ast.unparse(node.returns) if node.returns is not None else None,
        docstring=ast.get_docstring(node),
    )
    info.context = _context(tree, lines, node, funcs)
    if not info.docstring:
        info.warnings.append("no docstring: the intended behaviour will be inferred from the code and name")
    return info


def _context(tree: ast.Module, lines: list[str], target: ast.AST, funcs: dict) -> str:
    """Imports, and module-level definitions the target (transitively) refers to."""
    defs: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    defs[t.id] = node
    wanted: list[ast.AST] = []
    seen: set[int] = {id(target)}
    frontier = [target]
    depth = 0
    while frontier and depth < 3:
        nxt = []
        for n in frontier:
            for sub in ast.walk(n):
                if isinstance(sub, ast.Name) and sub.id in defs:
                    d = defs[sub.id]
                    if id(d) not in seen:
                        seen.add(id(d))
                        wanted.append(d)
                        nxt.append(d)
        frontier = nxt
        depth += 1
    imports = [ast.get_source_segment("\n".join(lines), n) or "" for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    parts = [i for i in imports if i]
    for d in sorted(wanted, key=lambda n: n.lineno):
        seg = _segment(lines, d)
        if len(seg) > 4000:
            seg = seg[:4000] + "\n# … (truncated)"
        parts.append(seg)
    return "\n\n".join(parts)


def splice_function(info: FunctionInfo, new_function_source: str) -> str:
    """Return the module source with the target function replaced."""
    lines = info.module_source.splitlines(keepends=True)
    indent = " " * info.col_offset
    body = new_function_source.strip("\n").splitlines()
    # Re-indent (methods) based on the first line's indentation.
    first_indent = len(body[0]) - len(body[0].lstrip()) if body else 0
    fixed = [indent + ln[first_indent:] if ln.strip() else "" for ln in body]
    new = "\n".join(fixed) + "\n"
    return "".join(lines[: info.lineno - 1]) + new + "".join(lines[info.end_lineno :])


# ---------------------------------------------------------------------------
# Classes (stateful components)
# ---------------------------------------------------------------------------

def _decorators(node) -> set[str]:
    out = set()
    for d in getattr(node, "decorator_list", []):
        if isinstance(d, ast.Name):
            out.add(d.id)
        elif isinstance(d, ast.Attribute):
            out.add(d.attr)
        elif isinstance(d, ast.Call):
            f = d.func
            out.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    return out


def _params_of(fn: ast.FunctionDef, used: set[str], skip_self: bool = True) -> list[PyParam]:
    a = fn.args
    positional = list(a.posonlyargs) + list(a.args)
    if skip_self and positional:
        positional = positional[1:]
    n_defaults = len(a.defaults)
    out = []
    for i, arg in enumerate(positional):
        ann = ast.unparse(arg.annotation) if arg.annotation is not None else None
        lean_name = lean_binder(arg.arg)
        while lean_name in used:
            lean_name += "'"
        used.add(lean_name)
        out.append(PyParam(arg.arg, ann, lean_name, has_default=i >= len(positional) - n_defaults))
    return out


def list_classes(path: Path) -> list[str]:
    tree = ast.parse(Path(path).read_text(), filename=str(path))
    def is_exception(c: ast.ClassDef) -> bool:
        return any(ast.unparse(b).split(".")[-1].endswith(("Error", "Exception", "Warning")) for b in c.bases)

    return [n.name for n in tree.body if isinstance(n, ast.ClassDef) and not n.name.startswith("_") and not is_exception(n)]


def extract_class(path: Path, name: str) -> ComponentInfo:
    path = Path(path)
    source = path.read_text()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        raise ExtractError(f"cannot parse {path}: {e}") from e
    node = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name), None)
    if node is None:
        raise ExtractError(f"class {name!r} not found in {path}")
    lines = source.splitlines()
    used: set[str] = set()
    init = next((f for f in node.body if isinstance(f, ast.FunctionDef) and f.name == "__init__"), None)
    if init is not None:
        a = init.args
        if a.vararg or a.kwarg or a.kwonlyargs:
            raise ExtractError("constructors with *args, **kwargs or keyword-only parameters are not supported yet")
        params = _params_of(init, used)
    elif "dataclass" in _decorators(node):
        params = []
        for st in node.body:
            if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and "ClassVar" not in ast.unparse(st.annotation):
                lean_name = lean_binder(st.target.id)
                used.add(lean_name)
                params.append(PyParam(st.target.id, ast.unparse(st.annotation), lean_name, has_default=st.value is not None))
    else:
        params = []
    methods: list[MethodInfo] = []
    warnings: list[str] = []
    for f in node.body:
        if not isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) or f.name.startswith("_"):
            continue
        decs = _decorators(f)
        if decs & {"staticmethod", "classmethod"} or any(d.endswith("setter") for d in decs):
            continue
        if isinstance(f, ast.AsyncFunctionDef) or any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(f)):
            warnings.append(f"method {f.name} is async or a generator: not modelled")
            continue
        a = f.args
        if a.vararg or a.kwarg or a.kwonlyargs:
            warnings.append(f"method {f.name} has *args/**kwargs/keyword-only parameters: not modelled")
            continue
        kind = "property" if "property" in decs or "cached_property" in decs else "method"
        methods.append(MethodInfo(
            name=f.name, params=_params_of(f, set()), kind=kind,
            returns=ast.unparse(f.returns) if f.returns is not None else None, docstring=ast.get_docstring(f),
        ))
    if not methods:
        raise ExtractError(f"class {name} has no public methods to model")
    info = ComponentInfo(
        path=path.resolve(), name=name, source=_segment(lines, node), module_source=source,
        lineno=min([node.lineno] + [d.lineno for d in node.decorator_list]), end_lineno=node.end_lineno or node.lineno,
        col_offset=node.col_offset, params=params, returns=None, docstring=ast.get_docstring(node),
        methods=methods, warnings=warnings,
    )
    info.context = _context(tree, lines, node, _functions(tree))
    if not info.docstring and not any(m.docstring for m in methods):
        info.warnings.append("no docstrings: the intended behaviour will be inferred from the code and names")
    return info
