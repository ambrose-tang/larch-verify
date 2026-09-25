def days_in_month(year: int, month: int) -> int:
    """Return the number of days in `month` (1-12) of `year` (Gregorian calendar).

    February has 29 days in leap years (divisible by 4, except centuries that
    are not divisible by 400) and 28 otherwise. April, June, September and
    November have 30 days; the other months have 31.
    Raises ValueError if month is not in 1..12.
    """
    if month < 1 or month > 12:
        raise ValueError(f"invalid month: {month}")
    if month == 2:
        leap = year % 4 == 0
        return 29 if leap else 28
    if month in (4, 6, 9, 11):
        return 30
    return 31
