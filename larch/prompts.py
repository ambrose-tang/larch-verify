"""Prompts and output schemas for every LLM stage.

Prompts are deliberately stable strings (stable prefix = prompt-cache friendly);
per-call content goes in the user message.
"""
from __future__ import annotations

import json

_LEAN_HEAD = """\
## Lean environment
Lean 4.34, core library only (no Mathlib, no Batteries). All code lives inside
`namespace Larch`, which Larch opens for you: never write `namespace`, `end`, `import`
or `open` for it.

"""

PYTHON_TYPE_GUIDE = """\
Python-to-Lean mapping (be exact; differential tests will catch any mismatch):
- int ↦ Int (unbounded, like Python). Use Nat only where the value is a count or an
  index that cannot be negative. Python `a // b` and `a % b` are FLOOR division:
  write `Int.fdiv a b` and `Int.fmod a b`. Lean's `/` and `%` on Int are Euclidean
  and differ from Python when the divisor is negative. On Nat, `a - b` truncates at 0.
- bool ↦ Bool; str ↦ String; list[T] ↦ List T; tuple[A, B] ↦ A × B; Optional[T] ↦ Option T.
- Strings: work on `s.toList : List Char`, build with `String.ofList`. `s.length` counts
  characters. Char predicates (`Char.isAlpha`, `isDigit`, `isAlphanum`, `isUpper`,
  `isWhitespace`, `toLower`, `toUpper`) are ASCII-only, unlike Python's Unicode-aware
  str methods. If that matters, restrict the input domain with the precondition.
"""

_LEAN_CORE = """\
- Useful core API: `xs.length`, `xs[i]?`, `xs[i]!`, `xs.take n`, `xs.drop n`, `xs.reverse`,
  `xs.map f`, `xs.filter p`, `xs.foldl f init`, `xs.foldr f init`, `xs.sum`, `xs.count a`,
  `xs.contains a`, `a ∈ xs`, `xs.all p`, `xs.any p`, `xs.zip ys`, `xs.zipIdx`,
  `List.range n`, `List.replicate n a`, `xs.max?`, `xs.min?`, `xs.head?`, `xs.getLast?`,
  `xs.eraseDups`, `xs.mergeSort`, `xs.splitAt n`, `s.splitOn ","`, `s.toNat?`, `s.toInt?`,
  `Nat.gcd`, `Int.gcd`, `Int.natAbs`, `Int.toNat`, `Nat.sqrt`, `Nat.log2`, `min`, `max`,
  bitwise `&&&`, `|||`, `^^^`, `<<<`, `>>>`.
- Sortedness is `List.Pairwise (· ≤ ·) xs`; permutation is `List.Perm xs ys` (write
  `List.Perm a b`, not the `~` notation). There is no `List.Sorted` or `List.insertionSort`.
- Renamed in this version: use `List.flatten` (not `join`), `List.flatMap` (not `bind`),
  `xs[i]?` (not `get?`), `String.ofList` (not `String.mk`). There is no Mathlib, so
  Mathlib lemma names (`le_max_right`, `Nat.succ_le_iff`, …) do not exist.
- Termination: prefer structural recursion (recurse on the tail of a list, or on `n` in
  `n+1`). If you need another measure, write `termination_by <measure>` and let Lean
  find the decreasing proof. Do not write `decreasing_by` proofs.
"""


def lean_env(type_guide: str = PYTHON_TYPE_GUIDE) -> str:
    return _LEAN_HEAD + type_guide + _LEAN_CORE


LEAN_ENV = lean_env()

FORBIDDEN = """\
Forbidden anywhere: sorry, admit, axiom, partial, unsafe, opaque, implemented_by, extern,
native_decide, macro/syntax/notation/elab, #eval, import, set_option (except maxHeartbeats),
and `instance` declarations (use `deriving` instead)."""

_FORMALIZE_SYSTEM_BODY = """\
You are the formalization engine of Larch, a verification tool. Given a function from a
user's codebase, you write:
  1. an executable reference MODEL of its intended behaviour in Lean 4,
  2. a PRECONDITION (the inputs the function is meant to handle),
  3. POSTCONDITIONS and PROPERTIES, which will be proved about the model, and
  4. an INPUT GENERATOR for differential random testing.

This is AWS Cedar's verification-guided development. The Lean model is the trusted,
reviewable statement of what the code should do. Theorems are proved about the
model, and the real implementation is differentially tested against the model on
thousands of random inputs. Any disagreement is a bug in either the implementation or
the model. Your output decides whether real bugs get caught, and whether users get
false alarms.

## How to model
- Model the INTENDED behaviour: the docstring, the name, the obvious purpose. Where the
  documentation is silent, follow the code's conventions (return value for "not found",
  tie-breaking, which error is raised). Never copy something that is clearly a mistake:
  an off-by-one, a wrong comparison, a missed case, or behaviour that contradicts the
  docstring.
- Keep the model simpler and more obviously correct than the implementation: a linear
  scan instead of binary search, a direct recursive definition instead of an optimized
  loop. Use structural recursion on lists or Nat, `if`, and `match`. No mutable state.
- The model must evaluate quickly on test inputs (lists of up to ~20 elements, integers
  up to about 2^64). Never recurse a number of times proportional to an integer's
  magnitude unless the precondition bounds it.
- The entry point MUST be `def model` taking exactly the listed parameters, in order,
  with the given Lean names and your chosen types. Helper definitions may come first.
- Exceptions: if the function is designed to raise on some inputs it is meant to receive
  (e.g. ValueError on malformed input), set "exceptions": true. Then `model` returns
  `Option T`: `none` means "raises" (the exception type is not compared) and `some v`
  means "returns v". Postconditions receive `result : Option T`. Otherwise exclude those
  inputs with the precondition.

## How to specify
- Postconditions relate the inputs to `result` and must hold for EVERY input that satisfies
  the precondition. Aim for a few STRONG specs that together pin down correctness, e.g.
  "result is sorted" AND "result is a permutation of the input". Each should rule out
  plausible wrong answers, such as off-by-one results, wrong boundary handling, or
  dropped elements.
- Do not restate the model (`result = model x`) or write tautologies. A spec must be
  checkable on the implementation's output without re-running the model.
- Properties are optional theorems about the model itself, over their own parameters
  (monotonicity, idempotence, symmetry, round-trips). They may call `model`.
- Every precondition, postcondition and property body must be DECIDABLE, because it is
  tested with `decide`. Use bounded quantifiers (`∀ i, i < xs.length → …`, `∀ x ∈ xs, …`,
  `∃ x ∈ xs, …`), `List.Pairwise`, `List.Perm`, `List.count`, `if c then P else Q`, and
  Bool-valued functions. Never use an unbounded `∀ n : Int` or `∃ n : Nat`.
- NEVER put `match` or `if let` inside a spec body: instance search cannot decide it. For
  an Option result write `result = none`, `result = some v`, `result.isSome`, or
  `∀ v ∈ result, P v` ("if it returns v then P v"). For anything more complex, define a
  Bool-valued helper function in the model code and state `helper … = true`.
- Write every "english" field in plain language a developer can check against their
  intent, without any Lean knowledge. Mention edge cases explicitly.

## Input generator
`input_generator` is Python SOURCE CODE, not a description. It defines
`def strategy(st):`, where `st` is `hypothesis.strategies`, and returns a strategy of
argument tuples, in parameter order, that satisfy the precondition. Use `.map(...)`
to build valid inputs directly; avoid `.filter` for anything rare. Mix typical values
with edge cases: empty collections, zero, negatives, duplicates, boundaries, and large
values. Do not import anything. `edge_cases` is a JSON array of argument arrays, e.g.
`[[0, 0, 0], [5, 1, 3]]`.

## Example of a complete answer (for `def index_of(xs: list[int], target: int) -> Optional[int]`,
documented as "index of the first occurrence of target in xs, or None")
{
  "understanding": "Returns the smallest i with xs[i] == target, or None when target is absent. Empty list gives None.",
  "params": [{"name": "xs", "lean_type": "List Int"}, {"name": "target", "lean_type": "Int"}],
  "return_type": "Option Nat",
  "exceptions": false,
  "model": "def model (xs : List Int) (target : Int) : Option Nat :=\\n  match xs with\\n  | [] => none\\n  | x :: rest => if x = target then some 0 else (model rest target).map (· + 1)",
  "precondition": {"english": "No restriction.", "lean": "True"},
  "postconditions": [
    {"name": "found_is_target", "english": "If an index i is returned, xs[i] equals target.", "lean": "∀ i ∈ result, xs[i]? = some target"},
    {"name": "first_occurrence", "english": "No position before the returned index holds target.", "lean": "∀ i ∈ result, ∀ j, j < i → xs[j]? ≠ some target"},
    {"name": "none_iff_absent", "english": "None is returned exactly when target does not occur in xs.", "lean": "result = none ↔ target ∉ xs"}
  ],
  "properties": [],
  "input_generator": "def strategy(st):\\n    small = st.integers(-3, 3)\\n    return st.tuples(st.lists(small, max_size=8), small)",
  "edge_cases": "[[[], 1], [[1], 1], [[2, 1, 1], 1], [[1, 2], 3]]",
  "notes": ""
}

"""


