"""The autouse ``_restore_sys_modules`` fixture in tests/conftest.py (E1).

Each pair is two tests in file order: the first changes ``sys.modules``, the second asserts
what the fixture put back. A shared dict carries the object seen in the first test.
"""

from __future__ import annotations

import importlib
import sys
import types

_SEEN: dict[str, object] = {}
_FRESH = "vnx_restore_guard_fresh_module"


def test_removed_entry_setup_pops_and_reimports():
    original = importlib.import_module("json.decoder")
    _SEEN["removed"] = original
    sys.modules.pop("json.decoder", None)
    again = importlib.import_module("json.decoder")
    assert again is not original


def test_removed_entry_is_back_as_the_original_object():
    assert sys.modules["json.decoder"] is _SEEN["removed"]


def test_replaced_entry_setup_swaps_in_another_object():
    _SEEN["replaced"] = sys.modules["json"]
    sys.modules["json"] = types.ModuleType("json")
    assert sys.modules["json"] is not _SEEN["replaced"]


def test_replaced_entry_is_back_as_the_original_object():
    assert sys.modules["json"] is _SEEN["replaced"]


def test_new_module_setup_imports_a_module_first_seen_here():
    sys.modules.pop(_FRESH, None)
    module = types.ModuleType(_FRESH)
    sys.modules[_FRESH] = module
    _SEEN["fresh"] = module


def test_new_module_stays_imported():
    assert sys.modules.get(_FRESH) is _SEEN["fresh"]
    del sys.modules[_FRESH]
