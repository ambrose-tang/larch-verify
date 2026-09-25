def chunk(xs: list[int], size: int) -> list[list[int]]:
    """Split xs into consecutive chunks of length `size`; the last chunk may be
    shorter. Requires size >= 1.

    chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]] and chunk([], 3) == [].
    """
    out = []
    for i in range(0, len(xs), size):
        out.append(xs[i:i + size - 1] if size > 1 else xs[i:i + 1])
    return out
