# Shop: what this code must do

Contracts for the storefront backend, checked by `larch verify` (see the Larch README).

# Contracts

## shop/shipping.py::shipping_cost_cents
- The cost is always positive.
- Express delivery always costs more than standard delivery for the same parcel and zone.
- A heavier parcel never costs less than a lighter one to the same zone at the same speed.
- A parcel of 20 kg or more pays the 15.00 heavy-parcel surcharge: it costs at least 15.00 more than the
  same parcel would without the surcharge.

## shop/pricing.py::apply_discount
- The discounted price is never negative and never more than the original price.
- The discount is rounded to the nearest cent, and an exact half cent is rounded in the customer's favour.

## shop/pricing.py::line_total

## web/src/payments.ts::splitPayment
- The instalments add up exactly to the total.
- There are exactly `parts` instalments and none of them is negative.
- No two instalments differ by more than one cent.

## shop/ledger.py::Ledger
- No balance is ever negative.
- A transfer never changes the total store credit outstanding.
- An operation that raises changes nothing.
- Withdrawing more than an account holds fails.

## web/src/cart.ts::Cart
- No quantity is ever negative.
- The total is the sum of quantity times unit price over all lines.
- Removing more units than are in the cart removes the whole line.

## service orders
start: uvicorn shop.api:app --port {port}
database: postgres
source: shop/api.py
- Retrying `POST /orders` with the same Idempotency-Key returns the original order and creates nothing new,
  whatever has happened to the order since.
- A payment that fails (402, 404 or 409) changes nothing: no wallet balance and no order status moves.
- Paying an order debits the customer's wallet by exactly the order amount, once.
- No wallet balance is ever negative.

# System rules
- At checkout, the instalments for a cart add up exactly to the cart's total.
  (uses: web/src/cart.ts::Cart, web/src/payments.ts::splitPayment)
- Paying an order a second time never charges the customer again.  (uses: service orders)
