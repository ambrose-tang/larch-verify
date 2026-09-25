"""The formal artifact for one function: executable Lean model + specs.

Every spec has the same shape, `spec_<name> : Prop := ∀ params, body`, where the
body is a *decidable* proposition. That uniformity is what lets one spec be
  * proved about the model (a theorem whose type is exactly `spec_<name>`),
  * tested on random inputs before any proof effort is spent, and
  * (for postconditions) evaluated on the real implementation's outputs, so a
    violation is a spec-level bug report rather than merely "differs from the model".
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .lean.types import LType, parse_type

LEAN_RESERVED = {
    "fun", "at", "by", "from", "have", "show", "in", "do", "then", "else", "if", "match",
    "with", "end", "open", "let", "where", "for", "unless", "return", "try", "catch",
    "finally", "mut", "structure", "class", "instance", "def", "theorem", "namespace",
    "section", "variable", "universe", "deriving", "lemma", "Type", "Prop", "Sort",
    "forall", "exists", "calc", "suffices", "obtain", "this", "example", "abbrev",
    "inductive", "axiom", "import", "private", "protected", "noncomputable", "partial",
    "unsafe", "macro", "syntax", "notation", "local", "scoped", "attribute", "set_option",
    "mutual", "termination_by", "decreasing_by", "nomatch", "nofun", "sorry", "admit",
    # names Larch itself defines inside `namespace Larch`
    "model", "pre", "result",
}

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*$")
_SPEC_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


def lean_binder(py_name: str) -> str:
    name = py_name if _IDENT.match(py_name) else re.sub(r"[^A-Za-z0-9_]", "_", py_name) or "arg"
    if name in LEAN_RESERVED or name.startswith(("spec_", "post_", "prop_")):
        name = name + "_"
    return name


@dataclass
class Param:
    name: str  # Lean binder name
    lean_type: str
    py_name: str = ""

    @property
    def ltype(self) -> LType:
        return parse_type(self.lean_type)


@dataclass
class Postcondition:
    name: str
    english: str
    lean: str  # Prop over the params and `result`
    status: str = "proposed"  # proposed | approved | rejected


@dataclass
class Property:
    name: str
    english: str
    params: list[Param]
    lean: str  # Prop over its own params; may call `model`
    status: str = "proposed"


@dataclass
class FormalSpec:
    function: str
    understanding: str
    params: list[Param]
    return_type: str
    exceptions: bool
    model_code: str
    pre_english: str
    pre_lean: str
    postconditions: list[Postcondition]
    properties: list[Property] = field(default_factory=list)
    strategy_code: str = ""
    edge_cases: list[list[Any]] = field(default_factory=list)
    notes: str = ""

    # -- derived ------------------------------------------------------------------------
    @property
    def ret_ltype(self) -> LType:
        return parse_type(self.return_type)

    @property
    def model_ret(self) -> str:
        return f"Except String ({self.return_type})" if self.exceptions else self.return_type

    def active_posts(self) -> list[Postcondition]:
        return [p for p in self.postconditions if p.status != "rejected"]

    def active_props(self) -> list[Property]:
        return [p for p in self.properties if p.status != "rejected"]

    def spec_names(self) -> list[str]:
        return [p.name for p in self.active_posts()] + [p.name for p in self.active_props()]

    def spec_kind(self, name: str) -> str:
        return "postcondition" if any(p.name == name for p in self.postconditions) else "property"

    def english_of(self, name: str) -> str:
        for p in self.postconditions:
            if p.name == name:
                return p.english
        for q in self.properties:
            if q.name == name:
                return q.english
        return ""

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(d: dict) -> "FormalSpec":
        d = dict(d)
        d["params"] = [Param(**p) for p in d["params"]]
        d["postconditions"] = [Postcondition(**p) for p in d["postconditions"]]
        d["properties"] = [
            Property(**{**q, "params": [Param(**pp) for pp in q["params"]]}) for q in d.get("properties", [])
        ]
        return FormalSpec(**d)

    def fingerprint(self) -> str:
        """Identity of what a user approves: statements + English, not proofs."""
        from .util import sha256

        parts = [self.model_code, self.pre_lean, self.return_type, str(self.exceptions)]
        parts += [f"{p.name}|{p.lean}|{p.english}" for p in self.postconditions]
        parts += [f"{q.name}|{q.lean}|{q.english}|{[(x.name, x.lean_type) for x in q.params]}" for q in self.properties]
        return sha256(*parts)[:16]

    # -- validation -----------------------------------------------------------------------
    def validate(self) -> list[str]:
        problems: list[str] = []
        seen: set[str] = set()
        for p in self.params:
            try:
                parse_type(p.lean_type)
            except ValueError as e:
                problems.append(f"parameter {p.name}: {e}")
        try:
            parse_type(self.return_type)
        except ValueError as e:
            problems.append(f"return type: {e}")
        if not re.search(r"\bdef\s+model\b", self.model_code):
            problems.append("the model code must define `def model ...`")
        for name in self.spec_names():
            if not _SPEC_NAME.match(name):
                problems.append(f"spec name {name!r} must be snake_case ([a-z][a-z0-9_]*)")
            if name in seen:
                problems.append(f"duplicate spec name {name!r}")
            seen.add(name)
        for q in self.properties:
            for p in q.params:
                try:
                    parse_type(p.lean_type)
                except ValueError as e:
                    problems.append(f"property {q.name} parameter {p.name}: {e}")
        if not self.postconditions and not self.properties:
            problems.append("at least one postcondition or property is required")
        return problems


# ---------------------------------------------------------------------------
# Lean source generation
# ---------------------------------------------------------------------------

def _binders(params: list[Param]) -> str:
    return " ".join(f"({p.name} : {p.lean_type})" for p in params)


def _names(params: list[Param]) -> str:
    return " ".join(p.name for p in params)


def _doc(text: str) -> str:
    text = " ".join(text.split()).replace("-/", "- /")
    return f"/-- {text} -/"


def model_module(spec: FormalSpec) -> str:
    """LarchModel.lean: the model, precondition, postconditions and spec statements."""
    ps = spec.params
    b, n = _binders(ps), _names(ps)
    arrow = " → ".join([f"({p.lean_type})" for p in ps] + [f"({spec.model_ret})"])
    lines = [
        f"/-! Larch executable model for `{spec.function}` (generated; review the specs, not this file). -/",
        "set_option linter.unusedVariables false",
        "namespace Larch",
        "",
        spec.model_code.strip(),
        "",
        "/-- Larch signature check: the model must take exactly the function's parameters. -/",
        f"example : {arrow} := model",
        "",
        _doc(f"Precondition: {spec.pre_english or 'none'}"),
        f"abbrev pre {b} : Prop :=",
        f"  {spec.pre_lean.strip() or 'True'}",
        "",
    ]
    for post in spec.active_posts():
        lines += [
            _doc(f"Postcondition `{post.name}`: {post.english}"),
            f"abbrev post_{post.name} {b} (result : {spec.model_ret}) : Prop :=",
            f"  {_indent_cont(post.lean.strip())}",
            "",
            _doc(f"Spec `{post.name}`: for every input satisfying the precondition, the model's output satisfies the postcondition."),
            f"def spec_{post.name} : Prop :=",
            f"  ∀ {b}, pre {n} → post_{post.name} {n} (model {n})",
            "",
        ]
    for prop in spec.active_props():
        pb, pn = _binders(prop.params), _names(prop.params)
        lines += [
            _doc(f"Property `{prop.name}`: {prop.english}"),
            f"abbrev prop_{prop.name} {pb} : Prop :=",
            f"  {_indent_cont(prop.lean.strip())}",
            "",
            f"def spec_{prop.name} : Prop :=",
            f"  ∀ {pb}, prop_{prop.name} {pn}",
            "",
        ]
    lines.append("end Larch")
    return "\n".join(lines) + "\n"


def _indent_cont(text: str) -> str:
    return text.replace("\n", "\n  ")


def harness_module(spec: FormalSpec) -> str:
    """LarchHarness.lean: a JSON-lines server evaluating the model, precondition and
    specs, used for differential testing (run with `lean --run`)."""
    ret = spec.return_type
    ps = spec.params
    n = _names(ps)
    arg_lines = "\n".join(f"  let {p.name} : {p.lean_type} ← argAt larchArgs {i}" for i, p in enumerate(ps))
    posts = spec.active_posts()
    post_model = ", ".join(f"decide (Larch.post_{p.name} {n} larchR)" for p in posts)
    post_impl = ", ".join(f"decide (Larch.post_{p.name} {n} larchRi)" for p in posts)
    if spec.exceptions:
        enc_dec = f"""
