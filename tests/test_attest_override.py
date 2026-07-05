"""Tests for D4: signed, budgeted, audited gate override.

Test filter: pytest -k "override or attest or verify_pr"

Covers:
  - A signed override for the exact diff verifies as 'override' (not 'pass')
  - An override for a different diff FAILS (diff-binding)
  - Budget-exhausted refuses write_override_record callers
  - count_overrides_in_window derived from the trail, not a mutable counter
  - Unsigned / rogue-key override FAILS signature check
  - Empty reason rejected at build time (ValueError)
  - verify_pr: valid override returns (0, ...) with "override" in message
  - verify_pr: override for wrong diff → FAIL (not override)
  - verify_pr: regular PASS still works after the override code path is added
"""
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from attest_override import (
    ATTEST_DIR,
    ATTESTATION_OVERRIDE,
    DEFAULT_OVERRIDE_BUDGET,
    OVERRIDE_RECORD_PREFIX,
    OVERRIDE_TRAIL_FILE,
    OVERRIDE_WINDOW_DAYS,
    OverrideRecord,
    build_override_manifest,
    count_overrides_in_window,
    get_override_budget,
    verify_override_record,
    write_override_record,
)
from verify_pr import verify_pr
from attest_record import write_attest_record


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def ephemeral_key_dir():
    """Ephemeral ed25519 test key + allowed_signers."""
    with tempfile.TemporaryDirectory() as tmpdir:
        key_path = Path(tmpdir) / "testkey"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", ""],
            check=True, capture_output=True,
        )
        pub = key_path.with_suffix(".pub").read_text().strip()
        identity = "vnx-override-test@local"
        allowed_signers = Path(tmpdir) / "allowed_signers"
        allowed_signers.write_text(f"{identity} {pub}\n")
        yield {
            "key_path": key_path,
            "identity": identity,
            "allowed_signers": allowed_signers,
        }


