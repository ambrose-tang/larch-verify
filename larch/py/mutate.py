"""AST mutation operators for the target function (spec/test adequacy checking).

A mutant is a small, plausible bug (flipped comparison, off-by-one, wrong operator,
dropped statement). If neither differential testing against the verified model nor
the approved postconditions notice a mutant, the specs or tests are too weak.
"""
from __future__ import annotations

import ast
import copy
import random
from typing import Callable

from ..lang import FunctionInfo, Mutant
from .extract import splice_function


_ROR = {
    ast.Lt: [ast.LtE, ast.Gt],
    ast.LtE: [ast.Lt, ast.GtE],
    ast.Gt: [ast.GtE, ast.Lt],
    ast.GtE: [ast.Gt, ast.LtE],
    ast.Eq: [ast.NotEq],
    ast.NotEq: [ast.Eq],
    ast.In: [ast.NotIn],
    ast.NotIn: [ast.In],
    ast.Is: [ast.IsNot],
    ast.IsNot: [ast.Is],
}
_AOR = {
    ast.Add: [ast.Sub],
    ast.Sub: [ast.Add],
    ast.Mult: [ast.Add, ast.FloorDiv],
    ast.FloorDiv: [ast.Mult, ast.Mod],
    ast.Div: [ast.Mult],
    ast.Mod: [ast.FloorDiv],
    ast.Pow: [ast.Mult],
    ast.BitAnd: [ast.BitOr],
    ast.BitOr: [ast.BitAnd],
    ast.BitXor: [ast.BitOr],
    ast.LShift: [ast.RShift],
    ast.RShift: [ast.LShift],
}
_OPSYM = {
    ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!=",
    ast.In: "in", ast.NotIn: "not in", ast.Is: "is", ast.IsNot: "is not",
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.FloorDiv: "//", ast.Div: "/", ast.Mod: "%",
    ast.Pow: "**", ast.BitAnd: "&", ast.BitOr: "|", ast.BitXor: "^", ast.LShift: "<<", ast.RShift: ">>",
    ast.And: "and", ast.Or: "or",
}

# A mutation site: (operator name, description, lineno, function that mutates the
# node at a given traversal index inside a *copy* of the function).
Site = tuple[str, str, int, int, Callable[[ast.AST], ast.AST | None]]


def _nodes(fn: ast.AST) -> list[ast.AST]:
    return list(ast.walk(fn))