def encRes (r : Except String ({ret})) : Json :=
  match r with
  | .ok v => Json.mkObj [("ok", toJson v)]
  | .error e => Json.mkObj [("error", Json.str e)]

def decRes (j : Json) : Except String (Except String ({ret})) := do
  match j.getObjVal? "ok" with
  | .ok v => return .ok (← fromJson? v)
  | .error _ =>
    match j.getObjVal? "error" with
    | .ok e => return .error (e.getStr?.toOption.getD "error")
    | .error _ => throw "implementation result must be an object with `ok` or `error`"

def sameRes (a b : Except String ({ret})) : Bool :=
  match a, b with
  | .ok x, .ok y => x == y
  | .error _, .error _ => true
  | _, _ => false
"""
    else:
        enc_dec = f"""
def encRes (r : {ret}) : Json := toJson r

def decRes (j : Json) : Except String ({ret}) := fromJson? j

def sameRes (a b : {ret}) : Bool := a == b
"""
    prop_cases = []
    for prop in spec.active_props():
        pargs = "\n".join(f"    let {p.name} : {p.lean_type} ← argAt larchArgs {i}" for i, p in enumerate(prop.params))
        prop_cases.append(
            f'  | "{prop.name}" => do\n{pargs}\n'
            f'    return Json.mkObj [("holds", toJson (decide (Larch.prop_{prop.name} {_names(prop.params)})))]'
        )
    prop_match = "\n".join(prop_cases) if prop_cases else ""
    return f"""import LarchModel
