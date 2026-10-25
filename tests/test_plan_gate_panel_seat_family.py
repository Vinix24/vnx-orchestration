"""gate-weight PR-C: the plan-gate panel refuses two seats from one provider family.

``load_panel_seats`` used to accept ``provider: claude`` twice and the panel then
read two "independent" votes from a single vendor. A seat's family is the wave7
registry key the constraint enforcer already assigns (ADR-036), never a second
mapping table: a new provider alias that routes to an existing vendor must land
in the same family without anyone remembering to update this check.

The folding rule the check needs on top of that key is only ``_harness``: it
names a lane, not a vendor, so ``deepseek-harness`` (registry
``deepseek_harness``) and ``litellm:deepseek`` (registry ``deepseek``) are one
family. ``glm-harness`` already resolves to the vendor key ``zai``, so it needs
no folding to meet ``litellm:zai``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
for _p in (str(_REPO / "scripts" / "lib"), str(_REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import plan_gate_panel as pgp
from dispatch_spec import Provider

_SHIPPED = _REPO / "configs" / "plan_gate_panel.yaml"


def _write_config(tmp_path: Path, seats: str) -> Path:
    cfg = tmp_path / "plan_gate_panel.yaml"
    cfg.write_text("version: 1\nseats:\n" + seats, encoding="utf-8")
    return cfg


def _two_seats(tmp_path: Path, first: str, second: str) -> Path:
    return _write_config(tmp_path, (
        f"  - {{label: a, provider: {first}, model_arg: m}}\n"
        f"  - {{label: b, provider: {second}, model_arg: m}}\n"
    ))


# ---------------------------------------------------------------------------
# The shipped roster
# ---------------------------------------------------------------------------

def test_the_shipped_config_loads_five_seats_from_five_families():
    """The shipped config still loads, and its five seats are five vendors."""
    seats = pgp.load_panel_seats(_SHIPPED)
    assert len(seats) == 5
    families = [pgp.seat_family(s["provider"]) for s in seats]
    assert len(set(families)) == 5, families
    assert "anthropic" in families


def test_default_panel_seats_one_seat_per_family():
    """The code constant the loader falls back to obeys the same invariant."""
    families = [pgp.seat_family(s["provider"]) for s in pgp.default_panel()]
    assert len(families) == len(set(families)), families


# ---------------------------------------------------------------------------
# Two seats on one vendor are refused, naming both
# ---------------------------------------------------------------------------

def test_two_claude_seats_are_refused_naming_both_labels_and_the_family(tmp_path):
    cfg = _write_config(tmp_path, (
        "  - {label: opus, provider: claude, model_arg: opus}\n"
        "  - {label: sonnet, provider: claude, model_arg: sonnet}\n"
    ))
    with pytest.raises(ValueError) as excinfo:
        pgp.load_panel_seats(cfg)
    msg = str(excinfo.value)
    assert "'opus'" in msg and "'sonnet'" in msg, msg
    assert "'anthropic'" in msg, msg
    assert "(claude, claude)" in msg, msg
    assert "each seat must come from a different provider family" in msg, msg


def test_glm_harness_and_litellm_zai_are_one_family(tmp_path):
    """Both strings route GLM models to the zai registry section (OI-1217)."""
    cfg = _two_seats(tmp_path, "glm-harness", "litellm:zai")
    with pytest.raises(ValueError) as excinfo:
        pgp.load_panel_seats(cfg)
    msg = str(excinfo.value)
    assert "'zai'" in msg, msg
    assert "(glm-harness, litellm:zai)" in msg, msg


def test_deepseek_harness_and_litellm_deepseek_are_one_family(tmp_path):
    """``_harness`` names a lane: both routes bill DeepSeek, so the trailing
    ``_harness`` of the registry key is folded away before comparing."""
    cfg = _two_seats(tmp_path, "deepseek-harness", "litellm:deepseek")
    with pytest.raises(ValueError) as excinfo:
        pgp.load_panel_seats(cfg)
    msg = str(excinfo.value)
    assert "'deepseek'" in msg, msg
    assert "(deepseek-harness, litellm:deepseek)" in msg, msg


def test_a_shared_family_is_refused_on_the_fallback_path_too(tmp_path, monkeypatch):
    """``default_panel()`` holds the same rule: an edited code constant fails loud
    instead of silently seating one vendor twice when the config is absent."""
    monkeypatch.setattr(pgp, "_DEFAULT_PANEL_SEATS", [
        {"label": "opus", "provider": "claude", "model_arg": "opus"},
        {"label": "sonnet", "provider": "claude", "model_arg": "sonnet"},
    ])
    with pytest.raises(ValueError, match="share provider family 'anthropic'"):
        pgp.default_panel()


# ---------------------------------------------------------------------------
# Order and fail-closed edges
# ---------------------------------------------------------------------------

def test_a_malformed_seat_keeps_its_own_error_before_the_family_check(tmp_path):
    """The family check runs AFTER ``_validate_seat``: an unknown provider is
    reported as such, not as a family clash with the second seat."""
    cfg = _write_config(tmp_path, (
        "  - {label: broke, provider: not-a-real-provider, model_arg: m}\n"
        "  - {label: sonnet, provider: claude, model_arg: sonnet}\n"
    ))
    with pytest.raises(ValueError, match="unknown provider"):
        pgp.load_panel_seats(cfg)


def test_a_model_name_is_not_a_seat_provider(tmp_path):
    """The ``Provider`` enum's claude member is the only Anthropic provider
    string; ``opus``/``sonnet`` are ``model_arg`` values and are refused as
    providers rather than admitted as a second anthropic seat."""
    cfg = _two_seats(tmp_path, "claude", "sonnet")
    with pytest.raises(ValueError, match="unknown provider 'sonnet'"):
        pgp.load_panel_seats(cfg)


def test_seat_family_refuses_a_provider_it_cannot_place():
    """An unreadable family is never a pass: it raises instead of returning a
    placeholder that would compare equal to another unreadable seat."""
    with pytest.raises(ValueError, match="cannot resolve the provider family"):
        pgp.seat_family("")


@pytest.mark.parametrize("provider", [p.value for p in Provider])
def test_every_provider_enum_member_has_a_non_empty_family(provider):
    family = pgp.seat_family(provider)
    assert family, f"{provider} resolved to an empty family"
    assert family == family.strip()
