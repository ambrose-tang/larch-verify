# Larch

**Verification-guided development for everyday code.** Larch takes a function from
your codebase, writes an executable **Lean 4 model** of what it is supposed to do,
**proves** the key properties of that model, and then **differentially tests** your real
implementation against the proven model on thousands of generated inputs. It is the
approach AWS used to build [Cedar](https://www.cedarpolicy.com/) (a verified model plus
differential random testing), packaged so it works on a single function in about a
minute, and on every pull request in CI.

Works with **Python**, **TypeScript** and **JavaScript**.

```text
$ larch verify examples/calendar_utils.py::is_leap_year

● Read calendar_utils.py::is_leap_year  def is_leap_year(year: int) -> bool  (7 lines) · Python 3.12.3

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
*implementation agrees with the model on N inputs*. It never claims your code was
proven correct.

## Install

```bash
uv tool install larch-verify        # or: pipx install larch-verify
larch doctor --install              # installs the pinned Lean toolchain via elan, builds the proof checker
```

Requirements:
- Python ≥ 3.11 for Larch itself (your project's code can run on Python 3.7+; Larch
  never installs anything into your project's environment).
- [elan](https://github.com/leanprover/elan), the Lean installer.
- For TypeScript: Node.js ≥ 22.6. For JavaScript: Node.js ≥ 20.6.
- An LLM: `ANTHROPIC_API_KEY`, Amazon Bedrock, Google Vertex AI, or a logged-in
  [Claude Code](https://claude.com/claude-code) (uses your Claude subscription; no key needed).

`larch doctor` checks all of these and tells you which interpreter will run your code.

## Quick start

```bash
larch init                                     # draft LARCH.md (your contracts) from the best candidates
larch verify                                   # check everything in LARCH.md
larch scan                                     # which functions in this repo can Larch verify? (ranked)
larch verify src/pricing.py::apply_discount    # one function
larch verify web/src/pagination.ts::pageCount  # TypeScript works the same way
larch verify src/pricing.py                    # every public function in a file
larch                                          # interactive: draft LARCH.md together, then verify
larch verify src/ --limit 10                   # the 10 best candidates in a directory
larch verify --changed origin/main             # functions this branch changed
larch verify src/pricing.py::f --apply         # offer to apply a validated fix (asks first)
larch show                                     # re-display the latest report
```

Exit codes: `0` passed · `1` bug found · `2` some spec unproved · `3` error.

## Interactive: `larch`

Run `larch` with no arguments in your repository for a guided session:

1. **Draft LARCH.md together.** Larch reads a compact summary of the repository (and the
   source of anything it needs), asks what the code is *for* and what must never go
   wrong, then proposes contracts in its own words and confirms them with you, often as
   a quick multiple choice. Each agreed change is written to LARCH.md at once.
2. **Verify.** When the draft covers what you confirmed, Larch asks whether to start, then
   runs `larch verify` with its usual step-by-step log.
3. **Result.** If anything fails, `.larch/what-to-fix.md` lists each failing call, what
   it returned and what was expected, the explanation, and any validated fix. If
   everything passes you get `LARCH-CERTIFICATE.json`: the contracts and every verified
   source file pinned by SHA-256, with how each contract was proved, the commit, and a
   digest over the whole certificate. `larch certificate` re-checks it later and says
   exactly what changed.

Every version of LARCH.md is kept in `.larch/history/`. In the session, `/undo` takes
back the last change, `/rollback` picks any earlier version (including "no LARCH.md"),
`/diff` shows what the last change did, `/show` prints the draft, `/verify` starts
verification, and `/quit` leaves. A drafting turn costs about 3k input tokens: the
conversation keeps only recent turns, since the draft itself carries what was agreed.

## Contracts: LARCH.md

One file at the repository root says what the code must do, in plain English:

```markdown
# Contracts

## shop/shipping.py::shipping_cost_cents
- The cost is always positive.
- Express delivery always costs more than standard delivery for the same parcel and zone.
- A heavier parcel never costs less than a lighter one to the same zone at the same speed.

