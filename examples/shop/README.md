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
| `shop/api.py` | orders and store-credit wallets (FastAPI + Postgres) | **service**: real database, request sequences |
| `LARCH.md` `# System rules` | checkout split, no double charge | **system rules** proved from component contracts |

The contracts are in [`LARCH.md`](LARCH.md). The code contains bugs of the kind that
reach production:

| bug | found by | real run (`claude-sonnet-5`, `larch verify --yes`) |
|---|---|---|
| heavy-parcel surcharge uses `>` instead of `>=` at exactly 20 kg | exhaustive test: exactly the 10 inputs at 20 kg disagree | ✓ `shipping_cost_cents(20, 1, False)` returned 2779, expected 4279; 5/5 specs proved |
| `round()` rounds half-cents to even, not in the customer's favour (and uses floats) | contract "an exact half cent is rounded in the customer's favour" | ✓ `apply_discount(1, 50)` returned 1, expected 0 |
| the same bug, inherited by `line_total` | proposed contracts + model disagreement | ✓ `line_total(2, 655, 15)` returned 1114, expected 1113 |
| instalments use `Math.round`, so the last one absorbs the error | contracts "no two instalments differ by more than one cent", "none negative" | ✓ `splitPayment(197, 8)` returned `[25, …, 25, 22]` |
| `transfer` credits the target before debiting the source, so a failed transfer still credits the target | sequence test against the state-machine model, observers compared after every call | ✓ `ledger.transfer('c', 'a', 3); ledger.total()` gave 3, expected 0; fix validated; 8/9 contracts proved, 30/30 mutants caught |
| `remove` deletes a line only when its quantity hits exactly 0, so over-removal leaves a negative line | contract "removing more units than are in the cart removes the whole line" | ✓ `cart.add('b', 1, 74); cart.remove('b', 4); cart.totalCents` gave −222, expected 0; fix validated; 6/8 proved, 23/23 mutants caught |
| idempotency lookup filters `AND status = 'pending'`, so retrying after payment creates a second order | contract "retrying with the same Idempotency-Key returns the original order … whatever has happened to the order since" | ✓ `create_order('k1','c2',50); deposit('c2',100); pay(1); create_order('k1','c2',100)` gave 201 with id 2, expected 200 with order 1 |
| `pay` debits the wallet before checking the order is still pending: paying twice charges twice | contract "a payment that fails changes nothing" | ✓ `deposit('c1',100); create_order(None,'c1',30); pay(1); pay(1); wallet('c1')` gave 40, expected 70; fix validated (on a copy of the service); 6/6 contracts proved, 7/7 mutants caught |

Larch finds both service bugs only because its request sequences reach deep states: it
grows sequences that reach rare model behaviour (an order paid, then paid again), so a
successful payment appears in about 40% of sequences instead of about 1%.

System rules (proved from the components' Lean contracts, see the main README):

| rule | result |
|---|---|
| At checkout, the instalments for a cart add up exactly to the cart's total. (uses Cart, splitPayment) | proved in 2 s from `splitPayment`'s "instalments add up to the total"; reported as **bug** because Cart and splitPayment themselves disagree with their models |
| Paying an order a second time never charges the customer again. (uses the orders service) | proved from the service's "a payment that fails changes nothing" (LLM proof, 4 attempts); reported as **bug** because the real service breaks it: the sequence test shows the second `pay` charging again |

Run it yourself (about $8 and an hour for everything; Postgres via Docker or local binaries):

```bash
cd examples/shop
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # the service's own dependencies
larch verify            # checks everything in LARCH.md; asks you to approve the specs
larch verify --yes      # the same, accepting specs unreviewed (what CI's `auto` mode does)
```
