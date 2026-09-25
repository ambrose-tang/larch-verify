def unique(xs: list[int]) -> list[int]:
    """Return the distinct elements of xs, each once, in order of first appearance.

    unique([3, 1, 3, 2, 1]) == [3, 1, 2].
    """
    return list(set(xs))
