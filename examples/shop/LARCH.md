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
