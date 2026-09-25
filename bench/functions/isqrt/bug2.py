def isqrt(n: int) -> int:
    """Return the integer square root of n: the largest r such that r * r <= n.

    Requires n >= 0. Uses Newton's method on integers, so it is exact and fast
    even for very large n.
    """
    if n < 2:
        return n
    x = n
    y = (x + 1) // 2
    while y < x:
        x = y
        y = int((x + n / x) // 2)
    return x
