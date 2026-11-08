"""Gate profile P5 (track gate-profiel-per-project): ``light`` and its refusal on signal paths.

``light`` gives one review seat. Under it, a head on a security, schema, irreversible,
outside-repo or store-resolution path is REFUSED (``RequiredSeats.error`` names the profile
and the path); a head on only a gate-merge path or a reversible path gets one seat; an
explicit two-seat stack wins. The path kinds are classified by the real
``review_risk_class.derive_risk_class`` (``smart_router`` tables), not by a copy.
"""
from __future__ import annotations

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
from review_stack_support import isolate_config_registry

import config_registry
import review_risk_class as rrc
import review_seat_evaluation as rse
import review_seats
from gate_profile import GateProfileConfigError
from review_seats import resolve_review_seats

HEAD = "c" * 40
STANDARD_SEATS = ("codex_gate", "kimi_gate")


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


def _risk(*paths, irreversible=False, caller=None):
    reading = rrc.DiffReading(tuple(paths), merge_base="a" * 40, base_ref="origin/main")
    spec = rrc.SpecFlag(True, "spec") if irreversible else rrc.SpecFlag(False)
    return rrc.derive_risk_class(reading, caller_class=caller, spec=spec, head_sha=HEAD)


def _required(tmp_path, risk, **kwargs):
    return rse.required_seats(tmp_path, "1", HEAD, risk=risk, **kwargs)


@pytest.fixture
def light(monkeypatch):
    monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "light")


REFUSED = {
    "security": (".github/workflows/ci.yml",),
    "schema": ("schemas/example.sql",),
    "outside-repo": ("../outside/file.py",),
    "store-resolution": ("scripts/lib/vnx_paths.py",),
}


class TestLightOnAReversiblePath:
    def test_light_gives_one_seat(self, light, tmp_path):
        required = _required(tmp_path, _risk("docs/readme.md"))
        assert required.seats == ("codex_gate",) and required.error == ""
        assert required.stack == ("codex_gate",), "the stack itself is one seat, not two narrowed to one"

    def test_light_resolves_a_one_seat_stack(self, light):
        assert resolve_review_seats().seats == ("codex_gate",)

    def test_light_gives_one_seat_on_a_gate_merge_only_path(self, light, tmp_path):
        risk = _risk("scripts/lib/review_seats.py")
        assert risk.risk_class == "high" and [c for _p, c in risk.deciding] == ["gate-merge"]
        required = _required(tmp_path, risk)
        assert required.seats == ("codex_gate",) and required.error == ""

    def test_gate_merge_only_over_several_files_is_still_one_seat(self, light, tmp_path):
        risk = _risk("scripts/lib/review_seats.py", "scripts/pr_merge.py", "docs/a.md")
        assert _required(tmp_path, risk).seats == ("codex_gate",)


class TestLightRefusesOnASignalPath:
    @pytest.mark.parametrize("kind", sorted(REFUSED))
    def test_each_path_kind_is_refused_with_profile_and_path(self, light, tmp_path, kind):
        (path,) = REFUSED[kind]
        risk = _risk(path)
        assert [c for _p, c in risk.deciding] == [kind], "the classification is the router's own"
        required = _required(tmp_path, risk)
        assert required.seats == () and required.ok_to_count is False if hasattr(required, "ok_to_count") else required.seats == ()
        assert "light" in required.error and kind in required.error
        assert path.lstrip("./") in required.error or path in required.error

    def test_an_irreversible_spec_on_a_reversible_path_is_refused(self, light, tmp_path):
        risk = _risk("docs/readme.md", irreversible=True)
        required = _required(tmp_path, risk)
        assert required.seats == () and "light" in required.error and "irreversible" in required.error

    def test_gate_merge_plus_a_signal_path_is_not_gate_merge_only(self, light, tmp_path):
        risk = _risk("scripts/lib/review_seats.py", ".github/workflows/ci.yml")
        assert "light" in _required(tmp_path, risk).error

    def test_gate_merge_with_an_irreversible_spec_is_refused(self, light, tmp_path):
        risk = _risk("scripts/lib/review_seats.py", irreversible=True)
        assert "light" in _required(tmp_path, risk).error

    @pytest.mark.parametrize("risk", [None, "critical", "high", "", "weird", 7])
    def test_an_unknown_or_missing_class_is_refused(self, light, tmp_path, risk):
        required = _required(tmp_path, risk)
        assert required.seats == () and "light" in required.error

    def test_an_unreadable_diff_is_refused(self, light, tmp_path):
        reading = rrc.DiffReading(None, error="no merge base")
        risk = rrc.derive_risk_class(reading, head_sha=HEAD)
        assert "light" in _required(tmp_path, risk).error

    def test_a_refusal_is_a_refusal_not_a_seat_count(self, light, tmp_path):
        required = _required(tmp_path, _risk(".github/workflows/ci.yml"))
        assert required.seats == () and required.error and required.rule == rse.RISK_SEAT_RULE