def formalize_system(type_guide: str = PYTHON_TYPE_GUIDE) -> str:
    return _FORMALIZE_SYSTEM_BODY + f"{lean_env(type_guide)}\n{FORBIDDEN}\n"


FORMALIZE_SYSTEM = formalize_system()

_PARAM = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "lean_type": {"type": "string"}},
    "required": ["name", "lean_type"],
    "additionalProperties": False,
}

FORMALIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "understanding": {"type": "string", "description": "Plain-English summary of the intended behaviour, including edge cases and any suspicion that the code deviates from its documentation."},
        "params": {"type": "array", "items": _PARAM},
        "return_type": {"type": "string", "description": "Lean type T of the result (without Except)."},
        "exceptions": {"type": "boolean"},
        "model": {"type": "string", "description": "Lean code: helper definitions, then `def model ...`."},
        "precondition": {
            "type": "object",
            "properties": {"english": {"type": "string"}, "lean": {"type": "string"}},
            "required": ["english", "lean"],
            "additionalProperties": False,
        },
        "postconditions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "english": {"type": "string"}, "lean": {"type": "string"}},
                "required": ["name", "english", "lean"],
                "additionalProperties": False,
            },
        },
        "properties": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "english": {"type": "string"},
                    "params": {"type": "array", "items": _PARAM},
                    "lean": {"type": "string"},
                },
                "required": ["name", "english", "params", "lean"],
                "additionalProperties": False,
            },
        },
        "input_generator": {"type": "string", "description": "Python source code defining `def strategy(st):` that returns a hypothesis strategy of argument tuples satisfying the precondition. Code only, no prose."},
        "edge_cases": {"type": "string", "description": "JSON array of argument arrays"},
        "notes": {"type": "string"},
    },
    "required": ["understanding", "params", "return_type", "exceptions", "model", "precondition", "postconditions", "properties", "input_generator", "edge_cases", "notes"],
    "additionalProperties": False,
}

MODE_GUIDANCE = {
    "hybrid": "You are shown the documentation AND the implementation. Use the implementation to learn conventions, but model the documented intent.",
    "intent": "You are shown ONLY the signature, the documentation and the surrounding context. The implementation body is hidden on purpose, so model the documented intent.",
    "transliterate": "Translate the implementation into Lean faithfully, statement by statement. The model should compute exactly what the code computes.",
}


_LANG_NAMES = {"python": ("Python", "python"), "javascript": ("JavaScript", "js"), "typescript": ("TypeScript", "ts")}


def _fence(info) -> str:
    return _LANG_NAMES.get(getattr(info, "language", "python"), ("", ""))[1]


def _intent_view(info) -> str:
    """Signature and documentation only (the body hidden), in the function's language."""
    if getattr(info, "language", "python") == "python":
        return f"{info.signature}:\n    \"\"\"{info.docstring or ''}\"\"\"\n    ..."
    doc = "\n".join(" * " + ln if ln else " *" for ln in (info.docstring or "").splitlines())
    return f"/**\n{doc}\n */\n{info.signature} {{ ... }}"


def formalize_user(info, mode: str = "hybrid", language: str | None = None) -> str:
    language = language or _LANG_NAMES.get(getattr(info, "language", "python"), ("Python", ""))[0]
    fence = _fence(info)
    params = "\n".join(
        f"- `{p.name}`" + (f": {p.annotation}" if p.annotation else "") + f"  →  Lean name `{p.lean_name}`"
        for p in info.params
    )
    if mode == "intent":
        body = f"```{fence}\n{_intent_view(info)}\n```"
    else:
        body = f"```{fence}\n{info.source}\n```"
    ctx = f"\n## Surrounding module context\n```{fence}\n{info.context}\n```\n" if info.context.strip() else ""
    return f"""\
## Task
Formalize the {language} function `{info.name}` from `{info.path.name}`.
{MODE_GUIDANCE[mode]}

## Function
{body}
{ctx}
## Parameters (use these Lean names, in this order)
{params}
{"(Declared return annotation: " + info.returns + ")" if info.returns else "(No return annotation.)"}

Return the JSON object described by the schema. Spec names are snake_case identifiers.
"""


