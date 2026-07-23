#!/usr/bin/env python3
"""Tests for `vnx init` CLI command (A-11 PR-1 scaffold).

Validates the .claude/ skeleton, local .vnx-data/ layout, .vnx-version pin,
root CLAUDE.md, FEATURE_PLAN.md, and safety/idempotency semantics.
"""

import argparse
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vnx_cli.commands.init_cmd import vnx_init
from vnx_cli import __version__
from vnx_cli._engine import resolve_data_root


def _args(tmp_path, **overrides):
    ns = argparse.Namespace(
        project_path=None,
        project_dir=str(tmp_path),
        project_id=None,
        template="default",
        force=False,
        non_interactive=False,
        set_version=None,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


class TestVnxInitCli:
    def test_init_creates_claude_dir(self, tmp_path):
        rc = vnx_init(_args(tmp_path))
        assert rc == 0
        assert (tmp_path / ".claude" / "terminals" / "T0" / "CLAUDE.md").is_file()
        assert (tmp_path / ".claude" / "skills").is_dir()
        assert (tmp_path / ".claude" / "settings.json").is_file()

    def test_init_creates_vnx_data_dir(self, tmp_path, monkeypatch):
        # Force the data root to be project-local so we can assert local layout.
        local_data = tmp_path / ".vnx-data"
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(local_data))

        rc = vnx_init(_args(tmp_path))
        assert rc == 0
        assert local_data.is_dir()
        assert (local_data / "dispatches" / "pending").is_dir()
        assert (local_data / "dispatches" / "active").is_dir()
        assert (local_data / "dispatches" / "completed").is_dir()
        assert (local_data / "events").is_dir()
        assert (local_data / "unified_reports").is_dir()

    def test_init_no_local_vnx_data_when_xdg(self, tmp_path, tmp_path_factory, monkeypatch):
        # When data_root is outside the project dir, vnx init must NOT create
        # a local .vnx-data/ — that would cause the resolver to prefer the
        # local dir on the next call, contradicting the config (PR-PIP-2).
        external_data = tmp_path_factory.mktemp("external_data")
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(external_data))

        rc = vnx_init(_args(tmp_path))
        assert rc == 0
        assert not (tmp_path / ".vnx-data").exists(), (
            "Local .vnx-data must not be created when data root is outside the project"
        )

    def test_init_reported_path_matches_resolver(self, tmp_path, tmp_path_factory, monkeypatch):
        # Core consistency invariant: what init writes into config.yml must
        # equal what resolve_data_root returns post-init (no XDG-vs-local drift).
        external_data = tmp_path_factory.mktemp("external_data")
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_DATA_DIR", str(external_data))

        rc = vnx_init(_args(tmp_path))
        assert rc == 0

        config_text = (tmp_path / ".vnx" / "config.yml").read_text()
        config_data_dir = None
        for line in config_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("vnx_data_dir:"):
                config_data_dir = Path(stripped.split('"')[1]).resolve()
                break
        assert config_data_dir is not None, "config.yml must contain vnx_data_dir"

        resolved = resolve_data_root(tmp_path)
        assert config_data_dir == resolved, (
            f"Path drift: init configured {config_data_dir!r} "
            f"but resolver returned {resolved!r}"
        )

    def test_init_writes_vnx_version(self, tmp_path):
        rc = vnx_init(_args(tmp_path))
        assert rc == 0
        version_file = tmp_path / ".vnx-version"
        assert version_file.is_file()
        assert version_file.read_text().strip() == __version__

    def test_init_idempotent_with_force(self, tmp_path):
        vnx_init(_args(tmp_path))
        original = (tmp_path / "CLAUDE.md").read_text()
        (tmp_path / "CLAUDE.md").write_text("modified")

        rc = vnx_init(_args(tmp_path, force=True))
        assert rc == 0
        assert (tmp_path / "CLAUDE.md").read_text() == original

    def test_init_aborts_on_existing_without_force(self, tmp_path):
        vnx_init(_args(tmp_path))
        rc = vnx_init(_args(tmp_path))
        assert rc != 0

    def test_init_minimal_template(self, tmp_path):
        rc = vnx_init(_args(tmp_path, template="minimal"))
        assert rc == 0
        assert (tmp_path / ".claude" / "terminals" / "T0" / "CLAUDE.md").is_file()
        assert (tmp_path / ".vnx-version").is_file()

    def test_init_force_preserves_existing_pin(self, tmp_path):
        # The direct cause of the fleet version-pin drift incident: a fleet-lift
        # running `vnx init --force` per project must NOT reset an operator's
        # pin back to the running package version.
        vnx_init(_args(tmp_path))
        version_file = tmp_path / ".vnx-version"
        version_file.write_text("v1.3.0\n")

        rc = vnx_init(_args(tmp_path, force=True))
        assert rc == 0
        assert version_file.read_text().strip() == "v1.3.0"

    def test_init_force_without_set_version_never_writes_running_version(self, tmp_path):
        vnx_init(_args(tmp_path))
        version_file = tmp_path / ".vnx-version"
        pinned = "v1.3.0"
        assert pinned != __version__, "test fixture must pin a version other than the running one"
        version_file.write_text(pinned + "\n")

        rc = vnx_init(_args(tmp_path, force=True))
        assert rc == 0
        assert version_file.read_text().strip() == pinned
        assert version_file.read_text().strip() != __version__

    def test_init_set_version_repins_existing(self, tmp_path):
        vnx_init(_args(tmp_path))
        version_file = tmp_path / ".vnx-version"
        version_file.write_text("v1.3.0\n")

        rc = vnx_init(_args(tmp_path, force=True, set_version="v1.4.0"))
        assert rc == 0
        assert version_file.read_text().strip() == "v1.4.0"

    def test_init_set_version_on_fresh_init(self, tmp_path):
        rc = vnx_init(_args(tmp_path, set_version="v1.4.0"))
        assert rc == 0
        version_file = tmp_path / ".vnx-version"
        assert version_file.read_text().strip() == "v1.4.0"

    def test_init_set_version_rejects_invalid_format(self, tmp_path):
        rc = vnx_init(_args(tmp_path, set_version="not a valid pin"))
        assert rc != 0
        assert not (tmp_path / ".vnx-version").exists()

    def test_init_set_version_without_force_on_existing_still_aborts(self, tmp_path):
        # --set-version does not bypass the existing-init safety gate; an
        # operator repinning an already-initialised project still needs
        # --force too (consistent with every other scaffold file).
        vnx_init(_args(tmp_path))
        rc = vnx_init(_args(tmp_path, set_version="v1.4.0"))
        assert rc != 0
        assert (tmp_path / ".vnx-version").read_text().strip() == __version__

    def test_init_project_path_positional(self, tmp_path):
        ns = argparse.Namespace(
            project_path=str(tmp_path),
            project_dir=".",
            project_id=None,
            template="default",
            force=False,
            non_interactive=False,
        )
        rc = vnx_init(ns)
        assert rc == 0
        assert (tmp_path / ".vnx-version").is_file()


