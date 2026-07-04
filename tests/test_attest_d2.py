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
    read_allowed_signers_from_base,
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


# ---------------------------------------------------------------------------
# Fix 1: allowed_signers trust anchor — base branch resolution
# ---------------------------------------------------------------------------

class TestAllowedSignersTrustAnchor:
    """read_allowed_signers_from_base reads from the base ref, ignoring PR tree."""

    def _base_with_allowed_signers(self, tmp: Path, pub_line: str) -> str:
        """Init repo, add allowed_signers at base commit. Returns base SHA."""
        _init_repo(tmp)
        attest_dir = tmp / ATTEST_DIR
        attest_dir.mkdir(parents=True, exist_ok=True)
        (attest_dir / "allowed_signers").write_text(pub_line)
        subprocess.run(["git", "add", ".vnx-attest/"], cwd=str(tmp), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add: allowed_signers"], cwd=str(tmp), check=True, capture_output=True)
        return _head_sha(tmp)

    def test_reads_from_base_ref_not_working_tree(self, ephemeral_key_dir):
        """Working-tree mutation of allowed_signers does not affect base-ref resolution."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            identity = ephemeral_key_dir["identity"]
            pub = ephemeral_key_dir["key_path"].with_suffix(".pub").read_text().strip()
            base_sha = self._base_with_allowed_signers(repo, f"{identity} {pub}\n")

            _add_file_commit(repo, "feature.py", "x = 1\n", "feat: feature")

            # Overwrite working-tree allowed_signers with rogue content (uncommitted)
            (repo / ATTEST_DIR / "allowed_signers").write_text("rogue@attacker rogue-key-blob\n")

            content = read_allowed_signers_from_base(repo, base_sha)
            assert content is not None, "Expected to find allowed_signers at base ref"
            assert b"rogue" not in content, "Base-ref resolution must ignore working-tree mutation"
            assert identity.encode() in content

    def test_rogue_key_in_pr_tree_not_trusted(self, ephemeral_key_dir):
        """Rogue key committed to PR tree cannot self-verify against base-branch signers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            identity = ephemeral_key_dir["identity"]
            pub = ephemeral_key_dir["key_path"].with_suffix(".pub").read_text().strip()
            base_sha = self._base_with_allowed_signers(repo, f"{identity} {pub}\n")

            _add_file_commit(repo, "feature.py", "x = 1\n", "feat: feature")

            # Generate a rogue key
            with tempfile.TemporaryDirectory() as rogue_dir:
                rogue_key = Path(rogue_dir) / "rogue"
                subprocess.run(
                    ["ssh-keygen", "-t", "ed25519", "-f", str(rogue_key), "-N", ""],
                    check=True, capture_output=True,
                )
                rogue_pub = rogue_key.with_suffix(".pub").read_text().strip()
                rogue_identity = "rogue@attacker"

                # Sign an attest record with the rogue key
                write_attest_record(
                    dispatch_id="D-rogue",
                    deliverable_id="D2",
                    track_id="t",
                    plan_gate_ref="r",
                    signer_identity=rogue_identity,
                    timestamp="2026-07-04T12:00:00Z",
                    key_path=rogue_key,
                    repo_root=repo,
                    base_ref=base_sha,
                )

                # PR-tree: commit rogue key into allowed_signers
                (repo / ATTEST_DIR / "allowed_signers").write_text(f"{rogue_identity} {rogue_pub}\n")
                subprocess.run(["git", "add", ".vnx-attest/"], cwd=str(repo), check=True, capture_output=True)
                subprocess.run(
                    ["git", "commit", "-m", "attack: add rogue key"],
                    cwd=str(repo), check=True, capture_output=True,
                )

                # Base-branch resolution returns ORIGINAL allowed_signers (no rogue key)
                base_content = read_allowed_signers_from_base(repo, base_sha)
                assert base_content is not None
                assert b"rogue" not in base_content

                # Verify against base-branch allowed_signers → must FAIL
                tmp_as = Path(rogue_dir) / "base_as"
                tmp_as.write_bytes(base_content)
                ok, reason = verify_attest_record(
                    allowed_signers=tmp_as,
                    repo_root=repo,
                    base_ref=base_sha,
                )
                assert not ok, "Rogue-signed record must not verify against base-branch signers"
                assert "signature" in reason.lower()

    def test_valid_key_in_base_branch_verifies(self, ephemeral_key_dir):
        """Valid signature passes when allowed_signers is resolved from base branch."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            identity = ephemeral_key_dir["identity"]
            pub = ephemeral_key_dir["key_path"].with_suffix(".pub").read_text().strip()
            base_sha = self._base_with_allowed_signers(repo, f"{identity} {pub}\n")

            _add_file_commit(repo, "feature.py", "x = 1\n", "feat: feature")

            write_attest_record(
                dispatch_id="D-base-verify",
                deliverable_id="D2",
                track_id="t",
                plan_gate_ref="r",
                signer_identity=identity,
                timestamp="2026-07-04T12:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
                base_ref=base_sha,
            )

            base_content = read_allowed_signers_from_base(repo, base_sha)
            assert base_content is not None

            with tempfile.TemporaryDirectory() as td:
                tmp_as = Path(td) / "allowed_signers"
                tmp_as.write_bytes(base_content)
                ok, reason = verify_attest_record(
                    allowed_signers=tmp_as,
                    repo_root=repo,
                    base_ref=base_sha,
                )
                assert ok, f"Valid key from base branch should pass verify: {reason}"


# ---------------------------------------------------------------------------
# Fix 2: diff-determinism — pinned git config
# ---------------------------------------------------------------------------

class TestDiffDeterminism:
    """content-key is byte-identical regardless of local git diff config."""

    def test_identical_under_patience_algorithm(self):
        """Switching local diff.algorithm=patience does not change the content-key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "f.py", "def hello():\n    return 'world'\n", "feat: f")

            key_default = compute_diff_hash(repo_root=repo, base_ref=base_sha)

            subprocess.run(
                ["git", "config", "diff.algorithm", "patience"],
                cwd=str(repo), check=True, capture_output=True,
            )
            key_patience = compute_diff_hash(repo_root=repo, base_ref=base_sha)
            subprocess.run(
                ["git", "config", "--unset", "diff.algorithm"],
                cwd=str(repo), capture_output=True,
            )

            assert key_default == key_patience, (
                f"diff.algorithm=patience changed content-key: {key_default!r} != {key_patience!r}"
            )

    def test_identical_under_mnemonic_prefix(self):
        """Enabling mnemonicPrefix does not change the content-key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)
            _add_file_commit(repo, "f.py", "x = 42\n", "feat: f")

            key_before = compute_diff_hash(repo_root=repo, base_ref=base_sha)

            subprocess.run(
                ["git", "config", "diff.mnemonicPrefix", "true"],
                cwd=str(repo), check=True, capture_output=True,
            )
            key_after = compute_diff_hash(repo_root=repo, base_ref=base_sha)
            subprocess.run(
                ["git", "config", "--unset", "diff.mnemonicPrefix"],
                cwd=str(repo), capture_output=True,
            )

            assert key_before == key_after, (
                f"mnemonicPrefix change affected content-key: {key_before!r} != {key_after!r}"
            )
