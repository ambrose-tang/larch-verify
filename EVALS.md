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
