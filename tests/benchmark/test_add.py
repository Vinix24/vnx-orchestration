import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "benchmarks"))

from add import add


def test_add_positive():
    assert add(2, 3) == 5


def test_add_negative():
    assert add(-1, 1) == 0


def test_add_zeros():
    assert add(0, 0) == 0


def test_add_both_negative():
    assert add(-3, -4) == -7
