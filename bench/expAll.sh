#!/bin/sh
# All comparison experiments, in priority order, on the final code. Resumable:
# re-running skips finished variants and retries ones that ended in an error.
set -u
PY=.venv/bin/python
R="$PY -m bench.run --retry-errors --parallel 6"
F="--model claude-sonnet-5 --effort medium"          # formalization settings shared by most runs
NP="--no-proofs --no-mutation --no-fix"               # bug-detection-only runs

# A: formalization approach
$R --name A2-intent   --formalize-mode intent        $F $NP
$R --name A2-hybrid   --formalize-mode hybrid        $F $NP
$R --name A2-translit --formalize-mode transliterate $F $NP

# D: proving (24 correct variants; auto = intent for documented code, so formalizations are cached)
D="--formalize-mode auto $F --variants correct --no-mutation --no-fix --proof-attempts 3 --proof-budget 0.30"
$R --name D2-portfolio        $D --proof-strategy portfolio
$R --name D2-port+llm-low     $D --proof-strategy portfolio+llm    --prover-efforts low
$R --name D2-port+sketch-low  $D --proof-strategy portfolio+sketch --prover-efforts low
$R --name D2-port+llm-haiku   $D --proof-strategy portfolio+llm    --prover-model claude-haiku-4-5

# E: test generation (formalizations cached; only adjudication may call the LLM)
$R --name E-typed --formalize-mode auto $F $NP --test-strategy typed
$R --name E-llm   --formalize-mode auto $F $NP --test-strategy llm

# X: documented examples
$R --name X-examples --formalize-mode auto $F $NP --doc-examples

# N: undocumented code (docstrings stripped; auto falls back to hybrid)
$R --name N-nodoc --formalize-mode auto $F $NP --strip-docstrings --variants correct

# C: formalization effort
$R --name C-low --formalize-mode auto --model claude-sonnet-5 --effort low $NP

# B: model choice
$R --name B-haiku --formalize-mode auto --model claude-haiku-4-5 --effort medium $NP
$R --name B-opus  --formalize-mode auto --model claude-opus-5 --effort medium $NP
echo "ALL EXPERIMENTS DONE"
