def is_leap_year(year: int) -> bool:
    """Return True if `year` is a leap year in the proleptic Gregorian calendar.

    A year is a leap year if it is divisible by 4, except for years divisible
    by 100, which are leap years only if they are also divisible by 400.
    """
    return year % 4 == 0 and year % 100 != 0