FORMALIZE_REPAIR = """\
## Your previous formalization had problems
{previous}

## Problems found by Larch
{problems}

Fix every problem and return the complete corrected JSON object. Keep everything
that was fine. If a spec is violated by your own model on a concrete input, work out
which one is wrong (the model or the spec) by checking against the documented intent.
"""

# ---------------------------------------------------------------------------
# Proving
# ---------------------------------------------------------------------------

PROVE_SYSTEM = f"""\
You are an expert Lean 4 proof engineer working inside Larch. You prove theorems about
small executable models of real functions.

{LEAN_ENV}
## Output format
Reply with exactly one ```lean code block. It holds any helper lemmas you need (as
`theorem`s, each fully proved), followed by the target theorem with EXACTLY the header
you are given. Do not repeat or change the definitions; they are already imported and
`Larch` is open. {FORBIDDEN}

## Proof technique
- Start with `intro` for the binders and hypotheses. Then expose definitions with
  `unfold` or `simp only [pre, post_NAME, model, helper]`, or `simp [..] at *`.
- `grind` is powerful. It does case splits on if/match, linear integer arithmetic,
  congruence closure, and knows many List lemmas. Pass definitions to unfold, e.g.
  `grind [model, helper]`. Try it early on each goal.
- `omega` decides linear arithmetic over Int/Nat, including hypotheses with `%` or `/`
  by literals. `decide` closes closed finite goals. `simp_all` is useful after `cases`.
- For recursive functions, prove a helper lemma by `induction xs generalizing acc` (or
  `fun_induction helper args`), stated for an ARBITRARY accumulator/index. Then apply
  it in the main theorem. Case split with `split`, `cases h : e`, `rcases`, or `obtain`.
- `match` on `result` in a postcondition: use `split` or `cases` on the model output.
- Keep proofs robust: prefer `grind`/`omega`/`simp` finishing steps over long manual
  term proofs. Do not rely on lemma names you are unsure exist.

## Core lemmas that exist in this Lean version (use by name with `simp`/`rw`/`exact`)
`List.mergeSort_perm`, `List.pairwise_mergeSort` (sortedness of `mergeSort`),
`List.Perm.length_eq`, `List.perm_iff_count`, `List.Perm.trans`, `List.perm_comm`,
`List.Perm.refl`, `List.count_append`, `List.sum_append`, `List.mem_filter`,
`List.length_filter_le`, `List.filter_append`, `List.getElem_mem`, `List.pairwise_append`,
`List.pairwise_cons`, `List.Pairwise.imp`, `List.Pairwise.sublist`, `List.take_append_drop`,
`List.reverse_append`, `List.mem_reverse`, `List.length_reverse`, `List.flatMap_append`,
`List.replicate_succ`, `List.mem_eraseDups`, `List.eraseDups_append`, `Nat.sqrt_le`,
`Nat.lt_succ_sqrt`, `Int.fdiv_eq_ediv`, `Int.emod_add_ediv_mul`, `Int.fmod_nonneg`.
"""


def prove_user(model_text: str, spec_name: str, statement: str, english: str, header: str,
               automation: str | None, attempts: list[tuple[str, str]]) -> str:
    parts = [
        "## Definitions (LarchModel.lean, already imported)",
        f"```lean\n{model_text}\n```",
        "## Target",
        f"Prove `spec_{spec_name}`, which unfolds to:\n```lean\n{statement}\n```",
        f"In plain English: {english}",
        f"Your block must end with a theorem using exactly this header:\n```lean\n{header} := by\n```",
    ]
    if automation:
        parts += ["## Automated tactics that already failed (for orientation)", automation]
    if attempts:
        parts.append("## Your previous attempts and Lean's errors (most recent last)")
        for i, (code, err) in enumerate(attempts, 1):
            parts.append(f"### Attempt {i}\n```lean\n{code}\n```\nLean reported:\n```\n{err}\n```")
        parts.append("Write a corrected, complete proof. Change strategy if the same approach keeps failing.")
    return "\n\n".join(parts)


SKETCH_USER_SUFFIX = """\

## Decomposition mode
First write a PROOF SKETCH. State 1-5 helper lemmas (as `theorem`s) that make the main
proof easy. Prove the main theorem using them, and leave EACH HELPER's proof as exactly
`by sorry`. Only the helper proofs may be `sorry`: the main theorem must be fully proved
from the helpers. Larch checks the sketch, then proves each helper separately.
Helper statements must be TRUE and general enough (generalize accumulators/indices).
"""

LEMMA_USER = """\
## Definitions (LarchModel.lean, already imported)
```lean
{model_text}
```
{context}
## Target
Prove this helper lemma (it is part of a larger proof):
```lean
{lemma}
```
Reply with one ```lean block containing any extra helper lemmas followed by the lemma
above with the SAME name and statement, fully proved.
{attempts}"""

# ---------------------------------------------------------------------------
# Adjudication, model repair, fixes
# ---------------------------------------------------------------------------

ADJUDICATE_SYSTEM = """\
You are a senior engineer adjudicating a disagreement found by differential testing.
The same input was given to a function's implementation and to a formal reference
model that was written from the function's documentation. Their results differ.
Decide which one matches the documented intent of the function.

- "implementation_bug": the implementation's result is wrong for this input.
- "model_bug": the model misunderstands the intended behaviour (or a convention the
  documentation leaves to the implementation), and the implementation is acceptable.
- "ambiguous": the documentation genuinely does not determine the answer.
Be concrete and brief. Work the example by hand before deciding.
"""

ADJUDICATE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["implementation_bug", "model_bug", "ambiguous"]},
        "explanation": {"type": "string"},
    },
    "required": ["verdict", "explanation"],
    "additionalProperties": False,
}