class TestTheFloorNeverWeakensOtherProfiles:
    @pytest.mark.parametrize("profile", [None, "standard", "heavy", "  standard  "])
    def test_standard_unset_and_heavy_keep_every_seat_on_a_signal_path(self, monkeypatch, tmp_path, profile):
        if profile is not None:
            monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", profile)
        required = _required(tmp_path, _risk(".github/workflows/ci.yml"))
        assert required.error == "" and required.seats[:2] == STANDARD_SEATS

    @pytest.mark.parametrize("profile", [None, "standard"])
    def test_standard_and_unset_equal_today_on_a_reversible_path(self, monkeypatch, tmp_path, profile):
        if profile is not None:
            monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", profile)
        assert _required(tmp_path, _risk("docs/readme.md")).seats == ("codex_gate",)
        assert resolve_review_seats().seats == STANDARD_SEATS

    def test_heavy_stack_is_unchanged_from_p1(self, monkeypatch):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "heavy")
        assert resolve_review_seats().seats == ("codex_gate", "kimi_gate", "claude_gate")

    def test_an_invalid_profile_value_still_fails_closed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "Light")
        required = _required(tmp_path, _risk("docs/readme.md"))
        assert required.seats == () and "Light" in required.error


class TestAnExplicitStackWins:
    def test_a_named_two_seat_list_does_not_refuse(self, light, tmp_path):
        required = _required(tmp_path, _risk(".github/workflows/ci.yml"), seats=list(STANDARD_SEATS))
        assert required.seats == STANDARD_SEATS and required.error == ""

    def test_an_explicit_two_seat_request_stack_resolves_two_seats(self, light):
        resolved = resolve_review_seats(explicit_stack=list(STANDARD_SEATS))
        assert resolved.seats == STANDARD_SEATS and resolved.profile == "light"

    def test_an_operator_override_stack_with_two_seats_does_not_refuse(self, light, monkeypatch, tmp_path):
        monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", "codex_gate,kimi_gate")
        required = _required(tmp_path, _risk(".github/workflows/ci.yml"))
        assert required.error == "" and required.seats == STANDARD_SEATS

    def test_an_operator_one_seat_stack_is_not_light_s_to_refuse(self, light, monkeypatch, tmp_path):
        monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", "codex_gate")
        assert review_seats.light_profile_governs_stack() is None
        assert _required(tmp_path, _risk(".github/workflows/ci.yml")).error == ""


class TestTheGovernsHelper:
    def test_it_returns_the_resolution_for_light(self, light):
        assert review_seats.light_profile_governs_stack().profile == "light"

    @pytest.mark.parametrize("profile", ["standard", "heavy"])
    def test_it_returns_none_for_the_other_profiles(self, monkeypatch, profile):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", profile)
        assert review_seats.light_profile_governs_stack() is None

    def test_it_raises_for_an_illegal_value(self, monkeypatch):
        monkeypatch.setenv("VNX_GOVERNANCE_PROFILE", "zwaar")
        with pytest.raises(GateProfileConfigError):
            review_seats.light_profile_governs_stack()

    def test_the_profile_table_holds_the_one_seat_stack(self):
        import gate_profile

        assert gate_profile.PROFILE_TABLE["light"]["review_stack"] == "codex_gate"
        assert config_registry.CONFIG_REGISTRY["VNX_DEFAULT_REVIEW_STACK"].default == ",".join(STANDARD_SEATS)