def _sites(fn: ast.FunctionDef) -> list[Site]:
    sites: list[Site] = []
    docstring = fn.body[0] if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(getattr(fn.body[0], "value", None), ast.Constant) and isinstance(fn.body[0].value.value, str) else None
    skip: set[int] = set()
    if docstring is not None:
        skip = {id(n) for n in ast.walk(docstring)}
    for idx, node in enumerate(_nodes(fn)):
        if id(node) in skip:
            continue
        line = getattr(node, "lineno", fn.lineno)
        if isinstance(node, ast.Compare):
            for k, op in enumerate(node.ops):
                for repl in _ROR.get(type(op), []):
                    def mut(n, k=k, repl=repl):
                        n.ops[k] = repl()
                        return n
                    sites.append(("ROR", f"`{_OPSYM[type(op)]}` → `{_OPSYM[repl]}`", line, idx, mut))
        elif isinstance(node, ast.BinOp) and type(node.op) in _AOR:
            for repl in _AOR[type(node.op)]:
                def mut(n, repl=repl):
                    n.op = repl()
                    return n
                sites.append(("AOR", f"`{_OPSYM[type(node.op)]}` → `{_OPSYM[repl]}`", line, idx, mut))
        elif isinstance(node, ast.AugAssign) and type(node.op) in _AOR:
            for repl in _AOR[type(node.op)][:1]:
                def mut(n, repl=repl):
                    n.op = repl()
                    return n
                sites.append(("AOR", f"`{_OPSYM[type(node.op)]}=` → `{_OPSYM[repl]}=`", line, idx, mut))
            def drop(n):
                return ast.Pass(lineno=n.lineno, col_offset=n.col_offset)
            sites.append(("SDL", "delete `" + _short(node) + "`", line, idx, drop))
        elif isinstance(node, ast.BoolOp):
            repl = ast.Or if isinstance(node.op, ast.And) else ast.And
            def mut(n, repl=repl):
                n.op = repl()
                return n
            sites.append(("LCR", f"`{_OPSYM[type(node.op)]}` → `{_OPSYM[repl]}`", line, idx, mut))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            sites.append(("UOI", "drop `not`", line, idx, lambda n: n.operand))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            sites.append(("UOI", "drop unary `-`", line, idx, lambda n: n.operand))
        elif isinstance(node, ast.Constant) and type(node.value) is int:
            v = node.value
            for nv in sorted({v + 1, v - 1} - {v}):
                def mut(n, nv=nv):
                    return ast.Constant(value=nv)
                sites.append(("CRP", f"constant {v} → {nv}", line, idx, mut))
        elif isinstance(node, ast.Constant) and type(node.value) is bool:
            def mut(n):
                return ast.Constant(value=not n.value)
            sites.append(("CRP", f"`{node.value}` → `{not node.value}`", line, idx, mut))
        elif isinstance(node, (ast.If, ast.While)) and not isinstance(node.test, ast.Constant):
            kw = "if" if isinstance(node, ast.If) else "while"
            if kw == "if":
                def mut(n):
                    n.test = ast.UnaryOp(op=ast.Not(), operand=n.test)
                    return n
                sites.append(("NEG", f"negate `{kw}` condition", line, idx, mut))
        elif isinstance(node, (ast.Break, ast.Continue)):
            sites.append(("SDL", f"delete `{type(node).__name__.lower()}`", line, idx, lambda n: ast.Pass()))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            fname = node.func.id
            if fname in ("min", "max"):
                other = "max" if fname == "min" else "min"
                def mut(n, other=other):
                    n.func = ast.Name(id=other, ctx=ast.Load())
                    return n
                sites.append(("CALL", f"`{fname}` → `{other}`", line, idx, mut))
            elif fname == "range" and node.args:
                last = len(node.args) - 1 if len(node.args) <= 2 else 1
                for delta in (1, -1):
                    def mut(n, delta=delta, last=last):
                        n.args[last] = ast.BinOp(left=n.args[last], op=ast.Add() if delta > 0 else ast.Sub(), right=ast.Constant(1))
                        return n
                    sites.append(("OBO", f"range end {'+' if delta > 0 else '-'} 1", line, idx, mut))
                if len(node.args) >= 2:
                    def mut(n):
                        n.args[0] = ast.BinOp(left=n.args[0], op=ast.Add(), right=ast.Constant(1))
                        return n
                    sites.append(("OBO", "range start + 1", line, idx, mut))
            elif fname == "len":
                def mut(n):
                    return ast.BinOp(left=n, op=ast.Sub(), right=ast.Constant(1))
                sites.append(("OBO", "`len(…)` → `len(…) - 1`", line, idx, mut))
    return sites


def _short(node: ast.AST, limit: int = 40) -> str:
    try:
        s = ast.unparse(node)
    except Exception:  # pragma: no cover
        return type(node).__name__
    return s if len(s) <= limit else s[: limit - 1] + "…"


class _Replace(ast.NodeTransformer):
    def __init__(self, target: ast.AST, fn: Callable[[ast.AST], ast.AST | None]):
        self.target = target
        self.fn = fn

    def generic_visit(self, node):
        return super().generic_visit(node)

    def visit(self, node):
        if node is self.target:
            return self.fn(node)
        return super().visit(node)


def generate_mutants(info: FunctionInfo, *, max_mutants: int = 40, seed: int = 0) -> list[Mutant]:
    tree = ast.parse(info.source.strip("\n") if info.col_offset == 0 else _dedent(info.source))
    fn = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    original = ast.unparse(fn)
    sites = _sites(fn)
    seen = {original}
    by_op: dict[str, list[Mutant]] = {}
    for n, (op, desc, line, idx, mut) in enumerate(sites):
        fcopy = copy.deepcopy(fn)
        target = _nodes(fcopy)[idx]
        new_fn = _Replace(target, mut).visit(fcopy)
        if new_fn is None:
            continue
        ast.fix_missing_locations(new_fn)
        try:
            text = ast.unparse(new_fn)
            compile(text, "<mutant>", "exec")
        except Exception:  # noqa: BLE001 - some mutations produce invalid code
            continue
        if text in seen:
            continue
        seen.add(text)
        abs_line = info.lineno + line - 1
        m = Mutant(
            id=f"m{n + 1}",
            operator=op,
            description=f"line {abs_line}: {desc}",
            lineno=abs_line,
            function_source=text,
            module_source=splice_function(info, text),
        )
        by_op.setdefault(op, []).append(m)
    # Stratified, deterministic sample: round-robin over operators.
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


def _dedent(src: str) -> str:
    import textwrap

    return textwrap.dedent(src)
