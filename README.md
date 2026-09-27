# Larch

**Verification-guided development for everyday code.** Larch is a terminal agent that
takes a function from your codebase, writes an executable **Lean 4 model** of what it
is supposed to do, **proves** the key properties of that model, and then
**differentially tests** your real implementation against the proven model on
thousands of random inputs. It is the approach AWS used to build
[Cedar](https://www.cedarpolicy.com/) (a verified model plus differential random
testing), packaged so it works on a single Python function in about a minute.

```text
$ larch verify examples/calendar_utils.py::is_leap_year

● Read calendar_utils.py::is_leap_year  def is_leap_year(year: int) -> bool  (7 lines)

● Formalize: write Lean model and specs  4 postconditions, 0 properties  21.5s
  ⎿  model compiles · tested on 834 inputs, no spec violations by the model
     implementation already disagrees with the model on 5 of 200 quick-test inputs

╭─ Proposed specification: please review ──────────────────────────────────────╮
│  1. div4_required      If year is a leap year, it must be divisible by 4.    │
│  2. century_rule       A year divisible by 100 but not 400 is not a leap year│
│  3. not_div4_not_leap  If year is not divisible by 4, it is not a leap year. │
│  4. div400_is_leap     If year is divisible by 400, it is a leap year.       │
│  Note: in quick testing the implementation already violates: div400_is_leap  │
╰──────────────────────────────────────────────────────────────────────────────╯
  [a]pprove all   [r]eject some   [e]dit (describe a change)   [l]ean view   [q]uit
  › a

● Differential test: implementation vs. model  1,521 valid inputs · 5 disagreements
● Prove 4 specs in Lean  4/4 proved  3.3s
● Mutation analysis: are the specs and tests strong enough?  13/13 mutants detected (100%)
● Propose a fix (validated against the verified model)  validated  9.2s

╭─ BUG FOUND ──────────────────────────────────────────────────────────────────╮
│  Implementation violates an approved spec: is_leap_year(0) returned False,   │
│  expected True.                                                              │
│   ✓ proved  div400_is_leap  auto: grind · violated by implementation on 5    │
│  ✗ Implementation violates an approved spec  (confirmed)                     │
│     input           is_leap_year(0)                                          │
│     implementation  False                                                    │
│     verified model  True                                                     │
╰──────────────────────────────────────────────────────────────────────────────╯
 Proposed fix · validated against the verified model
   -    return year % 4 == 0 and year % 100 != 0
   +    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
  $0.055 · 36s · 2 LLM calls
```

## Why

Unit tests check the examples you thought of. Formal proofs check everything, but not
about the code you actually ship, and nobody writes them for a date helper. Larch
works in between, the way Cedar's authors did:

1. **The Lean model is the specification.** It is a short, obviously correct definition
   of the intended behaviour, for example a linear scan in place of your binary search.
