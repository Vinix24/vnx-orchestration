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

    def test_the_real_shipped_yaml_parses_and_has_fifteen_checks(self):
        """The actual scripts/forge/branch_protection.yaml this dispatch ships.

        Fourteen until OP-B3, fifteen after it: that step moved
        ``vnx-gate/review`` out of ``pending_checks`` and into ``checks[]``. The
        blanket ``all(app_id == 15368)`` went with it — the fifteenth check is
        not a GitHub Actions context but the ``vnx-gate`` App's, bound to the
        App ID the same file declares under ``app:``. Read, not repeated here:
        a second literal copy of that number in the test could drift from the
        one that signs, which is the very thing the YAML comment warns about.
        """
        path = VNX_ROOT / "scripts" / "forge" / "branch_protection.yaml"
        config = fpd.load_protection_config(path)
        app_id = yaml.safe_load(path.read_text(encoding="utf-8"))["app"]["app_id"]

        by_context = {c.context: c.app_id for c in config.checks}
        assert len(config.checks) == 15
        assert by_context["vnx-gate/review"] == app_id
        assert all(
            c.app_id == 15368 for c in config.checks if not c.context.startswith("vnx-gate/")
        )
        assert config.pending_checks == ()
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


class TestAppBlock:
    """Golf B, B2a fix-forward 2: the top-level ``app:`` block is optional
    (already true since B2a's own commit), but its INTERNAL shape was never
    validated -- any garbage nested under ``app:`` parsed silently. This
    class locks in that the block, when present, is validated with the same
    rigor as every other object in this schema.
    """

    def test_app_block_is_accepted(self):
        doc = _base_config_dict(app={"slug": "vnx-gate", "app_id": None})
        config = fpd.parse_protection_config(yaml.safe_dump(doc))
        assert config.branch == "main"

    def test_app_block_with_a_bound_app_id_is_accepted(self):
        doc = _base_config_dict(app={"slug": "vnx-gate", "app_id": 987654})
        config = fpd.parse_protection_config(yaml.safe_dump(doc))
        assert config.branch == "main"

    def test_app_block_with_unknown_key_is_rejected(self):
        doc = _base_config_dict(app={"slug": "vnx-gate", "app_id": None, "bogus": 1})
        with pytest.raises(fpd.ProtectionConfigError, match="onbekende velden"):
            fpd.parse_protection_config(yaml.safe_dump(doc))

    def test_app_block_missing_slug_is_rejected(self):
        doc = _base_config_dict(app={"app_id": None})
        with pytest.raises(fpd.ProtectionConfigError, match="verplichte velden"):
            fpd.parse_protection_config(yaml.safe_dump(doc))

    def test_app_block_missing_app_id_is_rejected(self):
        doc = _base_config_dict(app={"slug": "vnx-gate"})
        with pytest.raises(fpd.ProtectionConfigError, match="verplichte velden"):
            fpd.parse_protection_config(yaml.safe_dump(doc))

    def test_app_slug_must_be_a_non_empty_string(self):
        doc = _base_config_dict(app={"slug": "", "app_id": None})
        with pytest.raises(fpd.ProtectionConfigError, match="app.slug"):
            fpd.parse_protection_config(yaml.safe_dump(doc))

    def test_app_id_must_be_an_integer_or_null(self):
        doc = _base_config_dict(app={"slug": "vnx-gate", "app_id": "987654"})
        with pytest.raises(fpd.ProtectionConfigError, match="app.app_id"):
            fpd.parse_protection_config(yaml.safe_dump(doc))

    def test_app_id_true_is_rejected_not_treated_as_an_integer(self):
        """bool is an int subclass; `app_id: true` is a typo, not an id."""
        doc = _base_config_dict(app={"slug": "vnx-gate", "app_id": True})
        with pytest.raises(fpd.ProtectionConfigError, match="app.app_id"):
            fpd.parse_protection_config(yaml.safe_dump(doc))

    def test_app_block_not_a_mapping_is_rejected(self):
        doc = _base_config_dict(app="vnx-gate")
        with pytest.raises(fpd.ProtectionConfigError, match="app"):
            fpd.parse_protection_config(yaml.safe_dump(doc))

    def test_compare_is_identical_with_and_without_an_app_block(self):
        """(3) the app: block never enters the normalized dict, so its
        presence or absence can never register as drift."""
        without = fpd.to_normalized_dict(fpd.parse_protection_config(_yaml_text()))
        with_app = fpd.to_normalized_dict(
            fpd.parse_protection_config(_yaml_text(app={"slug": "vnx-gate", "app_id": None}))
        )
        assert fpd.compare(without, with_app) == []

    def test_is_weakening_is_identical_with_and_without_an_app_block(self):
        without = fpd.to_normalized_dict(fpd.parse_protection_config(_yaml_text()))
        with_app = fpd.to_normalized_dict(
            fpd.parse_protection_config(_yaml_text(app={"slug": "vnx-gate", "app_id": 1}))
        )
        assert fpd.is_weakening(without, with_app) == (False, [])
        assert fpd.is_weakening(with_app, without) == (False, [])

    def test_apply_put_payload_never_contains_an_app_key(self):
        """The PUT body apply_branch_protection.py sends is built entirely
        from ProtectionConfig fields, which never carry ``app`` -- see
        ProtectionConfig's field list. Confirmed end-to-end here rather than
        just by inspection, against a config parsed from a YAML that DOES
        declare the block."""
        forge_dir = VNX_ROOT / "scripts" / "forge"
        sys.path.insert(0, str(forge_dir))
        import apply_branch_protection as abp

        doc = _base_config_dict(app={"slug": "vnx-gate", "app_id": 987654})
        config = fpd.parse_protection_config(yaml.safe_dump(doc))
        payload = abp.build_put_payload(config)
        assert "app" not in payload
        assert "slug" not in json.dumps(payload)


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

