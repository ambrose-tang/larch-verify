def run_length_encode(s: str) -> list[tuple[str, int]]:
    """Run-length encode s as a list of (character, count) pairs, one per maximal
    run of equal adjacent characters.

    run_length_encode("aaabcc") == [("a", 3), ("b", 1), ("c", 2)]; the empty
    string encodes to [].
    """
    if not s:
        return []
    runs = []
    cur, count = s[0], 1
    for ch in s[1:]:
        if ch == cur:
            count += 1
        else:
            runs.append((cur, count))
            cur, count = ch, 1
    return runs
