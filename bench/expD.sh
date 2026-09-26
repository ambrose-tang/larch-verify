#!/bin/sh
# Experiment D: proof strategies and prover models, on the 24 correct variants.
# Formalization settings must match the chosen Experiment A configuration so the
# (cached) formalizations are identical across D runs.
set -u
MODE=${1:-hybrid}
PY=.venv/bin/python
COMMON="--model claude-sonnet-5 --effort medium --formalize-mode $MODE --variants correct --no-mutation --no-fix --parallel 4 --proof-attempts 3 --proof-budget 0.30"
$PY -m bench.run --name D-portfolio        --proof-strategy portfolio $COMMON
$PY -m bench.run --name D-port+llm-low     --proof-strategy portfolio+llm    --prover-efforts low $COMMON
$PY -m bench.run --name D-port+sketch-low  --proof-strategy portfolio+sketch --prover-efforts low $COMMON
echo "EXPERIMENT D (strategies) DONE"