import Lean.Data.Json
open Lean

namespace LarchHarness

instance : ToJson Char := ⟨fun c => Json.str c.toString⟩
instance : FromJson Char := ⟨fun j => do
  let s ← j.getStr?
  match s.toList with
  | [c] => pure c
  | _ => throw s!"expected a single character, got {{s}}"⟩

def argAt {{α : Type}} [FromJson α] (args : Array Json) (i : Nat) : Except String α :=
  match args[i]? with
  | some j => fromJson? j
  | none => throw s!"missing argument {{i}}"
{enc_dec}
def evalCase (larchJ : Json) : Except String Json := do
  let larchArgs ← larchJ.getObjValAs? (Array Json) "args"
{arg_lines}
  if !(decide (Larch.pre {n})) then
    return Json.mkObj [("pre", Json.bool false)]
  let larchR := Larch.model {n}
  let larchPostModel : List Bool := [{post_model}]
  let mut larchOut : List (String × Json) :=
    [("pre", Json.bool true), ("model", encRes larchR), ("post_model", toJson larchPostModel)]
  match larchJ.getObjVal? "impl" with
  | .error _ => pure ()
  | .ok larchIj =>
    match decRes larchIj with
    | .error e => larchOut := larchOut ++ [("impl_decode_error", Json.str e)]
    | .ok larchRi =>
      let larchPostImpl : List Bool := [{post_impl}]
      larchOut := larchOut ++ [("eq", Json.bool (sameRes larchRi larchR)), ("post_impl", toJson larchPostImpl)]
  return Json.mkObj larchOut

def evalProp (larchName : String) (larchJ : Json) : Except String Json := do
  let larchArgs ← larchJ.getObjValAs? (Array Json) "args"
  match larchName with
{prop_match}
  | other => throw s!"unknown property {{other}}"

def handle (j : Json) : Except String Json := do
  match (j.getObjValAs? String "op").toOption.getD "case" with
  | "case" => evalCase j
  | "prop" => evalProp (← j.getObjValAs? String "name") j
  | "ping" => return Json.mkObj [("pong", Json.bool true)]
  | op => throw s!"unknown op {{op}}"

partial def loop (stdin stdout : IO.FS.Stream) : IO Unit := do
  let line ← stdin.getLine
  if line.isEmpty then return
  let out := match Json.parse line with
    | .error e => Json.mkObj [("error", Json.str s!"bad json: {{e}}")]
    | .ok j =>
      match handle j with
      | .ok r => r
      | .error e => Json.mkObj [("error", Json.str e)]
  stdout.putStrLn out.compress
  stdout.flush
  loop stdin stdout

end LarchHarness

def main : IO Unit := do
  LarchHarness.loop (← IO.getStdin) (← IO.getStdout)
"""


def proof_module(spec_name: str, body: str) -> str:
    """A proof attempt file for one spec. The LLM-written block is wrapped in its own
    namespace so helper lemma names from different specs cannot collide."""
    return (
        "import LarchModel\n"
        "set_option linter.unusedVariables false\n"
        f"namespace Larch.Proof_{spec_name}\n"
        "open Larch\n\n"
        f"{body.strip()}\n\n"
        f"end Larch.Proof_{spec_name}\n"
    )


def theorem_name(spec_name: str) -> str:
    return f"Larch.Proof_{spec_name}.{spec_name}_holds"


def theorem_header(spec_name: str) -> str:
    return f"theorem {spec_name}_holds : spec_{spec_name}"


def spec_constant(spec_name: str) -> str:
    return f"Larch.spec_{spec_name}"


def statement_text(spec: FormalSpec, name: str) -> str:
    """Human/LLM-readable statement of a spec (unfolded one level)."""
    ps = spec.params
    b, n = _binders(ps), _names(ps)
    for post in spec.active_posts():
        if post.name == name:
            return (
                f"∀ {b}, pre {n} → post_{name} {n} (model {n})\n"
                f"-- where pre {n} := {spec.pre_lean.strip() or 'True'}\n"
                f"-- and post_{name} {n} result := {post.lean.strip()}"
            )
    for prop in spec.active_props():
        if prop.name == name:
            return f"∀ {_binders(prop.params)}, {prop.lean.strip()}"
    raise KeyError(name)


def edge_cases_from_json(text: str) -> list[list[Any]]:
    try:
        data = json.loads(text) if text.strip() else []
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [c for c in data if isinstance(c, list)]