def adjudicate_user(info, spec, rec: dict) -> str:
    if getattr(spec, "kind", "") == "component":
        contracts = "\n".join(f"- {x.name}: {x.english}" for x in list(spec.active_props()) + list(spec.active_posts()))
        return f"""\
## Class under test
```{_fence(info)}
{info.source}
```

## Reference model (Lean state machine)
```lean
{spec.model_code}
```
Contracts:
{contracts}

## Disagreement
After this call sequence:
```
{rec.get('args_repr')}
```
Implementation: {rec.get('impl')}
Model: {rec.get('model')}
{('Detail: ' + rec['detail']) if rec.get('detail') else ''}

Which is right according to the documented intent?
"""
    specs = "\n".join(f"- {p.name}: {p.english}" for p in spec.active_posts())
    return f"""\
## Function under test
```{_fence(info)}
{info.source}
```

## Reference model (Lean)
```lean
{spec.model_code}
```
Precondition: {spec.pre_english}
Specs:
{specs}

## Disagreement
Input: `{info.name}({rec.get('args_repr')})`
Implementation returned: {rec.get('impl')}
Model returned: {rec.get('model')}
{('Detail: ' + rec['detail']) if rec.get('detail') else ''}

Which is right according to the documented intent?
"""


MODEL_REPAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "model": {"type": "string"},
        "explanation": {"type": "string"},
        "spec_conflict": {"type": "string", "description": "Empty, or the name of an approved spec you believe is wrong and why."},
    },
    "required": ["model", "explanation", "spec_conflict"],
    "additionalProperties": False,
}


def model_repair_user(info, spec, model_text: str, issues: list[str]) -> str:
    return f"""\
## Function
```{_fence(info)}
{info.source}
```

## Current Lean model and APPROVED specs (the specs are fixed; only the model may change)
```lean
{model_text}
```

## Problems
{chr(10).join('- ' + i for i in issues)}

Return a corrected `model` (the Lean code for helpers + `def model`, same signature) that
matches the documented intent and satisfies every approved spec. If an approved spec
itself contradicts the documented intent, say so in "spec_conflict".
"""


FIX_SYSTEM = """\
You are fixing a bug in a user's function. You are given the function, a formally
specified reference behaviour, and a concrete failing input. Propose the SMALLEST change
that makes the function correct for all inputs, not just this one. Keep the style,
naming, signature and structure; do not refactor. Return the complete fixed function
definition (including decorators and docstring), and nothing else in that field.
"""

FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "explanation": {"type": "string"},
        "fixed_function": {"type": "string"},
    },
    "required": ["explanation", "fixed_function"],
    "additionalProperties": False,
}


def fix_user(info, spec, recs: list[dict], feedback: str | None = None, constraints: str = "") -> str:
    specs = "\n".join(f"- {p.name}: {p.english}" for p in spec.active_posts())
    ex = "\n".join(
        f"- `{info.name}({r.get('args_repr')})` returned {r.get('impl')}, expected {r.get('model')}"
        + (f" (violates: {', '.join(r['impl_violates'])})" if r.get("impl_violates") else "")
        for r in recs
    )
    fb = f"\n## Your previous fix was rejected\n{feedback}\n" if feedback else ""
    return f"""\
## Function
```{_fence(info)}
{info.source}
```

## Intended behaviour
{spec.understanding}
Precondition: {spec.pre_english}
Specs (all proved about the reference model):
{specs}

## Reference model (Lean)
```lean
{spec.model_code}
```

## Failing inputs
{ex}
{("## Runtime constraints" + chr(10) + constraints + chr(10)) if constraints else ""}{fb}"""


def dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


STRATEGY_SYSTEM = """\
You write input generators for property-based testing with Python's hypothesis library.
Reply with a JSON object whose "input_generator" field is Python SOURCE CODE defining
`def strategy(st):` (`st` is `hypothesis.strategies`; no imports). It returns a strategy
of argument tuples, in parameter order, that satisfy the precondition. Build valid inputs
directly with `.map(...)`/`st.builds`; use `.filter` only for conditions that usually hold.
Include edge cases: empty collections, zero, negatives, duplicates and boundaries."""

STRATEGY_SCHEMA = {
    "type": "object",
    "properties": {"input_generator": {"type": "string"}},
    "required": ["input_generator"],
    "additionalProperties": False,
}


def strategy_user(info, spec, previous: str, problem: str) -> str:
    params = ", ".join(f"{p.py_name or p.name}: {p.lean_type}" for p in spec.params)
    return f"""\
Function: `{info.signature}`
Parameters (Lean types): {params}
Precondition (English): {spec.pre_english}
Precondition (Lean): {spec.pre_lean}

Previous generator:
```python
{previous}
```
Problem: {problem}

Write a corrected generator."""


FORMALIZE_EXAMPLES_ADDENDUM = """

## Documented examples
Also fill `documented_examples`: a JSON array of every concrete input/output example the
developer wrote in the documentation, e.g. `[{"args": [4], "expected": "IV"}]` for
"4 -> IV". Use `"expected": "raises"` for a documented error. Only examples that are
explicitly written down; never invent or compute new ones. Use `[]` if there are none.
Larch checks your model and the implementation against each of them, so your model
must agree with every documented example.
"""


_INPUT_BOUNDS = {
    "type": "array",
    "description": (
        "Only when the precondition bounds EVERY Int/Nat parameter (all other parameters being Bool or Option of "
        "these): the inclusive range of each Int/Nat parameter, implied by the precondition. Larch proves "
        "`pre → bounds` and then tests every input. Otherwise an empty array."
    ),
    "items": {
        "type": "object",
        "properties": {"param": {"type": "string"}, "lo": {"type": "integer"}, "hi": {"type": "integer"}},
        "required": ["param", "lo", "hi"],
        "additionalProperties": False,
    },
}

_CONTRACT_CHECKS = {
    "type": "array",
    "description": (
        "For every contract formalized as a postcondition: concrete cases judged from the ENGLISH alone. "
        "At least one output that violates the contract and one that satisfies it, for inputs that satisfy the precondition."
    ),
    "items": {
        "type": "object",
        "properties": {
            "contract": {"type": "integer"},
            "args": {"type": "string", "description": "JSON array of arguments"},
            "output": {"type": "string", "description": 'JSON value of a possible result, or "raises"'},
            "expect": {"type": "string", "enum": ["violates", "satisfies"]},
        },
        "required": ["contract", "args", "output", "expect"],
        "additionalProperties": False,
    },
}


def formalize_schema(with_examples: bool, with_contracts: bool = False) -> dict:
    import copy

    sch = copy.deepcopy(FORMALIZE_SCHEMA)
    sch["properties"]["input_bounds"] = _INPUT_BOUNDS
    sch["required"] = sch["required"] + ["input_bounds"]
    if with_examples:
        sch["properties"]["documented_examples"] = {
            "type": "string",
            "description": 'JSON array of {"args": [...], "expected": value or "raises"} taken verbatim from the documentation',
        }
        sch["required"] = sch["required"] + ["documented_examples"]
    if with_contracts:
        contract_field = {"type": "integer", "description": "number of the developer's contract this formalizes, or -1"}
        for key in ("postconditions", "properties"):
            item = sch["properties"][key]["items"]
            item["properties"]["contract"] = contract_field
            item["required"] = item["required"] + ["contract"]
        sch["properties"]["contract_checks"] = _CONTRACT_CHECKS
        sch["required"] = sch["required"] + ["contract_checks"]
    return sch


