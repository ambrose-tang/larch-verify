"""The wire format round-trips every value the test driver and adapters exchange.

Larch's own value language has no nested heterogeneous values, so this contract of
`larch/wire.py` is checked here by property test instead of by `larch verify`."""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from larch.wire import from_wire, to_wire

scalars = st.none() | st.booleans() | st.text() | st.integers(min_value=-(2**80), max_value=2**80)
values = st.recursive(scalars, lambda c: st.lists(c, max_size=4) | st.lists(c, max_size=4).map(tuple), max_leaves=12)


@settings(max_examples=500, deadline=None)
@given(values)
def test_decoding_what_to_wire_produced_gives_back_the_value(v):
    out = from_wire(to_wire(v))
    assert out == v and type(out) is type(v)


@given(st.integers())
def test_integers_beyond_json_precision_survive(n):
    assert from_wire(to_wire(n)) == n
    assert from_wire(to_wire((n, [n]))) == (n, [n])
