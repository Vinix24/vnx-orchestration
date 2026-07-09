"""Tests for scripts/lib/delegation_mandate.py (signed batch-delegation mandate).

Covers:
  - mandate_manifest builder and required fields
  - sign + verify round-trip using ephemeral SSH keys
  - emit_mandate writes to .vnx-attest/mandates.ndjson
  - mandate_covers: valid scope, expired, revoked, out-of-scope, unsigned
  - load_mandates filters active mandates
  - resolve_signed_delegation flag-off vs flag-on behavior
  - receipt fields include mandate_id when provided
  - CLI issue/revoke helpers
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vnx_cli"))

from delegation_mandate import (
    DelegationMandate,
    DispatchContext,
    MANDATE_ATTESTATION_TYPE,
    REVOKE_ATTESTATION_TYPE,
    emit_mandate,
    emit_mandate_revocation,
    is_signed_delegation_enabled,
    load_mandates,
    mandate_covers,
    mandate_manifest,
    resolve_signed_delegation,
)


@pytest.fixture(scope="session")
def ephemeral_key_dir():
    """Generate a non-interactive ed25519 test key in a temp dir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        key_path = Path(tmpdir) / "testkey"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", ""],
            check=True, capture_output=True,
        )
        pub = key_path.with_suffix(".pub").read_text().strip()
        identity = "vnx-test@local"
        allowed_signers = Path(tmpdir) / "allowed_signers"
        allowed_signers.write_text(f"{identity} {pub}\n")
        yield {
            "key_path": key_path,
            "identity": identity,
            "allowed_signers": allowed_signers,
            "tmpdir": Path(tmpdir),
        }


@pytest.fixture
def fresh_repo(tmp_path):
    """Create a temporary repo root with a .vnx-attest directory."""
    (tmp_path / ".vnx-attest").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def tomorrow() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def yesterday() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _issue_mandate(repo_root, key_dir, scope, expires_at, mandate_id="mandate-test-001"):
    manifest = mandate_manifest(
        mandate_id=mandate_id,
        project_id="vnx-dev",
        scope=scope,
        issued_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=expires_at,
        signer_identity=key_dir["identity"],
    )
    return emit_mandate(manifest, key_dir["key_path"], repo_root=repo_root)


# ---------------------------------------------------------------------------
# Manifest builder
# ---------------------------------------------------------------------------

class TestBuildMandateManifest:
    def test_required_fields_present(self):
        m = mandate_manifest(
            mandate_id="m-1",
            project_id="vnx-dev",
            scope={"dispatch_id_glob": "D-*"},
            issued_at="2026-07-09T10:00:00Z",
            expires_at="2026-07-10T10:00:00Z",
            signer_identity="vnx-test@local",
        )
        assert m["schema_version"] == "1"
        assert m["attestation_type"] == MANDATE_ATTESTATION_TYPE
        assert m["mandate_id"] == "m-1"
        assert m["project_id"] == "vnx-dev"
        assert m["scope"] == {"dispatch_id_glob": "D-*"}
        assert m["issued_at"] == "2026-07-09T10:00:00Z"
        assert m["expires_at"] == "2026-07-10T10:00:00Z"
        assert m["signer_identity"] == "vnx-test@local"
        assert "signature" not in m


# ---------------------------------------------------------------------------
# Sign + emit
# ---------------------------------------------------------------------------

class TestEmitMandate:
    def test_emits_signed_ledger_entry(self, ephemeral_key_dir, fresh_repo, tomorrow):
        rec = _issue_mandate(
            fresh_repo, ephemeral_key_dir,
            scope={"dispatch_id_glob": "D-*"},
            expires_at=tomorrow,
        )
        assert isinstance(rec, DelegationMandate)
        assert rec.manifest["signature"]
        assert rec.ledger_path.exists()
        assert rec.ledger_path.name == "mandates.ndjson"
        entry = json.loads(rec.ledger_path.read_text().strip())
        assert entry["attestation_type"] == MANDATE_ATTESTATION_TYPE
        assert entry["mandate_id"] == rec.mandate_id

    def test_chain_links_entries(self, ephemeral_key_dir, fresh_repo, tomorrow):
        rec1 = _issue_mandate(
            fresh_repo, ephemeral_key_dir,
            scope={"dispatch_id_glob": "D-*"},
            expires_at=tomorrow,
            mandate_id="m-chain-1",
        )
        rec2 = _issue_mandate(
            fresh_repo, ephemeral_key_dir,
            scope={"dispatch_id_glob": "D-*"},
            expires_at=tomorrow,
            mandate_id="m-chain-2",
        )
        lines = rec1.ledger_path.read_text().strip().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert second["prev_hash"] != "0" * 64
        assert second["prev_hash"] == __import__(
            "ndjson_hash_chain", fromlist=["compute_entry_hash"]
        ).compute_entry_hash(first)


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