def _contents_and_commits(
    *, contents: "subprocess.CompletedProcess[str]", commits: "subprocess.CompletedProcess[str]",
):
    """A gh stub that answers the contents read and the ref-confirmation read
    separately -- ``fetch_yaml_from_ref`` makes the second call only after a
    404 on the first (see ``_confirm_ref_exists``)."""

    def fake_run(argv, **kwargs):
        joined = " ".join(argv)
        if "/contents/" in joined:
            return contents
        if "/commits/" in joined:
            return commits
        raise AssertionError(f"unexpected gh call: {joined}")

    return fake_run


class TestFetchYamlFromRef:
    def test_404_is_not_found_not_error(self, monkeypatch, tmp_path):
        """A 404 on the path AT A REF THAT EXISTS is the named not-found case."""
        monkeypatch.setattr(
            fpd.subprocess, "run",
            _contents_and_commits(
                contents=_err("gh: Not Found (HTTP 404)", 1),
                commits=_ok("d" * 40),
            ),
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


class TestNotFoundRequiresAConfirmedRef:
    """Fix-forward punt 2: a 404 is only "this ref has no such file" once the
    ref itself is confirmed to exist. ``gh`` returns the same ``HTTP 404``
    marker for an unknown ref and for an unresolvable repo, and the merge
    door turns exactly that marker into a GO that skips every remaining
    check -- so an unconfirmed ref must be a fail-closed error instead.
    """

    def test_404_on_an_unknown_ref_is_an_error_not_not_found(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            fpd.subprocess, "run",
            _contents_and_commits(
                contents=_err("gh: No commit found for the ref refs/heads/does-not-exist-xyz (HTTP 404)", 1),
                commits=_err("gh: No commit found for the ref refs/heads/does-not-exist-xyz (HTTP 404)", 1),
            ),
        )
        result = fpd.fetch_yaml_from_ref(tmp_path, "does-not-exist-xyz")
        assert result.not_found is False
        assert result.error is not None
        assert "does-not-exist-xyz" in result.error

    def test_404_on_an_unresolvable_repo_is_an_error_not_not_found(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            fpd.subprocess, "run",
            _contents_and_commits(
                contents=_err("gh: Not Found (HTTP 404)", 1),
                commits=_err("gh: Not Found (HTTP 404)", 1),
            ),
        )
        result = fpd.fetch_yaml_from_ref(tmp_path, "main")
        assert result.not_found is False
        assert result.error is not None

    def test_an_empty_sha_from_the_confirmation_is_an_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            fpd.subprocess, "run",
            _contents_and_commits(
                contents=_err("gh: Not Found (HTTP 404)", 1),
                commits=_ok(""),
            ),
        )
        result = fpd.fetch_yaml_from_ref(tmp_path, "main")
        assert result.not_found is False
        assert result.error is not None

    def test_a_missing_gh_during_confirmation_is_an_error(self, monkeypatch, tmp_path):
        def fake_run(argv, **kwargs):
            if "/contents/" in " ".join(argv):
                return _err("gh: Not Found (HTTP 404)", 1)
            raise FileNotFoundError("gh not found")

        monkeypatch.setattr(fpd.subprocess, "run", fake_run)
        result = fpd.fetch_yaml_from_ref(tmp_path, "main")
        assert result.not_found is False
        assert result.error is not None

    def test_a_confirmed_head_sha_still_yields_not_found(self, monkeypatch, tmp_path):
        """The PR-side use: the head sha exists, the file does not."""
        head = "e" * 40
        monkeypatch.setattr(
            fpd.subprocess, "run",
            _contents_and_commits(contents=_err("gh: Not Found (HTTP 404)", 1), commits=_ok(head)),
        )
        result = fpd.fetch_yaml_from_ref(tmp_path, head)
        assert result.not_found is True
        assert result.error is None


