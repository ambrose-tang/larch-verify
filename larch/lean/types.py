"""The subset of Lean types that Larch can move across the Python <-> Lean boundary.

Supported: Int, Nat, Bool, String, Char, Unit, List T, Array T, Option T and
products A × B (right-nested, as Lean's JSON instances encode them).

This module is imported by the sandboxed worker process, so it depends only on the
standard library; hypothesis is imported lazily in `strategy_for`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SCALARS = {"Int", "Nat", "Bool", "String", "Char", "Unit"}
UNARY = {"List", "Array", "Option"}


class LeanTypeError(ValueError):
    pass


class EncodeError(ValueError):
    """A Python value does not fit the Lean type (e.g. -1 for Nat, None for Int)."""


@dataclass(frozen=True)
class LType:
    head: str
    args: tuple["LType", ...] = ()

    def __str__(self) -> str:
        return render(self)

    @property
    def is_prod(self) -> bool:
        return self.head == "Prod"

    def prod_components(self) -> list["LType"]:
        """Flatten a right-nested product A × (B × C) into [A, B, C]."""
        out: list[LType] = []
        t = self
        while t.head == "Prod":
            out.append(t.args[0])
            t = t.args[1]
        out.append(t)
        return out


def render(t: LType, top: bool = True) -> str:
    if t.head in SCALARS:
        return t.head
    if t.head in UNARY:
        inner = render(t.args[0], top=False)
        s = f"{t.head} {inner}"
        return s if top else f"({s})"
    if t.head == "Prod":
        left = render(t.args[0], top=False)
        right = render(t.args[1], top=True)  # × is right associative
        s = f"{left} × {right}"
        return s if top else f"({s})"
    raise LeanTypeError(t.head)


_TOKEN = re.compile(r"\s*(×|\(|\)|[A-Za-z_][A-Za-z0-9_.]*)")


def parse_type(src: str) -> LType:
    """Parse a Lean type expression from the supported subset."""
    text = src.strip()
    toks: list[str] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise LeanTypeError(f"cannot parse Lean type {src!r} near {text[pos:pos + 12]!r}")
        toks.append(m.group(1))
        pos = m.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
    i = 0

    def peek() -> str | None:
        return toks[i] if i < len(toks) else None

    def take() -> str:
        nonlocal i
        if i >= len(toks):
            raise LeanTypeError(f"unexpected end of type {src!r}")
        i += 1
        return toks[i - 1]

    def atom() -> LType:
        tok = take()
        if tok == "(":
            t = prod()
            if take() != ")":
                raise LeanTypeError(f"unbalanced parentheses in {src!r}")
            return t
        name = tok.split(".")[-1] if tok.startswith(("_root_.", "Std.", "Lean.")) else tok
        if name in SCALARS:
            return LType(name)
        if name in UNARY:
            return LType(name, (atom(),))
        raise LeanTypeError(
            f"type {tok!r} is not supported at the Python<->Lean boundary "
            f"(supported: {', '.join(sorted(SCALARS | UNARY))}, and products A × B)"
        )

    def prod() -> LType:
        left = atom()
        if peek() == "×":
            take()
            return LType("Prod", (left, prod()))
        return left

    t = prod()
    if i != len(toks):
        raise LeanTypeError(f"trailing tokens in type {src!r}: {toks[i:]}")
    return t


# ---------------------------------------------------------------------------
# Python value  <->  Lean JSON encoding
# ---------------------------------------------------------------------------

def encode(value, t: LType):
    """Encode a Python value as the JSON Lean's FromJson instance for `t` expects."""
    h = t.head
    if h in ("Int", "Nat"):
        if isinstance(value, bool):
            value = int(value)
        if not isinstance(value, int):
            raise EncodeError(f"expected int for {h}, got {type(value).__name__}")
        if h == "Nat" and value < 0:
            raise EncodeError(f"negative value {value} for Nat")
        return value
    if h == "Bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise EncodeError(f"expected bool, got {type(value).__name__}")
    if h == "String":
        if not isinstance(value, str):
            raise EncodeError(f"expected str, got {type(value).__name__}")
        return value
    if h == "Char":
        if not (isinstance(value, str) and len(value) == 1):
            raise EncodeError("expected a single character")
        return value
    if h == "Unit":
        return []
    if h in ("List", "Array"):
        if isinstance(value, (str, bytes, dict, set, frozenset)) or not hasattr(value, "__iter__"):
            raise EncodeError(f"expected a sequence for {h}, got {type(value).__name__}")
        return [encode(v, t.args[0]) for v in value]
    if h == "Option":
        if value is None:
            return None
        return encode(value, t.args[0])
    if h == "Prod":
        comps = t.prod_components()
        if not isinstance(value, (tuple, list)) or len(value) != len(comps):
            raise EncodeError(f"expected a {len(comps)}-tuple for {render(t)}")
        return _nest([encode(v, c) for v, c in zip(value, comps)])
    raise EncodeError(f"unsupported type {h}")


def _nest(items: list):
    if len(items) == 1:
        return items[0]
    return [items[0], _nest(items[1:])]