def contracts_section(contracts: list, extra: int = 2) -> str:
    """User-prompt section listing the developer's contracts (LARCH.md)."""
    lines = []
    for i, c in enumerate(contracts):
        lines.append(f"{i}. {c.text}")
        if c.lean:
            lines.append(f"   Exact Lean (use VERBATIM as the body): `{' '.join(c.lean.split())}`")
    return f"""
## The developer's contracts (from LARCH.md)
These are requirements written by the developer. Formalize EACH one: as exactly one
postcondition when it is about a single call, or as one property over `model` when it
relates several calls (e.g. "a heavier parcel never costs less"). Set its "contract"
field to the number below. Preserve the meaning exactly: do not weaken, strengthen or
reinterpret it. If the wording is ambiguous, take the reading a careful reviewer would
and explain it in "notes". In "english", restate what YOUR Lean says in plain words; the
reviewer compares it with the original. The model must satisfy every contract that
matches the documented intent; if a contract seems to contradict the documentation,
still formalize it and say so in "notes".
{chr(10).join(lines)}

You may add at most {extra} postconditions of your own ("contract": -1), only if they
catch plausible bugs the contracts above would miss. Fill "contract_checks" as described.
"""


def render_previous(data: dict) -> str:
    """Show a previous formalization readably (real newlines in code), so repairs do not
    copy JSON escaping into Lean code."""
    if not isinstance(data, dict):
        return "```json\n" + dump(data) + "\n```"
    from .util import unescape_code

    parts = ["Model:", "```lean", unescape_code(str(data.get("model", ""))), "```"]
    pre = data.get("precondition") or {}
    parts += [f"Precondition: `{pre.get('lean', '')}`  ({pre.get('english', '')})", "Postconditions:"]
    for p in data.get("postconditions") or []:
        if isinstance(p, dict):
            parts.append(f"- {p.get('name')}: `{p.get('lean')}`  ({p.get('english')})")
    for q in data.get("properties") or []:
        if isinstance(q, dict):
            ps = ", ".join(f"{x.get('name')} : {x.get('lean_type')}" for x in q.get("params") or [] if isinstance(x, dict))
            parts.append(f"- property {q.get('name')} ({ps}): `{q.get('lean')}`  ({q.get('english')})")
    gen = data.get("input_generator", data.get("strategy", ""))
    parts += ["Input generator:", "```python", unescape_code(str(gen)), "```"]
    rest = {k: v for k, v in data.items() if k not in ("model", "precondition", "postconditions", "properties", "input_generator", "strategy")}
    parts += ["Other fields:", "```json", dump(rest), "```"]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Stateful components (classes)
# ---------------------------------------------------------------------------

_COMPONENT_BODY = """\
You are the formalization engine of Larch, a verification tool. Given a CLASS from a
user's codebase, you write an executable reference model of the object as a Lean 4
STATE MACHINE, and its contracts:
  1. `structure State` holding exactly the information the object's behaviour depends on,
  2. `def init` (the constructor) and one `def op_<method>` per public method,
  3. `def obs_<name>` for read-only observers that can be compared after every call,
  4. INVARIANTS (true in every state the object can reach) and OPERATION CONTRACTS
     (what one call of one method guarantees), which Larch proves about the model,
  5. an INPUT GENERATOR for random call sequences.
Larch then runs thousands of random call sequences on the real object and on the model,
comparing every result and every observer after every call.

## Required shapes (inside `namespace Larch`, which Larch opens for you)
- `structure State where ... deriving Repr, DecidableEq` (use List (K × V) association
  lists for maps; keep it canonical, e.g. no duplicate keys, so equality is meaningful).
- `def init (ctor params) : Option State` — `none` iff the constructor raises.
- `def op_<method> (s : State) (params) : Option (State × R)` — `none` iff the method
  raises; a raised error leaves the state unchanged in the MODEL. If the real code
  changes state and then raises, that is a bug the comparison will find.
  R is `Unit` for methods returning None/void.
- `def obs_<name> (s : State) : T` for each observer you list. List as an observer EVERY
  zero-argument method or property that only reads state (e.g. `total()`, `size`), even if
  it is also an operation: Larch reads all observers after every call, which is what
  catches a method that corrupts the state and then raises.
- Model the INTENDED behaviour from the docstrings, names and obvious purpose. Never copy
  a bug (for example, a transfer that credits before checking funds).
- The developer's contracts are the specification. Where the code is narrower or
  different, the model and the contract follow the developer's words, not the code.
- Larch generates `Reachable`, `pre_init`, `inv_*`, `post_*` and `spec_*`; never define them.

## Contracts
- An invariant's `lean` is a decidable Prop over `s : State`, e.g. `∀ p ∈ s.balances, 0 ≤ p.2`.
  Larch proves `∀ s, Reachable s → inv s`.
- An operation contract names its `operation` (method). Its `lean` is a decidable Prop over
  `s : State`, the method's parameters (same names), and `result : Option (State × R)`:
  `result = none` means it raised; `∀ r ∈ result, P r.1 r.2` constrains the new state
  `r.1` and value `r.2`. Larch proves it for every call from every reachable state.
  Example: `amount > bal s account → result = none` ("fails when funds are short"), or
  `∀ r ∈ result, total r.1 = total s` ("never changes the total").
- Use bounded quantifiers only (`∀ p ∈ s.items`), never `match` inside a contract (define a
  Bool helper in the model instead).

## Input generator
`input_generator` is Python SOURCE CODE defining `def strategy(st):` (`st` is
`hypothesis.strategies`; no imports) that returns a dict: `"init"` -> a strategy of
constructor argument tuples, and each method name -> a strategy of its argument tuples.
Use small value pools so calls interact: e.g. account names from `st.sampled_from(["a", "b", "c"])`
and amounts from `st.integers(-2, 20)` or a handful of values, including invalid values the
methods must reject. Values should collide often, so later calls see the effects of earlier ones.
`exhaustive_domains` is a JSON object with the same keys, each a SHORT explicit list of
argument arrays (2-6 entries), e.g. {"init": [[]], "deposit": [["a", 1], ["b", 5], ["a", -1]]};
Larch runs every call sequence over them up to the longest length that fits its budget.
Use "" if the domains cannot be small.
"""

