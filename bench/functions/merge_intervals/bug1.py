def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping closed intervals.

    Each interval is a pair (start, end) with start <= end; the input may be in
    any order. Intervals that overlap or touch (share an endpoint) are merged.
    The result is sorted by start and contains no overlapping or touching
    intervals, e.g. merge_intervals([(5, 6), (1, 3), (2, 4)]) == [(1, 4), (5, 6)].
    """
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
