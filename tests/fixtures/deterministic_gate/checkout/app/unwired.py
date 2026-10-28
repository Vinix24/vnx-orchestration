"""Fixture module for the wiring-gate golden: two new public definitions."""


def used_symbol():
    return "called from app/caller.py"


def orphan_symbol():
    return "nobody calls this one"
