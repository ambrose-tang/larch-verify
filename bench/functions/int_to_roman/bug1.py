def int_to_roman(n: int) -> str:
    """Convert n to a Roman numeral in standard subtractive notation, e.g.
    4 -> "IV", 9 -> "IX", 40 -> "XL", 1994 -> "MCMXCIV". Requires 1 <= n <= 3999."""
    table = [
        (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
        (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
        (10, "X"), (9, "IX"), (5, "V"), (1, "I"),
    ]
    out = []
    for value, symbol in table:
        while n >= value:
            out.append(symbol)
            n -= value
    return "".join(out)