## web/src/payments.ts::splitPayment
- The instalments add up exactly to the total.
- No two instalments differ by more than one cent.

## shop/pricing.py::line_total
```

- **Each bullet becomes a spec.** Larch formalizes it in Lean, and you approve it once.
  The review shows your words next to Larch's reading of its Lean, so a mistranslation
  is visible. Before you ever see it, each formalized contract is also checked against
  concrete cases judged from your English alone (an output that should violate it and
  one that should satisfy it); a formalization that disagrees is sent back for repair.
- **Exact control when you want it:** put a ```` ```lean ```` block under a bullet and it is
  used verbatim.
- **A heading with no bullets** asks Larch to propose contracts. The ones you approve are
  written back under the heading, so LARCH.md stays the record of what was agreed.
- **Changing a bullet** makes Larch re-formalize that function on the next run.
- `include: services/billing/LARCH.md` pulls in another file (monorepos).

See [`examples/shop/LARCH.md`](examples/shop/LARCH.md) for a complete example.

### Exhaustive testing

When every input a function accepts fits in a small box (booleans, small enums,
bounded integers, e.g. `1 <= weight_kg <= 30`, `1 <= zone <= 5`), Larch runs the
implementation on **every** input instead of a random sample, smallest first. It also
proves in Lean that the box contains every input the precondition allows, so the claim
"checked on every valid input" is a theorem, not an assumption. On such a function a
postcondition that held on every output holds for the code itself, not only for the
model. Larger domains fall back to differential random testing; the limit is
`exhaustive_limit` (default 100,000 inputs).

### Stateful components (classes)

Put a class under a heading (`## shop/ledger.py::Ledger`) and Larch verifies the object,
not just one call:

- **The model is a Lean state machine:** a `State` structure, the constructor, one step
  per public method (raising = failing without changing the state), and read-only
  *observers* (such as `total()` or a `totalCents` getter).
- **Invariants** ("no balance is ever negative") are proved for **every reachable
  state**, meaning every state the object can get into through its constructor and any
  sequence of calls. **Operation contracts** ("withdrawing more than the balance fails",
  "a transfer never changes the total") are proved for every call from every such state.
- **The real object is tested with call sequences:** random sequences, plus every
  sequence up to a few calls over small argument pools. Results and observers are
  compared with the model after every call, so a method that corrupts state and then
  raises is caught at the next observation. Failures are shrunk to the shortest
  sequence:

  ```text
  ✗ Implementation disagrees with the verified model  (likely)
     calls  ledger = Ledger()
            ledger.transfer('a', 'b', 1)
            ledger.total()
     implementation  1
     verified model  0
  ```
- Mutation analysis and validated fixes work as for functions; a fix must agree with the
  model on every sequence before it is offered.

### Services and databases

A `## service NAME` heading verifies a running HTTP service, database included:

```markdown
## service orders
start: uvicorn shop.api:app --port {port}
database: postgres
- Retrying `POST /orders` with the same Idempotency-Key returns the original order.
- A payment that fails (402, 404 or 409) changes nothing.
```

- **Larch runs it.** It starts a throwaway database (`postgres` through Docker or local
  binaries, `sqlite`, `none`, or a URL you give), sets `DATABASE_URL` and `PORT`, runs
  `start:` (optionally after `setup:`), and reads the OpenAPI description and the route
  source (`source:` narrows it). Between request sequences it empties every table and
  restarts id sequences, so ids are predictable; `reset:` adds a request or command of
  your own.
- **The model is a state machine whose operations are endpoints.** Each returns the
  status code and the response fields that matter; contracts and invariants are proved
  exactly as for classes. Values that are random by design (UUIDs, timestamps) are
  compared by order of first appearance.
- **Request sequences** are compared with the model step by step. Mutation analysis and
  validated fixes run on a copy of your project started on its own port; your working
  tree is never changed. `--apply` writes a validated fix to the handler's file.

