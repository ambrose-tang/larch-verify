# Evaluations

Every place where approaches compete (formalization prompting, proof decomposition,
test generation, model choice, reasoning effort) was settled by running the main
candidates on the same benchmark and keeping the winners. This file holds the method,
the raw numbers, and the decisions they led to. Re-run instructions are at the end.

## Benchmark

`bench/functions/`: **24 small real-world functions × (1 correct + 2 seeded bugs) =
72 variants**.

| area | functions |
|---|---|
| arithmetic | `isqrt` (Newton), `gcd`, `ceil_div`, `is_power_of_two` |
| calendar | `is_leap_year`, `days_in_month` (raises on bad month) |
| search / sort | `binary_search` (leftmost), `insertion_sort`, `merge_sorted`, `unique`, `max_subarray_sum`, `second_largest` |
| lists / intervals | `rotate_left`, `chunk`, `merge_intervals` |
| strings / parsing | `is_palindrome`, `run_length_encode`, `caesar_shift`, `parse_int` (raises), `luhn_check`, `int_to_roman`, `compare_versions` |
| policy / infra | `is_authorized` (Cedar-style forbid-overrides-permit), `allow_request` (sliding-window rate limiter) |

The seeded bugs are the kinds of mistakes that ship: a missing century rule, `<` vs `<=`
at a window boundary, a forgotten tail after a merge loop, first-match-wins instead of
forbid-overrides, float division in integer code, a missing empty-list guard, `abs(k)`
destroying the sign, returning the wrong loop variable, off-by-one loop bounds, and a
missing `+` sign case.

**Ground truth is machine-checked** (`python -m bench.validate`, with the result in
`bench/ground_truth.json`). Every correct version passes known-answer tests. Every
seeded bug is *observable*: it disagrees with the correct version on at least one input
in the documented domain. The validator also measures how often each bug fires on
random in-domain inputs. Nine of the 48 bugs fire on fewer than 5% of inputs (for
example `is_power_of_two(0)`, 0.6%). These form the "rare bugs" column below.

**Blindness.** Every variant is copied to a neutral path (`<function>.py`), and all
three variants of a function share the same docstring, so nothing but the code differs.
The ground-truth generators and answers in `bench/domains.py` are never shown to Larch.

**Protocol.** Specs are auto-approved (`--yes`). This is a limitation of the benchmark:
in real use a person reviews the English specs and would reject some wrong ones, so
false-alarm rates here are pessimistic. Each variant is run once per configuration.
LLM responses are cached, and costs are *nominal*, meaning what the calls cost when first made.

**Metrics.**
- *bugs caught*: fraction of the 48 bug variants where Larch reports a confirmed or
  likely bug.
- *false alarms*: fraction of the 24 correct variants where Larch reports a confirmed
  or likely bug.
- *specs proved*: fraction of approved specs accepted by the checker, on correct
  variants.
- *cost / time per function*: mean over variants.

## What the pilot and the first runs taught us (before the comparisons)

A 6-function pilot and the first benchmark attempts found problems that the
comparisons depend on. All are fixed, and runs from before each fix were discarded.

