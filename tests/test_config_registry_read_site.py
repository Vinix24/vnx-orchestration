"""OI-2652: a registry key that production code reads through the resolver is ``read_site_wired``.

``VNX_GOVERNANCE_PROFILE`` stood at ``read_site_wired=False`` while ``gate_profile.resolve()``
read it since P1. The flag tells the cockpit that something reads the key, so a false
``False`` hides a live setting. This guard reads the production sources for
``resolve_with_source(<KEY>`` / ``_resolve(<KEY>`` (directly or through a module constant) and
fails for a key that has such a read site while carrying ``read_site_wired=False``.
"""
from __future__ import annotations

import re
from pathlib import Path

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)

import config_registry
import gate_profile

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_RESOLVER_CALL = re.compile(r"\b(?:resolve_with_source|_resolve)\(\s*([A-Za-z_][A-Za-z0-9_.]*|\"VNX_[A-Z0-9_]+\")")


def _constants(text: str) -> dict:
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)\s*=\s*\"(VNX_[A-Z0-9_]+)\"", text, re.MULTILINE))


def _keys_read_through_the_resolver() -> set:
    keys = set()
    for path in _LIB.glob("*.py"):
        if path.name == "config_registry.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        constants = _constants(text)
        for arg in _RESOLVER_CALL.findall(text):
            name = arg.split(".")[-1].strip('"')
            keys.add(name if name.startswith("VNX_") else constants.get(name, ""))
    return {key for key in keys if key}


def test_the_profile_key_is_read_site_wired():
    assert config_registry.CONFIG_REGISTRY[gate_profile.PROFILE_KEY].read_site_wired is True


def test_the_sweep_finds_the_profile_key_read_site():
    assert gate_profile.PROFILE_KEY in _keys_read_through_the_resolver(), (
        "the instrument is blind: gate_profile.resolve reads PROFILE_KEY through resolve_with_source"
    )


def test_no_key_with_a_production_read_site_carries_read_site_wired_false():
    registry = config_registry.CONFIG_REGISTRY
    unwired = sorted(
        key for key in _keys_read_through_the_resolver() if key in registry and not registry[key].read_site_wired
    )
    assert unwired == [], f"read through the resolver in production code but read_site_wired=False: {unwired}"


def test_flipping_the_profile_key_keeps_one_canonical_flag_per_subsystem():
    canonical = config_registry.canonical_flags()
    assert canonical["governance-enforcement-stack"] == "VNX_CI_GATE_REQUIRED"