### Finding deep bugs

Sequences are generated against the model first, which is cheap: Larch keeps the
sequences that reach new behaviour (a new outcome of an operation, or a new run of two
or three outcomes), grows them by adding and replacing calls, and runs the
implementation on the rarest. In the shop example this raises the share of sequences
that contain a successful payment from about 1% to 40%, so "pay, then pay again" and
"retry after paying" are tested hundreds of times rather than by luck. Failures shrink
by removing calls one at a time.

When the reviewer finds that the model, not the code, is wrong about a disagreement,
Larch revises the model (same operations and contracts), re-checks it and re-tests,
instead of reporting a bug in correct code.

### System rules

Guarantees about components working together go under `# System rules`, each naming the
components it relies on:

```markdown
# System rules
- At checkout, the instalments for a cart add up exactly to the cart's total.
  (uses: web/src/cart.ts::Cart, web/src/payments.ts::splitPayment)
- Paying an order a second time never charges the customer again.  (uses: service orders)
```

After verifying those components, Larch puts their Lean models side by side, writes the
rule as one Lean proposition over them, and proves

    contract of Cart → contract of splitPayment → rule

so the rule holds for **any** implementation that meets those contracts. The report says
which contracts the rule rests on and how each was established:

| result | meaning |
|---|---|
| passed | proved from contracts that are themselves proved, about models the code agrees with |
| partial | proved, but some contract it rests on is only tested; or not proved |
| bug | proved, but a component it uses disagrees with its model |

Rules run with `larch verify` (no arguments), after the components.

## Team workflow

1. **Write and approve contracts once.** `larch init` creates `.larch/` and drafts
   LARCH.md. Every spec you approve is stored in `.larch/specs/` as reviewable JSON.
   Commit both: they are the durable asset ("what this code must do", signed off by a person).
2. **Check every pull request.** In CI, `larch verify --changed --approved-only`
   re-tests every changed function that has approved specs against those specs. It
   makes no formalization calls; add `--no-proofs` to skip re-proving unchanged specs and
   only differentially test the new code (the verdict is then `partial`, exit 2, unless a
   bug is found).
3. **Grow coverage.** `larch scan` ranks the rest of the codebase by how much
   verification will pay off (documented, typed, branchy, pure); work down the list.

### GitHub Actions

```yaml
# .github/workflows/larch.yml
name: Larch
on: pull_request
permissions:
  contents: read
  security-events: write   # for code-scanning annotations
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      # Set up the project as your tests do, so Larch runs your code with your dependencies:
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e .
      - uses: ambrose-tang/larch-verify@v0.2
        with:
          approval: approved      # or `auto` to also formalize new functions (unreviewed specs)
          limit: 10
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

Findings appear as code-scanning annotations on the diff (SARIF), a summary on the job
page, and a JUnit report (`larch-junit.xml`). See [`action.yml`](action.yml) for all
inputs (`fail-on`, `budget`, `provider`, ...).

### Any other CI

```bash
larch verify --changed "$BASE_SHA" --approved-only --no-fix \
  --sarif larch.sarif --junit larch-junit.xml --markdown summary.md --json larch.json
