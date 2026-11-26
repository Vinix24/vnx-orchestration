"""OF-3: explicit CI waiver and one pinned snapshot on the actual merge receipt."""
import subprocess

import pytest

from test_pr_merge_door_permit import GO, PR_DATA, door, _run, _merged, _refusal
from test_optional_features import _yaml, GATES
from review_stack_support import isolate_config_registry
import config_runtime
import gate_profile
import optional_features as of
import pr_merge

MAIN = "a" * 40
REAL_FEATURES = getattr(pr_merge, "_door_features", None)


@pytest.fixture
def features(door, monkeypatch, tmp_path):
    isolation = isolate_config_registry(monkeypatch)
    next(isolation)
    monkeypatch.setattr(config_runtime, "autowire", lambda *a, **k: True)
    monkeypatch.setattr(config_runtime, "strict_resolver", lambda: None)
    (tmp_path / "no-dials").write_text("checks: {}\n")
    monkeypatch.setattr(gate_profile, "enforcement_config_path", lambda: tmp_path / "no-dials")
    monkeypatch.setattr(pr_merge, "active_store", lambda: (door.ledger.parent, "acme"))
    monkeypatch.setattr(pr_merge, "_gh", lambda *a, **k: subprocess.CompletedProcess([], 0, MAIN, ""))
    monkeypatch.setattr(pr_merge, "_query_pr", lambda pr: dict(PR_DATA))
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "0")
    monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", "off")
    door.yaml = _yaml(GATES)
    if REAL_FEATURES is not None:
        monkeypatch.setattr(pr_merge, "_door_features", REAL_FEATURES)
    yield door
    next(isolation, None)


@pytest.mark.parametrize("required", [False, True, "dial", "checks", "stack"])
def test_ci_requirement_and_receipt(features, monkeypatch, tmp_path, required):
    calls = []
    monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "1" if required is True else "0")
    if required == "dial":
        (tmp_path / "no-dials").write_text("checks:\n  ci_green_required:\n    level: 2\n")
    if required == "checks":
        features.yaml = _yaml([*GATES, "CI"])
    if required == "stack":
        monkeypatch.setenv("VNX_DEFAULT_REVIEW_STACK", "codex_gate,ci_gate")
    def ci(pr, **kwargs):
        assert required, "disabled CI must never call _run_ci_gate"
        calls.append((pr, kwargs))
        assert pr_merge._project_ci_workflow(None) == (None, "")
        return dict(GO), dict(PR_DATA)
    monkeypatch.setattr(pr_merge, "_run_ci_gate", ci)
    assert _run() == 0
    receipt = _merged(features)
    record = receipt["preflight_gates"][0]
    assert calls == ([(PR_DATA["number"], {"override_reason": None})] if required else [])
    assert receipt["waived_guarantees"] == record["waived_guarantees"]
    assert receipt["optional_features"] == record["optional_features"]
    assert {r["feature"] for r in receipt["waived_guarantees"]} == ({"plan_gate"} if required else {"ci", "plan_gate"})
    if not required:
        assert record["status"] == "waived" and record["reason_code"] == "ci_not_required"
        assert "VNX_CI_GATE_REQUIRED" in record["message"] and record["head_sha"] == PR_DATA["headRefOid"]


@pytest.mark.parametrize("mode, drift, verified", [("warn", True, False), ("enforce", False, True), ("off", True, False)])
def test_protection_measurement_on_receipt(features, monkeypatch, mode, drift, verified):
    features.yaml = _yaml(GATES, enforcement=mode)
    live = pr_merge.protection_to_normalized_dict(pr_merge.parse_protection_config(features.yaml))
    live["allow_force_pushes"] = drift
    monkeypatch.setattr(pr_merge, "fetch_live_protection", lambda *a, **k: live)
    assert _run() == 0
    receipt = _merged(features)
    protection = receipt["optional_features"]["branch_protection"]
    assert protection["server_verified"] is verified
    assert protection["source"] == f".vnx/branch_protection.yaml@{MAIN}"
    if mode == "warn":
        assert protection["drift_fields"] == ["allow_force_pushes"]
        assert any(r["key"] == "enforcement" and r["value"] == "warn" for r in receipt["waived_guarantees"])


@pytest.mark.parametrize("key, value, code", [
    ("VNX_CI_GATE_REQUIRED", "bogus", "ci_requirement_unreadable"),
    ("VNX_PLAN_GATE_ENFORCE", "bogus", "plan_gate_mode_unknown"),
    ("VNX_PLAN_GATE_ENFORCE", "", "plan_gate_mode_unknown"),
])
def test_unknown_state_refuses(features, monkeypatch, key, value, code):
    monkeypatch.setenv(key, value)
    assert _run() == 1
    assert code in _refusal(features)["preflight_gates"][0]["message"]
    assert features.receipts("pr_merged") == []


def test_unreadable_store_refuses(features, monkeypatch):
    def unreadable(*args):
        raise OSError("store unreadable")
    monkeypatch.setattr(config_runtime, "strict_resolver", lambda: (unreadable, "acme"))
    assert _run() == 1
    assert "ci_requirement_unreadable" in _refusal(features)["preflight_gates"][0]["message"]


