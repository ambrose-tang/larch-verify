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

(filled in as experiments complete)
