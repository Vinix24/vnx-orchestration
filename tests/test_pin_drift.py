#!/usr/bin/env python3
"""Tests for ``scripts/lib/pin_drift.py`` — distance from a pin to ``current``.

Every test builds its own tmp store: the operator's real ``~/.vnx-system`` and
the ``.vnx-data`` runtime tree are never read or written.
"""

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (REPO_ROOT, REPO_ROOT / "scripts" / "lib"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from pin_drift import (
    VERDICT_CURRENT,
    VERDICT_DEAD,
    VERDICT_LAGGING,
    VERDICT_NON_RELEASE,
    classify_pin,
    current_version,
    load_store,
)


def _make_store(root: Path, dirs, current="v1.7.6") -> Path:
    """``<root>/versions/<dir>`` for each name, plus ``<root>/current``."""
    versions = root / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    for name in dirs:
        (versions / name).mkdir(exist_ok=True)
    if current is not None:
        os.symlink(f"versions/{current}", root / "current")
    return root


@pytest.fixture()
def store(tmp_path):
    """The measured 24-10 layout, plus the two non-release entries a real
    store carries: ``.vnx-data`` and ``edge``."""
    root = _make_store(tmp_path / "store", ("v1.5.0", "v1.6.0", "v1.7.5", "v1.7.6"))
    (root / "versions" / ".vnx-data").mkdir()
    (root / "versions" / "edge").mkdir()
    return root


def test_load_store_returns_only_releases_in_semver_order(store):
    versions = store / "versions"
    (versions / "nightly-build").mkdir()
    (versions / "README").write_text("not a dir\n", encoding="utf-8")

    # ``.vnx-data``, ``edge``, ``nightly-build`` and the plain file are skipped.
    assert load_store(versions) == ["1.5.0", "1.6.0", "1.7.5", "1.7.6"]


def test_load_store_dedupes_one_release_under_two_spellings(store):
    (store / "versions" / "1.7.6").mkdir()

    assert load_store(store / "versions") == ["1.5.0", "1.6.0", "1.7.5", "1.7.6"]
    # A duplicate dir must not inflate the distance of a lagging pin.
    assert classify_pin("1.7.5", store).behind == 1


@pytest.mark.parametrize("pin, behind", [("1.7.5", 1), ("v1.5.0", 3), ("1.5.0", 3)])
def test_lagging_pin_counts_releases_up_to_current(store, pin, behind):
    drift = classify_pin(pin, store)

    assert drift.verdict == VERDICT_LAGGING
    assert drift.current == "1.7.6"
    assert drift.behind == behind
    assert drift.pin == pin.lstrip("v")
    assert drift.note is None


@pytest.mark.parametrize("pin", ("1.7.6", "v1.7.6"))
def test_pin_equal_to_current_is_current(store, pin):
    drift = classify_pin(pin, store)

    assert (drift.verdict, drift.behind, drift.note) == (VERDICT_CURRENT, 0, None)


def test_pin_without_a_store_dir_is_dead(store):
    drift = classify_pin("1.0.0", store)

    assert (drift.verdict, drift.behind, drift.current) == (VERDICT_DEAD, None, "1.7.6")


@pytest.mark.parametrize("pin", ("edge", ".vnx-data", "nightly-build"))
def test_existing_but_non_release_pin_is_non_release(store, pin):
    (store / "versions" / "nightly-build").mkdir(exist_ok=True)

    drift = classify_pin(pin, store)

    assert (drift.verdict, drift.behind) == (VERDICT_NON_RELEASE, None)


def test_pin_ahead_of_current_is_current_with_a_note(store):
    (store / "versions" / "v1.8.0").mkdir()

    drift = classify_pin("1.8.0", store)

    assert (drift.verdict, drift.behind) == (VERDICT_CURRENT, 0)
    assert drift.note is not None and "ahead" in drift.note


def test_prerelease_sorts_before_its_release_and_is_one_behind(tmp_path):
    root = _make_store(tmp_path / "store", ("v1.0.0", "v1.0.0-rc9"), current="v1.0.0")

    assert load_store(root / "versions") == ["1.0.0-rc9", "1.0.0"]
    assert classify_pin("1.0.0-rc9", root)[2] == 1


@pytest.mark.parametrize("pin", ("..", "../../outside/v1.7.5"))
def test_pin_resolving_outside_the_store_is_dead(store, pin):
    (store.parent / "outside" / "v1.7.5").mkdir(parents=True)

    drift = classify_pin(pin, store)

    assert (drift.verdict, drift.behind) == (VERDICT_DEAD, None)


def test_missing_store_has_nothing_to_load_and_every_pin_is_dead(tmp_path):
    drift = classify_pin("1.7.5", tmp_path / "no-store")

    assert (drift.verdict, drift.current, drift.behind) == (VERDICT_DEAD, None, None)
    assert load_store(tmp_path / "no-store" / "versions") == []


def test_current_version_reads_the_symlink_target(store):
    assert current_version(store) == "1.7.6"


def test_current_version_is_none_unless_it_resolves_to_a_release(tmp_path):
    cases = {
        "no link": lambda root: None,
        "dangling": lambda root: os.symlink("versions/v9.9.9", root / "current"),
        "plain dir": lambda root: (root / "current").mkdir(),
        "rolling target": lambda root: os.symlink("versions/edge", root / "current"),
        "non-version target": lambda root: os.symlink("versions/nightly", root / "current"),
    }
    for label, make_current in cases.items():
        root = _make_store(tmp_path / label, ("v1.7.6", "nightly", "edge"), current=None)
        make_current(root)
        assert current_version(root) is None, label


def test_current_on_a_rolling_dir_leaves_every_distance_unknown(store):
    (store / "current").unlink()
    os.symlink("versions/edge", store / "current")

    assert current_version(store) is None
    for pin in ("1.7.5", "v1.5.0", "1.7.6", "1.0.0", "edge"):
        drift = classify_pin(pin, store)
        assert (drift.current, drift.behind) == (None, None), pin
        # Unknown is not green: a release pin is never reported as active here.
        assert drift.verdict != VERDICT_CURRENT, pin
    release_pin = classify_pin("1.7.5", store)
    assert release_pin.verdict == VERDICT_LAGGING
    assert release_pin.note is not None and "distance unknown" in release_pin.note
