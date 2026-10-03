# Larch, verified by Larch

Contracts for the parts of Larch whose mistakes would silently weaken what it tells a
developer: the lint gate that keeps `sorry` and `axiom` out of proofs and the text helpers every
stage uses. (The wire format between the test driver and the code under test has nested,
heterogeneous values, which Larch's value language cannot express; it is covered by the
round-trip property test in `tests/test_wire_roundtrip.py`.)
Run `larch verify` to check them; `larch certificate` checks the result still matches the code.

# Contracts

## larch/lean/lint.py::strip_comments_and_strings
- The result has exactly as many characters as the source, and a newline in the result
  is at exactly the position where the source has one, so reported line numbers stay correct.
- Source with no comment or string-literal syntax (no `/-`, `--`, `"` or `'`) comes back unchanged.

## larch/util.py::extract_code_block
- If the text contains no triple-backtick fence, the result is `None`.
- If the text contains a fenced block, the result is not `None`.
- The result never starts or ends with a newline.

## larch/util.py::unescape_code
- Code that contains no backslash is returned unchanged.

## larch/util.py::truncate
- Text no longer than the limit is returned unchanged.
- Longer text keeps its first two thirds and last third of the limit, and the marker between them states exactly how many characters were dropped.

## larch/util.py::slug
- The result is never empty, is at most `maxlen` characters long, and contains only letters, digits and underscores.

## larch/util.py::indent
- Every non-blank line gains exactly `n` leading spaces; blank lines are left as they were. Lines are split as in Python's `splitlines`.

## larch/engine/prove.py::split_decls
- No line is lost: the non-blank lines of all the pieces, in order, are exactly the non-blank lines of the input (lines split as in Python's `splitlines`).
- Input none of whose lines starts with `theorem`, `lemma`, `def`, `abbrev`, `example` or `instance` gives at most one piece.

## larch/engine/prove.py::recursive_defs
- The result has no duplicates.
- Code with no `def` gives an empty list.

## larch/component.py::def_param_names
- A name that is not defined in the code gives `None`.
- Parameters come back in the order they are written, one name per binder.

## larch/chat.py::clean_draft
- The result ends with exactly one newline and has no leading blank line.
- Cleaning twice gives the same result as cleaning once.
- Text that is already a clean Markdown document (starts with a heading, no code fence) is unchanged apart from its final newline.
