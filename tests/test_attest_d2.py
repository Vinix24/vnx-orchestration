"""Tests for D2: content-key, attest_record, diff-binding.

Test filter: pytest -k "attest or content_key or diff_bind"

Covers:
  - Content-key stable across simulated squash (same tree → same key)
  - Content-key changes for different diffs
  - Rebase (amend) re-derives the same key
  - Writing .vnx-attest/ files excluded from content-key computation
  - write_attest_record produces a valid JSON record
  - Manifest contains diff_hash for diff-binding
  - verify round-trip passes against test allowed_signers
  - verify fails when no record exists
  - verify fails when diff changes after writing record (diff-binding)
  - verify fails with wrong allowed_signers
  - Forged manifest (track A → track B slot) fails diff-binding check
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from content_key import compute_content_key, compute_diff_hash
from attest_record import (
    ATTEST_DIR,
    AttestRecord,
    build_attest_manifest,
    verify_attest_record,
    write_attest_record,
)
from attestation import build_governed_manifest, sign_manifest


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
        identity = "vnx-test@local"
        allowed_signers = Path(tmpdir) / "allowed_signers"
        allowed_signers.write_text(f"{identity} {pub}\n")
        yield {
            "key_path": key_path,
            "identity": identity,
            "allowed_signers": allowed_signers,
        }


def _init_repo(tmp: Path) -> Path:
    """Init a git repo in tmp with a base commit (the merge-base)."""
    subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@vnx.local"], cwd=str(tmp), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "VNX Test"], cwd=str(tmp), check=True, capture_output=True)
    (tmp / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(tmp), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=str(tmp), check=True, capture_output=True)
    return tmp


def _add_file_commit(repo: Path, filename: str, content: str, msg: str) -> None:
    (repo / filename).write_text(content)
    subprocess.run(["git", "add", filename], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=str(repo), check=True, capture_output=True)


def _head_sha(repo: Path) -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True)
    return r.stdout.strip()


# ---------------------------------------------------------------------------
# content_key tests
# ---------------------------------------------------------------------------

class TestContentKey:
    def test_content_key_stable_across_squash(self):
        """Same code change → same content-key after squashing N commits to 1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)

            _add_file_commit(repo, "feature.py", "print('hello')\n", "feat: step 1")
            _add_file_commit(repo, "feature.py", "print('hello world')\n", "feat: step 2")
            key_multi = compute_diff_hash(repo_root=repo, base_ref=base_sha)

            # Simulate squash: reset to base, create one commit with the same final state
            subprocess.run(["git", "reset", "--soft", base_sha], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "feat: squashed"], cwd=str(repo), check=True, capture_output=True)
            key_squash = compute_diff_hash(repo_root=repo, base_ref=base_sha)

            assert key_multi == key_squash, (
                f"Squash changed content-key: {key_multi!r} != {key_squash!r}"
            )

    def test_content_key_rebase_same_key(self):
        """Amending commit message (rebase-like) re-derives the same content-key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)

            _add_file_commit(repo, "rebase_test.py", "# rebase test\n", "feat: rebase test")
            key_before = compute_diff_hash(repo_root=repo, base_ref=base_sha)

            # Amend commit message — same tree, different commit SHA
            subprocess.run(
                ["git", "commit", "--amend", "-m", "feat: rebase test (amended)"],
                cwd=str(repo), check=True, capture_output=True,
            )
            key_after = compute_diff_hash(repo_root=repo, base_ref=base_sha)

            assert key_before == key_after, (
                f"Amend changed content-key: {key_before!r} != {key_after!r}"
            )

    def test_content_key_differs_for_different_diffs(self):
        """Different code changes produce different content-keys."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)

            _add_file_commit(repo, "track_a.py", "# track A\n", "feat: track A")
            key_a = compute_diff_hash(repo_root=repo, base_ref=base_sha)

            subprocess.run(["git", "reset", "--hard", base_sha], cwd=str(repo), check=True, capture_output=True)
            _add_file_commit(repo, "track_b.py", "# track B\n", "feat: track B")
            key_b = compute_diff_hash(repo_root=repo, base_ref=base_sha)

            assert key_a != key_b

    def test_content_key_excludes_attest_dir(self):
        """Writing .vnx-attest/ files does not change the content-key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)

            _add_file_commit(repo, "myfeature.py", "x = 1\n", "feat: my feature")
            key_before = compute_diff_hash(repo_root=repo, base_ref=base_sha)

            # Commit an attest-dir file — should not affect the content-key
            attest_dir = repo / ATTEST_DIR
            attest_dir.mkdir(parents=True, exist_ok=True)
            (attest_dir / "fake_record.json").write_text('{"attest": true}\n')
            subprocess.run(["git", "add", ".vnx-attest/"], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "chore: attest record"], cwd=str(repo), check=True, capture_output=True)

            key_after = compute_diff_hash(repo_root=repo, base_ref=base_sha)
            assert key_before == key_after, (
                f"Writing .vnx-attest/ changed content-key: {key_before!r} != {key_after!r}"
            )

    def test_compute_content_key_alias(self):
        """compute_content_key is an alias for compute_diff_hash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "f.py", "x=1\n", "feat: f")
            assert compute_content_key(repo_root=repo, base_ref=base_sha) == \
                   compute_diff_hash(repo_root=repo, base_ref=base_sha)


