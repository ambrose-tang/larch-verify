def insertion_sort(xs: list[int]) -> list[int]:
    """Return a new list with the elements of xs in non-decreasing order.

    The input list is not modified.
    """
    result = list(xs)
    for i in range(1, len(result)):
        key = result[i]
        j = i - 1
        while j > 0 and result[j] > key:
            result[j + 1] = result[j]
            j -= 1
        result[j + 1] = key
    return result
