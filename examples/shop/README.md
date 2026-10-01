# Example: a storefront backend

A small but realistic e-commerce backend used to demonstrate and test Larch end to
end. Each phase of Larch's contract support is exercised here:

| file | what it is | Larch feature |
|---|---|---|
| `shop/shipping.py` | shipping rates by weight, zone and speed | contracts, **exhaustive testing** (300 inputs, domain proved complete) |
| `shop/pricing.py` | discounts and order-line totals, integer cents | contracts, proposed contracts (`line_total` has an empty heading) |
| `web/src/payments.ts` | pay-in-N instalment split (TypeScript) | contracts on TypeScript |
| `shop/ledger.py` | store-credit ledger (a class) | **stateful component**: invariants, operation contracts, call sequences |
| `web/src/cart.ts` | shopping cart (a TypeScript class) | stateful component on TypeScript |

The contracts are in [`LARCH.md`](LARCH.md). The code contains bugs of the kind that
reach production:

| bug | found by | real run (`claude-sonnet-5`, `larch verify --yes`) |
|---|---|---|
| heavy-parcel surcharge uses `>` instead of `>=` at exactly 20 kg | exhaustive test: exactly the 10 inputs at 20 kg disagree | ✓ `shipping_cost_cents(20, 1, False)` returned 2779, expected 4279; 5/5 specs proved |
| `round()` rounds half-cents to even, not in the customer's favour (and uses floats) | contract "an exact half cent is rounded in the customer's favour" | ✓ `apply_discount(1, 50)` returned 1, expected 0 |
| the same bug, inherited by `line_total` | proposed contracts + model disagreement | ✓ `line_total(2, 655, 15)` returned 1114, expected 1113 |
| instalments use `Math.round`, so the last one absorbs the error | contracts "no two instalments differ by more than one cent", "none negative" | ✓ `splitPayment(197, 8)` returned `[25, …, 25, 22]` |
| `transfer` credits the target before debiting the source, so a self-transfer from an empty account succeeds | sequence test against the state-machine model (3,455 sequences, 2,955 of them every sequence of up to 3 calls) | ✓ `ledger.transfer('a', 'a', 1)` returned instead of raising; fix validated; 7/9 contracts proved, 30/30 mutants caught |
| `remove` deletes a line only when its quantity hits exactly 0, so over-removal leaves a negative line | contract "removing more units than are in the cart removes the whole line" | ✓ `cart.add('a', 1, 5); cart.remove('a', 100); cart.totalCents` gave −495, expected 0; 5/6 proved, 23/23 mutants caught |

Run it yourself (about $1 and 10 minutes):

```bash
cd examples/shop
larch verify            # checks everything in LARCH.md; asks you to approve the specs
larch verify --yes      # the same, accepting specs unreviewed (what CI's `auto` mode does)
```
