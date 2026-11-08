"""Gate profile P6 (track gate-profiel-per-project): the profile and its source leave a trail.

The seat record (``SeatEvaluation.to_record``), the request record (``ReviewSeats.to_record``), the
``review_gate_result`` receipt (``gate_recorder._outcome_receipt_fields``) and the ``plan_gate_pass``
evidence carry ``profile``, ``profile_source`` and ``profile_error`` (B8, B13, B15). ``None`` or an
absent key is "unknown"; it is never ``standard``. Uses ``tmp_path`` and ``monkeypatch`` only.
"""
from __future__ import annotations

import json

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)
from review_stack_support import isolate_config_registry

import config_registry
import gate_recorder
import plan_gate_evidence
import planning_cli
import review_risk_class as rrc
import review_seat_evaluation as rse
from review_seats import resolve_review_seats

HEAD = "d" * 40
PR_ID = "2600"
KEY = "VNX_GOVERNANCE_PROFILE"


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


@pytest.fixture
def project_config(monkeypatch):
    rows = {}
    config_registry.set_db_resolver(lambda _pid, key: rows.get(key))
    monkeypatch.setattr("config_runtime.autowire", lambda *a, **k: True)
    return rows


def _risk(*paths):
    reading = rrc.DiffReading(tuple(paths), merge_base="a" * 40, base_ref="origin/main")
    return rrc.derive_risk_class(reading, head_sha=HEAD)


def _seat_record(tmp_path, risk=None):
    required = rse.required_seats(tmp_path / "requests", PR_ID, HEAD, risk=risk)
    scope = rse.SeatScope(pr_id=PR_ID, results_dir=tmp_path / "results", head_sha=HEAD)
    return rse.SeatEvaluation(scope=scope, required=required, fills=(), blocking=(), hints={}).to_record()


def _receipt(payload):
    return gate_recorder._outcome_receipt_fields(payload, gate="codex_gate", pr_ref="2600", extra_fields=None)[1]


def _payload(**extra):
    return dict({"gate": "codex_gate", "status": "completed", "pr_id": PR_ID, "commit_sha": HEAD}, **extra)


