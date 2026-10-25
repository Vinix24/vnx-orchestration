"""The refactor-safe skill ships as a registry-listed, byte-identical pair.

``skills/`` is canonical; ``.claude/skills/`` carries the copy the fleet loads.
A divergence between the two means one tree teaches a different refactor
contract than the other. The registry is what ``vnx skills sync`` and the
consumer contract test read, so an unregistered shipped skill is invisible.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CANONICAL = ROOT / "skills" / "refactor-safe" / "SKILL.md"
FLEET_COPY = ROOT / ".claude" / "skills" / "refactor-safe" / "SKILL.md"
REGISTRY = ROOT / "skills" / "skills.yaml"

# The three rules the body must name, as plain substrings and what each teaches.
CONTENT_PINS = (
    ("characterization before", "characterization before any move"),
    ("feature flag", "the flag rule for a behaviour switch"),
    ("patch the consumer namespace", "the import-binding rule"),
)


def _body() -> str:
    return CANONICAL.read_text(encoding="utf-8")


def test_canonical_skill_ships() -> None:
    assert CANONICAL.is_file(), f"missing canonical skill: {CANONICAL.relative_to(ROOT)}"
    assert CANONICAL.stat().st_size > 0, "canonical skill is empty"


def test_fleet_copy_is_byte_identical() -> None:
    assert FLEET_COPY.is_file(), f"missing fleet copy: {FLEET_COPY.relative_to(ROOT)}"
    assert FLEET_COPY.read_bytes() == CANONICAL.read_bytes(), (
        "skills/refactor-safe/SKILL.md and .claude/skills/refactor-safe/SKILL.md "
        "differ; skills/ is canonical, re-copy it instead of editing the fleet copy"
    )


def test_registry_lists_refactor_safe() -> None:
    registry = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    entry = (registry.get("skills") or {}).get("refactor-safe")
    assert entry is not None, "skills.yaml does not register refactor-safe"
    assert entry.get("file") == "refactor-safe/SKILL.md"
    assert (ROOT / "skills" / entry["file"]).is_file(), "registry points at a missing file"


@pytest.mark.parametrize("pin,teaches", CONTENT_PINS, ids=[pin for pin, _ in CONTENT_PINS])
def test_body_names_the_required_rule(pin: str, teaches: str) -> None:
    assert pin in _body().lower(), f"skill body does not name {teaches!r} (missing {pin!r})"
