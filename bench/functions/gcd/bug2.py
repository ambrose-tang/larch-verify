def gcd(a: int, b: int) -> int:
    """Return the greatest common divisor of a and b.

    The result is always non-negative, and gcd(0, 0) == 0. Negative inputs are
    allowed: gcd(-4, 6) == 2 and gcd(4, -6) == 2.
    """
    a, b = abs(a), abs(b)
    while b > 1:
        a, b = b, a % b
    return a
