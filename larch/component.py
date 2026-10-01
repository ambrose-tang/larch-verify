"""Stateful components (classes): a Lean state machine, its contracts, and the Lean
files Larch generates for proving and testing them.

A component model, written by the formalizer, defines in `namespace Larch`:

    structure State where ... deriving Repr, DecidableEq
    def init (ctor params) : Option State                    -- none: the constructor raises
    def op_<method> (s : State) (params) : Option (State × R) -- none: the method raises
    def obs_<name> (s : State) : T                           -- read-only observers

Larch adds the precondition of the constructor, the reachable states, and one spec per
contract:

    inductive Reachable : State → Prop                       -- init, then any operation
    def spec_<inv>  : Prop := ∀ s, Reachable s → inv_<inv> s
    def spec_<step> : Prop := ∀ s, Reachable s → ∀ args, post_<step> s args (op_<m> s args)

so invariants hold in every state the object can actually reach, and operation contracts
hold for every call from every such state. A raised exception is modelled as "the call
fails and the state is unchanged"; an implementation that changes its state and then
raises disagrees with the model on the next observation.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from .lean.types import parse_type
from .spec import FORBIDDEN_NAMES_RE, Param, _doc, _indent_cont

_IDENT = re.compile(r"^[a-z_][A-Za-z0-9_]*$")


@dataclass
class Operation:
    method: str  # the method name in the implementation
    params: list[Param]
    returns: str  # Lean type of the result ("Unit" for None/void)

    @property
    def lean(self) -> str:
        return f"op_{self.method}"


@dataclass
class Observer:
    name: str  # attribute / property / zero-argument method in the implementation
    lean_type: str
    access: str = "attribute"  # attribute | call


@dataclass
class Invariant:
    name: str
    english: str
    lean: str  # Prop over `s : State`
    status: str = "proposed"
    origin: str = "llm"
    contract: str = ""


@dataclass
class StepContract:
    name: str
    english: str
    operation: str  # method name
    lean: str  # Prop over `s`, the operation's params, and `result : Option (State × R)`
    status: str = "proposed"
    origin: str = "llm"
    contract: str = ""


@dataclass
class ComponentSpec:
    component: str  # class name
    understanding: str
    init_params: list[Param]
    init_pre_english: str
    init_pre_lean: str
    operations: list[Operation]
    observers: list[Observer]
    model_code: str
    invariants: list[Invariant] = field(default_factory=list)
    steps: list[StepContract] = field(default_factory=list)
    strategy_code: str = ""
    exhaustive_domains: dict = field(default_factory=dict)  # {"init": [[args]...], method: [[args]...]}
    notes: str = ""
    contracts: list[str] = field(default_factory=list)
    kind: str = "component"
    http: dict = field(default_factory=dict)  # services: operation -> {verb, path, params: [{name, in, key}], fields: [{key, opaque}]}

    # -- the interface the review UI, prover and store share with FormalSpec ------------------
    @property
    def function(self) -> str:
        return self.component

    @property
    def pre_english(self) -> str:
        return self.init_pre_english

    @property
    def pre_lean(self) -> str:
        return self.init_pre_lean

    @property
    def params(self) -> list[Param]:
        return self.init_params

    @property
    def postconditions(self) -> list[StepContract]:
        return self.steps

    @property
    def properties(self) -> list[Invariant]:
        return self.invariants

    examples: list = field(default_factory=list)

    def active_posts(self) -> list[StepContract]:
        return [p for p in self.steps if p.status != "rejected"]

    def active_props(self) -> list[Invariant]:
        return [p for p in self.invariants if p.status != "rejected"]

    def spec_names(self) -> list[str]:
        return [i.name for i in self.active_props()] + [s.name for s in self.active_posts()]

    def spec_kind(self, name: str) -> str:
        return "invariant" if any(i.name == name for i in self.invariants) else "operation"

    def kind_label(self, item) -> str:
        if isinstance(item, Invariant):
            return "invariant (every reachable state)"
        return f"contract of {item.operation}()"

    def english_of(self, name: str) -> str:
        for x in list(self.invariants) + list(self.steps):
            if x.name == name:
                return x.english
        return ""

    def op(self, method: str) -> Operation:
        return next(o for o in self.operations if o.method == method)

    # -- persistence ------------------------------------------------------------------------
    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(d: dict) -> "ComponentSpec":
        d = dict(d)
        d["init_params"] = [Param(**p) for p in d["init_params"]]
        d["operations"] = [Operation(o["method"], [Param(**p) for p in o["params"]], o["returns"]) for o in d["operations"]]
        d["observers"] = [Observer(**o) for o in d["observers"]]
        d["invariants"] = [Invariant(**i) for i in d.get("invariants", [])]
        d["steps"] = [StepContract(**s) for s in d.get("steps", [])]
        return ComponentSpec(**d)

    def fingerprint(self) -> str:
        from .util import sha256

        parts = [self.model_code, self.init_pre_lean, json.dumps(self.to_json()["operations"], sort_keys=True),
                 json.dumps(self.to_json()["observers"], sort_keys=True), *self.contracts]
        parts += [f"{i.name}|{i.lean}|{i.english}" for i in self.invariants]
        parts += [f"{s.name}|{s.operation}|{s.lean}|{s.english}" for s in self.steps]
        return sha256(*parts)[:16]

    # -- validation ---------------------------------------------------------------------------
    def validate(self) -> list[str]:
        problems: list[str] = []
        for p in self.init_params:
            _check_type(p.lean_type, f"constructor parameter {p.name}", problems)
        if not re.search(r"\bstructure\s+State\b", self.model_code):
            problems.append("the model must define `structure State where ...` (with `deriving Repr, DecidableEq`)")
        if not re.search(r"\bdef\s+init\b", self.model_code):
            problems.append("the model must define `def init ... : Option State`")
        seen = set()
        for o in self.operations:
            if not _IDENT.match(o.method):
                problems.append(f"operation name {o.method!r} is not a valid identifier")
            if o.method in seen:
                problems.append(f"operation {o.method} is listed twice")
            seen.add(o.method)
            for p in o.params:
                _check_type(p.lean_type, f"{o.method} parameter {p.name}", problems)
            _check_type(o.returns, f"{o.method} result", problems)
            if not re.search(rf"\bdef\s+op_{re.escape(o.method)}\b", self.model_code):
                problems.append(f"the model must define `def op_{o.method} (s : State) ... : Option (State × {o.returns})`")
        for ob in self.observers:
            _check_type(ob.lean_type, f"observer {ob.name}", problems)
            if not re.search(rf"\bdef\s+obs_{re.escape(ob.name)}\b", self.model_code):
                problems.append(f"the model must define `def obs_{ob.name} (s : State) : {ob.lean_type}`")
        ops = {o.method for o in self.operations}
        names = set()
        for x in list(self.invariants) + list(self.steps):
            if not re.match(r"^[a-z][a-z0-9_]{0,40}$", x.name):
                problems.append(f"contract name {x.name!r} must be snake_case")
            if x.name in names:
                problems.append(f"duplicate contract name {x.name!r}")
            names.add(x.name)
        for s in self.steps:
            if s.operation not in ops:
                problems.append(f"contract {s.name} is about `{s.operation}`, which is not one of the operations {sorted(ops)}")
        if not self.invariants and not self.steps:
            problems.append("at least one invariant or operation contract is required")
        if FORBIDDEN_NAMES_RE.search(self.model_code):
            problems.append("the model must not define `Reachable`, `pre_init`, `inv_*`, `post_*` or `spec_*`: Larch generates them")
        return problems


def _check_type(t: str, what: str, problems: list[str]) -> None:
    try:
        parse_type(t)
    except ValueError as e:
        problems.append(f"{what}: {e}")


def _binders(params: list[Param]) -> str:
    return " ".join(f"({p.name} : {p.lean_type})" for p in params)


def _names(params: list[Param]) -> str:
    return " ".join(p.name for p in params)


def _app(fn: str, *args: str) -> str:
    return " ".join([fn, *[a for a in args if a]])


# ---------------------------------------------------------------------------
# Lean generation
# ---------------------------------------------------------------------------

def module_text(spec: ComponentSpec) -> str:
    """LarchModel.lean for a component."""
    ib, ins = _binders(spec.init_params), _names(spec.init_params)
    lines = [
        f"/-! Larch state-machine model of `{spec.component}` (generated; review the contracts, not this file). -/",
        "set_option linter.unusedVariables false",
        "namespace Larch",
        "",
        spec.model_code.strip(),
        "",
        "/-- Larch signature checks: the model's operations take exactly these parameters. -/",
        f"example : {' → '.join([f'({p.lean_type})' for p in spec.init_params] + ['Option State'])} := init",
    ]
    for o in spec.operations:
        sig = " → ".join(["State"] + [f"({p.lean_type})" for p in o.params] + [f"Option (State × ({o.returns}))"])
        lines.append(f"example : {sig} := {o.lean}")
    for ob in spec.observers:
        lines.append(f"example : State → ({ob.lean_type}) := obs_{ob.name}")
    lines += [
        "",
        _doc(f"Constructor precondition: {spec.init_pre_english or 'none'}"),
        f"abbrev pre_init {ib} : Prop :=".replace("  :", " :"),
        f"  {spec.init_pre_lean.strip() or 'True'}",
        "",
        "/-- The states the object can actually be in: constructed, then any sequence of calls. -/",
        "inductive Reachable : State → Prop where",
        f"  | init {ib} (s : State) : pre_init {ins} → init {ins} = some s → Reachable s".replace("  (s", " (s"),
    ]
    for o in spec.operations:
        pb, pn = _binders(o.params), _names(o.params)
        lines.append(
            f"  | {o.lean} (s : State) {pb} (s' : State) (r : {o.returns}) : "
            f"Reachable s → {_app(o.lean, 's', pn)} = some (s', r) → Reachable s'".replace("  (s'", " (s'")
        )
    lines.append("")
    for inv in spec.active_props():
        lines += [
            _doc(f"Invariant `{inv.name}`: {inv.english}"),
            f"abbrev inv_{inv.name} (s : State) : Prop :=",
            f"  {_indent_cont(inv.lean.strip())}",
            "",
            f"def spec_{inv.name} : Prop :=",
            f"  ∀ s, Reachable s → inv_{inv.name} s",
            "",
        ]
    for st in spec.active_posts():
        o = spec.op(st.operation)
        pb, pn = _binders(o.params), _names(o.params)
        lines += [
            _doc(f"Contract `{st.name}` of `{o.method}`: {st.english}"),
            f"abbrev post_{st.name} (s : State) {pb} (result : Option (State × ({o.returns}))) : Prop :=".replace("  (result", " (result"),
            f"  {_indent_cont(st.lean.strip())}",
            "",
            f"def spec_{st.name} : Prop :=",
            f"  ∀ s, Reachable s → ∀ {pb}, post_{st.name} {_app('s', pn)} ({_app(o.lean, 's', pn)})"
            if o.params else f"  ∀ s, Reachable s → post_{st.name} s ({o.lean} s)",
            "",
        ]
    lines.append("end Larch")
    return "\n".join(lines) + "\n"


def statement(spec: ComponentSpec, name: str) -> str:
    for inv in spec.active_props():
        if inv.name == name:
            return f"∀ s, Reachable s → inv_{name} s\n-- where inv_{name} s := {inv.lean.strip()}"
    for st in spec.active_posts():
        if st.name == name:
            o = spec.op(st.operation)
            pb, pn = _binders(o.params), _names(o.params)
            return (f"∀ s, Reachable s → ∀ {pb}, post_{name} {_app('s', pn)} ({_app(o.lean, 's', pn)})\n"
                    f"-- where post_{name} s {pn} result := {st.lean.strip()}")
    raise KeyError(name)


def _defs(spec: ComponentSpec) -> str:
    names = re.findall(r"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+)?def\s+([A-Za-z_][A-Za-z0-9_'.]*)", spec.model_code, re.M)
    return ", ".join(dict.fromkeys(names))


def portfolio_scripts(spec: ComponentSpec, name: str) -> list[tuple[str, str]]:
    from .spec import theorem_header

    header = theorem_header(name)
    defs = _defs(spec)
    if spec.spec_kind(name) == "invariant":
        return [
            ("induction+grind", f"{header} := by\n  intro s h\n  induction h <;> simp_all [inv_{name}, {defs}] <;> grind [{defs}]"),
            ("induction+omega", f"{header} := by\n  intro s h\n  induction h <;> simp_all [inv_{name}, {defs}] <;> (repeat' split) <;> simp_all <;> omega"),
            ("cases", f"{header} := by\n  intro s h\n  cases h <;> grind [inv_{name}, {defs}]"),
        ]
    st = next(x for x in spec.steps if x.name == name)
    pn = _names(spec.op(st.operation).params)
    intro = f"intro s h {pn}".strip()
    return [
        ("grind", f"{header} := by\n  {intro}\n  simp only [post_{name}]\n  grind [{defs}]"),
        ("simp_all+omega", f"{header} := by\n  {intro}\n  simp only [post_{name}]\n  simp_all [{defs}]\n  all_goals (repeat' split) <;> simp_all <;> (first | omega | grind [{defs}])"),
    ]


def harness_text(spec: ComponentSpec) -> str:
    """LarchHarness.lean for a component: replays a call sequence on the model and reports,
    after every step, the result, the observers, the invariants, and the step contracts."""
    ib = spec.init_params
    init_args = "\n".join(f"  let {p.name} : {p.lean_type} ← argAt larchInit {i}" for i, p in enumerate(ib))
    cases = []
    for o in spec.operations:
        args = "\n".join(f"    let {p.name} : {p.lean_type} ← argAt larchArgs {i}" for i, p in enumerate(o.params))
        pn = _names(o.params)
        posts = [st for st in spec.active_posts() if st.operation == o.method]
        post_list = ", ".join(f'("{st.name}", decide (Larch.post_{st.name} {_app("larchS", pn)} larchR))' for st in posts)
        cases.append(
            f'  | "{o.method}" => do\n{args}\n'
            f"    let larchR := Larch.{_app(o.lean, 'larchS', pn)}\n"
            f"    let larchPosts : List (String × Bool) := [{post_list}]\n"
            f"    match larchR with\n"
            f'    | some (larchS2, larchV) => return (larchS2, Json.mkObj [("ok", toJson larchV), ("posts", toJson larchPosts)])\n'
            f'    | none => return (larchS, Json.mkObj [("error", Json.str "raises"), ("posts", toJson larchPosts)])'
        )
    obs = ", ".join(f'("{ob.name}", toJson (Larch.obs_{ob.name} larchS))' for ob in spec.observers)
    invs = ", ".join(f'("{i.name}", decide (Larch.inv_{i.name} larchS))' for i in spec.active_props())
    return f"""import LarchModel