# ---------------------------------------------------------------------------
# An ABSENT required_pull_request_reviews block (fix-forward punt 1)
# ---------------------------------------------------------------------------

def _gh_live_stub(protection_payload: Dict[str, Any], *, allow_auto_merge: bool = False):
    def fake_run(argv, **kwargs):
        joined = " ".join(argv)
        if "branches/main/protection" in joined:
            return _ok(protection_payload)
        if "rulesets" in joined:
            return _ok([])
        if argv[-1] == "repos/{owner}/{repo}":
            return _ok({"allow_auto_merge": allow_auto_merge})
        raise AssertionError(f"unexpected gh call: {joined}")

    return fake_run


def _single_check_yaml_norm(**overrides: Any) -> Dict[str, Any]:
    """A YAML normalization whose checks[] matches ``_PROTECTION_PAYLOAD``'s
    single entry, so a comparison against that payload isolates the field
    under test instead of drowning it in check diffs."""
    doc = _base_config_dict(**overrides)
    doc["required_status_checks"]["checks"] = [{"context": "Profile A", "app_id": 15368}]
    return fpd.to_normalized_dict(fpd.parse_protection_config(yaml.safe_dump(doc)))


class TestAbsentPullRequestReviewsBlock:
    """Fix-forward punt 1: GitHub OMITS ``required_pull_request_reviews``
    from the GET when "Require a pull request before merging" is off --
    exactly as it omits ``restrictions`` when unset. Normalizing an absent
    block into the same zero/false object the YAML declares makes the most
    consequential setting of the whole object invisible to both watchers.
    """

    def _live_without_the_block(self, monkeypatch, tmp_path) -> Dict[str, Any]:
        payload = dict(_PROTECTION_PAYLOAD)
        payload.pop("required_pull_request_reviews")
        monkeypatch.setattr(fpd.subprocess, "run", _gh_live_stub(payload))
        return fpd.fetch_live_protection(tmp_path, branch="main")

    def test_control_the_block_present_gives_zero_drift(self, monkeypatch, tmp_path):
        """The baseline that makes a zero below a measurement, not a bug."""
        monkeypatch.setattr(fpd.subprocess, "run", _gh_live_stub(_PROTECTION_PAYLOAD))
        live = fpd.fetch_live_protection(tmp_path, branch="main")
        assert fpd.compare(_single_check_yaml_norm(), live) == []

    def test_absent_block_normalizes_to_none_not_to_zeroes(self, monkeypatch, tmp_path):
        live = self._live_without_the_block(monkeypatch, tmp_path)
        assert live["required_pull_request_reviews"] is None

    def test_absent_block_is_drift_against_a_yaml_that_declares_one(self, monkeypatch, tmp_path):
        live = self._live_without_the_block(monkeypatch, tmp_path)
        diffs = fpd.compare(_single_check_yaml_norm(), live)
        assert [d["field"] for d in diffs] == ["required_pull_request_reviews"]
        assert diffs[0]["a_present"] is True
        assert diffs[0]["b_present"] is False

    def test_absent_block_counts_as_a_weakening(self, monkeypatch, tmp_path):
        live = self._live_without_the_block(monkeypatch, tmp_path)
        weak, fields = fpd.is_weakening(_single_check_yaml_norm(), live)
        assert weak is True
        assert "required_pull_request_reviews" in fields

    def test_adding_the_block_where_it_was_absent_is_not_a_weakening(self, monkeypatch, tmp_path):
        live = self._live_without_the_block(monkeypatch, tmp_path)
        weak, fields = fpd.is_weakening(live, _single_check_yaml_norm())
        assert weak is False
        assert fields == []

    def test_a_present_block_still_diffs_field_by_field(self, monkeypatch, tmp_path):
        """Control: the per-field comparison is untouched by the presence model."""
        payload = json.loads(json.dumps(_PROTECTION_PAYLOAD))
        payload["required_pull_request_reviews"]["required_approving_review_count"] = 2
        monkeypatch.setattr(fpd.subprocess, "run", _gh_live_stub(payload))
        live = fpd.fetch_live_protection(tmp_path, branch="main")
        diffs = fpd.compare(_single_check_yaml_norm(), live)
        assert [d["field"] for d in diffs] == [
            "required_pull_request_reviews.required_approving_review_count"
        ]

    def test_yaml_may_declare_the_block_absent_with_an_explicit_null(self):
        doc = _base_config_dict(required_pull_request_reviews=None)
        config = fpd.parse_protection_config(yaml.safe_dump(doc))
        assert config.required_pull_request_reviews_present is False
        assert fpd.to_normalized_dict(config)["required_pull_request_reviews"] is None

    def test_a_partial_block_in_the_yaml_is_still_refused(self):
        doc = _base_config_dict()
        del doc["required_pull_request_reviews"]["dismiss_stale_reviews"]
        with pytest.raises(fpd.ProtectionConfigError, match="verplichte velden"):
            fpd.parse_protection_config(yaml.safe_dump(doc))