class TestVnxInitAtomicWriteSafety:
    """Symlink-TOCTOU regression tests for the atomic temp-write path."""

    def test_init_ignores_pre_planted_vnx_version_tmp_symlink(self, tmp_path, tmp_path_factory):
        """A pre-existing .vnx-version.tmp symlink must not be followed or
        truncated; the real .vnx-version is still written atomically.
        """
        outside = tmp_path_factory.mktemp("outside") / "target.txt"
        outside.write_text("do-not-touch\n")

        planted = tmp_path / ".vnx-version.tmp"
        planted.symlink_to(outside)

        rc = vnx_init(_args(tmp_path))
        assert rc == 0

        # The symlink target outside the repo must be untouched.
        assert outside.read_text() == "do-not-touch\n"

        # The actual pin file must exist and contain the current version.
        version_file = tmp_path / ".vnx-version"
        assert version_file.is_file()
        assert version_file.read_text().strip() == __version__
        assert not version_file.is_symlink()

        # The planted symlink may be left behind; it must not be the pin.
        if planted.exists():
            assert planted.resolve() != version_file.resolve()


class TestVnxInitSymlinkHardening:
    """init-scaffold-symlink-hardening: every scaffold write is atomic +
    symlink-safe (refuses escaping parent symlinks and symlink targets)."""

    def test_escaping_parent_symlink_refused(self, tmp_path, tmp_path_factory):
        # agents/ is a pre-planted symlink pointing OUTSIDE the project root:
        # the agents/ scaffold writes must be refused, nothing lands outside.
        outside = tmp_path_factory.mktemp("outside")
        (tmp_path / "agents").symlink_to(outside, target_is_directory=True)

        rc = vnx_init(_args(tmp_path))
        assert rc != 0
        assert not (outside / "README.md").exists()
        assert not (outside / "CLAUDE.md.template").exists()

    def test_escaping_parent_symlink_error_names_component(
        self, tmp_path, tmp_path_factory, capsys
    ):
        outside = tmp_path_factory.mktemp("outside")
        (tmp_path / "agents").symlink_to(outside, target_is_directory=True)

        rc = vnx_init(_args(tmp_path))
        assert rc != 0
        err = capsys.readouterr().err
        assert "symlink" in err
        assert "agents" in err

    def test_broken_symlink_at_profiles_path_refused(self, tmp_path, tmp_path_factory):
        # A pre-planted (broken) symlink AT a scaffold target must be refused,
        # never followed — the escape target must not be created.
        outside_target = tmp_path_factory.mktemp("outside") / "profiles.yaml"
        vnx_dir = tmp_path / ".vnx"
        vnx_dir.mkdir()
        (vnx_dir / "governance_profiles.yaml").symlink_to(outside_target)

        rc = vnx_init(_args(tmp_path))
        assert rc != 0
        assert not outside_target.exists()

    def test_atomic_write_refuses_symlink_target_directly(self, tmp_path):
        from vnx_cli.commands import init_cmd

        outside = tmp_path / "outside.txt"
        outside.write_text("do-not-touch\n")
        link = tmp_path / "target.txt"
        link.symlink_to(outside)

        with pytest.raises(OSError, match="symlink"):
            init_cmd._atomic_write(link, "payload\n", tmp_path)

        # The symlink target is untouched and the link itself is left in place.
        assert outside.read_text() == "do-not-touch\n"
        assert link.is_symlink()

    def test_atomic_write_refuses_path_outside_root(self, tmp_path, tmp_path_factory):
        from vnx_cli.commands import init_cmd

        outside = tmp_path_factory.mktemp("outside") / "file.txt"
        with pytest.raises(OSError, match="outside project root"):
            init_cmd._atomic_write(outside, "payload\n", tmp_path)
        assert not outside.exists()

    def test_raw_callsites_routed_through_atomic_write(self, tmp_path, monkeypatch):
        # The three previously-raw writes (profiles / agents README /
        # agents CLAUDE.md.template) must go through the atomic helper.
        from vnx_cli.commands import init_cmd

        written: list[Path] = []
        orig = init_cmd._atomic_write

        def spy(path, content, root):
            written.append(Path(path))
            return orig(path, content, root)

        monkeypatch.setattr(init_cmd, "_atomic_write", spy)

        rc = vnx_init(_args(tmp_path))
        assert rc == 0

        profiles = tmp_path / ".vnx" / "governance_profiles.yaml"
        agents_readme = tmp_path / "agents" / "README.md"
        agents_claude = tmp_path / "agents" / "CLAUDE.md.template"
        assert profiles in written
        assert agents_readme in written
        assert agents_claude in written

        # Content is unchanged from the scaffold constants.
        assert profiles.read_text() == init_cmd.GOVERNANCE_PROFILES_YAML
        assert agents_readme.read_text() == init_cmd.AGENTS_README
        assert agents_claude.read_text() == init_cmd.CLAUDE_MD_TEMPLATE

    def test_full_scaffold_no_regression(self, tmp_path):
        # Normal (non-symlink) init still scaffolds every file correctly.
        rc = vnx_init(_args(tmp_path))
        assert rc == 0
        expected_files = [
            ".vnx-project-id",
            ".vnx/governance_profiles.yaml",
            ".vnx/config.yml",
            ".vnx-version",
            "agents/README.md",
            "agents/CLAUDE.md.template",
            ".claude/terminals/T0/CLAUDE.md",
            ".claude/settings.json",
            "CLAUDE.md",
            "FEATURE_PLAN.md",
            ".gitignore",
            "CODEOWNERS",
            ".vnx-attest/allowed_signers",
            ".vnx-attest/README.md",
            ".github/workflows/attestation-gate.yml",
        ]
        for rel in expected_files:
            assert (tmp_path / rel).is_file(), f"missing scaffold file: {rel}"


