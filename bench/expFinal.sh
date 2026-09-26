#!/bin/sh
set -u
PY=.venv/bin/python
R="$PY -m bench.run --retry-errors --parallel 6"
F="--formalize-mode auto --model claude-sonnet-5 --effort medium"
NP="--no-proofs --no-mutation --no-fix"
D="$F --variants correct --no-mutation --no-fix --proof-attempts 3 --proof-budget 0.30"
$R --name F-final $F --no-proofs
$R --name X-examples $F $NP --doc-examples
$R --name N-nodoc $F $NP --strip-docstrings --variants correct
$R --name B-haiku --formalize-mode auto --model claude-haiku-4-5 --effort medium $NP
$R --name D2-port+llm-haiku  $D --proof-strategy portfolio+llm --prover-model claude-haiku-4-5
$R --name C-low --formalize-mode auto --model claude-sonnet-5 --effort low $NP --functions allow_request,caesar_shift,chunk,days_in_month,insertion_sort,is_authorized,is_palindrome,isqrt,max_subarray_sum,merge_sorted,rotate_left,second_largest
$R --name B-opus --formalize-mode auto --model claude-opus-5 --effort medium $NP --functions allow_request,caesar_shift,chunk,days_in_month,insertion_sort,is_authorized,is_palindrome,isqrt,max_subarray_sum,merge_sorted,rotate_left,second_largest
echo "FINAL DONE"