# ---------------------------------------------------------------------------
# attest_record — write tests
# ---------------------------------------------------------------------------

class TestWriteAttestRecord:
    def test_write_creates_record_file(self, ephemeral_key_dir):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "code.py", "x = 1\n", "feat: code")

            rec = write_attest_record(
                dispatch_id="D-test-d2",
                deliverable_id="D2",
                track_id="governance-attribution-enforce",
                plan_gate_ref="gate-pass-ref",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-04T12:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
                base_ref=base_sha,
            )

            assert isinstance(rec, AttestRecord)
            assert rec.record_path.exists()
            assert rec.record_path.parent.name == ATTEST_DIR
            assert rec.record_path.name == f"{rec.content_key}.json"

    def test_record_file_is_valid_json(self, ephemeral_key_dir):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "code.py", "x = 1\n", "feat: code")

            rec = write_attest_record(
                dispatch_id="D-json-check",
                deliverable_id="D2",
                track_id="t",
                plan_gate_ref="r",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-04T00:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
                base_ref=base_sha,
            )

            loaded = json.loads(rec.record_path.read_text())
            assert loaded["diff_hash"] == rec.diff_hash
            assert loaded["dispatch_id"] == "D-json-check"

    def test_manifest_contains_diff_hash(self, ephemeral_key_dir):
        """Manifest embeds diff_hash so signature covers the diff binding."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "code.py", "x = 1\n", "feat: code")

            rec = write_attest_record(
                dispatch_id="D-diff-hash",
                deliverable_id="D2",
                track_id="t",
                plan_gate_ref="r",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-04T00:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
                base_ref=base_sha,
            )

            assert rec.manifest.get("diff_hash") == rec.diff_hash
            assert rec.manifest.get("diff_hash") == rec.content_key

    def test_unsigned_record_without_key(self):
        """key_path=None writes an unsigned record."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "code.py", "x = 1\n", "feat: code")

            rec = write_attest_record(
                dispatch_id="D-unsigned",
                deliverable_id="D2",
                track_id="t",
                plan_gate_ref="r",
                signer_identity="unsigned@local",
                timestamp="2026-07-04T00:00:00Z",
                key_path=None,
                repo_root=repo,
                base_ref=base_sha,
            )

            assert rec.record_path.exists()
            assert "signature" not in rec.manifest

    def test_content_key_is_diff_hash(self, ephemeral_key_dir):
        """content_key == diff_hash (they are the same value)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "code.py", "x = 1\n", "feat: code")

            rec = write_attest_record(
                dispatch_id="D-key-eq",
                deliverable_id="D2",
                track_id="t",
                plan_gate_ref="r",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-04T00:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
                base_ref=base_sha,
            )

            assert rec.content_key == rec.diff_hash


# ---------------------------------------------------------------------------
# verify_attest_record tests
# ---------------------------------------------------------------------------

class TestVerifyAttestRecord:
    def test_verify_round_trip_passes(self, ephemeral_key_dir):
        """write then verify succeeds against the test allowed_signers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "code.py", "x = 1\n", "feat: code")

            write_attest_record(
                dispatch_id="D-verify-rt",
                deliverable_id="D2",
                track_id="t",
                plan_gate_ref="r",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-04T00:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
                base_ref=base_sha,
            )

            ok, reason = verify_attest_record(
                allowed_signers=ephemeral_key_dir["allowed_signers"],
                repo_root=repo,
                base_ref=base_sha,
            )
            assert ok, f"verify failed: {reason}"
            assert reason == "ok"

    def test_verify_fails_when_no_record(self, ephemeral_key_dir):
        """verify returns False gracefully when no attest record exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "code.py", "x = 1\n", "feat: code")

            ok, reason = verify_attest_record(
                allowed_signers=ephemeral_key_dir["allowed_signers"],
                repo_root=repo,
                base_ref=base_sha,
            )
            assert not ok
            assert "no attest record" in reason

    def test_verify_diff_binding_fails_after_diff_change(self, ephemeral_key_dir):
        """Adding code after writing the record means the record no longer covers the diff."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)

            _add_file_commit(repo, "track_a.py", "# track A\n", "feat: track A")
            write_attest_record(
                dispatch_id="D-track-a",
                deliverable_id="D1",
                track_id="track-a",
                plan_gate_ref="gate-a",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-04T00:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
                base_ref=base_sha,
            )

            # Change the diff — old record no longer binds to the new diff
            _add_file_commit(repo, "extra.py", "# extra change\n", "feat: extra")

            ok, reason = verify_attest_record(
                allowed_signers=ephemeral_key_dir["allowed_signers"],
                repo_root=repo,
                base_ref=base_sha,
            )
            assert not ok
            # Either "no attest record" (new key has no file) is the correct failure
            assert "no attest record" in reason or "diff-binding" in reason

    def test_verify_fails_wrong_allowed_signers(self, ephemeral_key_dir):
        """verify fails when the signer key is not in allowed_signers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "code.py", "x = 1\n", "feat: code")

            write_attest_record(
                dispatch_id="D-wrong-signer",
                deliverable_id="D2",
                track_id="t",
                plan_gate_ref="r",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-04T00:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
                base_ref=base_sha,
            )

            empty_as = Path(tmpdir) / "empty_allowed_signers"
            empty_as.write_text("")

            ok, reason = verify_attest_record(
                allowed_signers=empty_as,
                repo_root=repo,
                base_ref=base_sha,
            )
            assert not ok
            assert "signature" in reason.lower() or "fail" in reason.lower()

    def test_verify_unsigned_record_fails_sig_check(self):
        """Unsigned record fails signature verification."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "code.py", "x = 1\n", "feat: code")

            write_attest_record(
                dispatch_id="D-unsigned-verify",
                deliverable_id="D2",
                track_id="t",
                plan_gate_ref="r",
                signer_identity="unsigned@local",
                timestamp="2026-07-04T00:00:00Z",
                key_path=None,
                repo_root=repo,
                base_ref=base_sha,
            )

            empty_as = Path(tmpdir) / "empty_allowed_signers"
            empty_as.write_text("")

            ok, reason = verify_attest_record(
                allowed_signers=empty_as,
                repo_root=repo,
                base_ref=base_sha,
            )
            assert not ok
            assert "signature" in reason.lower()