COMPONENT_SCHEMA = {
    "type": "object",
    "properties": {
        "understanding": {"type": "string"},
        "constructor": {
            "type": "object",
            "properties": {
                "params": {"type": "array", "items": _PARAM},
                "precondition": {"type": "object", "properties": {"english": {"type": "string"}, "lean": {"type": "string"}},
                                 "required": ["english", "lean"], "additionalProperties": False},
            },
            "required": ["params", "precondition"],
            "additionalProperties": False,
        },
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"method": {"type": "string"}, "params": {"type": "array", "items": _PARAM},
                               "returns": {"type": "string", "description": "Lean type of the result; Unit for None/void"}},
                "required": ["method", "params", "returns"],
                "additionalProperties": False,
            },
        },
        "observers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "lean_type": {"type": "string"},
                               "access": {"type": "string", "enum": ["attribute", "call"]}},
                "required": ["name", "lean_type", "access"],
                "additionalProperties": False,
            },
        },
        "model": {"type": "string", "description": "Lean code: structure State, helpers, init, op_*, obs_*"},
        "invariants": {
            "type": "array",
            "items": {"type": "object", "properties": {"name": {"type": "string"}, "english": {"type": "string"},
                                                       "lean": {"type": "string"}, "contract": {"type": "integer"}},
                      "required": ["name", "english", "lean", "contract"], "additionalProperties": False},
        },
        "operation_contracts": {
            "type": "array",
            "items": {"type": "object", "properties": {"name": {"type": "string"}, "english": {"type": "string"},
                                                       "operation": {"type": "string"}, "lean": {"type": "string"},
                                                       "contract": {"type": "integer"}},
                      "required": ["name", "english", "operation", "lean", "contract"], "additionalProperties": False},
        },
        "input_generator": {"type": "string"},
        "exhaustive_domains": {"type": "string", "description": "JSON object of short argument lists, or empty"},
        "notes": {"type": "string"},
    },
    "required": ["understanding", "constructor", "operations", "observers", "model", "invariants",
                 "operation_contracts", "input_generator", "exhaustive_domains", "notes"],
    "additionalProperties": False,
}


def component_system(type_guide: str = PYTHON_TYPE_GUIDE) -> str:
    return _COMPONENT_BODY + "\n" + lean_env(type_guide) + "\n" + FORBIDDEN + "\n"


def component_user(info, contracts: list, language: str | None = None) -> str:
    language = language or _LANG_NAMES.get(getattr(info, "language", "python"), ("Python", ""))[0]
    fence = _fence(info)
    methods = "\n".join(
        f"- `{m.name}`" + (" (read-only property)" if m.kind == "property" else
                            f"({', '.join(p.name + (': ' + p.annotation if p.annotation else '') for p in m.params)})"
                            + (f" -> {m.returns}" if m.returns else ""))
        for m in info.methods
    )
    ctor = ", ".join(f"`{p.name}`" + (f": {p.annotation}" if p.annotation else "") + f" → Lean `{p.lean_name}`" for p in info.params) or "(none)"
    ctx = f"\n## Surrounding module context\n```{fence}\n{info.context}\n```\n" if info.context.strip() else ""
    numbered = ""
    if contracts:
        lines = []
        for i, c in enumerate(contracts):
            lines.append(f"{i}. {c.text}")
            if c.lean:
                lines.append(f"   Exact Lean (use VERBATIM as the body): `{' '.join(c.lean.split())}`")
        numbered = (
            "\n## The developer's contracts (from LARCH.md)\nRequirements written by the developer. Formalize EACH one as "
            "an invariant or an operation contract and set its \"contract\" field to its number. Preserve the meaning exactly; "
            "in \"english\", restate what YOUR Lean says. You may add at most 2 contracts of your own (\"contract\": -1).\n"
            + "\n".join(lines) + "\n"
        )
    return f"""\
## Task
Formalize the {language} class `{info.name}` from `{info.path.name}` as a state machine.

## Class
```{fence}
{info.source}
```
{ctx}
## Constructor parameters (use these Lean names, in this order)
{ctor}

## Public methods (model each one as an operation, or as an observer if it only reads state and takes no arguments)
{methods}
{numbered}
Return the JSON object described by the schema. Contract names are snake_case identifiers.
"""


# ---------------------------------------------------------------------------
# Services (HTTP APIs with a database)
# ---------------------------------------------------------------------------

_SERVICE_BODY = """\
You are the formalization engine of Larch, a verification tool. Given an HTTP SERVICE
(its OpenAPI description and route source code), you write an executable reference
model of its behaviour as a Lean 4 STATE MACHINE, and its contracts:
  1. `structure State` holding what the service stores (its database, abstractly),
  2. `def init : Option State` (an empty database) and one `def op_<name>` per endpoint,
  3. INVARIANTS (true in every reachable state) and OPERATION CONTRACTS (what one request
     guarantees), which Larch proves about the model,
  4. an INPUT GENERATOR for random request sequences.
Larch starts the real service on a fresh database, sends thousands of random request
sequences to it and to the model, and compares status codes and response fields after
every request. Before every sequence the database is emptied and identity sequences
restart, so serial IDs are deterministic: the first row created gets id 1, then 2, ...
Model IDs the same way (a counter starting at 1).

## Required shapes (inside `namespace Larch`, which Larch opens for you)
- `structure State where ... deriving Repr, DecidableEq` (association lists for tables).
- `def init : Option State := some ⟨...⟩` (the empty service).
- For each operation: `def op_<name> (s : State) (params) : Option (State × (Nat × Option T1 × ... × Option Tn))`
  returning `some (s', (status, f1, ..., fn))` — the HTTP status code and the selected
  response fields, in the order you list them (all `none` when a field is absent, e.g. on
  errors). Requests never "raise": a rejected request returns its 4xx status and the
  unchanged state. If an operation selects no fields, its result is just `Nat`.
- Model the INTENDED behaviour from the API description and docstrings. Never copy a bug
  (for example, a payment that debits the wallet before checking the order's status).
- The developer's contracts are the specification. Where the code is narrower or
  different (a SQL filter such as `AND status = 'pending'`, an extra condition, a
  different error), the model and the contract follow the developer's words, not the
  code: the disagreement is exactly what testing is meant to find.
- Validation errors (e.g. 422 for a non-positive amount) are part of the behaviour: model
  them exactly as documented.
- Larch generates `Reachable`, `pre_init`, `inv_*`, `post_*`, `spec_*`; never define them.

## Contracts
- Invariant: decidable Prop over `s : State`. Operation contract: decidable Prop over `s`,
  the operation's parameters, and `result : Option (State × R)`; e.g. "a rejected
  payment changes nothing": `∀ r ∈ result, r.2.1 ≠ 200 → r.1 = s`.
- Bounded quantifiers only; no `match` inside a contract (define Bool helpers instead).

## Parameters
Each operation parameter says where it goes: `path` (fills `{key}` in the path), `query`,
`header` (e.g. Idempotency-Key), or `body` (a field of the JSON body). Use small value
pools in the generator so requests interact: ids from `st.integers(1, 4)`, customer names
from `st.sampled_from(["c1", "c2"])`, amounts from a handful of values such as
`st.sampled_from([-1, 0, 30, 50, 100])` (so deposits, prices and balances line up and the
success paths are reached often), header keys from `st.sampled_from(["k1", "k2"])`. Mark a
response field `opaque` only if its value is random (UUIDs, timestamps); serial ids are not
opaque.

`input_generator` is Python SOURCE CODE defining `def strategy(st):` (no imports) returning
a dict from operation name to a strategy of its argument tuples (and "init": st.just(())).
`exhaustive_domains`: a JSON object with the same keys, each a SHORT list (2-4) of argument
arrays, or "". Pick values that reach each endpoint's success path in a few calls (a deposit
large enough to pay for an order, the ids those orders get).
"""