```

`--markdown` writes a summary suitable for a merge-request comment; `--junit` is read by
GitLab, Jenkins, Azure DevOps, Buildkite and CircleCI test reports.

## How your code is run

Larch runs **your function in your project's own runtime**, so its imports resolve the
way they do in your tests. Input generation, the Lean model and shrinking run in Larch's
own process; a tiny adapter (standard library / Node built-ins only) loads and calls your
function on each input. Nothing is added to your project's import path or environment.

**Python interpreter**, first match wins:

| | |
|---|---|
| explicit | `--python PATH` (an interpreter or a virtualenv directory), `LARCH_PYTHON`, or `python = "..."` in `[tool.larch]` |
| in-project virtualenv | `.venv`, `venv`, `.env`, `env` from the file's directory up to the repository root (so a monorepo-root `.venv` is found), or `$UV_PROJECT_ENVIRONMENT` |
| active environment | `$VIRTUAL_ENV`, then `$CONDA_PREFIX` |
| environment manager | poetry, pipenv, pdm or hatch, when the project uses it |
| PATH | `python3`, then `python` |
| fallback | Larch's own interpreter, with a warning |

**Imports**: the package root, the file's directory, `pythonpath` from `[tool.larch]` and
from pytest's configuration, the project root, `src/`, and the repository root are
importable; implicit namespace packages work; your `PYTHONPATH` is kept.

**Node.js**: `--node PATH`, `LARCH_NODE`, or `node` on PATH (a version manager's shim
picks the project's pinned version). ESM and CommonJS are supported; TypeScript runs
through Node's built-in type stripping; non-exported functions can be verified.

**Before any LLM call**, Larch loads the module. If that fails it stops and says why:

```text
✗ mod.py imports `requests`, which is not installed for Python 3.11.9 at /usr/bin/python3
  (chosen: `python3` on PATH).
  It is importable with /work/app/.venv-ci/bin/python (active virtualenv $VIRTUAL_ENV): re-run with
  `--python /work/app/.venv-ci/bin/python`.
