#!/usr/bin/env python3
"""Tests for scripts/lib/forge_protection_drift.py (Golf B, B1).

Covers: schema validation of branch_protection.yaml (including the
vnx-gate/* app_id guard), the compare()/is_weakening() diff engine, and the
gh-backed fetchers (fetch_live_protection, fetch_yaml_from_ref) with the
``gh`` subprocess call mocked — the comparator itself is NEVER mocked, per
dispatch instructions.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import forge_protection_drift as fpd


# ---------------------------------------------------------------------------
# Fixture config helpers
# ---------------------------------------------------------------------------

def _base_config_dict(**overrides: Any) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "branch": "main",
        "required_status_checks": {
            "strict": False,
            "checks": [
                {"context": "Profile A", "app_id": 15368},
                {"context": "Profile B", "app_id": 15368},
            ],
        },
        "pending_checks": [],
        "required_pull_request_reviews": {
            "required_approving_review_count": 0,
            "dismiss_stale_reviews": False,
            "require_code_owner_reviews": False,
            "require_last_push_approval": False,
        },
        "enforce_admins": True,
        "required_signatures": False,
        "required_linear_history": False,
        "allow_force_pushes": False,
        "allow_deletions": False,
        "allow_fork_syncing": False,
        "block_creations": False,
        "lock_branch": False,
        "required_conversation_resolution": False,
        "restrictions": None,
        "repo": {"allow_auto_merge": False},
        "rulesets": [],
    }
    doc.update(overrides)
    return doc


def _yaml_text(**overrides: Any) -> str:
    return yaml.safe_dump(_base_config_dict(**overrides))


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

class TestParseProtectionConfig:
    def test_valid_config_parses(self):
        config = fpd.parse_protection_config(_yaml_text())
        assert config.branch == "main"
        assert len(config.checks) == 2
        assert config.checks[0].context == "Profile A"
        assert config.checks[0].app_id == 15368
        assert config.enforce_admins is True

    def test_the_real_shipped_yaml_parses_and_has_fourteen_checks(self):
        """The actual scripts/forge/branch_protection.yaml this dispatch ships."""
        path = VNX_ROOT / "scripts" / "forge" / "branch_protection.yaml"
        config = fpd.load_protection_config(path)
        assert len(config.checks) == 14
        assert all(c.app_id == 15368 for c in config.checks)
        assert config.branch == "main"
        assert config.enforce_admins is True
        assert config.allow_auto_merge is False
        assert config.rulesets == ()

    def test_unknown_top_level_field_is_rejected(self):
        doc = _base_config_dict()
        doc["mystery_field"] = True
        with pytest.raises(fpd.ProtectionConfigError, match="onbekende velden"):
            fpd.parse_protection_config(yaml.safe_dump(doc))

    def test_missing_top_level_field_is_rejected(self):
        doc = _base_config_dict()
        del doc["enforce_admins"]
        with pytest.raises(fpd.ProtectionConfigError, match="verplichte velden"):
            fpd.parse_protection_config(yaml.safe_dump(doc))

    def test_missing_required_status_checks_field_is_rejected(self):
        doc = _base_config_dict()
        del doc["required_status_checks"]["strict"]
        with pytest.raises(fpd.ProtectionConfigError, match="required_status_checks"):
            fpd.parse_protection_config(yaml.safe_dump(doc))

    def test_unknown_check_entry_field_is_rejected(self):
        doc = _base_config_dict()
        doc["required_status_checks"]["checks"][0]["extra"] = "nope"
        with pytest.raises(fpd.ProtectionConfigError, match="onbekende velden"):
            fpd.parse_protection_config(yaml.safe_dump(doc))

    def test_restrictions_must_be_null(self):
        doc = _base_config_dict(restrictions={"users": []})
        with pytest.raises(fpd.ProtectionConfigError, match="restrictions"):
            fpd.parse_protection_config(yaml.safe_dump(doc))

    def test_duplicate_check_context_is_rejected(self):
        doc = _base_config_dict()
        doc["required_status_checks"]["checks"].append({"context": "Profile A", "app_id": 999})
        with pytest.raises(fpd.ProtectionConfigError, match="dubbel"):
            fpd.parse_protection_config(yaml.safe_dump(doc))

    def test_empty_checks_list_is_rejected(self):
        doc = _base_config_dict()
        doc["required_status_checks"]["checks"] = []
        with pytest.raises(fpd.ProtectionConfigError, match="niet-lege lijst"):
            fpd.parse_protection_config(yaml.safe_dump(doc))

    def test_invalid_yaml_syntax_is_rejected(self):
        with pytest.raises(fpd.ProtectionConfigError, match="geldige YAML"):
            fpd.parse_protection_config("branch: main\n  bad indent: [")

    def test_pending_checks_is_parsed_but_not_in_normalized_dict(self):
        """(h) pending_checks with an entry: apply and drift ignore it."""
        doc = _base_config_dict()
        doc["pending_checks"] = ["vnx-gate/review"]
        config = fpd.parse_protection_config(yaml.safe_dump(doc))
        assert config.pending_checks == ("vnx-gate/review",)
        normalized = fpd.to_normalized_dict(config)
        assert "pending_checks" not in normalized
        assert "vnx-gate/review" not in json.dumps(normalized)

    class TestVnxGateAppIdGuard:
        """(e) a vnx-gate/*-entry with app_id null or -1 (ANY_APP_ID) is refused."""

        def test_vnx_gate_check_with_null_app_id_is_rejected(self):
            doc = _base_config_dict()
            doc["required_status_checks"]["checks"].append({"context": "vnx-gate/review", "app_id": None})
            with pytest.raises(fpd.ProtectionConfigError, match="vnx-gate/review"):
                fpd.parse_protection_config(yaml.safe_dump(doc))

        def test_vnx_gate_check_with_any_app_id_sentinel_is_rejected(self):
            doc = _base_config_dict()
            doc["required_status_checks"]["checks"].append(
                {"context": "vnx-gate/review", "app_id": fpd.ANY_APP_ID}
            )
            with pytest.raises(fpd.ProtectionConfigError, match="vnx-gate/review"):
                fpd.parse_protection_config(yaml.safe_dump(doc))

        def test_vnx_gate_check_with_a_bound_app_id_is_accepted(self):
            doc = _base_config_dict()
            doc["required_status_checks"]["checks"].append({"context": "vnx-gate/review", "app_id": 99999})
            config = fpd.parse_protection_config(yaml.safe_dump(doc))
            names = {c.context: c.app_id for c in config.checks}
            assert names["vnx-gate/review"] == 99999

        def test_non_vnx_gate_check_with_null_app_id_is_accepted(self):
            """The guard is specific to the vnx-gate/* prefix -- a legacy
            contexts[]-style entry elsewhere is not what this schema forbids."""
            doc = _base_config_dict()
            doc["required_status_checks"]["checks"].append({"context": "Some Other Check", "app_id": None})
            config = fpd.parse_protection_config(yaml.safe_dump(doc))
            names = {c.context: c.app_id for c in config.checks}
            assert names["Some Other Check"] is None


# ---------------------------------------------------------------------------
# compare()
# ---------------------------------------------------------------------------

class TestCompare:
    def test_identical_states_produce_no_diffs(self):
        norm = fpd.to_normalized_dict(fpd.parse_protection_config(_yaml_text()))
        assert fpd.compare(norm, dict(norm)) == []

    def test_missing_check_on_live_side_is_reported_with_its_name(self):
        """(a) YAML with 13 checks vs a live response with 14: drift names the missing check."""
        yaml_norm = fpd.to_normalized_dict(fpd.parse_protection_config(_yaml_text()))
        live_norm = dict(yaml_norm)
        live_norm["required_status_checks"] = {
            "strict": False,
            "checks": [
                {"context": "Profile A", "app_id": 15368},
                {"context": "Profile B", "app_id": 15368},
                {"context": "Profile C (extra on live)", "app_id": 15368},
            ],
        }
        diffs = fpd.compare(yaml_norm, live_norm)
        fields = {d["field"] for d in diffs}
        assert "required_status_checks.checks[Profile C (extra on live)]" in fields
        extra_diff = next(
            d for d in diffs if d["field"] == "required_status_checks.checks[Profile C (extra on live)]"
        )
        assert extra_diff["a_present"] is False
        assert extra_diff["b_present"] is True

    def test_allow_force_pushes_mismatch_is_reported(self):
        """(b) live allow_force_pushes: true vs YAML false: red on that field."""
        yaml_norm = fpd.to_normalized_dict(fpd.parse_protection_config(_yaml_text()))
        live_norm = dict(yaml_norm)
        live_norm["allow_force_pushes"] = True
        diffs = fpd.compare(yaml_norm, live_norm)
        assert {"field": "allow_force_pushes", "a": False, "b": True} in diffs

    def test_allow_auto_merge_mismatch_is_reported(self):
        """(c) live allow_auto_merge: true is red."""
        yaml_norm = fpd.to_normalized_dict(fpd.parse_protection_config(_yaml_text()))
        live_norm = dict(yaml_norm)
        live_norm["repo"] = {"allow_auto_merge": True}
        diffs = fpd.compare(yaml_norm, live_norm)
        assert {"field": "repo.allow_auto_merge", "a": False, "b": True} in diffs

    def test_nonempty_rulesets_is_reported(self):
        """(c) a non-empty rulesets list on live is red."""
        yaml_norm = fpd.to_normalized_dict(fpd.parse_protection_config(_yaml_text()))
        live_norm = dict(yaml_norm)
        live_norm["rulesets"] = ["some-ruleset"]
        diffs = fpd.compare(yaml_norm, live_norm)
        assert any(d["field"] == "rulesets" for d in diffs)

    def test_app_id_mismatch_on_a_shared_check_name_is_reported(self):
        yaml_norm = fpd.to_normalized_dict(fpd.parse_protection_config(_yaml_text()))
        live_norm = json.loads(json.dumps(yaml_norm))
        live_norm["required_status_checks"]["checks"][0]["app_id"] = 999
        diffs = fpd.compare(yaml_norm, live_norm)
        field = "required_status_checks.checks[Profile A]"
        assert any(d["field"] == field for d in diffs)


# ---------------------------------------------------------------------------
# is_weakening()
# ---------------------------------------------------------------------------

class TestIsWeakening:
    def _norm(self, **overrides: Any) -> Dict[str, Any]:
        return fpd.to_normalized_dict(fpd.parse_protection_config(_yaml_text(**overrides)))

    def test_identical_states_are_not_weakening(self):
        old = self._norm()
        weak, fields = fpd.is_weakening(old, dict(old))
        assert weak is False
        assert fields == []

    def test_removing_a_check_is_weakening(self):
        old = self._norm()
        new = json.loads(json.dumps(old))
        new["required_status_checks"]["checks"] = [{"context": "Profile A", "app_id": 15368}]
        weak, fields = fpd.is_weakening(old, new)
        assert weak is True
        assert "required_status_checks.checks[Profile B]" in fields

    def test_adding_a_check_is_not_weakening(self):
        old = self._norm()
        new = json.loads(json.dumps(old))
        new["required_status_checks"]["checks"].append({"context": "Profile C", "app_id": 1})
        weak, fields = fpd.is_weakening(old, new)
        assert weak is False

    def test_enforce_admins_true_to_false_is_weakening(self):
        old = self._norm(enforce_admins=True)
        new = self._norm(enforce_admins=False)
        weak, fields = fpd.is_weakening(old, new)
        assert weak is True
        assert "enforce_admins" in fields

    def test_strict_true_to_false_is_weakening(self):
        old_doc = _base_config_dict()
        old_doc["required_status_checks"]["strict"] = True
        old = fpd.to_normalized_dict(fpd.parse_protection_config(yaml.safe_dump(old_doc)))
        new = self._norm()  # strict: False
        weak, fields = fpd.is_weakening(old, new)
        assert weak is True
        assert "required_status_checks.strict" in fields

    def test_review_count_lowered_is_weakening(self):
        old_doc = _base_config_dict()
        old_doc["required_pull_request_reviews"]["required_approving_review_count"] = 2
        old = fpd.to_normalized_dict(fpd.parse_protection_config(yaml.safe_dump(old_doc)))
        new = self._norm()  # count: 0
        weak, fields = fpd.is_weakening(old, new)
        assert weak is True
        assert "required_pull_request_reviews.required_approving_review_count" in fields

    def test_review_count_raised_is_not_weakening(self):
        old = self._norm()  # count: 0
        new_doc = _base_config_dict()
        new_doc["required_pull_request_reviews"]["required_approving_review_count"] = 2
        new = fpd.to_normalized_dict(fpd.parse_protection_config(yaml.safe_dump(new_doc)))
        weak, _ = fpd.is_weakening(old, new)
        assert weak is False

    def test_allow_auto_merge_false_to_true_is_weakening(self):
        old = self._norm()
        new_doc = _base_config_dict()
        new_doc["repo"]["allow_auto_merge"] = True
        new = fpd.to_normalized_dict(fpd.parse_protection_config(yaml.safe_dump(new_doc)))
        weak, fields = fpd.is_weakening(old, new)
        assert weak is True
        assert "repo.allow_auto_merge" in fields

    def test_allow_force_pushes_false_to_true_is_weakening(self):
        old = self._norm()
        new_doc = _base_config_dict()
        new_doc["allow_force_pushes"] = True
        new = fpd.to_normalized_dict(fpd.parse_protection_config(yaml.safe_dump(new_doc)))
        weak, fields = fpd.is_weakening(old, new)
        assert weak is True
        assert "allow_force_pushes" in fields

    def test_rulesets_change_alone_is_never_weakening(self):
        """rulesets is checked, never applied/weakened -- not in the weaken vocabulary."""
        old = self._norm()
        new = json.loads(json.dumps(old))
        new["rulesets"] = ["something"]
        weak, fields = fpd.is_weakening(old, new)
        assert weak is False


# ---------------------------------------------------------------------------
# fetch_live_protection() — gh subprocess mocked, comparator untouched
# ---------------------------------------------------------------------------

_PROTECTION_PAYLOAD = {
    "required_status_checks": {
        "strict": False,
        "checks": [{"context": "Profile A", "app_id": 15368}],
    },
    "required_pull_request_reviews": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews": False,
        "require_code_owner_reviews": False,
        "require_last_push_approval": False,
    },
    "enforce_admins": {"enabled": True},
    "required_signatures": {"enabled": False},
    "required_linear_history": {"enabled": False},
    "allow_force_pushes": {"enabled": False},
    "allow_deletions": {"enabled": False},
    "allow_fork_syncing": {"enabled": False},
    "block_creations": {"enabled": False},
    "lock_branch": {"enabled": False},
    "required_conversation_resolution": {"enabled": False},
}


def _ok(stdout: Any) -> "subprocess.CompletedProcess[str]":
    text = stdout if isinstance(stdout, str) else json.dumps(stdout)
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=text, stderr="")


def _err(stderr: str, returncode: int = 1) -> "subprocess.CompletedProcess[str]":
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


class TestFetchLiveProtection:
    def test_reads_and_normalizes_all_three_endpoints(self, monkeypatch, tmp_path):
        # The repo-object call ("repos/{owner}/{repo}") is a prefix of the
        # rulesets/protection endpoints too, so it is matched last and only
        # on an exact final argv element.
        def fake_run(argv, **kwargs):
            joined = " ".join(argv)
            if "branches/main/protection" in joined:
                return _ok(_PROTECTION_PAYLOAD)
            if "rulesets" in joined:
                return _ok([])
            if argv[-1] == "repos/{owner}/{repo}":
                return _ok({"allow_auto_merge": True})
            raise AssertionError(f"unexpected gh call: {joined}")

        monkeypatch.setattr(fpd.subprocess, "run", fake_run)
        result = fpd.fetch_live_protection(tmp_path, branch="main")
        assert result["enforce_admins"] is True
        assert result["required_signatures"] is False
        assert result["repo"]["allow_auto_merge"] is True
        assert result["rulesets"] == []
        assert result["required_status_checks"]["checks"] == [{"context": "Profile A", "app_id": 15368}]

    def test_unreadable_protection_endpoint_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fpd.subprocess, "run", lambda argv, **k: _err("boom", 500))
        with pytest.raises(fpd.ProtectionDriftError):
            fpd.fetch_live_protection(tmp_path, branch="main")

    def test_non_dict_protection_payload_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fpd.subprocess, "run", lambda argv, **k: _ok([1, 2, 3]))
        with pytest.raises(fpd.ProtectionDriftError):
            fpd.fetch_live_protection(tmp_path, branch="main")

    def test_gh_missing_binary_raises(self, monkeypatch, tmp_path):
        def raise_missing(argv, **kwargs):
            raise FileNotFoundError("gh not found")

        monkeypatch.setattr(fpd.subprocess, "run", raise_missing)
        with pytest.raises(fpd.ProtectionDriftError, match="niet beschikbaar"):
            fpd.fetch_live_protection(tmp_path, branch="main")


# ---------------------------------------------------------------------------
# fetch_yaml_from_ref() — 404 vs other failures (i)
# ---------------------------------------------------------------------------

class TestFetchYamlFromRef:
    def test_404_is_not_found_not_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            fpd.subprocess, "run",
            lambda argv, **k: _err("gh: Not Found (HTTP 404)", 1),
        )
        result = fpd.fetch_yaml_from_ref(tmp_path, "main")
        assert result.not_found is True
        assert result.error is None
        assert result.text is None

    def test_500_is_an_error_not_not_found(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            fpd.subprocess, "run",
            lambda argv, **k: _err("gh: Internal Server Error (HTTP 500)", 1),
        )
        result = fpd.fetch_yaml_from_ref(tmp_path, "main")
        assert result.not_found is False
        assert result.error is not None

    def test_403_is_an_error_not_not_found(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            fpd.subprocess, "run",
            lambda argv, **k: _err("gh: Forbidden (HTTP 403)", 1),
        )
        result = fpd.fetch_yaml_from_ref(tmp_path, "main")
        assert result.not_found is False
        assert result.error is not None

    def test_successful_fetch_decodes_base64_content(self, monkeypatch, tmp_path):
        raw = "branch: main\n"
        payload = {"content": base64.b64encode(raw.encode("utf-8")).decode("ascii"), "sha": "abc123"}
        monkeypatch.setattr(fpd.subprocess, "run", lambda argv, **k: _ok(payload))
        result = fpd.fetch_yaml_from_ref(tmp_path, "main")
        assert result.not_found is False
        assert result.error is None
        assert result.text == raw

    def test_gh_missing_is_an_error(self, monkeypatch, tmp_path):
        def raise_missing(argv, **kwargs):
            raise FileNotFoundError("gh not found")

        monkeypatch.setattr(fpd.subprocess, "run", raise_missing)
        result = fpd.fetch_yaml_from_ref(tmp_path, "main")
        assert result.not_found is False
        assert result.error is not None

    def test_unparseable_json_is_an_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fpd.subprocess, "run", lambda argv, **k: _ok("not json"))
        result = fpd.fetch_yaml_from_ref(tmp_path, "main")
        assert result.not_found is False
        assert result.error is not None

    def test_missing_content_field_is_an_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fpd.subprocess, "run", lambda argv, **k: _ok({"sha": "abc"}))
        result = fpd.fetch_yaml_from_ref(tmp_path, "main")
        assert result.not_found is False
        assert result.error is not None
