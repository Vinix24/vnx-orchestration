"""Behavioral and unit tests for bench-claude-sonnet-4-6-03_refactoring output.

Verifies that the refactored process_active_items() and its sub-functions
produce output identical to the original proc() function in every case.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import refactored module
# ---------------------------------------------------------------------------

REFACTORED_MODULE = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "benchmark"
    / "output"
    / "bench-claude-sonnet-4-6-03_refactoring-1778967425"
    / "refactoring.py"
)

import importlib.util

_spec = importlib.util.spec_from_file_location("refactoring", REFACTORED_MODULE)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

process_active_items = _mod.process_active_items
_is_item_active = _mod._is_item_active
_build_timestamp_error = _mod._build_timestamp_error
_is_within_cutoff = _mod._is_within_cutoff
_apply_maximum_cap = _mod._apply_maximum_cap
_validate_minimum_value = _mod._validate_minimum_value
_apply_multiplier = _mod._apply_multiplier
_apply_rounding = _mod._apply_rounding
_build_result_entry = _mod._build_result_entry


# ---------------------------------------------------------------------------
# Original function for behavioral equivalence checks
# ---------------------------------------------------------------------------

def proc(d, t, cfg):
    res = []
    errs = []
    for i, item in enumerate(d):
        if item.get('s') != 'active':
            continue
        ts = item.get('ts')
        if ts is None:
            errs.append({'idx': i, 'msg': 'missing ts', 'id': item.get('id')})
            continue
        if ts > t:
            continue
        val = item.get('v', 0)
        lim = cfg.get('max_v', 100)
        if val > lim:
            val = lim
            item['capped'] = True
        mn = cfg.get('min_v', 0)
        if val < mn:
            errs.append({'idx': i, 'msg': 'below min', 'id': item.get('id'), 'val': val})
            continue
        m = cfg.get('mult', 1.0)
        val = val * m
        if cfg.get('round'):
            val = round(val, cfg.get('round_dp', 2))
        res.append({
            'id': item.get('id'),
            'v': val,
            'ts': ts,
            'src': item.get('src', 'unknown'),
        })
    return res, errs


# ---------------------------------------------------------------------------
# Sub-function unit tests
# ---------------------------------------------------------------------------

class TestIsItemActive:
    def test_active_status_returns_true(self):
        assert _is_item_active({'s': 'active'}) is True

    def test_inactive_status_returns_false(self):
        assert _is_item_active({'s': 'inactive'}) is False

    def test_missing_status_returns_false(self):
        assert _is_item_active({}) is False

    def test_other_status_returns_false(self):
        assert _is_item_active({'s': 'pending'}) is False


class TestBuildTimestampError:
    def test_returns_error_with_correct_fields(self):
        item = {'id': 'item-1'}
        error = _build_timestamp_error(item, 3)
        assert error == {'idx': 3, 'msg': 'missing ts', 'id': 'item-1'}

    def test_missing_id_uses_none(self):
        error = _build_timestamp_error({}, 0)
        assert error['id'] is None

    def test_index_is_preserved(self):
        error = _build_timestamp_error({'id': 'x'}, 99)
        assert error['idx'] == 99


class TestIsWithinCutoff:
    def test_timestamp_equal_to_cutoff_is_within(self):
        assert _is_within_cutoff(100.0, 100.0) is True

    def test_timestamp_before_cutoff_is_within(self):
        assert _is_within_cutoff(50.0, 100.0) is True

    def test_timestamp_after_cutoff_is_not_within(self):
        assert _is_within_cutoff(101.0, 100.0) is False


class TestApplyMaximumCap:
    def test_value_above_max_gets_capped(self):
        item = {}
        result = _apply_maximum_cap(item, 150, {'max_v': 100})
        assert result == 100
        assert item.get('capped') is True

    def test_value_at_max_not_capped(self):
        item = {}
        result = _apply_maximum_cap(item, 100, {'max_v': 100})
        assert result == 100
        assert 'capped' not in item

    def test_value_below_max_unchanged(self):
        item = {}
        result = _apply_maximum_cap(item, 50, {'max_v': 100})
        assert result == 50
        assert 'capped' not in item

    def test_default_max_is_100(self):
        item = {}
        result = _apply_maximum_cap(item, 200, {})
        assert result == 100
        assert item.get('capped') is True


class TestValidateMinimumValue:
    def test_value_below_min_returns_error(self):
        item = {'id': 'item-2'}
        error = _validate_minimum_value(item, 1, -5, {'min_v': 0})
        assert error == {'idx': 1, 'msg': 'below min', 'id': 'item-2', 'val': -5}

    def test_value_at_min_returns_none(self):
        result = _validate_minimum_value({'id': 'x'}, 0, 0, {'min_v': 0})
        assert result is None

    def test_value_above_min_returns_none(self):
        result = _validate_minimum_value({'id': 'x'}, 0, 10, {'min_v': 0})
        assert result is None

    def test_default_min_is_zero(self):
        error = _validate_minimum_value({'id': 'x'}, 0, -1, {})
        assert error is not None
        assert error['val'] == -1


class TestApplyMultiplier:
    def test_multiplier_applied(self):
        result = _apply_multiplier(10, {'mult': 2.5})
        assert result == 25.0

    def test_default_multiplier_is_one(self):
        result = _apply_multiplier(42, {})
        assert result == 42.0

    def test_zero_multiplier_gives_zero(self):
        result = _apply_multiplier(100, {'mult': 0})
        assert result == 0


class TestApplyRounding:
    def test_rounding_applied_with_decimal_places(self):
        result = _apply_rounding(3.14159, {'round': True, 'round_dp': 2})
        assert result == 3.14

    def test_rounding_not_applied_when_flag_false(self):
        result = _apply_rounding(3.14159, {'round': False})
        assert result == 3.14159

    def test_rounding_not_applied_when_flag_absent(self):
        result = _apply_rounding(3.14159, {})
        assert result == 3.14159

    def test_default_decimal_places_is_2(self):
        result = _apply_rounding(1.23456, {'round': True})
        assert result == 1.23


class TestBuildResultEntry:
    def test_all_fields_populated(self):
        item = {'id': 'item-5', 'src': 'sensor-a'}
        entry = _build_result_entry(item, 42.0, 1000.0)
        assert entry == {'id': 'item-5', 'v': 42.0, 'ts': 1000.0, 'src': 'sensor-a'}

    def test_missing_src_defaults_to_unknown(self):
        item = {'id': 'item-6'}
        entry = _build_result_entry(item, 10, 500.0)
        assert entry['src'] == 'unknown'

    def test_missing_id_uses_none(self):
        entry = _build_result_entry({}, 5, 100.0)
        assert entry['id'] is None


# ---------------------------------------------------------------------------
# Behavioral equivalence tests
# ---------------------------------------------------------------------------

def _make_item(
    item_id: str,
    status: str = 'active',
    timestamp: float | None = 100.0,
    value: float | int = 50,
    source: str | None = None,
) -> dict:
    item: dict = {'id': item_id, 's': status, 'ts': timestamp, 'v': value}
    if source is not None:
        item['src'] = source
    return item


class TestBehavioralEquivalence:
    """Every case must produce identical output from proc() and process_active_items()."""

    def _assert_equivalent(self, items: list[dict], cutoff: float, config: dict):
        import copy
        items_original = copy.deepcopy(items)
        items_refactored = copy.deepcopy(items)

        expected_results, expected_errors = proc(items_original, cutoff, config)
        actual_results, actual_errors = process_active_items(items_refactored, cutoff, config)

        assert actual_results == expected_results, (
            f"Results differ:\n  expected={expected_results}\n  actual={actual_results}"
        )
        assert actual_errors == expected_errors, (
            f"Errors differ:\n  expected={expected_errors}\n  actual={actual_errors}"
        )

        # Side-effect: 'capped' flag on items must match
        for orig_item, refactored_item in zip(items_original, items_refactored):
            assert orig_item.get('capped') == refactored_item.get('capped')

    def test_empty_input(self):
        self._assert_equivalent([], 1000.0, {})

    def test_all_inactive_items_skipped(self):
        items = [
            _make_item('a', status='inactive'),
            _make_item('b', status='pending'),
        ]
        self._assert_equivalent(items, 1000.0, {})

    def test_missing_timestamp_produces_error(self):
        items = [_make_item('a', timestamp=None)]
        self._assert_equivalent(items, 1000.0, {})

    def test_future_timestamp_skipped(self):
        items = [_make_item('a', timestamp=2000.0)]
        self._assert_equivalent(items, 1000.0, {})

    def test_happy_path_single_item(self):
        items = [_make_item('a', value=50)]
        self._assert_equivalent(items, 1000.0, {})

    def test_value_capped_at_maximum(self):
        items = [_make_item('a', value=200)]
        self._assert_equivalent(items, 1000.0, {'max_v': 100})

    def test_below_minimum_produces_error(self):
        items = [_make_item('a', value=-10)]
        self._assert_equivalent(items, 1000.0, {'min_v': 0})

    def test_multiplier_applied(self):
        items = [_make_item('a', value=10)]
        self._assert_equivalent(items, 1000.0, {'mult': 3.0})

    def test_rounding_applied(self):
        items = [_make_item('a', value=10)]
        self._assert_equivalent(items, 1000.0, {'mult': 3.333, 'round': True, 'round_dp': 2})

    def test_source_field_propagated(self):
        items = [_make_item('a', source='sensor-x')]
        self._assert_equivalent(items, 1000.0, {})

    def test_mixed_items_produce_correct_split(self):
        items = [
            _make_item('active-ok', value=50, timestamp=100.0),
            _make_item('inactive', status='inactive'),
            _make_item('no-ts', timestamp=None),
            _make_item('future', timestamp=9999.0),
            _make_item('capped', value=999),
            _make_item('below-min', value=-5, timestamp=50.0),
        ]
        config = {'max_v': 100, 'min_v': 0, 'mult': 1.5, 'round': True, 'round_dp': 1}
        self._assert_equivalent(items, 200.0, config)

    def test_cap_then_below_min_sequence(self):
        # After capping at max_v=5, value=5 which is >= min_v=0, so no error
        items = [_make_item('a', value=100)]
        self._assert_equivalent(items, 1000.0, {'max_v': 5, 'min_v': 0})

    def test_custom_min_v_produces_error(self):
        items = [_make_item('a', value=3)]
        self._assert_equivalent(items, 1000.0, {'min_v': 10})

    def test_index_in_error_matches_position(self):
        items = [
            _make_item('skip', status='inactive'),
            _make_item('err', timestamp=None),
        ]
        _, errors = process_active_items(items, 1000.0, {})
        # 'err' is at index 1 in the full list
        assert errors[0]['idx'] == 1

    def test_multiple_errors_collected(self):
        items = [
            _make_item('no-ts', timestamp=None),
            _make_item('below', value=-1),
            _make_item('ok', value=5),
        ]
        results, errors = process_active_items(items, 1000.0, {'min_v': 0})
        assert len(errors) == 2
        assert len(results) == 1

    def test_rounding_default_decimal_places(self):
        items = [_make_item('a', value=10)]
        config = {'mult': 1.0 / 3.0, 'round': True}
        self._assert_equivalent(items, 1000.0, config)

    def test_zero_value_default(self):
        # Item without 'v' key defaults to 0
        item = {'id': 'a', 's': 'active', 'ts': 100.0}
        self._assert_equivalent([item], 1000.0, {})