```

## Configuration

Set options on the command line, in `[tool.larch]` in `pyproject.toml`, in
`.larch.toml` (any language), in `~/.config/larch/config.toml`, or as `LARCH_*`
environment variables. Project files are found at or above the verified file, up to the
repository root.

| option | default | meaning |
|---|---|---|
| `--model` | `claude-sonnet-5` | LLM used for formalization, adjudication and fixes |
| `--prover-model` | same as `--model` | LLM used for proofs |
| `--provider` | `auto` | `anthropic`, `bedrock`, `vertex` or `claude-code` (see below) |
| `--effort` | `low` | reasoning effort for formalization (`low`…`max`) |
| `--tests N` | 2000 | generated inputs for differential testing |
| `exhaustive_limit` (config) | 100000 | test every input when the proved-complete domain is at most this large |
| `--mutants N` | 40 | injected bugs for mutation analysis |
| `--budget USD` | 5 | hard cap on LLM spend per function |
| `--python PATH` | discovered | interpreter (or virtualenv) for Python code |
| `pythonpath` (config) | `[]` | extra import roots, relative to the project root |
| `--node PATH` | `node` on PATH | Node.js for JavaScript/TypeScript |
| `--formalize-mode` | `auto` | `auto` (intent when documented, else hybrid) \| `intent` \| `hybrid` \| `transliterate` |
| `--proof-strategy` | `portfolio+llm` | `llm` \| `portfolio+llm` \| `portfolio+sketch` |
| `--test-strategy` | `mixed` | `typed` \| `llm` \| `mixed` input generation |

```toml
# .larch.toml
model = "claude-sonnet-5"
budget_usd = 2.0
python = ".venv-ci"        # an interpreter or virtualenv, relative to the project root
pythonpath = ["libs"]
```

### LLM providers

| provider | how it is selected | credentials |
|---|---|---|
| Anthropic API | `--provider anthropic`, or `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` set | API key, `ant auth login`, or workload identity federation; `ANTHROPIC_BASE_URL` for gateways |
| Amazon Bedrock | `--provider bedrock`, or `CLAUDE_CODE_USE_BEDROCK=1` | standard AWS credentials; `AWS_REGION`. `pip install 'larch-verify[bedrock]'` |
| Google Vertex AI | `--provider vertex`, or `CLAUDE_CODE_USE_VERTEX=1` | Application Default Credentials; `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION`. `pip install 'larch-verify[vertex]'` |
| Claude Code | `--provider claude-code`, or the `claude` CLI is installed | your Claude Code login |

What is sent to the provider, and what runs locally, is described in [SECURITY.md](SECURITY.md).
Every prompt and response is kept under `~/.cache/larch/runs/<run>/llm/` for audit.

## What gets reported

- **Specs:** each one proved or unproved, with the method (`auto: grind`,
  `llm (2 attempts)`, `sketch (3 lemmas)`), and a warning if a spec looks vacuous.
- **Findings,** ranked by evidence:
  - *confirmed*: your implementation's output violates a spec you approved, contradicts
    a documented example, or it hangs;
  - *likely*: it disagrees with the proved model, and an adjudicator attributes the
    disagreement to the implementation;
  - *possible*: a disagreement the documentation does not settle.
  - When the *model* turns out to be wrong, Larch repairs the model, which must still
    satisfy your approved specs, instead of blaming your code.
- **Fixes:** a minimal diff, validated by loading it in your runtime and re-running
  differential tests against the proved model, saved as a `.patch`. Your files are never
  modified unless you pass `--apply` and confirm.
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
separate process with per-call timeouts (not a security sandbox: it has your
permissions, like your tests), and Larch writes nothing into your repository.

## Scope

| | Python | TypeScript / JavaScript |
|---|---|---|
| functions | module-level functions, `@staticmethod`s | top-level `function`s, `const f = (…) =>`, static methods (exported or not) |
| classes | public methods and `@property`s; `__init__` or `@dataclass` constructors | public methods and getters; constructors (incl. parameter properties) |
| values | `int`, `bool`, `str`, single characters, lists, tuples, `Optional` | integer `number`s (within ±2^53−1), `bigint`, `boolean`, `string`, arrays, tuples, `null`/`undefined`/optional |
| errors | documented exceptions | documented `throw`s |
| services | any HTTP service you can start with a command (JSON bodies); mutants and fixes for Python handlers | the same; mutants and fixes are Python-only for now |
| not yet | floats; dicts, sets and objects as arguments or results; I/O; async; generators; `*args` | non-integer numbers; objects/`Map`/`Date` as arguments or results; async; generators; rest/destructured parameters |

Larch refuses these up front (and `larch scan` says why) rather than giving an
unreliable answer. Linux and macOS are supported; Windows is not yet (use WSL).

## How well does it work?

On a benchmark of 24 real-world Python functions, each with 2 seeded bugs ([EVALS.md](EVALS.md)):

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
reasoning behind each design decision is in [DECISIONS.md](DECISIONS.md). The
TypeScript/JavaScript backend reuses the same pipeline and prompts with a
language-specific type mapping; it is covered by integration tests but not yet by
the benchmark.

## Troubleshooting

| symptom | cause and fix |
|---|---|
| `imports X, which is not installed for …` | Larch picked an interpreter without your dependencies. Pass `--python` (or set `python` in config) to the one your tests use; the message names one that works when it can find it. |
| `imports X, which is in your repository … but not on the import path` | Add the directory to `pythonpath` in `[tool.larch]`. |
| `… through a path alias` (TypeScript) | Node cannot resolve tsconfig `paths`; verify functions whose imports are relative or installed packages. |
| `TypeScript needs Node.js >= 22.6` | Install a newer Node or pass `--node`. |
| `Lean toolchain … is not installed` | `larch doctor --install`. |
| a function is listed as skipped by `larch scan` | the reason is shown with `larch scan --all`; `larch verify FILE::function` still tries it. |

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest                                  # unit tests, adapter tests (real Python/Node), integration (real Lean, scripted LLM)
python -m bench.validate                # check the benchmark's ground truth
python -m bench.run --name my-exp ...   # run an experiment (see bench/run.py)
python -m bench.analyze my-exp --detail
```

Architecture in brief: `larch/engine/` is the language-neutral pipeline (formalize,
prove, test, adjudicate, fix) and the test driver; `larch/lean/` the Lean toolchain,
harness and checker; `larch/py/` and `larch/js/` the language backends (extraction,
mutation, runtime discovery, adapter); `larch/repo.py` repository scanning and change
detection; `larch/outputs.py` SARIF/JUnit/Markdown. See [DECISIONS.md](DECISIONS.md).
