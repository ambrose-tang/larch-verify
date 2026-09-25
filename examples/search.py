from typing import Optional


def binary_search(xs: list[int], target: int) -> Optional[int]:
    """Return the index of the first occurrence of target in xs, or None.

    xs must be sorted in non-decreasing order. If target occurs several times,
    the smallest index at which it occurs is returned.
    """
    lo, hi = 0, len(xs) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if xs[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    if xs and xs[lo] == target:
        return lo
    return None
