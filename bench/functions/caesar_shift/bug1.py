def caesar_shift(s: str, k: int) -> str:
    """Shift every ASCII letter in s by k positions in the alphabet, wrapping
    around ('z' shifted by 1 is 'a'). Case is preserved and every other
    character is left unchanged. k may be any integer, including negative.

    caesar_shift("Hello, World!", 3) == "Khoor, Zruog!"
    """
    out = []
    for c in s:
        if "a" <= c <= "z":
            out.append(chr((ord(c) - ord("a") + k) % 26 + ord("a")))
        elif "A" <= c <= "Z":
            out.append(chr((ord(c) - ord("a") + k) % 26 + ord("A")))
        else:
            out.append(c)
    return "".join(out)