@pytest.mark.parametrize("value", [None, "", "not-a-sha", 0, "missing"])
def test_ci_off_still_requires_head(features, monkeypatch, value):
    data = dict(PR_DATA, headRefOid=value)
    if value == "missing":
        del data["headRefOid"]
    monkeypatch.setattr(pr_merge, "_query_pr", lambda pr: data)
    assert _run() == 1
    assert "head" in _refusal(features)["preflight_gates"][0]["message"].lower()


def test_pinned_read_reused_and_snapshot_taken_once(features, monkeypatch):
    reads, snapshots = [], []
    take = of.snapshot
    monkeypatch.setattr(of, "snapshot", lambda *a, **k: snapshots.append(k) or take(*a, **k))
    def fetch(root, ref, path):
        reads.append((ref, path))
        return pr_merge.YamlFetchResult(features.yaml, False, None)
    monkeypatch.setattr(pr_merge, "fetch_yaml_from_ref", fetch)
    assert _run() == 0
    assert len(snapshots) == 1 and snapshots[0]["protection_source"].endswith("@" + MAIN)
    assert reads == [(MAIN, ".vnx/branch_protection.yaml"), (PR_DATA["headRefOid"], ".vnx/branch_protection.yaml")]


@pytest.mark.parametrize("repermit", [False, True])
def test_of8_summary_keeps_preflight_snapshot_after_config_change(features, monkeypatch, repermit):
    import forge_gate_publisher as publisher
    publish, snapshots, take = publisher.publish_door_permit, [], of.snapshot
    monkeypatch.setattr(of, "snapshot", lambda *a, **k: snapshots.append(k) or take(*a, **k))
    if repermit:
        features.runs.append({"id": 900, "name": "vnx-gate/door", "app": {"id": 4869217},
                              "conclusion": "failure"})
    def changed(evidence, **kwargs):
        monkeypatch.setenv("VNX_CI_GATE_REQUIRED", "1")
        monkeypatch.setenv("VNX_PLAN_GATE_ENFORCE", "required")
        return publish(evidence, **kwargs)
    monkeypatch.setattr(publisher, "publish_door_permit", changed)
    assert _run() == 0
    rows = _merged(features)["preflight_gates"][0]["waived_guarantees"]
    body = features.of("PATCH" if repermit else "POST")[0][2]
    expected = "\n".join(f"- {r['feature']}: {r['key']}={r['value']} (source: {r['source']}); "
                         f"waived: {r['guarantee']}" for r in rows)
    assert body["output"]["summary"].split("Waived guarantees:\n")[1] == expected
    assert any(r["feature"] == "ci" for r in rows) and len(snapshots) == 1


@pytest.mark.parametrize("failure", [OSError("offline"), subprocess.TimeoutExpired("gh", 30), "", "bogus"])
def test_main_unreadable_refuses(features, monkeypatch, failure):
    def gh(*a, **k):
        if isinstance(failure, Exception):
            raise failure
        return subprocess.CompletedProcess([], 0, failure, "")
    monkeypatch.setattr(pr_merge, "_gh", gh)
    assert _run() == 1
    assert "branch_protection_unreadable" in _refusal(features)["preflight_gates"][0]["message"]


@pytest.mark.parametrize("text, error", [(None, None), (None, "denied"), ("", None), ("enforcement: bogus", None)])
def test_protection_unreadable_refuses(features, monkeypatch, text, error):
    monkeypatch.setattr(pr_merge, "fetch_yaml_from_ref", lambda *a: pr_merge.YamlFetchResult(text, False, error))
    assert _run() == 1
    assert "branch_protection_unreadable" in _refusal(features)["preflight_gates"][0]["message"]


@pytest.mark.parametrize("value", [None, "false", "unknown", 0])
def test_non_boolean_requirement_never_waives(features, monkeypatch, value):
    monkeypatch.setattr(pr_merge, "_door_features", lambda: {"ci": of.FeatureState("ci", value, "", "test")}, raising=False)
    assert _run() == 1
    assert "ci_requirement_unreadable" in _refusal(features)["preflight_gates"][0]["message"]


@pytest.mark.parametrize("missing_file", [True, False])
def test_absent_protection_is_named_and_unverified(features, monkeypatch, missing_file):
    features.yaml = _yaml(GATES, enforcement="warn")
    if missing_file:
        monkeypatch.setattr(pr_merge, "fetch_yaml_from_ref", lambda *a: pr_merge.YamlFetchResult(None, True, None))
    else:
        def unprotected(*a, **k):
            raise pr_merge.BranchNotProtectedError("main has no protection")
        monkeypatch.setattr(pr_merge, "fetch_live_protection", unprotected)
    assert _run() == 0
    receipt = _merged(features)
    state = receipt["optional_features"]["branch_protection"]
    assert state["server_verified"] is False
    assert state.get("source") == "file_absent" if missing_file else state["drift_fields"] == ["branch_protection"]
    assert any(row["feature"] == "branch_protection" for row in receipt["waived_guarantees"])


def test_base_ref_refusal_preserves_snapshot(features, monkeypatch):
    monkeypatch.setattr(pr_merge, "_check_dispatch_base_ref", lambda *a, **k: dict(verdict="NO-GO", message="wrong base"))
    assert _run() == 1
    receipt = _refusal(features)
    assert receipt["waived_guarantees"] == receipt["preflight_gates"][0]["waived_guarantees"]
    assert receipt["optional_features"]["ci"]["on"] is False
