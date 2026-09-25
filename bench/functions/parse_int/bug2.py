def parse_int(s: str) -> int:
    """Parse a decimal integer with an optional leading '+' or '-' sign.

    The string must be an optional sign followed by one or more ASCII digits,
    with no whitespace or other characters; otherwise ValueError is raised.
    parse_int("-42") == -42, parse_int("+7") == 7 and parse_int("007") == 7.
    """
    if not s:
        raise ValueError("empty string")
    sign = 1
    if s[0] == "-":
        sign = -1
        s = s[1:]
    if not s:
        raise ValueError("no digits")
    value = 0
    for c in s:
        if not ("0" <= c <= "9"):
            raise ValueError(f"invalid character {c!r}")
        value = value * 10 + (ord(c) - ord("0"))
    return sign * value
