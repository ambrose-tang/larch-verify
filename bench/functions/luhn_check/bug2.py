def luhn_check(number: str) -> bool:
    """Return True if the digit string `number` passes the Luhn checksum.

    Starting from the rightmost digit and moving left, every second digit
    (the 2nd, 4th, ... from the right) is doubled, and 9 is subtracted from any
    doubled value above 9. The number is valid if the sum of all resulting
    digits is divisible by 10. Requires `number` to be a non-empty string of
    ASCII digits. luhn_check("79927398713") is True.
    """
    total = 0
    for i, c in enumerate(reversed(number)):
        d = ord(c) - ord("0")
        if i % 2 == 1:
            d *= 2
            if d > 10:
                d -= 9
        total += d
    return total % 10 == 0