def decode(j, t: LType):
    """Decode Lean ToJson output into a Python value (tuples for products)."""
    h = t.head
    if h in ("Int", "Nat", "Bool", "String", "Char"):
        return j
    if h == "Unit":
        return None
    if h in ("List", "Array"):
        return [decode(v, t.args[0]) for v in j]
    if h == "Option":
        return None if j is None else decode(j, t.args[0])
    if h == "Prod":
        comps = t.prod_components()
        out = []
        cur = j
        for idx, c in enumerate(comps):
            if idx == len(comps) - 1:
                out.append(decode(cur, c))
            else:
                out.append(decode(cur[0], c))
                cur = cur[1]
        return tuple(out)
    raise LeanTypeError(h)


# ---------------------------------------------------------------------------
# Type-directed generators (hypothesis) and output perturbation
# ---------------------------------------------------------------------------

# Classic overflow/boundary values: machine-word edges and their neighbours.
_INT_BOUNDARIES = sorted({
    s * v for s in (1, -1)
    for b in (7, 8, 15, 16, 31, 32, 63, 64)
    for v in (2**b - 1, 2**b, 2**b + 1)
} | {0, 1, -1})


def strategy_for(t: LType, *, max_len: int = 10, int_bound: int | None = None):
    """Type-directed inputs. `int_bound` limits integer magnitude for runtimes whose
    numbers are not arbitrary-precision (JavaScript `number`: 2^53 - 1)."""
    from hypothesis import strategies as st

    h = t.head
    big = 2**64 if int_bound is None else int_bound
    bounds = _INT_BOUNDARIES if int_bound is None else sorted(
        {b for b in _INT_BOUNDARIES if abs(b) <= int_bound} | {int_bound, -int_bound, int_bound - 1, 1 - int_bound})
    if h == "Int":
        return st.one_of(
            st.integers(-12, 12),
            st.integers(-1000, 1000),
            st.integers(-big, big),
            st.sampled_from(bounds),
        )
    if h == "Nat":
        return st.one_of(
            st.integers(0, 12), st.integers(0, 1000), st.integers(0, big),
            st.sampled_from([b for b in bounds if b >= 0]),
        )
    if h == "Bool":
        return st.booleans()
    if h == "String":
        return st.one_of(
            st.text(alphabet="abcxyzABC 019-_.,", max_size=max_len),
            st.text(alphabet=st.characters(codec="utf-8"), max_size=max_len),
        )
    if h == "Char":
        return st.one_of(
            st.sampled_from(list("aZ0 -_.\n")),
            st.characters(codec="utf-8"),
        )
    if h == "Unit":
        return st.just(None)
    if h in ("List", "Array"):
        return st.lists(strategy_for(t.args[0], max_len=max(2, max_len // 2), int_bound=int_bound), max_size=max_len)
    if h == "Option":
        return st.one_of(st.none(), strategy_for(t.args[0], max_len=max_len, int_bound=int_bound))
    if h == "Prod":
        return st.tuples(*[strategy_for(c, max_len=max_len, int_bound=int_bound) for c in t.prod_components()])
    raise LeanTypeError(h)


def perturb(value, t: LType, rng) -> list:
    """Nearby-but-different values of type t, used to test that a postcondition
    actually constrains the output (a spec that accepts every perturbation of the
    correct answer is likely vacuous)."""
    h = t.head
    out: list = []
    if h in ("Int", "Nat"):
        cands = [value + 1, value - 1, -value, 0, value * 2 + 1]
        out = [c for c in cands if c != value and (h == "Int" or c >= 0)]
    elif h == "Bool":
        out = [not value]
    elif h == "String":
        out = [value + "a", value[:-1] if value else "x", value[::-1] if len(set(value)) > 1 else value + value + "b"]
        out = [s for s in out if s != value]
    elif h == "Char":
        out = [c for c in ("a", "b", " ") if c != value][:2]
    elif h in ("List", "Array"):
        seq = list(value)
        if seq:
            out.append(seq[:-1])
            out.append(seq[1:])
            out.append(seq + [seq[0]])
            if len(seq) > 1:
                out.append(list(reversed(seq)) if seq != list(reversed(seq)) else seq[1:] + seq[:1])
            i = rng.randrange(len(seq))
            for alt in perturb(seq[i], t.args[0], rng)[:2]:
                s2 = list(seq)
                s2[i] = alt
                out.append(s2)
        else:
            out.extend([v] for v in _sample_values(t.args[0], rng))
        out = [o for o in out if o != seq]
    elif h == "Option":
        if value is None:
            out = [v for v in _sample_values(t.args[0], rng)]
        else:
            out = [None] + perturb(value, t.args[0], rng)[:2]
    elif h == "Prod":
        comps = t.prod_components()
        for idx, c in enumerate(comps):
            for alt in perturb(value[idx], c, rng)[:2]:
                v2 = list(value)
                v2[idx] = alt
                out.append(tuple(v2))
    return out


def _sample_values(t: LType, rng) -> list:
    h = t.head
    if h in ("Int",):
        return [0, 1, -1, rng.randint(-50, 50)]
    if h == "Nat":
        return [0, 1, rng.randint(2, 50)]
    if h == "Bool":
        return [True, False]
    if h == "String":
        return ["", "a"]
    if h == "Char":
        return ["a"]
    if h in ("List", "Array"):
        return [[], [x] if (x := (_sample_values(t.args[0], rng) or [None])[0]) is not None else []]
    if h == "Option":
        return [None]
    if h == "Prod":
        comps = [(_sample_values(c, rng) or [None])[0] for c in t.prod_components()]
        return [tuple(comps)]
    return []
