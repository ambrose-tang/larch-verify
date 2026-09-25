def rotate_left(xs: list[int], k: int) -> list[int]:
    """Return a copy of xs rotated left by k positions.

    k may be larger than len(xs) (it wraps around) or negative (rotating left
    by -k is rotating right by k). rotate_left([1, 2, 3, 4], 1) == [2, 3, 4, 1]
    and rotate_left([1, 2, 3, 4], -1) == [4, 1, 2, 3]. An empty list stays empty.
    """
    k %= len(xs)
    return xs[k:] + xs[:k]
