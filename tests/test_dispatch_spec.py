"""test_dispatch_spec.py — Tests for dispatch_spec.validate().

Covers every validate() rule with a failing-case assertion on the exact Reject.code,
plus the rule-6 design decision (instruction text containing spawn tokens still validates).
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from dispatch_spec import (  # noqa: E402
    DispatchPath,
    DispatchSpec,
    Isolation,
    PathAccess,
    Provider,
    Reject,
    ValidatedSpec,
    validate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_PROJECT_ID = "vnx-dev"  # matches VNX_PROJECT_ID default


def _write_instruction(tmp_path: Path, text: str = "Do the work.") -> Path:
    p = tmp_path / "instruction.md"
    p.write_text(text, encoding="utf-8")
    return p


def _valid_spec(instruction_file: Path, **overrides) -> DispatchSpec:
    defaults: dict = dict(
        schema_version=1,
        project_id=_VALID_PROJECT_ID,
        dispatch_id="20260615-test-dispatch",
        staging_id="20260615-test-staging",
        instruction_file=instruction_file,
        role="backend-developer",
        target_slot="T1",
        gate="human-promoted",
        dispatch_paths=(DispatchPath(PurePosixPath("scripts/lib/foo.py")),),
    )
    defaults.update(overrides)
    return DispatchSpec(**defaults)


def _do_validate(spec: DispatchSpec, monkeypatch, project_id: str = _VALID_PROJECT_ID) -> ValidatedSpec | Reject:
    monkeypatch.setenv("VNX_PROJECT_ID", project_id)
    return validate(spec, project_id=project_id, repo_root=Path("/fake/repo"))


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestValidSpecPasses:
    def test_valid_spec_returns_validated_spec(self, tmp_path, monkeypatch):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)
        assert result.spec is spec
        assert result.instruction_text == "Do the work."

    def test_validated_spec_contains_normalized_paths(self, tmp_path, monkeypatch):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, dispatch_paths=(
            DispatchPath(PurePosixPath("scripts/lib/foo.py"), PathAccess.WRITE),
        ))
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)
        assert len(result.normalized_paths) == 1
        assert str(result.normalized_paths[0].path) == "scripts/lib/foo.py"


# ---------------------------------------------------------------------------
# Rule 1 — schema_version
# ---------------------------------------------------------------------------

class TestRule1SchemaVersion:
    def test_rejects_schema_version_0(self, tmp_path, monkeypatch):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, schema_version=0)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "bad-schema"

    def test_rejects_schema_version_2(self, tmp_path, monkeypatch):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, schema_version=2)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "bad-schema"


# ---------------------------------------------------------------------------
# Rule 2 — project_id mismatch
# ---------------------------------------------------------------------------

class TestRule2ProjectMismatch:
    def test_rejects_wrong_project_id(self, tmp_path, monkeypatch):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, project_id="other-project")
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
        result = validate(spec, project_id="other-project", repo_root=Path("/fake"))
        # validate() resolves from env, spec.project_id=other-project != env vnx-dev
        assert isinstance(result, Reject)
        assert result.code == "project-mismatch"

    def test_accepts_matching_project_id(self, tmp_path, monkeypatch):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)


# ---------------------------------------------------------------------------
# Rule 3 — dispatch_id format
# ---------------------------------------------------------------------------

class TestRule3DispatchId:
    @pytest.mark.parametrize("bad_id", [
        "",
        "has spaces",
        "!invalid",
        "a" * 129,  # too long
        "-starts-with-dash",
    ])
    def test_rejects_bad_dispatch_id(self, tmp_path, monkeypatch, bad_id):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, dispatch_id=bad_id)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "bad-dispatch-id"

    def test_accepts_valid_dispatch_id(self, tmp_path, monkeypatch):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, dispatch_id="20260615-my.dispatch-001")
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)


# ---------------------------------------------------------------------------
# Rule 4 — staging_id format
# ---------------------------------------------------------------------------

class TestRule4StagingId:
    def test_rejects_bad_staging_id(self, tmp_path, monkeypatch):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, staging_id="!bad")
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "bad-staging-id"


# ---------------------------------------------------------------------------
# Rule 5 — instruction_file
# ---------------------------------------------------------------------------

class TestRule5InstructionFile:
    def test_rejects_relative_instruction_file(self, tmp_path, monkeypatch):
        spec = _valid_spec(Path("relative/path.md"))
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "instruction-unreadable"

    def test_rejects_nonexistent_instruction_file(self, tmp_path, monkeypatch):
        spec = _valid_spec(tmp_path / "missing.md")
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "instruction-unreadable"

    def test_rejects_symlink_instruction_file(self, tmp_path, monkeypatch):
        real = _write_instruction(tmp_path)
        link = tmp_path / "link.md"
        link.symlink_to(real)
        spec = _valid_spec(link)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "instruction-unreadable"

    def test_rejects_directory_as_instruction_file(self, tmp_path, monkeypatch):
        spec = _valid_spec(tmp_path)  # tmp_path is a directory
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "instruction-unreadable"

    def test_rejects_invalid_utf8_instruction_file(self, tmp_path, monkeypatch):
        """P1 (PR-4c): a non-UTF-8 instruction must Reject, not raise UnicodeDecodeError
        out of the door. The 'door never panics' invariant covers validation too."""
        ifile = tmp_path / "instruction.md"
        ifile.write_bytes(b"# Dispatch\n\xff\xfe invalid utf-8 \x80\x81\n")
        spec = _valid_spec(ifile)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "instruction-unreadable"


# ---------------------------------------------------------------------------
# Rule 6 — spawn tokens in instruction text MUST still validate (design decision)
# ---------------------------------------------------------------------------

class TestRule6NoSpawnScan:
    def test_instruction_with_spawn_tokens_still_validates(self, tmp_path, monkeypatch):
        """Rule 6: instruction text containing 'claude -p' or 'codex exec' must NOT be rejected.

        The file-reference design already neutralizes prompt injection; scanning text
        would falsely reject legitimate instructions that discuss CLI invocation patterns.
        """
        ifile = _write_instruction(
            tmp_path,
            text="Run: claude -p 'do something'. Also: codex exec --task foo.",
        )
        spec = _valid_spec(ifile)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec), (
            f"Expected ValidatedSpec but got Reject: {result}"
        )


# ---------------------------------------------------------------------------
# Rule 7 — role non-empty
# ---------------------------------------------------------------------------

class TestRule7Role:
    def test_rejects_empty_role(self, tmp_path, monkeypatch):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, role="")
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "bad-role"

    def test_rejects_whitespace_only_role(self, tmp_path, monkeypatch):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, role="   ")
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "bad-role"


# ---------------------------------------------------------------------------
# Rule 8 — target_slot
# ---------------------------------------------------------------------------

class TestRule8TargetSlot:
    @pytest.mark.parametrize("bad_slot", ["T4", "t1", "Worker", "", "0"])
    def test_rejects_bad_target_slot(self, tmp_path, monkeypatch, bad_slot):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, target_slot=bad_slot)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "bad-target-slot"

    @pytest.mark.parametrize("good_slot", ["T0", "T1", "T2", "T3"])
    def test_accepts_valid_target_slots(self, tmp_path, monkeypatch, good_slot):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, target_slot=good_slot)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)


# ---------------------------------------------------------------------------
# Rule 9 — model format
# ---------------------------------------------------------------------------

class TestRule9Model:
    def test_rejects_empty_model_string(self, tmp_path, monkeypatch):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, model="")
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "bad-model"

    def test_accepts_none_model(self, tmp_path, monkeypatch):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, model=None)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)

    def test_accepts_nonempty_model(self, tmp_path, monkeypatch):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, model="claude-sonnet-4-6")
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)


# ---------------------------------------------------------------------------
# Rule 10 — dispatch_paths structural validation
# ---------------------------------------------------------------------------

class TestRule10DispatchPaths:
    def test_rejects_absolute_path(self, tmp_path, monkeypatch):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, dispatch_paths=(
            DispatchPath(PurePosixPath("/etc/passwd")),
        ))
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "bad-path"

    def test_rejects_dotdot_component(self, tmp_path, monkeypatch):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, dispatch_paths=(
            DispatchPath(PurePosixPath("scripts/../../../etc/passwd")),
        ))
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "bad-path"

    def test_rejects_dotgit_prefix(self, tmp_path, monkeypatch):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, dispatch_paths=(
            DispatchPath(PurePosixPath(".git/config")),
        ))
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "bad-path"

    def test_rejects_vnx_data_prefix(self, tmp_path, monkeypatch):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, dispatch_paths=(
            DispatchPath(PurePosixPath(".vnx-data/state/foo.json")),
        ))
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "bad-path"

    def test_accepts_valid_paths(self, tmp_path, monkeypatch):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, dispatch_paths=(
            DispatchPath(PurePosixPath("scripts/lib/foo.py"), PathAccess.READ),
            DispatchPath(PurePosixPath("tests/test_foo.py"), PathAccess.WRITE),
        ))
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)
        assert len(result.normalized_paths) == 2


# ---------------------------------------------------------------------------
# instruction_sha256 in ValidatedSpec
# ---------------------------------------------------------------------------

class TestInstructionSha256:
    def test_validated_spec_has_sha256(self, tmp_path, monkeypatch):
        """validate() always populates instruction_sha256 in the returned ValidatedSpec."""
        import hashlib
        content = "Do the work."
        ifile = _write_instruction(tmp_path, text=content)
        spec = _valid_spec(ifile)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)
        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert result.instruction_sha256 == expected

    def test_dispatch_spec_sha256_mismatch_rejects(self, tmp_path, monkeypatch):
        """If DispatchSpec.instruction_sha256 is set and wrong, validate() returns Reject."""
        ifile = _write_instruction(tmp_path, text="Real content here.")
        spec = _valid_spec(ifile, instruction_sha256="0" * 64)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "instruction-hash-mismatch"

    def test_dispatch_spec_sha256_correct_passes(self, tmp_path, monkeypatch):
        """If DispatchSpec.instruction_sha256 matches the file content, validate() passes."""
        import hashlib
        content = "Real content here."
        ifile = _write_instruction(tmp_path, text=content)
        correct_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        spec = _valid_spec(ifile, instruction_sha256=correct_sha256)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)
        assert result.instruction_sha256 == correct_sha256

    def test_dispatch_spec_no_sha256_skips_verification(self, tmp_path, monkeypatch):
        """instruction_sha256=None on DispatchSpec means no pre-check; ValidatedSpec still gets sha."""
        ifile = _write_instruction(tmp_path, text="Do the work.")
        spec = _valid_spec(ifile, instruction_sha256=None)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)
        assert len(result.instruction_sha256) == 64  # sha256 hex digest is 64 chars


# ---------------------------------------------------------------------------
# Rule 11 — deadline_seconds bounds
# ---------------------------------------------------------------------------

class TestRule11Deadline:
    @pytest.mark.parametrize("bad_deadline", [0, 59, 14401, 99999])
    def test_rejects_out_of_range_deadline(self, tmp_path, monkeypatch, bad_deadline):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, deadline_seconds=bad_deadline)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "bad-deadline"

    @pytest.mark.parametrize("good_deadline", [60, 3600, 14400])
    def test_accepts_boundary_deadlines(self, tmp_path, monkeypatch, good_deadline):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, deadline_seconds=good_deadline)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)


# ---------------------------------------------------------------------------
# Rule 12 — headless opt-in (PR-5)
# ---------------------------------------------------------------------------

class TestHeadlessOptin:
    def test_allow_headless_requires_reason(self, tmp_path, monkeypatch):
        """allow_headless=True + headless_reason=None → Reject(headless-reason-required)."""
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, allow_headless=True, headless_reason=None)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "headless-reason-required"

    def test_allow_headless_empty_reason_rejected(self, tmp_path, monkeypatch):
        """allow_headless=True + whitespace-only reason → Reject(headless-reason-required)."""
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, allow_headless=True, headless_reason="   ")
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "headless-reason-required"

    def test_allow_headless_with_reason_passes(self, tmp_path, monkeypatch):
        """allow_headless=True + non-empty reason → ValidatedSpec."""
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, allow_headless=True, headless_reason="burst benchmark run")
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)

    def test_default_no_headless_passes(self, tmp_path, monkeypatch):
        """Default spec (allow_headless=False, headless_reason=None) → ValidatedSpec without extra check."""
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)

    def test_allow_headless_false_with_no_reason_passes(self, tmp_path, monkeypatch):
        """allow_headless=False + no reason → ValidatedSpec (rule only fires when allow_headless=True)."""
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, allow_headless=False, headless_reason=None)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)


# ---------------------------------------------------------------------------
# Rule 12 — headless opt-in: non-claude provider rejection (MED fix)
# ---------------------------------------------------------------------------

class TestHeadlessNonClaudeProvider:
    @pytest.mark.parametrize("non_claude_provider", [
        Provider.CODEX,
        Provider.GEMINI,
        Provider.KIMI,
        Provider.LITELLM_DEEPSEEK,
        Provider.LITELLM_ZAI,
    ])
    def test_non_claude_provider_with_allow_headless_rejected(self, tmp_path, monkeypatch, non_claude_provider):
        """allow_headless=True + non-claude provider → Reject(headless-claude-only)."""
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, allow_headless=True, headless_reason="benchmark", provider=non_claude_provider)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "headless-claude-only"

    def test_auto_provider_with_allow_headless_passes(self, tmp_path, monkeypatch):
        """allow_headless=True + provider=auto → ValidatedSpec (auto could resolve to claude)."""
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, allow_headless=True, headless_reason="benchmark", provider=Provider.AUTO)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)

    def test_claude_provider_with_allow_headless_passes(self, tmp_path, monkeypatch):
        """allow_headless=True + provider=claude + reason → ValidatedSpec."""
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, allow_headless=True, headless_reason="benchmark", provider=Provider.CLAUDE)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)

    def test_non_claude_with_allow_headless_false_passes(self, tmp_path, monkeypatch):
        """allow_headless=False + non-claude provider → ValidatedSpec (check only fires when True)."""
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, allow_headless=False, headless_reason=None, provider=Provider.CODEX)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)


# ---------------------------------------------------------------------------
# Rule 13 — track_id format (TL-D1)
# ---------------------------------------------------------------------------

class TestRule13TrackId:
    def test_default_none_passes(self, tmp_path, monkeypatch):
        """track_id defaults to None → ValidatedSpec (rule only fires when set)."""
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)
        assert result.spec.track_id is None

    def test_accepts_valid_track_id(self, tmp_path, monkeypatch):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, track_id="track-linkage-enforcement")
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)
        assert result.spec.track_id == "track-linkage-enforcement"

    @pytest.mark.parametrize("bad_track_id", [
        "",
        "   ",
        "has spaces",
        "!invalid",
        "a" * 129,  # too long
    ])
    def test_rejects_bad_track_id(self, tmp_path, monkeypatch, bad_track_id):
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, track_id=bad_track_id)
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, Reject)
        assert result.code == "bad-track-id"

    def test_existence_against_tracks_db_is_not_checked_here(self, tmp_path, monkeypatch):
        """validate() has no DB access — a well-formed but nonexistent track_id still
        passes format validation; existence is a dispatch_cli door rule."""
        ifile = _write_instruction(tmp_path)
        spec = _valid_spec(ifile, track_id="totally-nonexistent-track")
        result = _do_validate(spec, monkeypatch)
        assert isinstance(result, ValidatedSpec)
