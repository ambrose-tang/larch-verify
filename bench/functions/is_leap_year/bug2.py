def is_leap_year(year: int) -> bool:
    """Return True if `year` is a leap year in the proleptic Gregorian calendar.

    A year is a leap year if it is divisible by 4, except that years divisible
    by 100 are leap years only if they are also divisible by 400. Works for any
    integer year, including zero and negative (astronomical) years.
    """
    if year % 4 == 0:
        return True
    if year % 100 == 0:
        return False
    return year % 400 == 0