# ---------------------------------------------------------------------------
# diff-binding specific tests
# ---------------------------------------------------------------------------

class TestDiffBinding:
    def test_diff_bind_forged_manifest_fails(self, ephemeral_key_dir):
        """Forging: placing track A manifest at track B's content-key slot fails verify."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)

            # Build and sign a manifest for track A's diff
            _add_file_commit(repo, "feature_a.py", "a = 1\n", "feat: A")
            key_a = compute_diff_hash(repo_root=repo, base_ref=base_sha)

            m_a = build_governed_manifest(
                dispatch_id="D-a",
                deliverable_id="D1",
                track_id="track-a",
                plan_gate_ref="gate-a",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-04T00:00:00Z",
            )
            m_a["diff_hash"] = key_a
            m_a_signed = sign_manifest(m_a, ephemeral_key_dir["key_path"])

            # Extend the diff to simulate "track B" code landing on top
            _add_file_commit(repo, "feature_b.py", "b = 2\n", "feat: B")
            key_b = compute_diff_hash(repo_root=repo, base_ref=base_sha)

            assert key_a != key_b, "Tracks A and B must produce different content-keys"

            # Forge: write track A's manifest at track B's content-key slot
            attest_dir = repo / ATTEST_DIR
            attest_dir.mkdir(parents=True, exist_ok=True)
            forged_path = attest_dir / f"{key_b}.json"
            forged_path.write_text(json.dumps(m_a_signed, sort_keys=True, indent=2))

            # Verify must fail: manifest.diff_hash (key_a) != current diff (key_b)
            ok, reason = verify_attest_record(
                allowed_signers=ephemeral_key_dir["allowed_signers"],
                repo_root=repo,
                base_ref=base_sha,
            )
            assert not ok, "diff-binding should prevent reuse of track A manifest for track B"
            assert "diff-binding" in reason

    def test_diff_bind_record_survives_attest_commit(self, ephemeral_key_dir):
        """The attest record commit itself does not break verify (attest excluded from key)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "code.py", "x = 1\n", "feat: code")

            rec = write_attest_record(
                dispatch_id="D-attest-commit",
                deliverable_id="D2",
                track_id="t",
                plan_gate_ref="r",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-04T00:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
                base_ref=base_sha,
            )

            # Commit the attest record itself
            subprocess.run(["git", "add", ".vnx-attest/"], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "chore: attest record"], cwd=str(repo), check=True, capture_output=True)

            # Verify should still pass: .vnx-attest/ excluded from key computation
            ok, reason = verify_attest_record(
                allowed_signers=ephemeral_key_dir["allowed_signers"],
                repo_root=repo,
                base_ref=base_sha,
            )
            assert ok, f"verify failed after committing attest record: {reason}"