class TestMandateCovers:
    def test_valid_scope_covers(self, ephemeral_key_dir, fresh_repo, tomorrow):
        rec = _issue_mandate(
            fresh_repo, ephemeral_key_dir,
            scope={"dispatch_id_glob": "D-123-*"},
            expires_at=tomorrow,
        )
        ctx = DispatchContext(
            project_id="vnx-dev", dispatch_id="D-123-foo", task_class="refactor"
        )
        assert mandate_covers(
            rec.manifest, ctx, allowed_signers=ephemeral_key_dir["allowed_signers"],
            repo_root=fresh_repo
        )

    def test_wrong_project_not_covered(self, ephemeral_key_dir, fresh_repo, tomorrow):
        rec = _issue_mandate(
            fresh_repo, ephemeral_key_dir,
            scope={"dispatch_id_glob": "*"},
            expires_at=tomorrow,
        )
        ctx = DispatchContext(project_id="other", dispatch_id="D-1")
        assert not mandate_covers(
            rec.manifest, ctx, allowed_signers=ephemeral_key_dir["allowed_signers"],
            repo_root=fresh_repo
        )

    def test_out_of_scope_dispatch_not_covered(self, ephemeral_key_dir, fresh_repo, tomorrow):
        rec = _issue_mandate(
            fresh_repo, ephemeral_key_dir,
            scope={"dispatch_id_glob": "D-123-*"},
            expires_at=tomorrow,
        )
        ctx = DispatchContext(project_id="vnx-dev", dispatch_id="D-999-foo")
        assert not mandate_covers(
            rec.manifest, ctx, allowed_signers=ephemeral_key_dir["allowed_signers"],
            repo_root=fresh_repo
        )

    def test_task_class_scope(self, ephemeral_key_dir, fresh_repo, tomorrow):
        rec = _issue_mandate(
            fresh_repo, ephemeral_key_dir,
            scope={"allowed_task_classes": ["refactor", "closeout"]},
            expires_at=tomorrow,
        )
        ctx = DispatchContext(project_id="vnx-dev", dispatch_id="D-1", task_class="refactor")
        assert mandate_covers(
            rec.manifest, ctx, allowed_signers=ephemeral_key_dir["allowed_signers"],
            repo_root=fresh_repo
        )
        ctx2 = DispatchContext(project_id="vnx-dev", dispatch_id="D-1", task_class="research")
        assert not mandate_covers(
            rec.manifest, ctx2, allowed_signers=ephemeral_key_dir["allowed_signers"],
            repo_root=fresh_repo
        )

    def test_session_id_scope(self, ephemeral_key_dir, fresh_repo, tomorrow):
        rec = _issue_mandate(
            fresh_repo, ephemeral_key_dir,
            scope={"session_id": "sess-A"},
            expires_at=tomorrow,
        )
        ctx = DispatchContext(
            project_id="vnx-dev", dispatch_id="D-1", session_id="sess-A"
        )
        assert mandate_covers(
            rec.manifest, ctx, allowed_signers=ephemeral_key_dir["allowed_signers"],
            repo_root=fresh_repo
        )
        ctx2 = DispatchContext(
            project_id="vnx-dev", dispatch_id="D-1", session_id="sess-B"
        )
        assert not mandate_covers(
            rec.manifest, ctx2, allowed_signers=ephemeral_key_dir["allowed_signers"],
            repo_root=fresh_repo
        )

    def test_empty_scope_covers_nothing(self, ephemeral_key_dir, fresh_repo, tomorrow):
        rec = _issue_mandate(
            fresh_repo, ephemeral_key_dir,
            scope={},
            expires_at=tomorrow,
        )
        ctx = DispatchContext(project_id="vnx-dev", dispatch_id="D-1")
        assert not mandate_covers(
            rec.manifest, ctx, allowed_signers=ephemeral_key_dir["allowed_signers"],
            repo_root=fresh_repo
        )

    def test_expired_mandate_not_covered(self, ephemeral_key_dir, fresh_repo, yesterday):
        rec = _issue_mandate(
            fresh_repo, ephemeral_key_dir,
            scope={"dispatch_id_glob": "*"},
            expires_at=yesterday,
        )
        ctx = DispatchContext(project_id="vnx-dev", dispatch_id="D-1")
        assert not mandate_covers(
            rec.manifest, ctx, allowed_signers=ephemeral_key_dir["allowed_signers"],
            repo_root=fresh_repo
        )

    def test_unsigned_mandate_not_covered(self, ephemeral_key_dir, fresh_repo, tomorrow):
        manifest = mandate_manifest(
            mandate_id="m-unsigned",
            project_id="vnx-dev",
            scope={"dispatch_id_glob": "*"},
            issued_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            expires_at=tomorrow,
            signer_identity=ephemeral_key_dir["identity"],
        )
        ctx = DispatchContext(project_id="vnx-dev", dispatch_id="D-1")
        assert not mandate_covers(
            manifest, ctx, allowed_signers=ephemeral_key_dir["allowed_signers"],
            repo_root=fresh_repo
        )


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------

