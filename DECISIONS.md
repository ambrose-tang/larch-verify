# Design decisions

This file records each major decision and why it was made. Decisions marked
**(eval-driven)** were settled by the benchmark comparisons in [EVALS.md](EVALS.md).

---

## D1. What the product is: Cedar's verification-guided development, per function

**Decision.** For a target function, Larch
1. writes an **executable Lean 4 model** of the function's *intended* behaviour, plus
   a precondition and specs (postconditions and model properties);
2. shows the specs to the user **in plain English for approval**;
3. **differentially tests** the real implementation against the model on thousands of
   random inputs, evaluating the approved postconditions on the implementation's own
   outputs;
4. **proves** every approved spec about the model in Lean, and accepts a proof only
   through an out-of-process checker;
5. runs **mutation analysis** on the implementation, to check that the specs and tests
   would catch plausible bugs;
6. reports findings with minimal counterexamples and proposes fixes as **validated
   diffs**, without touching user code.

**Why.** This is the methodology AWS used for Cedar: a readable, verified model plus
differential random testing (DRT) of the production code against it. Proving things
about the *real* Python is out of reach for an LLM-driven tool today. Python has no
formal semantics, and a tool that claimed to prove them would be lying. Proving
things about a small model is tractable, and the model is only useful because DRT ties it
to the code. The report is explicit about this split. It says "specs *proved* about
the model" and "implementation *agrees* with the model on N inputs", never "your code is
verified".

## D2. Tool language: Python. First target language: Python

**Decision.** Larch is written in Python (3.11+), packaged with hatchling, and installable with
`uv tool install` / `pipx`. The first supported target language is Python.

**Why.**
- The first target is Python, and the standard library's `ast` gives exact function
  extraction, context slicing, source splicing and mutation operators for free.
- Hypothesis is the best property-based generation and *shrinking* engine available,
  and it is Python-native. Minimal counterexamples are what make bug reports actionable.
- Rich gives a Claude-Code-like terminal UX (streamed steps, spinners, panels, diffs)
  with little code.
- The Anthropic Python SDK is first-party.

The pipeline is target-agnostic apart from `larch/py/` (extraction, worker, mutation),
so a TypeScript or Rust adapter only has to provide those three pieces.

## D3. First domain: pure functions over exact data

**Decision.** v1 supports module-level functions and `@staticmethod`s whose parameters
and results are built from `int`, `bool`, `str`, single characters, `list`, `tuple`,
and `Optional`, and that may raise documented exceptions. Floats, dicts, classes with
state, I/O, async and generators are rejected with a clear message.

**Why.** These types map *exactly* onto Lean (`Int`, `Bool`, `String`, `Char`, `List`,
`×`, `Option`), and raising maps to `none` (see D14). Python ints are unbounded like Lean's `Int`, so no overflow
modelling is needed. The two-way JSON codec is total and unambiguous
(`larch/lean/types.py`), which rules out a whole class of false alarms. Floats would
need a rounding-aware model and proofs about IEEE-754, which is a separate product.
This subset still covers the "critical little functions" where bugs hide: parsers,
checksums, calendars, intervals, search, authorization rules and rate limiters.

## D4. Lean: pinned toolchain, core library only, no Mathlib

**Decision.** Larch pins `leanprover/lean4:v4.34.1`, installs it through elan
(`larch doctor --install`), and uses only Lean's core library.

**Why.**
- *Zero setup.* Mathlib is several GB and adds seconds to every import. Core Lean now
  ships `grind` (an SMT-style tactic that does case splits, linear integer arithmetic and
  congruence closure, and knows many List lemmas), `omega`, `simp`, `decide` and
  `fun_induction`. That proved enough for this domain. On the benchmark, most specs are
  closed by the no-LLM portfolio alone (see EVALS.md).
- *Pinning.* Prompts, the tactic portfolio, the JSON harness and the checker's use of
  the Lean API are version-sensitive: `String.get` was deprecated and `List.enum` was
  renamed `zipIdx` in this release. A pinned toolchain makes results reproducible.
- Larch calls the toolchain binary directly with `LEAN_SYSROOT` set. Going through
  elan's proxy costs about 3 s per invocation.

## D5. One uniform spec shape: decidable propositions