| finding | evidence | change |
|---|---|---|
| Proving at `high` effort is unaffordable | 15 proof calls for `run_length_encode` cost $2.41 (215k output tokens). Single `merge_intervals` calls cost up to **$1.31 and 18 minutes**. The same hard prompt cost $0.18 at `low` and $0.45 at `medium` | Prover effort is configurable per attempt (`--prover-efforts`). Per-spec budget (default $0.75). Effort compared in Experiment D |
| The automation portfolio did not cover recursive models | 0/12 pilot specs about `rle`/`merge_intervals` were closed without an LLM | `fun_induction` scripts along each recursive helper (with `s.toList` generalization) now close 2/6 `rle` specs for free |
| Specs over `Option`/`Except` results were often not decidable | `match result with …` inside a spec cannot be `decide`d, which cost a whole repair round | Exceptions are modelled as `Option` (`none` = raises). The prompt forbids `match` in specs. The repair hint names the fix |
| The generator field was misread | Sonnet often returned an English *description* in `strategy`, which triggered full re-formalizations | Field renamed `input_generator` and described as code, plus a Lean-validated worked example. A broken generator now gets one cheap targeted repair instead of a re-formalization |
| **Concurrent runs shared a workspace** | Variants of one function started in the same second wrote into the same run directory, and overwrote each other's Lean model and worker files | Unique run directories (regression test). All earlier results discarded |
| Lean panics are slow | `xs[i]!` on a *wrong* output prints a symbolicated backtrace: 2000 evaluations took 25.9 s | `LEAN_BACKTRACE=0` for the harness: 0.4 s |
| Harness stdout noise | `lean --run` prints linter warnings on stdout, which is the JSON channel | Linters off in the harness. The client skips non-JSON lines |
| Slow models crashed formalization | A `gcd` model that recursed on integer magnitude blew the job deadline | 3 s per-evaluation deadline, early stop, and a repair hint naming the slow inputs |
| Account usage limits | The benchmark exhausted a Claude subscription session window mid-run, and 58 variants were recorded as failures | `UsageLimitError` is never retried. The runner pauses until the reset time and resumes |

## Results

All runs: Sonnet 5 unless stated. Every row is the full benchmark (48 bug variants + 24
correct variants) unless stated.

### A. How to derive the model: from the documentation, or from the code?

The central design question in verification-guided development: what the reference
model is written *from*. Three prompting approaches, same model (Sonnet 5, medium
effort), bug detection only (no proofs):

- **intent**: the formalizer sees only the signature, the docstring and the
  surrounding module. The implementation body is hidden.
- **hybrid**: it sees the docstring and the implementation, and is told to model the
  documented intent and use the code only for conventions.
- **transliterate**: it translates the implementation into Lean faithfully. This is the
  "prove things about a translation of the code" approach.

| approach | bugs caught | not visible in docstring | rare (<5% of inputs) | false alarms | errors | cost / fn | time / fn |
|---|---|---|---|---|---|---|---|
| intent | **48/48 (100%)** | 28/28 | 9/9 | **0/24** | 0* | $0.081 | 27 s |
| hybrid | 46/48 (96%) | 28/28 | 9/9 | **0/24** | 0 | **$0.082** | 28 s |
| transliterate | 6/48 (12%) | 2/28 | 1/9 | 0/24 | 0 | $0.109 | 84 s |

\* In the first pass, two intent variants (`compare_versions`) ended in errors. The
model had copied JSON escaping into its Lean code (`splitOn \\".\\"`). A lexer now
detects a backslash outside any Lean literal and undoes one level of escaping, and on
re-run both variants passed: the correct one was clean and the bug was caught.

**Reading.**
- *Transliteration defeats differential testing.* A model translated from buggy code
  contains the bug, so the implementation and model always agree. The 6 bugs it did
  catch came from postconditions contradicting the transliterated behaviour. This is
  the quantitative version of Cedar's lesson: the model must be an independent
  statement of intent.
- *Hybrid copies code-level mistakes the docstring would have prevented.* Its only
  misses are both `int_to_roman` variants. It copied the implementation's symbol table
  (missing `IV`, or misordered `XC`), and none of its specs (round-trip decoding, valid
  characters, length) distinguishes `IIII` from `IV`, even though the docstring says
  `4 -> "IV"`.
- *Intent never saw the code, so it could not copy it*, and it had no false alarms here.
  Every benchmark docstring states the behaviour, which favours intent. Real code is often
  undocumented, and that case is measured in Experiment N.

**Decision.** The default is `--formalize-mode auto`: *intent* when the function has
real documentation (8 or more words), *hybrid* otherwise. Transliteration is kept only
as a baseline.

### D. Proving: automation, direct LLM repair, or sketch decomposition?

These runs cover the 24 correct variants, 122 approved specs, with the formalizations
shared with Experiment A. LLM attempts run at `low` effort (the pilot ruled out `high`),
with 3 attempts and a $0.30 budget per spec. Every "proved" passed the out-of-process
checker.