class TestRevokeMandate:
    def test_revoke_removes_coverage(self, ephemeral_key_dir, fresh_repo, tomorrow):
        rec = _issue_mandate(
            fresh_repo, ephemeral_key_dir,
            scope={"dispatch_id_glob": "*"},
            expires_at=tomorrow,
        )
        emit_mandate_revocation(
            mandate_id=rec.mandate_id,
            signer_identity=ephemeral_key_dir["identity"],
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            key_path=ephemeral_key_dir["key_path"],
            repo_root=fresh_repo,
        )
        ctx = DispatchContext(project_id="vnx-dev", dispatch_id="D-1")
        active = load_mandates(
            rec.ledger_path, allowed_signers=ephemeral_key_dir["allowed_signers"]
        )
        assert not any(m["mandate_id"] == rec.mandate_id for m in active)
        assert not mandate_covers(
            rec.manifest, ctx, allowed_signers=ephemeral_key_dir["allowed_signers"],
            repo_root=fresh_repo
        )

    def test_revoke_uses_distinct_attestation_type(self, ephemeral_key_dir, fresh_repo, tomorrow):
        rec = _issue_mandate(
            fresh_repo, ephemeral_key_dir,
            scope={"dispatch_id_glob": "*"},
            expires_at=tomorrow,
        )
        emit_mandate_revocation(
            mandate_id=rec.mandate_id,
            signer_identity=ephemeral_key_dir["identity"],
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            key_path=ephemeral_key_dir["key_path"],
            repo_root=fresh_repo,
        )
        lines = rec.ledger_path.read_text().strip().splitlines()
        revoke_entry = json.loads(lines[-1])
        assert revoke_entry["attestation_type"] == REVOKE_ATTESTATION_TYPE
        assert revoke_entry["mandate_id"] == rec.mandate_id


# ---------------------------------------------------------------------------
# Resolution (feature flag)
# ---------------------------------------------------------------------------

