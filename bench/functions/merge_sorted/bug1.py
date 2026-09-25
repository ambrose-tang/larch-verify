def merge_sorted(a: list[int], b: list[int]) -> list[int]:
    """Merge two lists, each sorted in non-decreasing order, into one sorted
    list that contains every element of both inputs (duplicates are kept)."""
    result = []
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i] <= b[j]:
            result.append(a[i])
            i += 1
        else:
            result.append(b[j])
            j += 1
    result.extend(a[i:])
    return result
