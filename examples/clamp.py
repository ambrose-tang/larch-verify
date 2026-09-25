def clamp(x: int, lo: int, hi: int) -> int:
    """Clamp x into the closed interval [lo, hi].

    Requires lo <= hi. Values below lo become lo, values above hi become hi,
    and values inside the interval are returned unchanged.
    """
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x