**Decision.** Every spec is `spec_NAME : Prop := ∀ params, body`, where `body` is a
*decidable* proposition. Postconditions are `pre inputs → post inputs (model inputs)`,
and `post` takes the result as a parameter. Properties quantify over their own
parameters and may call the model.

**Why.** Decidability lets one statement do three jobs:
1. **Tested before proved.** The formalizer's sanity pass evaluates every spec on
   hundreds of inputs with `decide` before any proof effort. A false spec is caught in
   about a second, with a concrete counterexample, instead of after a failed proof search.
2. **Evaluated on the implementation's outputs.** Because `post` takes the result as an
   argument, the harness also checks `post inputs (implementation's output)`. A violation
   is a *confirmed* bug against a user-approved spec, and needs no LLM judgement.
3. **Vacuity checks.** Correct outputs are perturbed, and a postcondition that accepts
   every perturbation is flagged as possibly vacuous.

## D6. Soundness: the LLM's claims are never trusted

**Decision.** A spec counts as *proved* only if the **out-of-process checker**
(`larch/lean/assets/Checker.lean`, compiled once and cached) loads the compiled
environment and confirms three things:
- the theorem exists and its type is *exactly* the approved spec constant, compared as
  kernel terms and not as text;
- its transitive axioms are a subset of `{propext, Classical.choice, Quot.sound}`, which
  rejects `sorry`, `admit`, `native_decide` (whose auxiliary axiom is caught) and new axioms;
- `leanchecker` replays the module through the kernel.

A lint pass (defence in depth, and early feedback for the model) rejects `sorry`,
`admit`, `axiom`, `native_decide`, `implemented_by`/`extern`, `partial`/`unsafe`/`opaque`,
macros, notation, `#eval`, imports and non-allowlisted `set_option`. The model file may
not declare `instance`s.

**Why.** The failure modes are real: the model sometimes weakens a statement, or reaches for
`native_decide` or `sorry` under pressure. Comparing the type to a spec constant that
was elaborated *before* any LLM proof code runs means notation or instance tricks cannot
change what is proved. `tests/test_integration.py` includes these attacks. Forbidding
`implemented_by`/`extern`/`partial` guarantees that the code differentially tested is
the same logical definition the proofs are about.

## D7. Architecture: a staged pipeline of small agent loops, not one free-roaming agent

**Decision.** Larch is a deterministic pipeline: formalize → review → DRT → adjudicate or
repair → prove → mutation analysis → fix. Each LLM stage is a *bounded repair loop* with
tool feedback: Lean errors, failing counterexamples, lint results. Every LLM call is
stateless (a single prompt holding the relevant history) and uses structured output
where the result is data.

**Why.** Verification needs predictable cost and auditability. Each stage has a
machine-checkable success criterion (compiles, tests pass, checker accepts), so an
open-ended agent adds cost and nondeterminism without adding capability. Stateless
calls make the stages provider-agnostic, cacheable (the benchmark re-runs for free) and
easy to log. Every prompt and response is written to `<run>/llm/`.

## D8. LLM providers: Anthropic API, or the user's Claude Code login

**Decision.** `--provider auto` uses the Anthropic API when `ANTHROPIC_API_KEY` or
`ANTHROPIC_AUTH_TOKEN` is set. Otherwise it uses headless Claude Code (`claude -p`) with
tools, hooks, MCP and settings disabled and the system prompt replaced. A
content-addressed response cache (`--cache`, always on in the benchmark) records nominal
cost separately from actual spend. A global semaphore limits concurrent CLI calls.

**Why.** Developers with a Claude subscription and no API key can use Larch
immediately, and teams can use API keys in CI. Cost is reported either way: the CLI
reports `total_cost_usd`, and the API path prices tokens from a table.

## D9. User code runs in a sandboxed worker; read-only by default

**Decision.** User code runs in a separate process (`larch/py/worker.py`) under the
*user's* interpreter (their project's `.venv` is detected). It gets per-call SIGALRM
timeouts, stdout/stderr redirected away from the result channel, stdin closed,
arguments deep-copied, and no bytecode writes. Hypothesis runs with `database=None`
and a scratch storage directory. Artifacts (the Lean model, proofs, transcripts,
reports, patches) go to `~/.cache/larch/runs/…`, not the repository. Fixes are shown as
diffs and saved as `.patch` files. `--apply` asks before writing, and only applies fixes
that were validated against the model.

