def compare_versions(a: str, b: str) -> int:
    """Compare two version strings of the form "MAJOR.MINOR.PATCH", where each
    part is a non-empty string of ASCII digits (leading zeros allowed).

    Parts are compared numerically from left to right. Returns -1 if a is older
    than b, 0 if they are equal, and 1 if a is newer. For example
    compare_versions("1.10.0", "1.9.3") == 1 and compare_versions("2.0.0", "2.0.00") == 0.
    """
    pa = [int(p) for p in a.split(".")]
    pb = [int(p) for p in b.split(".")]
    for x, y in zip(pa, pb):
        if x != y:
            return -1 if x < y else 1
    return 0
