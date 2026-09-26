# Larch

**Verification-guided development for everyday code.** Larch is a terminal agent that
takes a function from your codebase, writes an executable **Lean 4 model** of what it
is supposed to do, **proves** the key properties of that model, and then
**differentially tests** your real implementation against the proven model on
thousands of random inputs. It is the approach AWS used to build
[Cedar](https://www.cedarpolicy.com/) (a verified model plus differential random
testing), packaged so it works on a single Python function in about a minute.

```text
$ larch verify billing/calendar.py::days_in_month

● Read calendar.py::days_in_month  def days_in_month(year: int, month: int) -> int  (16 lines)

● Formalize: write Lean model and specs  4 postconditions, 1 property  41.2s
  ⎿  model compiles · tested on 312 inputs, no spec violations by the model

╭─ Proposed specification: please review ──────────────────────────────────────╮
│  Assumes: nothing (all inputs; months outside 1..12 must raise ValueError)   │
│   1. raises_iff_invalid_month   Raises ValueError exactly when month ∉ 1..12 │
│   2. february_leap              February has 29 days iff the year is a leap  │
│                                 year (div. by 4, centuries only if by 400)   │
│   ...                                                                        │
╰──────────────────────────────────────────────────────────────────────────────╯
  [a]pprove all   [r]eject some   [e]dit (describe a change)   [l]ean view   [q]uit
  › a

● Differential test: implementation vs. model  1,618 valid inputs · 12 disagreements
● Prove 5 specs in Lean  5/5 proved
● Mutation analysis: 31/33 mutants detected (94%) · 2 likely equivalent

╭─ BUG FOUND ──────────────────────────────────────────────────────────────────╮
│  Implementation violates an approved spec: days_in_month(1900, 2) returned   │
│  29, expected 28.                                                            │
╰──────────────────────────────────────────────────────────────────────────────╯
 Proposed fix · validated against the verified model
   -        leap = year % 4 == 0
   +        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
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
| `--effort` | `medium` | reasoning effort for formalization (`low`…`max`) |
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

See [EVALS.md](EVALS.md) for a benchmark of 24 real-world functions, each with 2
seeded bugs, and the comparisons that chose Larch's defaults: formalization prompting,
proof decomposition, test generation, model choice and effort. The design rationale is
in [DECISIONS.md](DECISIONS.md).

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest                                  # unit tests + integration tests (real Lean, scripted LLM)
python -m bench.validate                # check the benchmark's ground truth
python -m bench.run --name my-exp ...   # run an experiment (see bench/run.py)
python -m bench.analyze my-exp --detail
```
