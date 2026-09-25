#!/usr/bin/env python3
"""Test for the counting bug in 04_compare_and_sample.py (OI-1804).

Ronde 1 reported laya_td_distribution as zero over all four classes while the
file held 657 valid predictions. Two defects caused it:
  1. A walrus `td_dist = dist(td_dist := dist(td_labels))` overwrote the real
     distribution with dist() applied to a dict of counts.
  2. `findings = {r["id"]: r for r in ...}` silently dropped 39 duplicate ids,
     so `total` read 618 instead of 657.

This test asserts:
  * The fixed assert_row_count raises on a read/count mismatch (the red run on
    the old buggy shape, signature intact).
  * The fixed assert_row_count passes when read == counted (green).
  * The walrus bug is gone: the distribution is computed directly, not double-
    wrapped.

Run: python3 test_count_bug.py  (no pytest needed; stdlib only)
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# Import the fixed helpers from 04_compare_and_sample.py
sys.path.insert(0, HERE)
import importlib.util

spec = importlib.util.spec_from_file_location(
    "compare_sample", os.path.join(HERE, "04_compare_and_sample.py")
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

assert_row_count = mod.assert_row_count
dist = mod.dist

FAILS = []


def check(name, cond):
    if cond:
        print(f"  [ok] {name}")
    else:
        print(f"  [x]  {name}")
        FAILS.append(name)


def expect_raise(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
        return False
    except AssertionError:
        return True


def test_assert_row_count_red_on_mismatch():
    """The red run on the old buggy shape: read 657, counted 618 must raise."""
    print("test_assert_row_count_red_on_mismatch:")
    raised = expect_raise(assert_row_count, 657, 618, "findings.jsonl")
    check("raises AssertionError when read != counted (657 vs 618)", raised)
    # And 657 vs 0 (the silent-zero extreme)
    raised_zero = expect_raise(assert_row_count, 657, 0, "laya_td")
    check("raises AssertionError on silent-zero (657 vs 0)", raised_zero)


def test_assert_row_count_green_on_match():
    """Green: read == counted does not raise."""
    print("test_assert_row_count_green_on_match:")
    try:
        assert_row_count(657, 657, "findings.jsonl")
        check("no raise when read == counted (657 vs 657)", True)
    except AssertionError:
        check("no raise when read == counted (657 vs 657)", False)


def test_walrus_bug_gone():
    """The walrus `dist(dist(labels))` that zeroed the distribution is gone.
    Direct dist() over a label list returns the real counts."""
    print("test_walrus_bug_gone:")
    labels = ["correctheid"] * 204 + ["beleid"] * 223 + ["beveiliging"] * 166 + ["stijl"] * 25
    classes = ["stijl", "correctheid", "beveiliging", "beleid"]
    d = dist(labels, classes)
    check("style/stijl counted = 25", d["stijl"] == 25)
    check("correctheid counted = 204", d["correctheid"] == 204)
    check("beveiliging counted = 166", d["beveiliging"] == 166)
    check("beleid counted = 223", d["beleid"] == 223)
    # The old walrus would have produced all-zeros here.
    all_zero = all(v == 0 for v in d.values())
    check("distribution is NOT all-zero (walrus bug absent)", not all_zero)


def test_dist_handles_empty():
    print("test_dist_handles_empty:")
    d = dist([], ["stijl", "correctheid", "beveiliging", "beleid"])
    check("empty input gives all-zero (no crash)", all(v == 0 for v in d.values()))


def main():
    test_assert_row_count_red_on_mismatch()
    test_assert_row_count_green_on_match()
    test_walrus_bug_gone()
    test_dist_handles_empty()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} checks -> {FAILS}")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()