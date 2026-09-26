"""Prompts and output schemas for every LLM stage.

Prompts are deliberately stable strings (stable prefix = prompt-cache friendly);
per-call content goes in the user message.
"""
from __future__ import annotations

import json

LEAN_ENV = """\
## Lean environment
Lean 4.34, core library only (no Mathlib, no Batteries). All code lives inside
`namespace Larch`, which Larch opens for you: never write `namespace`, `end`, `import`
or `open` for it.

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

FORBIDDEN = """\
Forbidden anywhere: sorry, admit, axiom, partial, unsafe, opaque, implemented_by, extern,
native_decide, macro/syntax/notation/elab, #eval, import, set_option (except maxHeartbeats),
and `instance` declarations (use `deriving` instead)."""

FORMALIZE_SYSTEM = f"""\
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
{{
  "understanding": "Returns the smallest i with xs[i] == target, or None when target is absent. Empty list gives None.",
  "params": [{{"name": "xs", "lean_type": "List Int"}}, {{"name": "target", "lean_type": "Int"}}],
  "return_type": "Option Nat",
  "exceptions": false,
  "model": "def model (xs : List Int) (target : Int) : Option Nat :=\\n  match xs with\\n  | [] => none\\n  | x :: rest => if x = target then some 0 else (model rest target).map (· + 1)",
  "precondition": {{"english": "No restriction.", "lean": "True"}},
  "postconditions": [
    {{"name": "found_is_target", "english": "If an index i is returned, xs[i] equals target.", "lean": "∀ i ∈ result, xs[i]? = some target"}},
    {{"name": "first_occurrence", "english": "No position before the returned index holds target.", "lean": "∀ i ∈ result, ∀ j, j < i → xs[j]? ≠ some target"}},
    {{"name": "none_iff_absent", "english": "None is returned exactly when target does not occur in xs.", "lean": "result = none ↔ target ∉ xs"}}
  ],
  "properties": [],
  "input_generator": "def strategy(st):\\n    small = st.integers(-3, 3)\\n    return st.tuples(st.lists(small, max_size=8), small)",
  "edge_cases": "[[[], 1], [[1], 1], [[2, 1, 1], 1], [[1, 2], 3]]",
  "notes": ""
}}

{LEAN_ENV}
{FORBIDDEN}
"""

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


def formalize_user(info, mode: str = "hybrid", language: str = "Python") -> str:
    params = "\n".join(
        f"- `{p.name}`" + (f": {p.annotation}" if p.annotation else "") + f"  →  Lean name `{p.lean_name}`"
        for p in info.params
    )
    if mode == "intent":
        body = f"```python\n{info.signature}:\n    \"\"\"{info.docstring or ''}\"\"\"\n    ...\n```"
    else:
        body = f"```python\n{info.source}\n```"
    ctx = f"\n## Surrounding module context\n```python\n{info.context}\n```\n" if info.context.strip() else ""
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
```json
{previous}
```

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
    specs = "\n".join(f"- {p.name}: {p.english}" for p in spec.active_posts())
    return f"""\
## Function under test
```python
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
```python
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


def fix_user(info, spec, recs: list[dict], feedback: str | None = None) -> str:
    specs = "\n".join(f"- {p.name}: {p.english}" for p in spec.active_posts())
    ex = "\n".join(
        f"- `{info.name}({r.get('args_repr')})` returned {r.get('impl')}, expected {r.get('model')}"
        + (f" (violates: {', '.join(r['impl_violates'])})" if r.get("impl_violates") else "")
        for r in recs
    )
    fb = f"\n## Your previous fix was rejected\n{feedback}\n" if feedback else ""
    return f"""\
## Function
```python
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
{fb}"""


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


def formalize_schema(with_examples: bool) -> dict:
    if not with_examples:
        return FORMALIZE_SCHEMA
    import copy

    sch = copy.deepcopy(FORMALIZE_SCHEMA)
    sch["properties"]["documented_examples"] = {
        "type": "string",
        "description": 'JSON array of {"args": [...], "expected": value or "raises"} taken verbatim from the documentation',
    }
    sch["required"] = sch["required"] + ["documented_examples"]
    return sch