2. **Proofs make the model trustworthy.** Every spec you approve ("the result is always
   in range", "no earlier index holds the target") is proved about the model in Lean,
   and an independent checker rejects `sorry`, new axioms and weakened statements.
3. **Differential testing ties the model to your code.** Thousands of generated inputs,
   biased toward edge cases, are run through both. Any disagreement, and any output
   that breaks an approved spec, is reported with a *minimal* counterexample.
4. **Mutation analysis checks the checkers.** Larch injects plausible bugs into your
   function and confirms the specs and tests catch them, so a vacuous spec cannot give
   you false confidence.

It is honest about what is proved. The report says *specs proved about the model* and
*implementation agrees with the model on N inputs*. It never claims your Python was
proven correct.

## Install

```bash
uv tool install larch-verify        # or: pipx install larch-verify
larch doctor --install              # installs the pinned Lean toolchain via elan, builds the proof checker
```

Requirements: Python ≥ 3.11, [elan](https://github.com/leanprover/elan) (the Lean
installer), and either an `ANTHROPIC_API_KEY` or a logged-in
[Claude Code](https://claude.com/claude-code). Larch can use your Claude subscription
through the `claude` CLI, so no API key is needed.

## Usage

```bash
larch verify path/to/module.py::function     # one function
larch verify path/to/module.py               # every public function in the file
larch verify module.py -k parse              # functions whose name contains "parse"
larch verify module.py::f --apply            # offer to apply a validated fix (asks first)
larch verify module.py::f --yes --json out.json   # CI: accept specs unreviewed, machine-readable output
larch show                                   # re-display the latest report
larch init                                   # keep approved specs in .larch/specs (commit them!)
```

Exit codes: `0` passed · `1` bug found · `2` some spec unproved · `3` error.

### In CI

Run `larch init` and approve the specs locally once. The specs are written to
`.larch/specs/` for you to review and commit. In CI:

```bash
larch verify src/pricing.py --reuse --no-fix --json larch.json
```

Reusing approved specs makes CI deterministic, and it is a regression check: when
someone changes the implementation, it is re-tested against the specs your team
approved.

### Useful options

| option | default | meaning |
|---|---|---|
| `--model` | `claude-sonnet-5` | LLM used for formalization, adjudication and fixes |
| `--prover-model` | same as `--model` | LLM used for proofs |
| `--effort` | `low` | reasoning effort for formalization (`low`…`max`) |
| `--tests N` | 2000 | random inputs for differential testing |
| `--mutants N` | 40 | injected bugs for mutation analysis |
| `--budget USD` | 5 | hard cap on LLM spend per function |
| `--python PATH` | project `.venv` | interpreter used to run your code |
| `--formalize-mode` | `auto` | `auto` (intent when documented, else hybrid) \| `intent` \| `hybrid` \| `transliterate` (see EVALS.md) |
| `--proof-strategy` | `portfolio+llm` | `llm` \| `portfolio+llm` \| `portfolio+sketch` |
| `--test-strategy` | `mixed` | `typed` \| `llm` \| `mixed` input generation |

Defaults can also be set in `[tool.larch]` in `pyproject.toml`, in `.larch.toml`, in
`~/.config/larch/config.toml`, or with `LARCH_*` environment variables.

## What gets reported

- **Specs:** each one proved or unproved, with the method (`auto: grind`,
  `llm (2 attempts)`, `sketch (3 lemmas)`), and a warning if a spec looks vacuous.
- **Findings,** ranked by evidence:
  - *confirmed*: your implementation's output violates a spec you approved, or it hangs;
  - *likely*: it disagrees with the proved model, and an adjudicator attributes the
    disagreement to the implementation;
  - *possible*: a disagreement the documentation does not settle.
  - When the *model* turns out to be wrong, Larch repairs the model, which must still
    satisfy your approved specs, instead of blaming your code.
- **Fixes:** a minimal diff, validated by re-running differential tests against the
  proved model, saved as a `.patch`. Your files are never modified unless you pass
  `--apply` and confirm.
- **Artifacts** (under `~/.cache/larch/runs/<run>/`): the Lean model, proofs, the
  full LLM transcript, `report.md`, and `report.json`.

## Trust model

A spec is reported as **proved** only if a separate checker process, which loads
the compiled Lean environment rather than parsing text, confirms three things:
1. the theorem's type is *exactly* the approved spec,
2. it depends on no axioms beyond `propext`, `Classical.choice` and `Quot.sound`
   (so no `sorry`, `admit`, `native_decide` or new axioms),
3. the kernel accepts it on replay (`leanchecker`).

The model file cannot use `implemented_by`, `extern`, `partial`, `unsafe` or custom
instances, so the model that is tested is the model that is proved. Your code runs in a
sandboxed subprocess with timeouts, and Larch writes nothing into your repository.

## Scope (v0.1)

- **Language:** Python.
- **Functions:** module-level functions and static methods over `int`, `bool`, `str`,
  single characters, lists, tuples and `Optional`, including functions that raise
  documented exceptions.
- **Not yet supported:** floats, dicts, sets, objects and methods with state, I/O, async,
  generators.

Larch refuses these up front with a clear message rather than giving an unreliable
answer.

## How well does it work?

On a benchmark of 24 real-world functions, each with 2 seeded bugs ([EVALS.md](EVALS.md)):

| | |
|---|---|
| seeded bugs caught | **48/48**, including all 28 not visible from docstring examples and all 9 that fire on under 5% of inputs |
| false alarms on correct code | **0/24** (1/24 with docstrings stripped) |
| specs proved in Lean | 82/122 (67%); half of them with no LLM call at all |
| injected mutants caught | 400/412 (all 12 survivors likely equivalent); 81% caught by the specs alone |
| fixes validated | 46/48 |
| cost | about $0.09 and 1 minute per function to find bugs; proofs add about $0.47 |

The comparisons that chose these defaults cover formalization prompting,
proof decomposition, test generation, model choice and effort, and are in EVALS.md. The
reasoning behind each design decision is in [DECISIONS.md](DECISIONS.md).

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest                                  # unit tests + integration tests (real Lean, scripted LLM)
python -m bench.validate                # check the benchmark's ground truth
python -m bench.run --name my-exp ...   # run an experiment (see bench/run.py)
python -m bench.analyze my-exp --detail
```
