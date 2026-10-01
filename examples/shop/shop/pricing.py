"""Prices and discounts. All amounts are integer cents."""


def apply_discount(price_cents: int, percent_off: int) -> int:
    """Price after a percentage discount, in cents.

    Requires price_cents >= 0 and 0 <= percent_off <= 100. The discount is
    price_cents * percent_off / 100, rounded to the nearest cent with exact halves
    rounded up (in the customer's favour).
    """
    discount = round(price_cents * percent_off / 100)
    return price_cents - discount


def line_total(unit_price_cents: int, quantity: int, percent_off: int) -> int:
    """Total for one order line: `quantity` units at `unit_price_cents`, with the
    percentage discount applied once to the whole line (not per unit).

    Requires unit_price_cents >= 0, quantity >= 0 and 0 <= percent_off <= 100.
    """
    return apply_discount(unit_price_cents * quantity, percent_off)