@pytest.fixture(scope="session")
def rogue_key_dir():
    """A second ed25519 key NOT in allowed_signers (used for forgery tests)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        key_path = Path(tmpdir) / "roguekey"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", ""],
            check=True, capture_output=True,
        )
        yield {"key_path": key_path, "identity": "rogue@attacker"}


def _init_repo(tmp: Path) -> Path:
    subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@vnx.local"], cwd=str(tmp), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "VNX Test"], cwd=str(tmp), check=True, capture_output=True)
    (tmp / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(tmp), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=str(tmp), check=True, capture_output=True)
    return tmp


def _head_sha(repo: Path) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    )
    return r.stdout.strip()


def _add_file_commit(repo: Path, filename: str, content: str, msg: str) -> None:
    full = repo / filename
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    subprocess.run(["git", "add", filename], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=str(repo), check=True, capture_output=True)


# ---------------------------------------------------------------------------
# Unit tests: build_override_manifest
# ---------------------------------------------------------------------------

class TestBuildOverrideManifest:
    def test_required_fields(self):
        m = build_override_manifest(
            content_key="abc123",
            reason="emergency hotfix for prod outage",
            dispatch_id="D-hotfix",
            signer_identity="vnx@local",
            timestamp="2026-07-05T10:00:00Z",
        )
        assert m["schema_version"] == "1"
        assert m["attestation_type"] == ATTESTATION_OVERRIDE
        assert m["content_key"] == "abc123"
        assert m["diff_hash"] == "abc123"
        assert m["reason"] == "emergency hotfix for prod outage"
        assert m["dispatch_id"] == "D-hotfix"
        assert m["signer_identity"] == "vnx@local"
        assert m["timestamp"] == "2026-07-05T10:00:00Z"

    def test_empty_reason_raises(self):
        with pytest.raises(ValueError, match="reason must be non-empty"):
            build_override_manifest(
                content_key="abc",
                reason="",
                dispatch_id="x",
                signer_identity="y",
                timestamp="2026-07-05T10:00:00Z",
            )

    def test_whitespace_only_reason_raises(self):
        with pytest.raises(ValueError):
            build_override_manifest(
                content_key="abc",
                reason="   ",
                dispatch_id="x",
                signer_identity="y",
                timestamp="2026-07-05T10:00:00Z",
            )

    def test_reason_is_stripped(self):
        m = build_override_manifest(
            content_key="abc",
            reason="  reason with spaces  ",
            dispatch_id="x",
            signer_identity="y",
            timestamp="2026-07-05T10:00:00Z",
        )
        assert m["reason"] == "reason with spaces"


# ---------------------------------------------------------------------------
# Unit tests: count_overrides_in_window
# ---------------------------------------------------------------------------

class TestCountOverridesInWindow:
    def test_empty_trail_returns_zero(self, tmp_path):
        trail = tmp_path / "override-trail.ndjson"
        assert count_overrides_in_window(trail) == 0

    def test_missing_trail_returns_zero(self, tmp_path):
        assert count_overrides_in_window(tmp_path / "nonexistent.ndjson") == 0

    def test_counts_recent_overrides(self, tmp_path):
        trail = tmp_path / "trail.ndjson"
        now = "2026-07-05T12:00:00Z"
        # Three recent override entries
        for i in range(3):
            entry = {
                "attestation_type": ATTESTATION_OVERRIDE,
                "timestamp": "2026-07-04T10:00:00Z",
                "content_key": f"key{i}",
            }
            with trail.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        assert count_overrides_in_window(trail, _now_ts=now) == 3

    def test_excludes_old_overrides(self, tmp_path):
        trail = tmp_path / "trail.ndjson"
        now = "2026-07-05T12:00:00Z"
        # Entry older than 30 days
        old_entry = {
            "attestation_type": ATTESTATION_OVERRIDE,
            "timestamp": "2026-05-01T10:00:00Z",
            "content_key": "old",
        }
        # Entry within 30 days
        recent_entry = {
            "attestation_type": ATTESTATION_OVERRIDE,
            "timestamp": "2026-07-04T10:00:00Z",
            "content_key": "recent",
        }
        with trail.open("a") as f:
            f.write(json.dumps(old_entry) + "\n")
            f.write(json.dumps(recent_entry) + "\n")
        assert count_overrides_in_window(trail, _now_ts=now) == 1

    def test_excludes_non_override_entries(self, tmp_path):
        trail = tmp_path / "trail.ndjson"
        now = "2026-07-05T12:00:00Z"
        entries = [
            {"attestation_type": "governed", "timestamp": "2026-07-04T10:00:00Z"},
            {"attestation_type": ATTESTATION_OVERRIDE, "timestamp": "2026-07-04T10:00:00Z"},
            {"attestation_type": "ad-hoc", "timestamp": "2026-07-04T10:00:00Z"},
        ]
        with trail.open("a") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        assert count_overrides_in_window(trail, _now_ts=now) == 1

    def test_ignores_invalid_json_lines(self, tmp_path):
        trail = tmp_path / "trail.ndjson"
        now = "2026-07-05T12:00:00Z"
        with trail.open("a") as f:
            f.write("not-json\n")
            f.write(json.dumps({
                "attestation_type": ATTESTATION_OVERRIDE,
                "timestamp": "2026-07-04T10:00:00Z",
            }) + "\n")
        assert count_overrides_in_window(trail, _now_ts=now) == 1


# ---------------------------------------------------------------------------
# Unit tests: get_override_budget
# ---------------------------------------------------------------------------

class TestGetOverrideBudget:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("VNX_ATTEST_OVERRIDE_BUDGET", raising=False)
        assert get_override_budget() == DEFAULT_OVERRIDE_BUDGET

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("VNX_ATTEST_OVERRIDE_BUDGET", "10")
        assert get_override_budget() == 10

    def test_invalid_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("VNX_ATTEST_OVERRIDE_BUDGET", "abc")
        assert get_override_budget() == DEFAULT_OVERRIDE_BUDGET


# ---------------------------------------------------------------------------
# Integration tests: write_override_record + verify_override_record
# ---------------------------------------------------------------------------

class TestWriteAndVerifyOverride:
    def test_roundtrip_exact_diff_passes(self, ephemeral_key_dir):
        """A signed override for the exact diff verifies as 'override'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "scripts/lib/feature.py", "x = 1\n", "feat: feature")

            from content_key import compute_diff_hash
            ck = compute_diff_hash(repo_root=repo, base_ref=base_sha)

            rec = write_override_record(
                content_key=ck,
                reason="approved by architect — prod incident",
                dispatch_id="D-override-test",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-05T10:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
            )

            assert rec.record_path.exists()
            assert rec.trail_path.exists()
            assert rec.manifest["attestation_type"] == ATTESTATION_OVERRIDE

            ok, reason, manifest = verify_override_record(
                allowed_signers=ephemeral_key_dir["allowed_signers"],
                repo_root=repo,
                base_ref=base_sha,
            )
            assert ok, f"Expected OK but got: {reason}"
            assert manifest is not None
            assert manifest["attestation_type"] == ATTESTATION_OVERRIDE
            assert manifest["content_key"] == ck

    def test_override_for_different_diff_fails(self, ephemeral_key_dir):
        """An override signed for diff A does not verify for diff B."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)

            # Write feature A and sign override for it
            _add_file_commit(repo, "scripts/lib/feature_a.py", "a = 1\n", "feat: A")
            from content_key import compute_diff_hash
            ck_a = compute_diff_hash(repo_root=repo, base_ref=base_sha)

            write_override_record(
                content_key=ck_a,
                reason="override for diff A",
                dispatch_id="D-override-a",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-05T10:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
            )

            # Now add more code — diff changes
            _add_file_commit(repo, "scripts/lib/feature_b.py", "b = 2\n", "feat: B")

            ok, reason, manifest = verify_override_record(
                allowed_signers=ephemeral_key_dir["allowed_signers"],
                repo_root=repo,
                base_ref=base_sha,
            )
            assert not ok, "Override for wrong diff should FAIL"
            assert manifest is None

    def test_rogue_key_override_fails(self, ephemeral_key_dir, rogue_key_dir):
        """An override signed with an unknown key fails signature check."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "scripts/lib/feature.py", "x = 1\n", "feat: feature")

            from content_key import compute_diff_hash
            ck = compute_diff_hash(repo_root=repo, base_ref=base_sha)

            # Sign with rogue key (not in allowed_signers)
            write_override_record(
                content_key=ck,
                reason="unauthorized override attempt",
                dispatch_id="D-rogue",
                signer_identity=rogue_key_dir["identity"],
                timestamp="2026-07-05T10:00:00Z",
                key_path=rogue_key_dir["key_path"],
                repo_root=repo,
            )

            ok, reason, manifest = verify_override_record(
                allowed_signers=ephemeral_key_dir["allowed_signers"],
                repo_root=repo,
                base_ref=base_sha,
            )
            assert not ok, f"Rogue-key override should FAIL but got ok=True"
            assert manifest is None

    def test_trail_is_appended(self, ephemeral_key_dir):
        """Each write_override_record appends one entry to the trail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "scripts/lib/f.py", "x = 1\n", "feat")
            from content_key import compute_diff_hash
            ck = compute_diff_hash(repo_root=repo, base_ref=base_sha)

            trail = repo / ATTEST_DIR / OVERRIDE_TRAIL_FILE
            assert not trail.exists()

            write_override_record(
                content_key=ck,
                reason="test trail append",
                dispatch_id="D-trail",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-05T10:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
            )

            assert trail.exists()
            lines = [l for l in trail.read_text().splitlines() if l.strip()]
            assert len(lines) == 1
            entry = json.loads(lines[0])
            assert entry["attestation_type"] == ATTESTATION_OVERRIDE

    def test_record_count_derived_from_trail(self, ephemeral_key_dir):
        """Override count comes from the trail; count is accurate after two writes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)

            # First override — diff A
            _add_file_commit(repo, "scripts/lib/a.py", "a = 1\n", "feat: a")
            from content_key import compute_diff_hash
            ck_a = compute_diff_hash(repo_root=repo, base_ref=base_sha)
            trail = repo / ATTEST_DIR / OVERRIDE_TRAIL_FILE

            write_override_record(
                content_key=ck_a,
                reason="first override",
                dispatch_id="D-1",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-04T10:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
            )

            count1 = count_overrides_in_window(trail, _now_ts="2026-07-05T12:00:00Z")
            assert count1 == 1

            # Second override — diff B (different content_key, same trail)
            _add_file_commit(repo, "scripts/lib/b.py", "b = 2\n", "feat: b")
            ck_b = compute_diff_hash(repo_root=repo, base_ref=base_sha)

            write_override_record(
                content_key=ck_b,
                reason="second override",
                dispatch_id="D-2",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-04T11:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
            )

            count2 = count_overrides_in_window(trail, _now_ts="2026-07-05T12:00:00Z")
            assert count2 == 2