class TestResolveSignedDelegation:
    def test_flag_off_requires_approval_id(self, ephemeral_key_dir, fresh_repo, tomorrow, monkeypatch):
        monkeypatch.setenv("VNX_SIGNED_DELEGATION", "0")
        ctx = DispatchContext(project_id="vnx-dev", dispatch_id="D-1")
        ok, mandate_id, reason = resolve_signed_delegation(
            ctx, approval_id="appr-1", mandate_id=None,
            allowed_signers=ephemeral_key_dir["allowed_signers"],
            repo_root=fresh_repo,
        )
        assert ok is True
        assert mandate_id is None
        ok2, _, _ = resolve_signed_delegation(
            ctx, approval_id=None, mandate_id=None,
            allowed_signers=ephemeral_key_dir["allowed_signers"],
            repo_root=fresh_repo,
        )
        assert ok2 is False

    def test_flag_on_valid_mandate_allowed(self, ephemeral_key_dir, fresh_repo, tomorrow, monkeypatch):
        monkeypatch.setenv("VNX_SIGNED_DELEGATION", "1")
        rec = _issue_mandate(
            fresh_repo, ephemeral_key_dir,
            scope={"dispatch_id_glob": "D-1"},
            expires_at=tomorrow,
        )
        ctx = DispatchContext(project_id="vnx-dev", dispatch_id="D-1")
        ok, mandate_id, reason = resolve_signed_delegation(
            ctx, approval_id=None, mandate_id=rec.mandate_id,
            allowed_signers=ephemeral_key_dir["allowed_signers"],
            repo_root=fresh_repo,
        )
        assert ok is True
        assert mandate_id == rec.mandate_id
        assert "mandate" in reason

    def test_flag_on_expired_mandate_falls_back(self, ephemeral_key_dir, fresh_repo, yesterday, monkeypatch):
        monkeypatch.setenv("VNX_SIGNED_DELEGATION", "1")
        rec = _issue_mandate(
            fresh_repo, ephemeral_key_dir,
            scope={"dispatch_id_glob": "D-1"},
            expires_at=yesterday,
        )
        ctx = DispatchContext(project_id="vnx-dev", dispatch_id="D-1")
        ok, mandate_id, reason = resolve_signed_delegation(
            ctx, approval_id=None, mandate_id=rec.mandate_id,
            allowed_signers=ephemeral_key_dir["allowed_signers"],
            repo_root=fresh_repo,
        )
        assert ok is False
        assert mandate_id is None

    def test_flag_on_out_of_scope_not_covered(self, ephemeral_key_dir, fresh_repo, tomorrow, monkeypatch):
        monkeypatch.setenv("VNX_SIGNED_DELEGATION", "1")
        rec = _issue_mandate(
            fresh_repo, ephemeral_key_dir,
            scope={"dispatch_id_glob": "D-2"},
            expires_at=tomorrow,
        )
        ctx = DispatchContext(project_id="vnx-dev", dispatch_id="D-1")
        ok, mandate_id, reason = resolve_signed_delegation(
            ctx, approval_id=None, mandate_id=rec.mandate_id,
            allowed_signers=ephemeral_key_dir["allowed_signers"],
            repo_root=fresh_repo,
        )
        assert ok is False


# ---------------------------------------------------------------------------
# Receipt fields
# ---------------------------------------------------------------------------

class TestReceiptMandateId:
    def test_subprocess_receipt_payload_includes_mandate_id(self):
        from subprocess_dispatch_internals.receipt_writer import _build_receipt_payload
        payload = _build_receipt_payload(
            dispatch_id="D-1",
            terminal_id="T1",
            status="done",
            event_count=0,
            session_id=None,
            attempt=0,
            failure_reason=None,
            commit_missing=False,
            committed=False,
            commit_hash_before="",
            commit_hash_after="",
            manifest_path=None,
            stuck_event_count=0,
            mandate_id="m-sub-1",
        )
        assert payload["mandate_id"] == "m-sub-1"

    def test_governance_receipt_includes_mandate_id(self, tmp_path):
        from governance_emit import emit_dispatch_receipt
        receipt_path = emit_dispatch_receipt(
            dispatch_id="D-1",
            terminal_id="T1",
            provider="kimi",
            model="kimi-k2",
            pr_id=None,
            status="success",
            completion_pct=100,
            risk=0.0,
            findings=[],
            duration_seconds=1.0,
            token_usage={"input": 0, "output": 0},
            cost_usd=None,
            state_dir=tmp_path,
            mandate_id="m-gov-1",
        )
        line = json.loads(receipt_path.read_text().strip())
        assert line["mandate_id"] == "m-gov-1"


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