SERVICE_SCHEMA = {
    "type": "object",
    "properties": {
        "understanding": {"type": "string"},
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "snake_case operation name, e.g. pay_order"},
                    "verb": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                    "path": {"type": "string", "description": "path template, e.g. /orders/{order_id}/pay"},
                    "params": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}, "lean_type": {"type": "string"},
                                       "in": {"type": "string", "enum": ["path", "query", "header", "body"]},
                                       "key": {"type": "string", "description": "path placeholder, query/header name, or JSON body key"}},
                        "required": ["name", "lean_type", "in", "key"], "additionalProperties": False}},
                    "response_fields": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"key": {"type": "string", "description": "JSON path in the response body, e.g. status or items.0.id"},
                                       "lean_type": {"type": "string", "description": "type of the field when present (Larch wraps it in Option)"},
                                       "opaque": {"type": "boolean"}},
                        "required": ["key", "lean_type", "opaque"], "additionalProperties": False}},
                },
                "required": ["name", "verb", "path", "params", "response_fields"],
                "additionalProperties": False,
            },
        },
        "model": {"type": "string"},
        "invariants": COMPONENT_SCHEMA["properties"]["invariants"],
        "operation_contracts": COMPONENT_SCHEMA["properties"]["operation_contracts"],
        "input_generator": {"type": "string"},
        "exhaustive_domains": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["understanding", "operations", "model", "invariants", "operation_contracts", "input_generator",
                 "exhaustive_domains", "notes"],
    "additionalProperties": False,
}


def service_system(type_guide: str = PYTHON_TYPE_GUIDE) -> str:
    return _SERVICE_BODY + "\n" + lean_env(type_guide) + "\n" + FORBIDDEN + "\n"


def service_user(name: str, endpoints: str, sources: list[tuple[str, str]], contracts: list) -> str:
    srcs = "\n\n".join(f"### {path}\n```\n{text}\n```" for path, text in sources) or "(source not available)"
    numbered = ""
    if contracts:
        lines = []
        for i, c in enumerate(contracts):
            lines.append(f"{i}. {c.text}")
            if c.lean:
                lines.append(f"   Exact Lean (use VERBATIM as the body): `{' '.join(c.lean.split())}`")
        numbered = (
            "\n## The developer's contracts (from LARCH.md)\nFormalize EACH one as an invariant or an operation contract and "
            "set its \"contract\" field to its number. Preserve the meaning exactly; in \"english\", restate what YOUR Lean "
            "says. You may add at most 2 contracts of your own (\"contract\": -1).\n" + "\n".join(lines) + "\n"
        )
    return f"""\
## Task
Formalize the HTTP service `{name}` as a state machine.

## Endpoints (from its OpenAPI description)
{endpoints}

## Source code
{srcs}
{numbered}
Model every endpoint that changes or reads state. Return the JSON object described by the
schema. Operation and contract names are snake_case identifiers.
"""


# ---------------------------------------------------------------------------
# System rules (a `# System rules` bullet in LARCH.md)
# ---------------------------------------------------------------------------

RULE_SCHEMA = {
    "type": "object",
    "properties": {
        "english": {"type": "string", "description": "what YOUR Lean statement says, in plain English"},
        "lean": {"type": "string", "description": "the rule as a Lean Prop over the components' definitions (no `def`, no `theorem`)"},
        "assumes": {"type": "array", "items": {"type": "string"},
                    "description": "component contracts the rule follows from, by their full names as listed"},
        "notes": {"type": "string"},
    },
    "required": ["english", "lean", "assumes", "notes"],
    "additionalProperties": False,
}

RULE_SYSTEM = f"""\
You are Larch's system-rule formalizer. A developer states a guarantee about how several
components of their system work together. Each component already has an executable Lean
model, checked against the real code by differential testing, and a set of contracts
(Lean propositions `spec_*`) about that model.

Write the rule as ONE Lean proposition over the components' definitions, and name the
component contracts it follows from. Larch then proves

    (contract₁) → (contract₂) → … → (your rule)

so the rule is established from the contracts (assume–guarantee reasoning): it holds for
any implementation that meets them, and Larch reports which contracts it rests on.

Rules:
- Refer to definitions by their namespaced names exactly as shown (e.g. `Cart.Reachable`,
  `Cart.op_add`, `splitPayment.model`, `splitPayment.pre`). `Larch` is open.
- Preserve the developer's meaning exactly. Quantify over everything the English leaves
  free; for a stateful component, quantify over its reachable states (`X.Reachable s`).
  Add only the hypotheses a component's own precondition requires, and say so in "english".
- "assumes" lists contracts by full name (`Cart.spec_no_negative_qty`). Choose the ones
  a proof needs; prefer few. Listing a contract you do not need is harmless; missing one
  may make the rule unprovable.
- The proposition must type-check against the definitions shown. Use only core Lean 4
  (no Mathlib). {FORBIDDEN}
"""