import Lean.Data.Json
open Lean

set_option linter.all false

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

def observe (larchS : Larch.State) : Json :=
  Json.mkObj [("obs", Json.mkObj [{obs}]), ("inv", toJson ([{invs}] : List (String × Bool)))]

def step (larchS : Larch.State) (larchOp : String) (larchArgs : Array Json) : Except String (Larch.State × Json) := do
  match larchOp with
{chr(10).join(cases)}
  | other => throw s!"unknown operation {{other}}"

def evalSeq (larchJ : Json) : Except String Json := do
  let larchInit ← larchJ.getObjValAs? (Array Json) "init"
{init_args}
  if !(decide (Larch.pre_init {_names(ib)})) then
    return Json.mkObj [("pre", Json.bool false)]
  match Larch.{_app("init", _names(ib))} with
  | none => return Json.mkObj [("pre", Json.bool true), ("init", Json.str "raises")]
  | some larchS0 =>
    let larchSteps ← larchJ.getObjValAs? (Array Json) "steps"
    let mut larchS := larchS0
    let mut larchOut : Array Json := #[]
    for larchStep in larchSteps do
      let larchOp ← larchStep.getArrVal? 0 >>= Json.getStr?
      let larchArgs ← larchStep.getArrVal? 1 >>= fun j => j.getArr?
      let (larchS2, larchRes) ← step larchS larchOp larchArgs
      larchS := larchS2
      larchOut := larchOut.push (larchRes.mergeObj (observe larchS))
    return Json.mkObj [("pre", Json.bool true), ("init", observe larchS0), ("steps", Json.arr larchOut)]

def handle (j : Json) : Except String Json := do
  match (j.getObjValAs? String "op").toOption.getD "seq" with
  | "seq" => evalSeq j
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
