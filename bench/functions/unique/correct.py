def unique(xs: list[int]) -> list[int]:
    """Return the distinct elements of xs, each once, in order of first appearance.

    unique([3, 1, 3, 2, 1]) == [3, 1, 2].
    """
    seen = set()
    out = []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out
