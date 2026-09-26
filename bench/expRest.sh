#!/bin/sh
# Remaining experiments, quota-aware. HALF = every other function (12 of 24).
set -u
PY=.venv/bin/python
R="$PY -m bench.run --retry-errors --parallel 6"
F="--formalize-mode auto --model claude-sonnet-5 --effort medium"
NP="--no-proofs --no-mutation --no-fix"
D="$F --variants correct --no-mutation --no-fix --proof-attempts 3 --proof-budget 0.30"
HALF=allow_request,caesar_shift,chunk,days_in_month,insertion_sort,is_authorized,is_palindrome,isqrt,max_subarray_sum,merge_sorted,rotate_left,second_largest
$R --name D2-port+llm-low    $D --proof-strategy portfolio+llm    --prover-efforts low
$R --name E-typed $F $NP --test-strategy typed
$R --name E-llm   $F $NP --test-strategy llm
$R --name D2-port+sketch-low $D --proof-strategy portfolio+sketch --prover-efforts low
$R --name D2-port+llm-haiku  $D --proof-strategy portfolio+llm    --prover-model claude-haiku-4-5
$R --name B-haiku --formalize-mode auto --model claude-haiku-4-5 --effort medium $NP
$R --name N-nodoc $F $NP --strip-docstrings --variants correct
$R --name X-examples $F $NP --doc-examples
$R --name F-final $F --no-proofs
$R --name C-low --formalize-mode auto --model claude-sonnet-5 --effort low $NP --functions $HALF
$R --name B-opus --formalize-mode auto --model claude-opus-5 --effort medium $NP --functions $HALF
echo "REST DONE"