class TestAttestMandateCli:
    def test_issue_writes_signed_ledger_entry(self, ephemeral_key_dir, fresh_repo, tomorrow):
        from vnx_cli.commands.attest import vnx_attest_mandate_issue
        args = SimpleNamespace(
            project_dir=str(fresh_repo),
            key=str(ephemeral_key_dir["key_path"]),
            signer=ephemeral_key_dir["identity"],
            mandate_id="m-cli-1",
            project_id="vnx-dev",
            expires_at=tomorrow,
            session_id="sess-cli",
            task_class="refactor",
            dispatch_id_glob=None,
        )
        assert vnx_attest_mandate_issue(args) == 0
        ledger = fresh_repo / ".vnx-attest" / "mandates.ndjson"
        entry = json.loads(ledger.read_text().strip())
        assert entry["mandate_id"] == "m-cli-1"
        assert entry["scope"]["session_id"] == "sess-cli"
        assert "refactor" in entry["scope"]["allowed_task_classes"]

    def test_revoke_appends_revocation_entry(self, ephemeral_key_dir, fresh_repo, tomorrow):
        from vnx_cli.commands.attest import vnx_attest_mandate_issue, vnx_attest_mandate_revoke
        issue_args = SimpleNamespace(
            project_dir=str(fresh_repo),
            key=str(ephemeral_key_dir["key_path"]),
            signer=ephemeral_key_dir["identity"],
            mandate_id="m-cli-2",
            project_id="vnx-dev",
            expires_at=tomorrow,
            session_id=None,
            task_class=None,
            dispatch_id_glob="*",
        )
        assert vnx_attest_mandate_issue(issue_args) == 0
        revoke_args = SimpleNamespace(
            project_dir=str(fresh_repo),
            key=str(ephemeral_key_dir["key_path"]),
            signer=ephemeral_key_dir["identity"],
            mandate_id="m-cli-2",
        )
        assert vnx_attest_mandate_revoke(revoke_args) == 0
        lines = (fresh_repo / ".vnx-attest" / "mandates.ndjson").read_text().strip().splitlines()
        assert len(lines) == 2
        revoke_entry = json.loads(lines[-1])
        assert revoke_entry["attestation_type"] == REVOKE_ATTESTATION_TYPE
        assert revoke_entry["mandate_id"] == "m-cli-2"


# ---------------------------------------------------------------------------
# Feature-flag predicate
# ---------------------------------------------------------------------------

class TestFeatureFlag:
    def test_default_is_off(self, monkeypatch):
        monkeypatch.delenv("VNX_SIGNED_DELEGATION", raising=False)
        assert is_signed_delegation_enabled() is False

    def test_explicit_on(self, monkeypatch):
        monkeypatch.setenv("VNX_SIGNED_DELEGATION", "1")
        assert is_signed_delegation_enabled() is True

    def test_garbage_is_off(self, monkeypatch):
        monkeypatch.setenv("VNX_SIGNED_DELEGATION", "yes")
        assert is_signed_delegation_enabled() is False


# ---------------------------------------------------------------------------
# Dispatch arg forwarding
# ---------------------------------------------------------------------------

class TestDispatchArgForwarding:
    def test_cheap_lane_argv_forwards_delegation_args(self):
        from subprocess_dispatch import _build_cheap_lane_argv, _ROLE_FALLBACK
        args = SimpleNamespace(
            terminal_id="T1",
            dispatch_id="D-1",
            instruction="do it",
            model="sonnet",
            role="backend-developer",
            max_retries=3,
            gate="",
            no_auto_commit=False,
            dispatch_paths="",
            pr_id=None,
            no_repo_map=False,
            approval_id="appr-X",
            mandate_id="m-X",
            session_id="sess-X",
            task_class="refactor",
        )
        argv = _build_cheap_lane_argv(args, "litellm:moonshot:kimi-k2")
        assert "--approval-id" in argv
        assert argv[argv.index("--approval-id") + 1] == "appr-X"
        assert "--mandate-id" in argv
        assert argv[argv.index("--mandate-id") + 1] == "m-X"
        assert "--session-id" in argv
        assert argv[argv.index("--session-id") + 1] == "sess-X"
        assert "--task-class" in argv
        assert argv[argv.index("--task-class") + 1] == "refactor"
