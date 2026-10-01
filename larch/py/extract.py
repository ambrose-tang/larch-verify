"""Find a Python function and the context the model writer needs to understand it."""
from __future__ import annotations

import ast
from pathlib import Path

from ..lang import ExtractError, FunctionInfo, PyParam
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
