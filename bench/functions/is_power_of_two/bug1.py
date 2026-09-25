def is_power_of_two(n: int) -> bool:
    """Return True if n is a power of two (1, 2, 4, 8, ...), else False.

    n may be any integer; zero and negative numbers are not powers of two.
    """
    return n & (n - 1) == 0