| strategy | specs proved | functions fully proved | proof cost / fn | proof time / fn |
|---|---|---|---|---|
| portfolio only (no LLM) | 61/122 (50%) | 5/24 | **$0.00** | 30 s |
| portfolio → LLM repair loop | **82/122 (67%)** | **9/24** | $0.47 | 296 s |
| portfolio → sketch decomposition | 74/122 (61%) | 6/24 | $0.46 | 297 s |

- **The free portfolio does half the work.** `grind` alone closes 39 specs,
  `simp_all`+`omega` 10, and the new `fun_induction` scripts 6.
- **Direct repair beat decomposition** at the same budget: it added 21 proofs at
  $0.54 each, against 13 proofs at $0.85 each for the sketch. Sketches spend budget on
  helper lemmas that are often individually as hard as the goal, and a single failed
  lemma sinks the whole sketch.
- What stays unproved is genuinely hard in core Lean without Mathlib: sortedness plus
  permutation of `merge_intervals`, `isqrt`'s `n < (r+1)²` (non-linear arithmetic), and
  string-character arithmetic in `caesar_shift`. An unproved spec is still *tested*
  against the implementation on every run. It just is not reported as proved.

**Decision.** `portfolio+llm` is the default, with `low` effort for proof attempts.
`portfolio+sketch` remains an option.

### E. Test generation: type-directed, LLM-written, or mixed?

Each run has the same formalizations (cached) and 2000 inputs per function.

| generator | bugs caught | not visible in docstring | rare (<5%) | false alarms | cost / fn |
|---|---|---|---|---|---|
| type-directed only (Hypothesis strategies from the Lean types) | 39/48 (81%) | 23/28 | 6/9 | 0/24 | $0.082 |
| LLM-written only | **48/48** | 28/28 | 9/9 | 0/24 | $0.078 |
| mixed (2/3 LLM, 1/3 type-directed; default) | **48/48** | 28/28 | 9/9 | 0/24 | $0.081 |

The type-directed generator misses bugs that need *structured* inputs: well-formed
version strings, policy tuples, digit strings, century years in February, and signed
integer literals. Random strings almost never satisfy those preconditions. LLM-written
generators build them directly. **Decision:** keep `mixed`. It ties the LLM-only
generator here, and its type-directed third still covers what the LLM does not think
of, like the `2^64` boundary that exposed a wrong spec during development.

### X. Documented examples as checks

Added after Experiment A showed hybrid copying a bug that the docstring's own example
(`4 -> "IV"`) contradicted. The formalizer lists every concrete example written in the
docstring. The model must agree with each one (checked during sanity testing), and an
implementation that contradicts one is a confirmed finding.

| configuration | bugs caught | false alarms | cost / fn |
|---|---|---|---|
| auto (intent) | 48/48 | 0/24 | $0.081 |
| auto (intent) + documented examples | 48/48 | 0/24 | **$0.064** |

It adds no detection on top of intent, which was already at 48/48. It did make runs
cheaper: models that disagreed with an example were rejected in the cheap sanity pass,
instead of after a repair round. It also changed how 19 of the 48 bugs are reported,
as "implementation contradicts the documented example `int_to_roman(4) = 'IV'`": the
most convincing evidence a reviewer can get. **Decision:** on by default
(`--no-doc-examples` turns it off).

### N. Undocumented code

The 24 correct variants were run with their docstrings stripped. `auto` then falls back
to hybrid, because there is no intent to model from.

| configuration | false alarms | cost / fn |
|---|---|---|
| auto, documented (A) | 0/24 | $0.081 |
| auto, docstrings stripped | 1/24 (4%) | $0.074 |

The one false alarm: with no documentation, the formalizer decided version parts must
be ASCII digits and flagged that `compare_versions("+0.0", "+0.0")` returns 0 instead of
raising. Python's `int()` quietly accepts a sign. This is a real behavioural quirk, but
not a bug by the benchmark's ground truth, so it is counted against Larch. In real use it
surfaces during spec review, where the user sees "each part is a string of ASCII digits"
and can reject that spec.

### B. Model choice

