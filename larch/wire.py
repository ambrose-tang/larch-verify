"""Values crossing the boundary between Larch's driver and a language adapter.

The driver generates inputs and checks results in Larch's own Python; the code under
test runs in its own runtime (the project's Python interpreter, Node, ...), behind a
small adapter process. Values travel as JSON with a few tagged forms for what plain
JSON cannot say:

    {"$tuple": [...]}           tuple (Python), fixed-length array
    {"$int": "123"}             integer that does not fit a JSON number losslessly
    {"$float": "0.1"}           non-integral number
    {"$dict": [[k, v], ...]}    mapping
    {"$repr": "...", "$type": "Name"}   anything else (shown, never equal to a model value)

Plain JSON numbers, strings, booleans, null and arrays mean themselves.
Adapters (larch/py/adapter.py, larch/js/adapter.mjs) implement the same format.
"""
from __future__ import annotations

SAFE_INT = 2**53 - 1  # largest integer every JSON parser reads exactly


class Opaque:
    """A value the adapter could only describe (an object, a set, ...)."""

    __slots__ = ("type_name", "text")

    def __init__(self, type_name: str, text: str):
        self.type_name = type_name
        self.text = text

    def __repr__(self) -> str:
        return self.text

    def __eq__(self, other) -> bool:
        return isinstance(other, Opaque) and other.text == self.text and other.type_name == self.type_name

    def __hash__(self) -> int:
        return hash((self.type_name, self.text))


def to_wire(v):
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return v if -SAFE_INT <= v <= SAFE_INT else {"$int": str(v)}
    if isinstance(v, float):
        return {"$float": repr(v)}
    if isinstance(v, tuple):
        return {"$tuple": [to_wire(x) for x in v]}
    if isinstance(v, list):
        return [to_wire(x) for x in v]
    if isinstance(v, dict):
        return {"$dict": [[to_wire(k), to_wire(x)] for k, x in v.items()]}
    if isinstance(v, Opaque):
        return {"$repr": v.text, "$type": v.type_name}
    return {"$repr": repr(v), "$type": type(v).__name__}


def from_wire(j):
    if isinstance(j, list):
        return [from_wire(x) for x in j]
    if not isinstance(j, dict):
        return j
    if "$tuple" in j:
        return tuple(from_wire(x) for x in j["$tuple"])
    if "$int" in j:
        return int(j["$int"])
    if "$float" in j:
        return float(j["$float"])
    if "$dict" in j:
        out = {}
        for k, v in j["$dict"]:
            k = from_wire(k)
            try:
                out[k] = from_wire(v)
            except TypeError:  # unhashable key
                return Opaque("dict", repr(j["$dict"]))
        return out
    if "$repr" in j:
        return Opaque(str(j.get("$type", "object")), str(j["$repr"]))
    return Opaque("object", repr(j))