class TestVnxInitAttestDelivery:
    """D5a + D5b: attestation trust-root and gate workflow delivery."""

    def test_attest_dir_created(self, tmp_path):
        rc = vnx_init(_args(tmp_path))
        assert rc == 0
        assert (tmp_path / ".vnx-attest").is_dir()

    def test_allowed_signers_scaffold_created(self, tmp_path):
        rc = vnx_init(_args(tmp_path))
        assert rc == 0
        signers = tmp_path / ".vnx-attest" / "allowed_signers"
        assert signers.is_file()
        content = signers.read_text()
        assert "KEY_PROVISIONING.md" in content

    def test_attest_readme_created(self, tmp_path):
        rc = vnx_init(_args(tmp_path))
        assert rc == 0
        readme = tmp_path / ".vnx-attest" / "README.md"
        assert readme.is_file()
        assert "allowed_signers" in readme.read_text()

    def test_no_signing_key_generated(self, tmp_path):
        rc = vnx_init(_args(tmp_path))
        assert rc == 0
        attest_dir = tmp_path / ".vnx-attest"
        for entry in attest_dir.iterdir():
            name = entry.name
            assert not (name.endswith("_sk") or name.endswith("_ed25519")), (
                f"Private key must never be generated by init: {name}"
            )
            assert entry.suffix not in {".pem", ".key"}, (
                f"Private key must never be generated by init: {name}"
            )

    def test_codeowners_has_attest_entries(self, tmp_path):
        rc = vnx_init(_args(tmp_path))
        assert rc == 0
        codeowners = tmp_path / "CODEOWNERS"
        assert codeowners.is_file()
        content = codeowners.read_text()
        assert ".vnx-attest/allowed_signers" in content
        assert ".github/workflows/attestation-gate.yml" in content
        assert ".vnx/governance_enforcement.yaml" in content

    def test_codeowners_no_duplicate_on_reinit(self, tmp_path):
        vnx_init(_args(tmp_path))
        rc = vnx_init(_args(tmp_path, force=True))
        assert rc == 0
        content = (tmp_path / "CODEOWNERS").read_text()
        count = content.count(".vnx-attest/allowed_signers")
        assert count == 1, f"CODEOWNERS entry duplicated: found {count} occurrences"

    def test_codeowners_appended_to_existing(self, tmp_path):
        existing = "# Existing CODEOWNERS\n*.py @dev-team\n"
        (tmp_path / "CODEOWNERS").write_text(existing)
        rc = vnx_init(_args(tmp_path))
        assert rc == 0
        content = (tmp_path / "CODEOWNERS").read_text()
        assert "*.py @dev-team" in content
        assert ".vnx-attest/allowed_signers" in content

    def test_gate_workflow_delivered(self, tmp_path):
        rc = vnx_init(_args(tmp_path))
        assert rc == 0
        workflow = tmp_path / ".github" / "workflows" / "attestation-gate.yml"
        assert workflow.is_file()
        content = workflow.read_text()
        assert "Attestation Gate" in content

    def test_gate_workflow_not_clobbered_on_reinit(self, tmp_path):
        vnx_init(_args(tmp_path))
        workflow = tmp_path / ".github" / "workflows" / "attestation-gate.yml"
        workflow.write_text("# custom workflow content")
        rc = vnx_init(_args(tmp_path, force=True))
        assert rc == 0
        assert workflow.read_text() == "# custom workflow content", (
            "Re-init must not clobber an existing workflow"
        )
