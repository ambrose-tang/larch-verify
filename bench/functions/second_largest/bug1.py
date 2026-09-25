from typing import Optional


def second_largest(xs: list[int]) -> Optional[int]:
    """Return the largest value in xs that is strictly smaller than the maximum,
    or None if there is no such value (xs is empty or all its elements are equal).

    second_largest([3, 5, 5, 1]) == 3.
    """
    largest: Optional[int] = None
    second: Optional[int] = None
    for x in xs:
        if largest is None or x > largest:
            second = largest
            largest = x
        elif second is None or x > second:
            second = x
    return second
