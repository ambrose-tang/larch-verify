def is_palindrome(s: str) -> bool:
    """Return True if s reads the same forwards and backwards, ignoring case and
    ignoring every character that is not an ASCII letter or digit.

    is_palindrome("A man, a plan, a canal: Panama") is True; the empty string
    is a palindrome.
    """
    chars = [c.lower() for c in s if c.isascii() and c.isalnum()]
    i, j = 0, len(chars) - 1
    while i < j - 1:
        if chars[i] != chars[j]:
            return False
        i += 1
        j -= 1
    return True
