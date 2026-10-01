"""Shipping rates for the storefront."""

BASE_CENTS = {1: 499, 2: 599, 3: 699, 4: 899, 5: 1199}  # standard rate for the first kg, by zone
PER_KG_CENTS = 120  # each additional kg
HEAVY_KG = 20
HEAVY_SURCHARGE_CENTS = 1500
EXPRESS_PERCENT = 180


def shipping_cost_cents(weight_kg: int, zone: int, express: bool) -> int:
    """Shipping cost of one parcel, in cents.

    Requires 1 <= weight_kg <= 30 and 1 <= zone <= 5. The standard cost is the zone's
    base rate, plus 1.20 for every kg above the first, plus a 15.00 heavy-parcel
    surcharge for parcels of 20 kg or more. Express costs 180% of the standard cost,
    rounded up to the next cent.
    """
    cost = BASE_CENTS[zone] + PER_KG_CENTS * (weight_kg - 1)
    if weight_kg > HEAVY_KG:
        cost += HEAVY_SURCHARGE_CENTS
    if express:
        cost = -(-cost * EXPRESS_PERCENT // 100)
    return cost