**Formalization** (bug detection). Haiku ran on the full benchmark. Opus is 2.5× the
per-token price of Sonnet, so it ran on half the benchmark: every other function, 36
variants. The Sonnet row for that half comes from the same runs as Experiment A.

| model | functions | bugs caught | false alarms | errors | cost / fn | time / fn |
|---|---|---|---|---|---|---|
| Haiku 4.5 | all 24 | 45/48 (94%) | 0/24 | 4 | $0.140 | 229 s |
| **Sonnet 5** | all 24 | **48/48** | 0/24 | 0 | $0.081 | 27 s |
| Sonnet 5 | half (12) | 24/24 | 0/12 | 0 | $0.074 | 23 s |
| Opus 5 | half (12) | 24/24 | 0/12 | 0 | $0.091 | 41 s |

Haiku is cheaper per token but not per function. Its formalizations failed the compile
and sanity checks more often, needed more repair rounds, and 4 variants never converged.
Opus matched Sonnet at 23% higher cost.

**Proving** (the same 24 correct variants and portfolio as Experiment D):

| prover | specs proved | proof cost / fn | proof time / fn |
|---|---|---|---|
| Sonnet 5, low effort | **82/122** | **$0.47** | 296 s |
| Haiku 4.5 | 63/122 | $0.62 | 704 s |

Haiku added only 2 proofs beyond the free portfolio, and spent more doing it.
**Decision:** Sonnet 5 for every stage. `--model`/`--prover-model` can switch to Opus
for hard functions.

### C. Reasoning effort for formalization

The same half of the benchmark (36 variants), Sonnet 5:

| effort | bugs caught | false alarms | cost / fn |
|---|---|---|---|
| medium | 24/24 | 0/12 | $0.074 |
| **low** | 24/24 | 0/12 | **$0.051** |

Low effort matched medium at 31% lower cost. (The pilot had already shown that `high`
costs several times more for proofs.) **Decision:** the default formalization effort is
`low`. This is the weakest-supported decision in this file: 36 variants, with both
settings at the ceiling. `--effort medium` remains available for gnarly functions.

## Final configuration (headline numbers)

The defaults chosen above: `auto` formalization (intent when documented) with Sonnet 5,
documented examples on, mixed test generation (2000 inputs), `portfolio+llm` proofs at
low effort, and 40 mutants. The run `F-final` covers all 72 variants, with proof
numbers from Experiment D:

| metric | result |
|---|---|
| seeded bugs caught | **48/48 (100%)**: 28/28 not visible in the docstring, 9/9 firing on <5% of inputs |
| false alarms on correct code | **0/24** (1/24 when docstrings are stripped, Experiment N) |
| specs proved in Lean (checker-accepted, no sorry, no axioms) | 82/122 (67%); 9/24 functions fully proved |
| mutation analysis on correct code | 400/412 injected bugs detected (97%). All 12 survivors behaved identically to the model on ~1000 extra inputs (likely equivalent). **332 (81%) were caught by the approved specs alone** |
| fix proposals for caught bugs | 48 proposed; **46 validated** against the verified model |
| cost / time per function | $0.09 and about 1 min (bug finding); proofs add $0.47 and about 5 min |

## Limitations

- The benchmark's functions are small, pure and well documented. Real code often has
  vaguer documentation, and N measures only the no-docstring extreme.
- Specs are auto-approved here. In real use a person reviews them, which should lower
  false alarms further, but that is not measured.
- One run per configuration. LLM outputs vary between runs, so differences of one or
  two variants are within noise. The large gaps (transliterate vs intent,
  type-directed vs LLM-written generation, Haiku vs Sonnet proving) are not.

## Re-running

```bash
python -m bench.validate                       # ground truth
./bench/launch.sh ./bench/expAll.sh bench/expAll.log   # comparisons (resumable)
python -m bench.analyze A2-intent A2-hybrid A2-translit --detail
python -m bench.analyze D2-portfolio D2-port+llm-low D2-port+sketch-low --proofs
```
Per-variant results are committed in `bench/results/<experiment>.jsonl` (summary: `bench/results_summary.json`); live runs write to `bench/runs/`. The LLM response cache makes
re-analysis free.
