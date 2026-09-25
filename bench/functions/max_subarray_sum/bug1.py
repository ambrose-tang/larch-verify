def max_subarray_sum(xs: list[int]) -> int:
    """Return the largest sum of a non-empty contiguous sub-list of xs.

    Requires xs to be non-empty. For example
    max_subarray_sum([-2, 1, -3, 4, -1, 2, 1, -5, 4]) == 6 and
    max_subarray_sum([-3, -1, -2]) == -1.
    """
    best = cur = 0
    for x in xs:
        cur = max(0, cur + x)
        best = max(best, cur)
    return best