def rule_user(rule_text: str, modules: str, inventory: list[tuple[str, str, str]], feedback: str = "") -> str:
    inv = "\n".join(f"- `{n}` ({status}): {english}" for n, english, status in inventory) or "(none)"
    out = (f"## The rule (from LARCH.md)\n{rule_text}\n\n## Component definitions (already imported)\n```lean\n{modules}\n```\n\n"
           f"## Component contracts available as assumptions\n{inv}\n")
    if feedback:
        out += f"\n## Your previous answer had problems\n{feedback}\n\nFix them and answer again.\n"
    return out


def rule_prove_user(modules: str, statement: str, english: str, header: str, attempts: list[tuple[str, str]]) -> str:
    parts = [
        "## Definitions (already imported; `Larch` and `Larch.System` are open)",
        f"```lean\n{modules}\n```",
        "## Target",
        f"Prove `rule`, which is:\n```lean\n{statement}\n```",
        f"In plain English: {english}",
        "The hypotheses are component contracts: `intro` them and use them (unfold a `spec_*` with `unfold` or "
        "`simp only [...] at h` to see its statement). Unfold the component definitions as needed.\n"
        "- A state reached by an operation is reachable again: from `hs : X.Reachable s` and "
        "`h : X.op_m s args = some (s', r)` you get `X.Reachable.op_m s args s' r hs h : X.Reachable s'` "
        "(constructor arguments in the order shown by `inductive Reachable`). Use it to apply a contract twice.\n"
        "- `∀ r ∈ o, P r` for an `Option`: `intro r hr` then `simp at hr` or `cases o` / `Option.mem_def`.\n"
        "- Facts about a lookup after an update (e.g. `List.find?` after `List.map`) need a helper lemma "
        "proved by `induction xs with | nil => simp | cons x xs ih => simp [List.map, List.find?]; split <;> simp_all`. "
        "State it for an arbitrary list, prove it first, then use it.",
        f"Your block must end with a theorem using exactly this header:\n```lean\n{header} := by\n```",
    ]
    if attempts:
        parts.append("## Your previous attempts and Lean's errors (most recent last)")
        for i, (code, err) in enumerate(attempts, 1):
            parts.append(f"### Attempt {i}\n```lean\n{code}\n```\nLean reported:\n```\n{err}\n```")
        parts.append("Write a corrected, complete proof. Change strategy if the same approach keeps failing.")
    return "\n\n".join(parts)


COMPONENT_MODEL_REPAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "model": {"type": "string", "description": "the complete corrected model code (State, init, op_*, obs_*, helpers)"},
        "explanation": {"type": "string", "description": "one sentence: what was wrong in the model"},
    },
    "required": ["model", "explanation"],
    "additionalProperties": False,
}


def component_model_repair_user(info, model_text: str, issues: list[str], problems: list[str]) -> str:
    out = f"""\
## The code
```
{info.source}
```

## Current Lean model and APPROVED contracts (generated file; only the model code may change)
```lean
{model_text}
```

## Where the model is wrong (a reviewer compared it with the code and its documented intent)
{chr(10).join('- ' + i for i in issues)}

Return the complete corrected model code: the `structure State`, `init`, every `op_*` and
`obs_*` with the SAME names and signatures, and any helpers. Change only what the problem
requires. Every approved contract must still hold for the corrected model.
"""
    if problems:
        out += "\n## Your previous revision was rejected\n" + "\n".join(f"- {p}" for p in problems) + "\n"
    return out


FIDELITY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {"index": {"type": "integer"}, "faithful": {"type": "boolean"},
                           "problem": {"type": "string", "description": "empty if faithful; else what was narrowed, widened or changed"}},
            "required": ["index", "faithful", "problem"], "additionalProperties": False}},
    },
    "required": ["items"],
    "additionalProperties": False,
}

FIDELITY_SYSTEM = """\
You review formalizations of a developer's requirements. For each requirement you get the
developer's words and a formal reading (English restatement and Lean). Decide whether
the reading means the same thing. It is NOT faithful if it narrows the requirement
(adds a condition the developer did not state, such as "if the order is still pending"
when the developer said "whatever has happened since"), widens it, or changes what is
promised. Precision the developer left implicit (types, how absent values are encoded,
the status codes of the API) is fine. Judge only meaning; do not judge style."""


def fidelity_user(items: list[tuple[str, str, str]]) -> str:
    out = []
    for i, (wrote, english, lean) in enumerate(items):
        out.append(f"### {i}\nDeveloper: {wrote}\nReading: {english}\nLean: `{' '.join(lean.split())}`")
    return "\n\n".join(out)


# ---------------------------------------------------------------------------
# When most inputs disagree: one diagnosis over many examples
# ---------------------------------------------------------------------------

SYSTEMATIC_SYSTEM = """\
You are diagnosing a differential test in which the implementation and a formal reference
model (written from the documentation) disagree on MOST inputs. A real bug rarely makes
code wrong on almost every input; usually one thing explains them all. Look at the
examples together, work two of them by hand, and name the common cause:

- "model_misread": the model gets the function's purpose or a central convention wrong
  (for example sorts descending instead of ascending, counts from 1 instead of 0, or
  treats a parameter as something else). The implementation is what the documentation
  describes.
- "representation": both compute the same thing but present it differently (a tuple
  versus a list, None versus an empty value, cents versus units, a different ordering
  the documentation does not fix).
- "larch_calls_it_wrong": the implementation fails or misbehaves because the inputs it
  receives do not match what it actually accepts (wrong types, wrong argument order,
  values it cannot handle that the documentation excludes), not because of its logic.
- "implementation_wrong": the implementation really is wrong in general, against the
  documentation.
- "mixed": no single cause.
"""

SYSTEMATIC_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["model_misread", "representation", "larch_calls_it_wrong",
                                               "implementation_wrong", "mixed"]},
        "common_cause": {"type": "string", "description": "one or two sentences: the single thing that explains the examples"},
        "explanation": {"type": "string"},
    },
    "required": ["verdict", "common_cause", "explanation"],
    "additionalProperties": False,
}


def systematic_user(info, spec, diag: dict) -> str:
    ex = "\n".join(f"- input ({s.get('args_repr')}): implementation {s.get('impl')}; model {s.get('model')}"
                   for s in diag["samples"])
    crash = (f"\nThe implementation raised the same exception on nearly every disagreeing input: {diag['uniform_crash']}\n"
             if diag.get("uniform_crash") else "")
    return f"""\
## Function
```{_fence(info)}
{info.source}
```

## The model's understanding
{spec.understanding}

## Reference model (Lean)
```lean
{spec.model_code}
```

## Disagreements: {diag['dis']:,} of {diag['valid']:,} inputs ({diag['rate']:.0%})
{ex}
{crash}"""