**Why.** It is the user's code and the user's repository. A verification tool that
edits files or leaves `__pycache__`/`.hypothesis` behind is not trusted in CI. Timeouts
turn infinite loops into reportable findings ("does not terminate on a valid input")
instead of hung runs.

## D10. Findings are classified by evidence, not by LLM opinion

**Decision.**
- *Confirmed*: the implementation's output violates an approved postcondition, or the
  implementation hangs.
- *Likely*: the implementation disagrees with the model (a value, crash or type
  mismatch) and an adjudicator LLM, given the docstring and the concrete case, attributes
  it to the implementation.
- *Possible*: the adjudicator cannot tell. This is reported, but does not make the
  verdict "bug".
- If the adjudicator says the *model* is wrong, the model is repaired under the fixed,
  approved specs, and the repaired model must still pass them.

**Why.** Specs approved by a person are the strongest evidence available, so violating
them needs no judgement. When the model and the code merely disagree, either could be
wrong. The approved specs constrain how the model can be repaired, so the tool cannot
"fix" the model to match a buggy implementation, which is the Cedar-style triangle.

## D11. Spec approval UX

**Decision.** Specs are shown as an intent summary, the precondition, and each spec in
plain English. The reviewer can approve all, reject some, or describe a change (which
triggers re-formalization), and `l` shows the Lean. In a non-interactive session nothing
is verified until specs are approved: pass `--yes` (recorded as "auto-approved") or
reuse specs a person approved earlier. Only human-approved specs are persisted and
reused (`.larch/specs/` after `larch init`, so teams can review and commit them).

**Why.** The non-negotiable is that every spec is shown for approval. Persisting
approvals makes re-runs and CI deterministic and cheap. A saved spec is re-used even
after the implementation changes, which is exactly regression checking.

## D12. Mutation analysis with survivor re-testing

**Decision.** Up to 40 AST mutants (relational/arithmetic operator swaps, constant ±1,
`and`/`or`, dropped `not`, negated `if`, deleted `break`/`+=`, `min`↔`max`, `range`/`len`
off-by-one) are run against the verified model on a shared input sample. For each killed
mutant Larch records which postconditions caught it. Survivors get about 1000 extra
inputs before being labelled "likely equivalent".

**Why.** This is the "specs aren't vacuous" non-negotiable, made measurable. It
reports two numbers: the fraction of mutants DRT catches, and the fraction the
*specs alone* catch, which shows how much of the model's knowledge the proofs actually
guarantee. Equivalent mutants (for example `x < lo` vs `x <= lo` in `clamp`) are
common, so a survivor is re-tested before it is blamed on the specs.

## D13. Verdicts and exit codes

`passed` (0): every approved spec is proved **and** DRT found no disagreement.
`bug` (1): a confirmed or likely finding. `partial` (2): no finding, but some spec is
unproved. `error` (3). The word "verified" appears only for individual specs that passed
the checker.

## D14. Exceptions are modelled as `Option`; spec problems are not code bugs

**Decision.** A function that raises by design is modelled as `model : … → Option T`,
where `none` means "raises" (the exception type is not compared). Separately, when
the implementation violates an approved spec on some input, Larch reports a bug only
if the verified model satisfies that spec on the same input. If the model violates it
too, the spec is wrong, and Larch warns about the spec instead.

**Why.** The first design used `Except String T`. Specs then naturally matched on the
result (`match result with …`), which is not decidable, and `Except` has no decidable
equality: whole repair rounds were lost to this in the pilot. `Option` has decidable
equality and membership (`∀ v ∈ result, …`). The second rule comes from a benchmark
false alarm. The formalizer bounded an exponent (`∃ k < 64, n = 2^k`) to make a spec
decidable, and at `n = 2^64` the implementation and the model agreed but *both*
violated the spec. Blaming the code there would be wrong.

## D15. Name

**Larch**: a tree, as a nod to Cedar. Larch was also a family of formal specification
languages from the 1980s.

---

## Eval-driven decisions

See [EVALS.md](EVALS.md) for the data. Summaries are appended below as experiments complete.
