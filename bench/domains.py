"""Ground truth for the benchmark (never shown to Larch).

For every function: a generator of inputs inside the documented domain (used to
prove each seeded bug is observable and to measure how rare it is), and
known-answer tests taken from the docstrings / definitions.
"""
from __future__ import annotations

import random
import string


def _sorted_list(rng, lo=-5, hi=5, n=10):
    return sorted(rng.randint(lo, hi) for _ in range(rng.randint(0, n)))


def _ints(rng, lo=-20, hi=20, n=10):
    return [rng.randint(lo, hi) for _ in range(rng.randint(0, n))]


def _bigint(rng):
    return rng.choice([rng.randint(-50, 50), rng.randint(-10**6, 10**6), rng.randint(-10**30, 10**30)])


def _version(rng):
    return ".".join(rng.choice(["0", "1", "2", "9", "10", "01", "11"]) for _ in range(3))


DOMAINS = {
    "is_leap_year": {
        "gen": lambda r: (rng_choice_year(r),),
        "kat": [((2000,), True), ((1900,), False), ((2024,), True), ((2023,), False), ((0,), True), ((-4,), True), ((-100,), False)],
    },
    "days_in_month": {
        "gen": lambda r: (rng_choice_year(r), r.randint(-1, 14)),
        "kat": [((2024, 2), 29), ((1900, 2), 28), ((2000, 2), 29), ((2023, 4), 30), ((2023, 12), 31), ((2023, 0), ValueError), ((2023, 13), ValueError)],
    },
    "isqrt": {
        "gen": lambda r: (r.choice([r.randint(0, 100), r.randint(0, 10**6), r.randint(0, 10**40), r.randint(0, 60) ** 2]),),
        "kat": [((0,), 0), ((1,), 1), ((3,), 1), ((4,), 2), ((15,), 3), ((16,), 4), ((10**20,), 10**10), ((10**20 - 1,), 10**10 - 1)],
    },
    "gcd": {
        "gen": lambda r: (r.randint(-60, 60), r.randint(-60, 60)),
        "kat": [((0, 0), 0), ((-4, 6), 2), ((4, -6), 2), ((12, 18), 6), ((7, 0), 7), ((0, -7), 7), ((3, 2), 1)],
    },
    "ceil_div": {
        "gen": lambda r: (_bigint(r), r.choice([1, 2, 3, 7, r.randint(1, 1000)])),
        "kat": [((7, 2), 4), ((-7, 2), -3), ((6, 3), 2), ((0, 5), 0), ((10**30 + 1, 10), 10**29 + 1)],
    },
    "binary_search": {
        "gen": lambda r: (_sorted_list(r, -4, 4), r.randint(-5, 5)),
        "kat": [(([], 1), None), (([1], 1), 0), (([1, 1, 2], 1), 0), (([1, 2, 2, 2, 3], 2), 1), (([1, 3], 2), None)],
    },
    "insertion_sort": {
        "gen": lambda r: (_ints(r),),
        "kat": [(([],), []), (([2, 1],), [1, 2]), (([3, 1, 2],), [1, 2, 3]), (([1, 3, 2],), [1, 2, 3])],
    },
    "merge_sorted": {
        "gen": lambda r: (_sorted_list(r, -5, 5, 6), _sorted_list(r, -5, 5, 6)),
        "kat": [(([], []), []), (([1, 3], [2]), [1, 2, 3]), (([1], [1]), [1, 1]), (([], [2, 3]), [2, 3])],
    },
    "unique": {
        "gen": lambda r: (_ints(r, -4, 4),),
        "kat": [(([3, 1, 3, 2, 1],), [3, 1, 2]), (([],), []), (([2, 1],), [2, 1])],
    },
    "max_subarray_sum": {
        "gen": lambda r: ([r.randint(-10, 10) for _ in range(r.randint(1, 10))],),
        "kat": [(([-2, 1, -3, 4, -1, 2, 1, -5, 4],), 6), (([-3, -1, -2],), -1), (([5],), 5), (([1, 2],), 3)],
    },
    "rotate_left": {
        "gen": lambda r: (_ints(r, 0, 9, 6), r.randint(-15, 15)),
        "kat": [(([1, 2, 3, 4], 1), [2, 3, 4, 1]), (([1, 2, 3, 4], -1), [4, 1, 2, 3]), (([], 3), []), (([1, 2], 5), [2, 1])],
    },
    "chunk": {
        "gen": lambda r: (_ints(r, 0, 9, 9), r.randint(1, 4)),
        "kat": [(([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]]), (([], 3), []), (([1, 2], 5), [[1, 2]]), (([1, 2, 3], 1), [[1], [2], [3]])],
    },
    "merge_intervals": {
        "gen": lambda r: ([tuple(sorted((r.randint(-8, 8), r.randint(-8, 8)))) for _ in range(r.randint(0, 5))],),
        "kat": [(([(5, 6), (1, 3), (2, 4)],), [(1, 4), (5, 6)]), (([(1, 2), (2, 3)],), [(1, 3)]), (([(1, 10), (2, 3)],), [(1, 10)]), (([],), [])],
    },
    "second_largest": {
        "gen": lambda r: (_ints(r, -4, 4, 6),),
        "kat": [(([3, 5, 5, 1],), 3), (([],), None), (([2, 2],), None), (([-1, -3],), -3), (([1, 3, 3],), 1)],
    },
    "is_palindrome": {
        "gen": lambda r: ("".join(r.choice("aAbB1 ,!é") for _ in range(r.randint(0, 8))),),
        "kat": [(("A man, a plan, a canal: Panama",), True), (("",), True), (("ab",), False), (("Aa",), True), (("race a car",), False)],
    },
    "run_length_encode": {
        "gen": lambda r: ("".join(r.choice("aab") for _ in range(r.randint(0, 8))),),
        "kat": [(("aaabcc",), [("a", 3), ("b", 1), ("c", 2)]), (("",), []), (("a",), [("a", 1)]), (("ab",), [("a", 1), ("b", 1)])],
    },
    "caesar_shift": {
        "gen": lambda r: ("".join(r.choice("abzAYZ !") for _ in range(r.randint(0, 7))), r.randint(-30, 30)),
        "kat": [(("Hello, World!", 3), "Khoor, Zruog!"), (("z", 1), "a"), (("Z", 1), "A"), (("a", -1), "z"), (("", 5), "")],
    },
    "parse_int": {
        "gen": lambda r: ("".join(r.choice("+-0127x ") for _ in range(r.randint(0, 5))),),
        "kat": [(("-42",), -42), (("+7",), 7), (("007",), 7), (("",), ValueError), (("-",), ValueError), (("+",), ValueError), (("1a",), ValueError), (("--1",), ValueError)],
    },
    "luhn_check": {
        "gen": lambda r: ("".join(r.choice(string.digits) for _ in range(r.randint(1, 12))),),
        "kat": [(("79927398713",), True), (("79927398710",), False), (("0",), True), (("18",), True), (("59",), True)],
    },
    "int_to_roman": {
        "gen": lambda r: (r.randint(1, 3999),),
        "kat": [((4,), "IV"), ((9,), "IX"), ((40,), "XL"), ((90,), "XC"), ((1994,), "MCMXCIV"), ((3999,), "MMMCMXCIX"), ((1,), "I")],
    },
    "compare_versions": {
        "gen": lambda r: (_version(r), _version(r)),
        "kat": [(("1.10.0", "1.9.3"), 1), (("2.0.0", "2.0.00"), 0), (("1.2.3", "1.2.4"), -1), (("0.0.9", "0.0.10"), -1)],
    },
    "is_authorized": {
        "gen": lambda r: (
            [(r.choice(["permit", "forbid"]), r.choice(["alice", "bob", "*"]), r.choice(["read", "write", "*"])) for _ in range(r.randint(0, 4))],
            r.choice(["alice", "bob"]),
            r.choice(["read", "write"]),
        ),
        "kat": [
            (([], "alice", "read"), False),
            (([("permit", "alice", "read")], "alice", "read"), True),
            (([("permit", "*", "*"), ("forbid", "alice", "read")], "alice", "read"), False),
            (([("forbid", "alice", "read"), ("permit", "*", "*")], "alice", "read"), False),
            (([("permit", "alice", "*")], "alice", "write"), True),
        ],
    },
    "allow_request": {
        "gen": lambda r: ([r.randint(0, 20) for _ in range(r.randint(0, 6))], r.randint(0, 22), r.randint(0, 4), r.randint(1, 8)),
        "kat": [(([], 10, 1, 5), True), (([10], 10, 1, 5), False), (([5], 10, 1, 5), True), (([6], 10, 1, 5), False), (([], 10, 0, 5), False)],
    },
    "is_power_of_two": {
        "gen": lambda r: (r.choice([r.randint(-20, 70), 2 ** r.randint(0, 80), -(2 ** r.randint(0, 10)), 2 ** r.randint(0, 40) + r.choice([-1, 1])]),),
        "kat": [((1,), True), ((2,), True), ((0,), False), ((-4,), False), ((6,), False), ((2**64,), True)],
    },
}


def rng_choice_year(r: random.Random) -> int:
    return r.choice([r.randint(-3000, 3000), r.randint(0, 40) * 100, r.randint(-10, 10) * 400, r.randint(1890, 2110)])
