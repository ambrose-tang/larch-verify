def ceil_div(a: int, b: int) -> int:
    """Return the ceiling of a / b, computed exactly with integer arithmetic.

    Requires b > 0; a may be any integer, including negative and very large
    values. For example ceil_div(7, 2) == 4 and ceil_div(-7, 2) == -3.
    """
    return -((-a) // b)
