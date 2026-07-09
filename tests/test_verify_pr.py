"""Tests for D3: verify_pr helper — server-side attestation gate logic.

Test filter: pytest -k "verify_pr or gate"

Covers:
  - classify_pr: feature vs exempt vs empty
  - verify_pr: validly-signed+diff-bound attest PASSES
  - verify_pr: manifest for a different diff FAILS (diff-binding)
  - verify_pr: PR-tree allowed_signers is IGNORED (base-branch used)
  - verify_pr: unsigned feature PR FAILS
  - verify_pr: docs-only PR is EXEMPT (exit 0)
  - verify_pr: tests-only PR is EXEMPT
  - verify_pr: *.md-only PR is EXEMPT
  - verify_pr: mixed feature+docs triggers gate
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from verify_pr import classify_pr, verify_pr, _is_feature_file, _is_exempt_file
from attest_record import ATTEST_DIR, write_attest_record
from content_key import compute_diff_hash
from evidence_bound_gate import (
    emit_evidence_attestation,
    EVIDENCE_TEST_PASS,
    EVIDENCE_TIER1_GATE_PASS,
    EVIDENCE_TIER2_PANEL_PASS,
)


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
    """Init a git repo in tmp with a base commit."""
    subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@vnx.local"], cwd=str(tmp), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "VNX Test"], cwd=str(tmp), check=True, capture_output=True)
    (tmp / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(tmp), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=str(tmp), check=True, capture_output=True)
    return tmp


def _head_sha(repo: Path) -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _add_file_commit(repo: Path, filename: str, content: str, msg: str) -> None:
    # Support sub-paths
    full = repo / filename
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    subprocess.run(["git", "add", filename], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=str(repo), check=True, capture_output=True)


# ---------------------------------------------------------------------------
# Unit tests: classify_pr
# ---------------------------------------------------------------------------

class TestClassifyPR:
    def test_feature_scripts(self):
        assert classify_pr(["scripts/lib/foo.py"]) == "feature"

    def test_feature_vnx_cli(self):
        assert classify_pr(["vnx_cli/main.py", "vnx_cli/commands/attest.py"]) == "feature"

    def test_feature_dashboard(self):
        assert classify_pr(["dashboard/api_config.py"]) == "feature"

    def test_feature_schemas(self):
        assert classify_pr(["schemas/quality_intelligence.sql"]) == "feature"

    def test_feature_vnx_dir(self):
        assert classify_pr([".vnx/governance_enforcement.yaml"]) == "feature"

    def test_feature_github_dir(self):
        assert classify_pr([".github/workflows/vnx-ci.yml"]) == "feature"

    def test_exempt_docs_only(self):
        assert classify_pr(["docs/README.md", "docs/core/ARCH.md"]) == "exempt"

    def test_exempt_tests_only(self):
        assert classify_pr(["tests/test_foo.py", "tests/conftest.py"]) == "exempt"

    def test_exempt_md_only(self):
        assert classify_pr(["CHANGELOG.md", "README.md"]) == "exempt"

    def test_exempt_mixed_docs_tests(self):
        assert classify_pr(["docs/foo.md", "tests/test_bar.py"]) == "exempt"

    def test_exempt_empty(self):
        assert classify_pr([]) == "empty"

    def test_feature_mixed_feature_and_docs(self):
        # feature file + docs file → feature (gate fires)
        assert classify_pr(["scripts/lib/foo.py", "docs/ARCH.md"]) == "feature"

    def test_non_exempt_non_feature_treated_as_feature(self):
        # A file outside both feature and exempt prefixes (e.g. setup.py) → feature
        result = classify_pr(["setup.py"])
        assert result == "feature"

    def test_new_top_level_dir_requires_attestation(self):
        # Fail-closed: a new top-level directory is not in the exempt allowlist → feature
        assert classify_pr(["newdir/somefile.py"]) == "feature"

    def test_unclassified_build_file_requires_attestation(self):
        # Fail-closed: pyproject.toml is not in the exempt allowlist → feature
        assert classify_pr(["pyproject.toml"]) == "feature"


class TestPathHelpers:
    def test_is_feature_file_scripts(self):
        assert _is_feature_file("scripts/lib/foo.py")

    def test_is_feature_file_github(self):
        assert _is_feature_file(".github/workflows/foo.yml")

    def test_is_feature_file_not_matching(self):
        assert not _is_feature_file("docs/ARCH.md")

    def test_is_exempt_docs(self):
        assert _is_exempt_file("docs/ARCH.md")

    def test_is_exempt_tests(self):
        assert _is_exempt_file("tests/test_foo.py")

    def test_is_exempt_md(self):
        assert _is_exempt_file("CHANGELOG.md")

    def test_is_exempt_not_matching(self):
        assert not _is_exempt_file("scripts/lib/foo.py")


# ---------------------------------------------------------------------------
# Integration tests: verify_pr
# ---------------------------------------------------------------------------

class TestVerifyPRPass:
    def test_valid_attest_passes(self, ephemeral_key_dir):
        """A validly-signed, diff-bound attest record passes verify_pr."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)

            _add_file_commit(repo, "scripts/lib/feature.py", "x = 1\n", "feat: feature")

            write_attest_record(
                dispatch_id="D-vpr-pass",
                deliverable_id="D3",
                track_id="governance-attribution-enforce",
                plan_gate_ref="gate-ref",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-04T12:00:00Z",
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
            assert exit_code == 0, f"Expected PASS but got: {message}"
            assert "PASS" in message


class TestVerifyPRFail:
    def test_manifest_for_different_diff_fails(self, ephemeral_key_dir):
        """A manifest signed for a different diff fails diff-binding check."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)

            # Write feature code and sign it
            _add_file_commit(repo, "scripts/lib/track_a.py", "a = 1\n", "feat: track A")
            write_attest_record(
                dispatch_id="D-track-a",
                deliverable_id="D1",
                track_id="track-a",
                plan_gate_ref="gate-a",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-04T12:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
                base_ref=base_sha,
            )

            # Add more feature code — the diff changes, old record no longer covers it
            _add_file_commit(repo, "scripts/lib/track_b.py", "b = 2\n", "feat: track B")

            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=ephemeral_key_dir["allowed_signers"],
            )
            assert exit_code == 1, f"Expected FAIL (diff-binding) but got {exit_code}: {message}"
            assert "FAIL" in message

    def test_unsigned_feature_pr_fails(self):
        """Feature PR with no attest record fails verify_pr."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)

            _add_file_commit(repo, "vnx_cli/main.py", "# feature\n", "feat: cli change")

            # No attest record written
            empty_as = Path(tmpdir) / "empty_allowed_signers"
            empty_as.write_text("")

            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=empty_as,
            )
            assert exit_code == 1, f"Expected FAIL but got {exit_code}: {message}"
            assert "FAIL" in message

    def test_wrong_allowed_signers_fails(self, ephemeral_key_dir):
        """A valid attest record fails if allowed_signers does not contain the key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)

            _add_file_commit(repo, "scripts/lib/feature.py", "x = 1\n", "feat: feature")

            write_attest_record(
                dispatch_id="D-wrong-key",
                deliverable_id="D3",
                track_id="t",
                plan_gate_ref="r",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-04T12:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
                base_ref=base_sha,
            )

            empty_as = Path(tmpdir) / "empty_allowed_signers"
            empty_as.write_text("")

            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=empty_as,
            )
            assert exit_code == 1, f"Expected FAIL but got {exit_code}: {message}"
            assert "FAIL" in message


class TestVerifyPRExempt:
    def test_docs_only_pr_is_exempt(self, ephemeral_key_dir):
        """A PR touching only docs/ files is exempt — exit 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)

            _add_file_commit(repo, "docs/ARCH.md", "# arch\n", "docs: update arch")

            empty_as = Path(tmpdir) / "empty_allowed_signers"
            empty_as.write_text("")

            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=empty_as,
            )
            assert exit_code == 0, f"Expected exempt (0) but got {exit_code}: {message}"
            assert "exempt" in message

    def test_tests_only_pr_is_exempt(self, ephemeral_key_dir):
        """A PR touching only tests/ files is exempt — exit 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)

            _add_file_commit(repo, "tests/test_new.py", "# test\n", "test: add test")

            empty_as = Path(tmpdir) / "empty_allowed_signers"
            empty_as.write_text("")

            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=empty_as,
            )
            assert exit_code == 0, f"Expected exempt (0) but got {exit_code}: {message}"
            assert "exempt" in message

    def test_md_only_pr_is_exempt(self):
        """A PR touching only *.md files is exempt — exit 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)

            _add_file_commit(repo, "CHANGELOG.md", "# change\n", "docs: changelog")

            empty_as = Path(tmpdir) / "empty_allowed_signers"
            empty_as.write_text("")

            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=empty_as,
            )
            assert exit_code == 0, f"Expected exempt (0) but got {exit_code}: {message}"
            assert "exempt" in message