# ---------------------------------------------------------------------------
# Budget enforcement tests (check before write — caller responsibility)
# ---------------------------------------------------------------------------

class TestBudgetEnforcement:
    def test_budget_count_exhausted_scenario(self, ephemeral_key_dir, monkeypatch):
        """Budget logic: used >= budget should block the caller from writing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            trail = repo / ATTEST_DIR / OVERRIDE_TRAIL_FILE

            # Force budget to 2 for this test
            monkeypatch.setenv("VNX_ATTEST_OVERRIDE_BUDGET", "2")

            from content_key import compute_diff_hash

            now_ts = "2026-07-05T12:00:00Z"

            # Write two overrides to fill the budget
            for i in range(2):
                _add_file_commit(repo, f"scripts/lib/feat{i}.py", f"x={i}\n", f"feat: {i}")
                ck = compute_diff_hash(repo_root=repo, base_ref=base_sha)
                write_override_record(
                    content_key=ck,
                    reason=f"override {i}",
                    dispatch_id=f"D-budget-{i}",
                    signer_identity=ephemeral_key_dir["identity"],
                    timestamp="2026-07-04T10:00:00Z",
                    key_path=ephemeral_key_dir["key_path"],
                    repo_root=repo,
                )

            budget = get_override_budget()
            used = count_overrides_in_window(trail, _now_ts=now_ts)
            assert used >= budget, "Budget should be exhausted"

    def test_old_overrides_do_not_count(self, ephemeral_key_dir):
        """Overrides older than the window do not count against the budget."""
        with tempfile.TemporaryDirectory() as tmpdir:
            trail = Path(tmpdir) / "trail.ndjson"
            # Write 10 entries older than 30 days
            now_ts = "2026-07-05T12:00:00Z"
            for i in range(10):
                entry = {
                    "attestation_type": ATTESTATION_OVERRIDE,
                    "timestamp": "2026-05-01T10:00:00Z",
                    "content_key": f"old{i}",
                }
                with trail.open("a") as f:
                    f.write(json.dumps(entry) + "\n")

            count = count_overrides_in_window(trail, _now_ts=now_ts)
            assert count == 0, "Old overrides must not count against the budget"


# ---------------------------------------------------------------------------
# Integration tests: verify_pr with override
# ---------------------------------------------------------------------------

class TestVerifyPRWithOverride:
    def test_valid_override_returns_override_verdict(self, ephemeral_key_dir):
        """verify_pr returns (0, ...'override'...) when a valid override exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "scripts/lib/feature.py", "x = 1\n", "feat: feature")

            from content_key import compute_diff_hash
            ck = compute_diff_hash(repo_root=repo, base_ref=base_sha)

            write_override_record(
                content_key=ck,
                reason="prod incident — SLA breach imminent",
                dispatch_id="D-vpr-override",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-05T10:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
            )

            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=ephemeral_key_dir["allowed_signers"],
            )
            assert exit_code == 0, f"Expected override PASS but got: {message}"
            assert "override" in message.lower(), (
                f"Message should say 'override', got: {message!r}"
            )
            assert "PASS" not in message, (
                f"Override verdict must not say 'PASS' (must be distinct): {message!r}"
            )

    def test_override_for_wrong_diff_fails(self, ephemeral_key_dir):
        """verify_pr fails when the override was signed for a different diff."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)

            # Sign override for diff A
            _add_file_commit(repo, "scripts/lib/a.py", "a = 1\n", "feat: a")
            from content_key import compute_diff_hash
            ck_a = compute_diff_hash(repo_root=repo, base_ref=base_sha)
            write_override_record(
                content_key=ck_a,
                reason="override for diff A only",
                dispatch_id="D-wrong-diff",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-05T10:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
            )

            # Extend the branch — diff changes
            _add_file_commit(repo, "scripts/lib/b.py", "b = 2\n", "feat: b")

            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=ephemeral_key_dir["allowed_signers"],
            )
            assert exit_code == 1, f"Expected FAIL (wrong diff) but got {exit_code}: {message}"
            assert "FAIL" in message

    def test_rogue_key_override_fails_in_verify_pr(self, ephemeral_key_dir, rogue_key_dir):
        """verify_pr rejects an override signed by a key not in allowed_signers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "scripts/lib/attack.py", "evil = 1\n", "feat: attack")

            from content_key import compute_diff_hash
            ck = compute_diff_hash(repo_root=repo, base_ref=base_sha)

            # Sign with rogue key
            write_override_record(
                content_key=ck,
                reason="unauthorized override",
                dispatch_id="D-rogue",
                signer_identity=rogue_key_dir["identity"],
                timestamp="2026-07-05T10:00:00Z",
                key_path=rogue_key_dir["key_path"],
                repo_root=repo,
            )

            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=ephemeral_key_dir["allowed_signers"],
            )
            assert exit_code == 1, f"Rogue override should FAIL, got {exit_code}: {message}"

    def test_regular_pass_still_works(self, ephemeral_key_dir):
        """The regular PASS path still works after the override code path was added."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "scripts/lib/feature.py", "x = 1\n", "feat")

            write_attest_record(
                dispatch_id="D-regular-pass",
                deliverable_id="D4",
                track_id="governance-attribution-enforce",
                plan_gate_ref="gate-ref",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-05T10:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
                base_ref=base_sha,
            )

            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=ephemeral_key_dir["allowed_signers"],
            )
            assert exit_code == 0, f"Regular PASS should still work, got: {message}"
            assert "PASS" in message
            assert "override" not in message.lower()

    def test_no_attest_no_override_fails(self, ephemeral_key_dir):
        """A feature PR with neither attest nor override FAILS."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "vnx_cli/main.py", "# code\n", "feat: code")

            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=ephemeral_key_dir["allowed_signers"],
            )
            assert exit_code == 1
            assert "FAIL" in message
