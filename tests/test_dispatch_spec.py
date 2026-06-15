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