class TestPRTreeAllowedSignersIgnored:
    """PR-tree allowed_signers is IGNORED — base-branch is used.

    When allowed_signers_override is not set, verify_pr calls
    read_allowed_signers_from_base (base-ref resolution).
    This test simulates the trust-anchor property directly: even if the
    working tree has a rogue allowed_signers, the base-ref copy is used.
    """

    def test_rogue_key_in_pr_tree_not_self_verifying(self, ephemeral_key_dir):
        """A rogue key committed to .vnx-attest/allowed_signers in the PR cannot self-verify."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))

            # Set up base branch with the legitimate key
            identity = ephemeral_key_dir["identity"]
            pub = ephemeral_key_dir["key_path"].with_suffix(".pub").read_text().strip()
            attest_dir = repo / ATTEST_DIR
            attest_dir.mkdir(parents=True, exist_ok=True)
            (attest_dir / "allowed_signers").write_text(f"{identity} {pub}\n")
            subprocess.run(["git", "add", ATTEST_DIR], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "add: allowed_signers"], cwd=str(repo), check=True, capture_output=True)
            base_sha = _head_sha(repo)

            # PR: add feature code
            _add_file_commit(repo, "scripts/lib/attack.py", "evil = 1\n", "feat: attack")

            # PR tree: generate a rogue key and commit it into allowed_signers
            rogue_dir = Path(tmpdir) / "rogue_keys"
            rogue_dir.mkdir()
            rogue_key = rogue_dir / "rogue"
            subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-f", str(rogue_key), "-N", ""],
                check=True, capture_output=True,
            )
            rogue_pub = rogue_key.with_suffix(".pub").read_text().strip()
            rogue_identity = "rogue@attacker"

            # Sign attest with rogue key
            write_attest_record(
                dispatch_id="D-attack",
                deliverable_id="D3",
                track_id="attack",
                plan_gate_ref="none",
                signer_identity=rogue_identity,
                timestamp="2026-07-04T12:00:00Z",
                key_path=rogue_key,
                repo_root=repo,
                base_ref=base_sha,
            )

            # Commit rogue key into PR-tree allowed_signers (self-authorization attempt)
            (attest_dir / "allowed_signers").write_text(f"{rogue_identity} {rogue_pub}\n")
            subprocess.run(["git", "add", ATTEST_DIR], cwd=str(repo), check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "attack: add rogue key to allowed_signers"],
                cwd=str(repo), check=True, capture_output=True,
            )

            # verify_pr without override uses read_allowed_signers_from_base →
            # base_sha:.vnx-attest/allowed_signers contains only the LEGITIMATE key
            # → rogue-signed record must FAIL
            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
            )
            assert exit_code == 1, (
                f"Rogue self-authorization should fail but got exit_code={exit_code}: {message}"
            )
            assert "FAIL" in message


# ---------------------------------------------------------------------------
# Integration tests: evidence-bound gate (D3 bootstrap)
# ---------------------------------------------------------------------------

def _write_evidence(repo: Path, content_key: str, evidence_type: str, key_dir: dict, track_id: str = "evidence-bound") -> None:
    """Append a signed, diff-bound evidence entry to .vnx-attest/governed.ndjson."""
    emit_evidence_attestation(
        evidence_type=evidence_type,
        content_key=content_key,
        dispatch_id=f"D-ev-{evidence_type}",
        track_id=track_id,
        signer_identity=key_dir["identity"],
        timestamp="2026-07-09T12:00:00Z",
        key_path=key_dir["key_path"],
        repo_root=repo,
    )


def _setup_feature_branch_with_attest(
    tmpdir: str,
    ephemeral_key_dir: dict,
    *,
    task_class: "str | None" = None,
) -> tuple[Path, str, str]:
    """Create a repo with a feature commit and a signed attestation record.

    Returns (repo, base_sha, content_key).
    """
    repo = _init_repo(Path(tmpdir))
    base_sha = _head_sha(repo)

    _add_file_commit(repo, "scripts/lib/feature.py", "x = 1\n", "feat: feature")
    content_key = compute_diff_hash(repo_root=repo, base_ref=base_sha, head_ref="HEAD")

    write_attest_record(
        dispatch_id="D-evidence-bound",
        deliverable_id="D3",
        track_id="evidence-bound-gate",
        plan_gate_ref="gate-ref",
        signer_identity=ephemeral_key_dir["identity"],
        timestamp="2026-07-09T12:00:00Z",
        key_path=ephemeral_key_dir["key_path"],
        repo_root=repo,
        base_ref=base_sha,
        task_class=task_class,
    )
    return repo, base_sha, content_key


class TestEvidenceBoundGate:
    """VNX_EVIDENCE_BOUND_GATE off/advisory/required bootstrap behaviour."""

    def test_required_mode_blocks_missing_evidence(
        self, ephemeral_key_dir, monkeypatch, capsys,
    ):
        monkeypatch.setenv("VNX_EVIDENCE_BOUND_GATE", "required")
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, base_sha, _content_key = _setup_feature_branch_with_attest(
                tmpdir, ephemeral_key_dir, task_class="implementation"
            )

            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=ephemeral_key_dir["allowed_signers"],
            )
            assert exit_code == 1, f"Expected FAIL (required missing evidence) but got {exit_code}: {message}"
            assert "evidence-bound gate required" in message.lower()
            assert "test_pass" in message.lower() or "tier1_gate_pass" in message.lower()

    def test_advisory_mode_allows_missing_evidence_with_log(
        self, ephemeral_key_dir, monkeypatch, capsys,
    ):
        monkeypatch.setenv("VNX_EVIDENCE_BOUND_GATE", "advisory")
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, base_sha, _content_key = _setup_feature_branch_with_attest(
                tmpdir, ephemeral_key_dir, task_class="implementation"
            )

            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=ephemeral_key_dir["allowed_signers"],
            )
            captured = capsys.readouterr()
            assert exit_code == 0, f"Expected PASS/advisory but got {exit_code}: {message}"
            assert "PASS" in message
            assert "[evidence-bound advisory]" in captured.err
            assert "test_pass" in captured.err.lower() or "tier1_gate_pass" in captured.err.lower()

    def test_default_advisory_allows_missing_evidence(
        self, ephemeral_key_dir, monkeypatch, capsys,
    ):
        """With no env set, the gate defaults to advisory and never blocks."""
        monkeypatch.delenv("VNX_EVIDENCE_BOUND_GATE", raising=False)
        monkeypatch.delenv("VNX_OVERRIDE_EVIDENCE_BOUND_GATE", raising=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, base_sha, _content_key = _setup_feature_branch_with_attest(
                tmpdir, ephemeral_key_dir, task_class="implementation"
            )

            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=ephemeral_key_dir["allowed_signers"],
            )
            captured = capsys.readouterr()
            assert exit_code == 0, f"Expected default-advisory PASS but got {exit_code}: {message}"
            assert "[evidence-bound advisory]" in captured.err

    def test_off_mode_skips_evidence_check(
        self, ephemeral_key_dir, monkeypatch, capsys,
    ):
        monkeypatch.setenv("VNX_EVIDENCE_BOUND_GATE", "off")
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, base_sha, _content_key = _setup_feature_branch_with_attest(
                tmpdir, ephemeral_key_dir, task_class="implementation"
            )

            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=ephemeral_key_dir["allowed_signers"],
            )
            captured = capsys.readouterr()
            assert exit_code == 0, f"Expected PASS but got {exit_code}: {message}"
            assert "evidence-bound advisory" not in captured.err.lower()

    def test_required_mode_passes_with_valid_evidence(
        self, ephemeral_key_dir, monkeypatch, capsys,
    ):
        monkeypatch.setenv("VNX_EVIDENCE_BOUND_GATE", "required")
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, base_sha, content_key = _setup_feature_branch_with_attest(
                tmpdir, ephemeral_key_dir, task_class="implementation"
            )
            _write_evidence(repo, content_key, EVIDENCE_TEST_PASS, ephemeral_key_dir)
            _write_evidence(repo, content_key, EVIDENCE_TIER1_GATE_PASS, ephemeral_key_dir)

            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=ephemeral_key_dir["allowed_signers"],
            )
            assert exit_code == 0, f"Expected PASS with evidence but got {exit_code}: {message}"
            assert "PASS" in message
            assert "evidence-bound gate required" not in message.lower()

    def test_closeout_requires_tier2_panel_evidence(
        self, ephemeral_key_dir, monkeypatch, capsys,
    ):
        monkeypatch.setenv("VNX_EVIDENCE_BOUND_GATE", "required")
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, base_sha, content_key = _setup_feature_branch_with_attest(
                tmpdir, ephemeral_key_dir, task_class="closeout"
            )
            _write_evidence(repo, content_key, EVIDENCE_TEST_PASS, ephemeral_key_dir)
            _write_evidence(repo, content_key, EVIDENCE_TIER1_GATE_PASS, ephemeral_key_dir)

            # Missing Tier-2 panel evidence should block closeout.
            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=ephemeral_key_dir["allowed_signers"],
            )
            captured = capsys.readouterr()
            assert exit_code == 1, f"Expected FAIL (missing panel evidence) but got {exit_code}: {message}"
            assert "tier2_panel_pass" in captured.err or "tier2_panel_pass" in message

            # Add the panel evidence and verify the gate now passes.
            _write_evidence(repo, content_key, EVIDENCE_TIER2_PANEL_PASS, ephemeral_key_dir)
            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=ephemeral_key_dir["allowed_signers"],
            )
            assert exit_code == 0, f"Expected PASS after panel evidence but got {exit_code}: {message}"

    def test_stale_wrong_diff_evidence_does_not_satisfy_bound(
        self, ephemeral_key_dir, monkeypatch, capsys,
    ):
        """A test receipt from an older green run must not cover a new diff."""
        monkeypatch.setenv("VNX_EVIDENCE_BOUND_GATE", "required")
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _init_repo(Path(tmpdir))
            base_sha = _head_sha(repo)

            _add_file_commit(repo, "scripts/lib/feature.py", "x = 1\n", "feat: v1")
            old_content_key = compute_diff_hash(
                repo_root=repo, base_ref=base_sha, head_ref="HEAD"
            )
            # Evidence bound to the OLD diff.
            _write_evidence(repo, old_content_key, EVIDENCE_TEST_PASS, ephemeral_key_dir)
            _write_evidence(repo, old_content_key, EVIDENCE_TIER1_GATE_PASS, ephemeral_key_dir)

            # Add more feature code — the diff (and content-key) changes.
            _add_file_commit(repo, "scripts/lib/feature.py", "x = 2\n", "feat: v2")

            # Attest the NEW diff.
            write_attest_record(
                dispatch_id="D-evidence-bound-stale",
                deliverable_id="D3",
                track_id="evidence-bound-gate",
                plan_gate_ref="gate-ref",
                signer_identity=ephemeral_key_dir["identity"],
                timestamp="2026-07-09T12:00:00Z",
                key_path=ephemeral_key_dir["key_path"],
                repo_root=repo,
                base_ref=base_sha,
                task_class="implementation",
            )

            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=ephemeral_key_dir["allowed_signers"],
            )
            assert exit_code == 1, f"Expected FAIL (stale evidence) but got {exit_code}: {message}"
            assert "evidence-bound gate required" in message.lower()
            assert "missing evidence" in message.lower()

    def test_invalid_signature_evidence_is_rejected(
        self, ephemeral_key_dir, monkeypatch, capsys,
    ):
        monkeypatch.setenv("VNX_EVIDENCE_BOUND_GATE", "required")
        with tempfile.TemporaryDirectory() as tmpdir:
            repo, base_sha, content_key = _setup_feature_branch_with_attest(
                tmpdir, ephemeral_key_dir, task_class="implementation"
            )
            # Evidence signed with the legitimate key is accepted.
            _write_evidence(repo, content_key, EVIDENCE_TEST_PASS, ephemeral_key_dir)
            _write_evidence(repo, content_key, EVIDENCE_TIER1_GATE_PASS, ephemeral_key_dir)

            # Corrupt the Tier-1 evidence signature.
            ledger = repo / ".vnx-attest" / "governed.ndjson"
            lines = ledger.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                entry = json.loads(line)
                if entry.get("evidence_type") == EVIDENCE_TIER1_GATE_PASS:
                    entry["signature"] = entry["signature"][:-8] + "deadbeef"
                    lines[i] = json.dumps(entry)
                    break
            ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

            exit_code, message = verify_pr(
                repo_root=repo,
                base_ref=base_sha,
                head_ref="HEAD",
                allowed_signers_override=ephemeral_key_dir["allowed_signers"],
            )
            assert exit_code == 1, f"Expected FAIL (invalid evidence signature) but got {exit_code}: {message}"
            assert "evidence-bound gate required" in message.lower()
            assert "invalid signature" in message.lower()
