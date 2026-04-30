"""Pytest collection config for the smoke suite.

The repo's ``pyproject.toml`` does not override ``python_files``, so
pytest's default ``test_*.py`` glob would skip files named
``smoke_*.py``. We extend the ini option at config time so directory
collection (``pytest tests/smoke/``) and explicit-path collection
(``pytest tests/smoke/smoke_foo.py``) both pick up exactly one copy
of each test.
"""
from __future__ import annotations


def pytest_configure(config):
    pf = config.getini("python_files")
    if "smoke_*.py" not in pf:
        pf.append("smoke_*.py")