class TestSeatRecord:
    def test_heavy_from_a_project_config_row(self, tmp_path, project_config):
        project_config[KEY] = "heavy"
        record = _seat_record(tmp_path)
        assert (record["profile"], record["profile_source"], record["profile_error"]) == ("heavy", "project_config", None)
        assert record["required_error"] is None
        assert record["stack_source"] == "profile"
        assert "profile_raw" not in record

    def test_unset_is_standard_by_default(self, tmp_path):
        record = _seat_record(tmp_path)
        assert (record["profile"], record["profile_source"], record["profile_error"]) == ("standard", "default", None)

    def test_a_refused_value_never_reads_as_standard(self, tmp_path, monkeypatch):
        monkeypatch.setenv(KEY, "zwaar")
        record = _seat_record(tmp_path)
        assert record["profile"] is None
        assert record["profile_raw"] == "zwaar" and record["profile_source"] == "env"
        assert "zwaar" in record["profile_error"] and record["required_error"] == record["profile_error"]
        assert record["verdict"] == "NO-GO"

    def test_a_light_refusal_on_a_signal_path_is_the_required_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv(KEY, "light")
        record = _seat_record(tmp_path, risk=_risk(".github/workflows/ci.yml"))
        assert record["profile"] == "light" and record["profile_error"] is None
        assert "gate profile light" in record["required_error"] and record["seats"] == []

    def test_a_recorded_stack_keeps_the_profile_of_its_request(self, tmp_path, monkeypatch):
        monkeypatch.setenv(KEY, "heavy")
        request = {
            "gate": "codex_gate", "pr_id": PR_ID, "pr_number": int(PR_ID), "commit_sha": HEAD,
            "requested_at": "2026-11-08T08:00:00Z",
            "review_seats": dict(resolve_review_seats().to_record(), source=rse.SOURCE_RECORDED),
        }
        monkeypatch.setenv(KEY, "light")  # changed after the request: the record must not be rewritten
        (tmp_path / "requests").mkdir()
        (tmp_path / "requests" / f"pr-{PR_ID}-codex_gate.json").write_text(json.dumps(request), encoding="utf-8")
        record = _seat_record(tmp_path)
        assert (record["profile"], record["profile_source"], record["stack_source"]) == ("heavy", "env", "profile")

    def test_a_recorded_stack_from_before_p6_reads_as_unknown(self, tmp_path):
        (tmp_path / "requests").mkdir()
        (tmp_path / "requests" / f"pr-{PR_ID}-codex_gate.json").write_text(json.dumps({
            "gate": "codex_gate", "pr_id": PR_ID, "pr_number": int(PR_ID), "commit_sha": HEAD,
            "requested_at": "2026-11-08T08:00:00Z",
            "review_seats": {"source": rse.SOURCE_RECORDED, "seats": ["codex_gate", "kimi_gate"], "value": "codex_gate,kimi_gate"},
        }), encoding="utf-8")
        record = _seat_record(tmp_path)
        assert (record["profile"], record["profile_source"], record["profile_error"]) == (None, None, None)

    def test_a_named_seat_list_has_no_profile(self, tmp_path):
        required = rse.required_seats(tmp_path, PR_ID, HEAD, seats=["codex_gate"])
        assert (required.profile, required.profile_source, required.profile_error) == (None, None, None)

    def test_an_unreadable_stack_is_a_required_error_without_a_profile_claim(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_OVERRIDE_DEFAULT_REVIEW_STACK", "no_such_gate")
        record = _seat_record(tmp_path)
        assert "no_such_gate" in record["required_error"]
        assert record["profile"] is None and record["profile_error"] is None


class TestRequestRecordCarriesTheProfile:
    def test_config_request(self, project_config):
        project_config[KEY] = "heavy"
        record = resolve_review_seats().to_record()
        assert (record["profile"], record["profile_source"], record["profile_error"]) == ("heavy", "project_config", None)
        assert record["stack_source"] == "profile"

    def test_explicit_stack_with_a_refused_profile_records_the_error(self, monkeypatch):
        monkeypatch.setenv(KEY, "Heavy")
        record = resolve_review_seats(explicit_stack=["codex_gate"]).to_record()
        assert record["profile"] is None and "Heavy" in record["profile_error"]


class TestResultReceipt:
    # Helper only: these hand _outcome_receipt_fields a payload that already carries the profile keys,
    # which the production result writers do not. The proof of B15 on the production path is
    # tests/test_gate_profile_result_path.py.
    def test_the_request_record_profile_lands_on_the_receipt(self):
        fields = _receipt(_payload(profile="heavy", profile_source="project_config", profile_error=None))
        assert fields["profile"] == "heavy" and fields["profile_source"] == "project_config"
        assert "profile_error" not in fields

    def test_the_seat_record_inside_the_payload_is_read_too(self, project_config):
        project_config[KEY] = "heavy"
        fields = _receipt(_payload(review_seats=resolve_review_seats().to_record()))
        assert (fields["profile"], fields["profile_source"]) == ("heavy", "project_config")

    def test_a_refused_profile_stamps_the_error_and_no_profile(self):
        fields = _receipt(_payload(profile=None, profile_source="env", profile_error="VNX_GOVERNANCE_PROFILE='zwaar' is refused"))
        assert "profile" not in fields and fields["profile_source"] == "env" and "zwaar" in fields["profile_error"]

    @pytest.mark.parametrize("bad", [None, "", "   ", 7, ["heavy"], {}])
    def test_a_missing_or_malformed_value_writes_no_key(self, bad):
        fields = _receipt(_payload(profile=bad, profile_source=bad, profile_error=bad))
        assert not {"profile", "profile_source", "profile_error"} & set(fields)

    def test_a_payload_without_the_keys_gives_a_receipt_without_them(self):
        fields = _receipt(_payload())
        assert not {"profile", "profile_source", "profile_error"} & set(fields)
        assert fields["gate"] == "codex_gate" and fields["commit_sha"] == HEAD


class TestPlanGateEvidence:
    def test_the_label_is_the_one_the_trace_carries(self):
        composition = type("C", (), {
            "seats": [{"label": "a"}], "chosen_by": "default", "default_label": "a", "second_seat": None,
            "escalation": None, "profile": "heavy (project_config)",
        })()
        switched = type("S", (), {"left_out": [], "note": lambda self: ""})()
        trace = planning_cli._panel_gov_trace("light", composition, switched)
        assert plan_gate_evidence.profile_label_from_trace(trace) == "heavy (project_config)"

    @pytest.mark.parametrize("trace", ["", None, "weight=light -> 1 seat(s)", "profile", "xprofile=heavy (env)"])
    def test_a_trace_without_the_part_gives_no_label(self, trace):
        assert plan_gate_evidence.profile_label_from_trace(trace) == ""

    def test_the_record_carries_gate_profile_next_to_the_seats(self, tmp_path):
        record = plan_gate_evidence.emit_plan_gate_pass(
            repo_root=tmp_path, track_id="t", project_id="p", resolver="run", timestamp="2026-11-08T08:00:00Z",
            seats=2, gov_trace="weight=light -> 2 seat(s) by default (a, b); profile=heavy (project_config)",
        )
        assert record["seats"] == 2 and record["gate_profile"] == "heavy (project_config)"

    def test_no_profile_in_the_trace_writes_no_gate_profile(self, tmp_path):
        record = plan_gate_evidence.emit_plan_gate_pass(
            repo_root=tmp_path, track_id="t", project_id="p", resolver="run", timestamp="2026-11-08T08:00:00Z",
            seats=1, gov_trace="weight=light -> 1 seat(s) by default (a)",
        )
        assert "gate_profile" not in record