class TestBypassPullRequestAllowances:
    """Fix-forward punt 5: a bypass grant on the PR requirement lets named
    users/teams/apps merge around it. Dropped in normalization, it is
    invisible to the merge door and to ``vnx doctor`` alike.
    """

    def _live_with_a_bypass(self, monkeypatch, tmp_path) -> Dict[str, Any]:
        payload = json.loads(json.dumps(_PROTECTION_PAYLOAD))
        payload["required_pull_request_reviews"]["bypass_pull_request_allowances"] = {
            "users": [{"login": "vincent"}],
            "teams": [],
            "apps": [{"slug": "some-bot"}],
        }
        monkeypatch.setattr(fpd.subprocess, "run", _gh_live_stub(payload))
        return fpd.fetch_live_protection(tmp_path, branch="main")

    def test_a_live_grant_survives_normalization(self, monkeypatch, tmp_path):
        live = self._live_with_a_bypass(monkeypatch, tmp_path)
        assert live["required_pull_request_reviews"]["bypass_pull_request_allowances"] == [
            "apps:some-bot", "users:vincent",
        ]

    def test_a_live_grant_is_drift_against_a_yaml_without_one(self, monkeypatch, tmp_path):
        live = self._live_with_a_bypass(monkeypatch, tmp_path)
        diffs = fpd.compare(_single_check_yaml_norm(), live)
        assert [d["field"] for d in diffs] == [
            "required_pull_request_reviews.bypass_pull_request_allowances"
        ]

    def test_a_live_grant_counts_as_a_weakening(self, monkeypatch, tmp_path):
        live = self._live_with_a_bypass(monkeypatch, tmp_path)
        weak, fields = fpd.is_weakening(_single_check_yaml_norm(), live)
        assert weak is True
        assert "required_pull_request_reviews.bypass_pull_request_allowances" in fields

    def test_removing_a_grant_is_not_a_weakening(self, monkeypatch, tmp_path):
        live = self._live_with_a_bypass(monkeypatch, tmp_path)
        weak, fields = fpd.is_weakening(live, _single_check_yaml_norm())
        assert weak is False

    def test_no_grant_anywhere_is_an_empty_list_not_a_missing_key(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fpd.subprocess, "run", _gh_live_stub(_PROTECTION_PAYLOAD))
        live = fpd.fetch_live_protection(tmp_path, branch="main")
        assert live["required_pull_request_reviews"]["bypass_pull_request_allowances"] == []
        assert _single_check_yaml_norm()["required_pull_request_reviews"][
            "bypass_pull_request_allowances"
        ] == []

    def test_the_yaml_may_declare_a_grant_explicitly(self):
        doc = _base_config_dict()
        doc["required_pull_request_reviews"]["bypass_pull_request_allowances"] = {
            "users": ["vincent"], "teams": [], "apps": [],
        }
        config = fpd.parse_protection_config(yaml.safe_dump(doc))
        assert config.bypass_pull_request_allowances == ("users:vincent",)

    def test_a_malformed_grant_in_the_yaml_is_refused(self):
        doc = _base_config_dict()
        doc["required_pull_request_reviews"]["bypass_pull_request_allowances"] = ["vincent"]
        with pytest.raises(fpd.ProtectionConfigError, match="bypass_pull_request_allowances"):
            fpd.parse_protection_config(yaml.safe_dump(doc))
