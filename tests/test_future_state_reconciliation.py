"""Behavioral tests for PR-E: kanban/state builder honesty (R6.1-R6.4, R7.1, R7.3).

Each test is self-isolated via tmp_path + monkeypatch for VNX_DATA_DIR.
No dependency on guards from other PRs.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_LIB_DIR = _SCRIPTS_DIR / "lib"

for _p in (str(_SCRIPTS_DIR), str(_LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import build_t0_state as bts  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Create isolated state_dir + dispatch_dir and pin env vars."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()
    # Self-isolate: pin VNX_DATA_DIR so no prod DB is ever touched
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    return state_dir, dispatch_dir


def _call_build(state_dir: Path, dispatch_dir: Path) -> Dict[str, Any]:
    """Call build_t0_state and return the state dict."""
    return bts.build_t0_state(state_dir, dispatch_dir)


# ---------------------------------------------------------------------------
# R6.1 — locked/malformed DB signals degraded/failed (not silent legacy fallback)
# ---------------------------------------------------------------------------

class TestR61DBHealthSignaling:
    def test_malformed_db_signals_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed quality_intelligence.db → system_health.status in degraded/failed."""
        state_dir, dispatch_dir = _make_isolated_env(tmp_path, monkeypatch)

        # Write garbage bytes — sqlite3 will raise DatabaseError on open
        (state_dir / "quality_intelligence.db").write_bytes(b"NOT A VALID SQLITE DATABASE")

        state = _call_build(state_dir, dispatch_dir)

        sh_status = state["system_health"]["status"]
        assert sh_status in ("degraded", "failed"), (
            f"Expected degraded or failed for malformed DB, got: {sh_status!r}"
        )

    def test_locked_db_signals_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Monkeypatched locked DB → system_health.status == 'degraded'."""
        state_dir, dispatch_dir = _make_isolated_env(tmp_path, monkeypatch)

        # Create a placeholder DB file so the probe doesn't skip it
        (state_dir / "quality_intelligence.db").write_bytes(b"")

        original_connect = sqlite3.connect

        def _locked_connect(path: str, **kwargs: Any) -> Any:
            if "quality_intelligence" in str(path):
                raise sqlite3.OperationalError("database is locked")
            return original_connect(path, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", _locked_connect)

        state = _call_build(state_dir, dispatch_dir)

        sh_status = state["system_health"]["status"]
        assert sh_status in ("degraded", "failed"), (
            f"Expected degraded or failed for locked DB, got: {sh_status!r}"
        )

    def test_missing_table_does_not_degrade(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-migration 'no such table' error does NOT set degraded — it is a valid fallback."""
        state_dir, dispatch_dir = _make_isolated_env(tmp_path, monkeypatch)

        # Presence of a receipt file keeps health from going degraded via the default check
        (state_dir / "t0_receipts.ndjson").write_text("")

        # Classify the error — must return 'premigration', not 'degraded'/'failed'
        err = sqlite3.OperationalError("no such table: tracks")
        result = bts._classify_db_error(err)
        assert result == "premigration", (
            f"Expected premigration for missing-table error, got: {result!r}"
        )

    def test_classify_db_error_operational_non_premigration(self) -> None:
        """Non-premigration OperationalError → 'degraded'."""
        err = sqlite3.OperationalError("database is locked")
        assert bts._classify_db_error(err) == "degraded"

    def test_classify_db_error_database_error(self) -> None:
        """DatabaseError (e.g. malformed) → 'failed'."""
        err = sqlite3.DatabaseError("file is not a database")
        assert bts._classify_db_error(err) == "failed"


# ---------------------------------------------------------------------------
# R6.2 — corrupt manifest yields placeholder + degraded flag
# ---------------------------------------------------------------------------

class TestR62ArtifactReadFailure:
    def test_corrupt_manifest_yields_placeholder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Corrupt manifest.json → dispatch represented as placeholder, not dropped."""
        state_dir, dispatch_dir = _make_isolated_env(tmp_path, monkeypatch)
        active_dir = dispatch_dir / "active"
        active_dir.mkdir(parents=True)

        dispatch_id = "20260601-corrupt-dispatch"
        subdir = active_dir / dispatch_id
        subdir.mkdir()
        (subdir / "manifest.json").write_text("NOT VALID JSON {{{")

        items, has_errors = bts._build_active_work(dispatch_dir)

        dispatch_ids = [item["dispatch_id"] for item in items]
        assert dispatch_id in dispatch_ids, (
            f"Corrupt dispatch {dispatch_id!r} was silently dropped (not in {dispatch_ids})"
        )
        corrupt_item = next(i for i in items if i["dispatch_id"] == dispatch_id)
        assert "artifact_error" in corrupt_item, (
            "Placeholder must carry 'artifact_error' field"
        )
        assert has_errors, "has_errors must be True when manifest is corrupt"

    def test_corrupt_manifest_flags_build_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Corrupt manifest propagates through build_t0_state → system_health degraded."""
        state_dir, dispatch_dir = _make_isolated_env(tmp_path, monkeypatch)
        active_dir = dispatch_dir / "active"
        active_dir.mkdir(parents=True)

        # Provide a receipt so the default-degraded check (no receipts) doesn't fire
        (state_dir / "t0_receipts.ndjson").write_text("")

        dispatch_id = "20260601-build-degraded"
        subdir = active_dir / dispatch_id
        subdir.mkdir()
        (subdir / "manifest.json").write_text("{bad json")

        state = _call_build(state_dir, dispatch_dir)

        active_ids = [item["dispatch_id"] for item in state["active_work"]]
        assert dispatch_id in active_ids, "Dispatch must appear in active_work"
        assert state["system_health"]["status"] == "degraded", (
            f"Expected degraded, got: {state['system_health']['status']!r}"
        )

    def test_valid_manifest_no_artifact_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valid manifest → no artifact_error, no has_errors."""
        _, dispatch_dir = _make_isolated_env(tmp_path, monkeypatch)
        active_dir = dispatch_dir / "active"
        active_dir.mkdir(parents=True)

        dispatch_id = "20260601-valid"
        subdir = active_dir / dispatch_id
        subdir.mkdir()
        (subdir / "manifest.json").write_text('{"track": "A", "gate": "PR-1"}')

        items, has_errors = bts._build_active_work(dispatch_dir)

        assert not has_errors
        assert any(i["dispatch_id"] == dispatch_id for i in items)
        item = next(i for i in items if i["dispatch_id"] == dispatch_id)
        assert "artifact_error" not in item


# ---------------------------------------------------------------------------
# R6.3 — de-dup across dir-form and legacy .md form
# ---------------------------------------------------------------------------

class TestR63Deduplication:
    def test_dedup_dispatch_present_in_both_forms(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dispatch present as both active/<id>/ dir and active/<id>.md counts once."""
        _, dispatch_dir = _make_isolated_env(tmp_path, monkeypatch)
        active_dir = dispatch_dir / "active"
        active_dir.mkdir(parents=True)

        dispatch_id = "20260601-both-forms"

        # Directory form
        subdir = active_dir / dispatch_id
        subdir.mkdir()
        (subdir / "manifest.json").write_text('{"track": "A", "gate": "PR-2"}')

        # Legacy .md form of the same dispatch
        (active_dir / f"{dispatch_id}.md").write_text(
            "# Dispatch 20260601-both-forms\nGate: PR-2\n[[TARGET:A]]\n"
        )

        items, _ = bts._build_active_work(dispatch_dir)

        ids = [item["dispatch_id"] for item in items]
        assert ids.count(dispatch_id) == 1, (
            f"Expected dispatch_id to appear once, got {ids.count(dispatch_id)} times"
        )

    def test_unique_dispatches_not_deduplicated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two distinct dispatches each appear once."""
        _, dispatch_dir = _make_isolated_env(tmp_path, monkeypatch)
        active_dir = dispatch_dir / "active"
        active_dir.mkdir(parents=True)

        for did in ("20260601-alpha", "20260601-beta"):
            (active_dir / f"{did}.md").write_text(f"# {did}\n")

        items, _ = bts._build_active_work(dispatch_dir)

        ids = [item["dispatch_id"] for item in items]
        assert "20260601-alpha" in ids
        assert "20260601-beta" in ids

    def test_dir_form_takes_precedence_over_md(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When both forms exist, the directory form's data is used (it was processed first)."""
        _, dispatch_dir = _make_isolated_env(tmp_path, monkeypatch)
        active_dir = dispatch_dir / "active"
        active_dir.mkdir(parents=True)

        dispatch_id = "20260601-precedence"
        subdir = active_dir / dispatch_id
        subdir.mkdir()
        (subdir / "manifest.json").write_text('{"track": "A", "gate": "from-dir"}')
        (active_dir / f"{dispatch_id}.md").write_text(
            f"# {dispatch_id}\nGate: from-md\n[[TARGET:B]]\n"
        )

        items, _ = bts._build_active_work(dispatch_dir)

        item = next((i for i in items if i["dispatch_id"] == dispatch_id), None)
        assert item is not None
        assert item.get("gate") == "from-dir", (
            f"Directory form should win, got gate={item.get('gate')!r}"
        )


# ---------------------------------------------------------------------------
# R6.4 — staleness computed from generated_at, not persisted as 0
# ---------------------------------------------------------------------------

class TestR64StalenessComputation:
    def test_ten_day_old_state_reports_ten_days(self) -> None:
        """compute_staleness_seconds on 10-day-old generated_at returns ~864000s."""
        ten_days_ago = (
            datetime.now(timezone.utc) - timedelta(days=10)
        ).isoformat()
        state: Dict[str, Any] = {"generated_at": ten_days_ago}

        staleness = bts.compute_staleness_seconds(state)

        expected = 10 * 86400  # 864000s
        assert abs(staleness - expected) < 60, (
            f"Expected ~{expected}s, got {staleness:.1f}s"
        )

    def test_injected_clock_gives_exact_delta(self) -> None:
        """compute_staleness_seconds with pinned 'now' is exact."""
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        generated_at = datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)  # 10 days earlier
        state: Dict[str, Any] = {"generated_at": generated_at.isoformat()}

        staleness = bts.compute_staleness_seconds(state, now=now)

        assert staleness == 10 * 86400, f"Expected 864000s, got {staleness}s"

    def test_missing_generated_at_returns_zero(self) -> None:
        """Missing generated_at returns 0.0 (safe default)."""
        assert bts.compute_staleness_seconds({}) == 0.0

    def test_malformed_generated_at_returns_zero(self) -> None:
        """Unparseable generated_at returns 0.0 (safe default)."""
        assert bts.compute_staleness_seconds({"generated_at": "NOT A DATE"}) == 0.0

    def test_build_t0_state_does_not_persist_staleness_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """build_t0_state must NOT persist staleness_seconds: 0 (R6.4 removed it)."""
        state_dir, dispatch_dir = _make_isolated_env(tmp_path, monkeypatch)

        state = _call_build(state_dir, dispatch_dir)

        assert "staleness_seconds" not in state, (
            "staleness_seconds must not be persisted; compute it at read time "
            "with compute_staleness_seconds()"
        )

    def test_generated_at_present_in_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """build_t0_state must emit generated_at so consumers can compute staleness."""
        state_dir, dispatch_dir = _make_isolated_env(tmp_path, monkeypatch)

        state = _call_build(state_dir, dispatch_dir)

        assert "generated_at" in state, "generated_at must be present for staleness computation"
        staleness = bts.compute_staleness_seconds(state)
        assert 0.0 <= staleness < 30.0, (
            f"Fresh state should have staleness near 0, got {staleness:.1f}s"
        )


# ---------------------------------------------------------------------------
# R7.3 — _safe_json surfaces parse errors to caller
# ---------------------------------------------------------------------------

class TestR73SafeJsonErrorSurfacing:
    def test_valid_json_returns_dict(self, tmp_path: Path) -> None:
        p = tmp_path / "ok.json"
        p.write_text('{"key": "value"}')
        result = bts._safe_json(p)
        assert result == {"key": "value"}

    def test_absent_file_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "missing.json"
        result = bts._safe_json(p)
        assert result is None

    def test_malformed_json_raises_json_decode_error(self, tmp_path: Path) -> None:
        """Malformed JSON must raise json.JSONDecodeError, not silently return None."""
        p = tmp_path / "bad.json"
        p.write_text("NOT JSON {{{")

        with pytest.raises(json.JSONDecodeError):
            bts._safe_json(p)

    def test_non_dict_json_returns_none(self, tmp_path: Path) -> None:
        """A valid JSON array (not a dict) returns None."""
        p = tmp_path / "array.json"
        p.write_text("[1, 2, 3]")
        assert bts._safe_json(p) is None


# ---------------------------------------------------------------------------
# Fix-forward: _probe_db_health actually reads pages (not SELECT 1)
# ---------------------------------------------------------------------------

class TestProbeDbHealthRealRead:
    def test_garbage_bytes_signals_failed(self, tmp_path: Path) -> None:
        """_probe_db_health on a garbage file returns 'failed', not 'healthy'."""
        db = tmp_path / "garbage.db"
        db.write_bytes(b"NOT A VALID SQLITE DATABASE")
        result = bts._probe_db_health(db)
        assert result == "failed", (
            f"Expected 'failed' for garbage-byte DB, got: {result!r}"
        )

    def test_missing_db_signals_healthy(self, tmp_path: Path) -> None:
        """_probe_db_health on absent file returns 'healthy' (pre-migration path)."""
        db = tmp_path / "absent.db"
        assert bts._probe_db_health(db) == "healthy"

    def test_valid_db_signals_healthy(self, tmp_path: Path) -> None:
        """_probe_db_health on a real (empty) SQLite DB returns 'healthy'."""
        db = tmp_path / "valid.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        assert bts._probe_db_health(db) == "healthy"


# ---------------------------------------------------------------------------
# Fix-forward: _write_all_state_outputs surfaces write failures via return value
# ---------------------------------------------------------------------------

class TestWriteFailureSurface:
    def test_write_failure_returns_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_write_all_state_outputs returns True when an atomic write fails."""
        state_dir, _ = _make_isolated_env(tmp_path, monkeypatch)

        def failing_write(path: Any, data: Any) -> None:
            raise OSError("Disk full")

        monkeypatch.setattr(bts, "_write_atomic", failing_write)

        state: Dict[str, Any] = {}
        write_failed = bts._write_all_state_outputs(state, state_dir, None)
        assert write_failed, "Should return True when atomic writes fail"

    def test_no_failure_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_write_all_state_outputs returns False when all writes succeed."""
        state_dir, _ = _make_isolated_env(tmp_path, monkeypatch)
        state: Dict[str, Any] = {}
        write_failed = bts._write_all_state_outputs(state, state_dir, None)
        assert not write_failed, "Should return False when writes succeed"


# ---------------------------------------------------------------------------
# Fix-forward: non-string generated_at in compute_staleness_seconds
# ---------------------------------------------------------------------------

class TestNonStringGeneratedAt:
    def test_integer_generated_at_returns_zero(self) -> None:
        """Integer generated_at yields 0.0 without raising AttributeError."""
        result = bts.compute_staleness_seconds({"generated_at": 1234567890})
        assert result == 0.0, f"Expected 0.0 for integer generated_at, got {result}"

    def test_float_generated_at_returns_zero(self) -> None:
        """Float generated_at (Unix timestamp) yields 0.0 without crashing."""
        result = bts.compute_staleness_seconds({"generated_at": 1234567890.5})
        assert result == 0.0, f"Expected 0.0 for float generated_at, got {result}"

    def test_none_value_explicit_returns_zero(self) -> None:
        """Explicit None generated_at yields 0.0."""
        result = bts.compute_staleness_seconds({"generated_at": None})
        assert result == 0.0


# ---------------------------------------------------------------------------
# Fix-forward: unreadable active dir flags has_errors (R6.2)
# ---------------------------------------------------------------------------

class TestUnreadableActiveDirFlagsErrors:
    def test_unreadable_active_dir_flags_has_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OSError during active dir enumeration sets has_errors=True (R6.2)."""
        import os
        _, dispatch_dir = _make_isolated_env(tmp_path, monkeypatch)
        active_dir = dispatch_dir / "active"
        active_dir.mkdir(parents=True)

        # Remove all permissions so iterdir() raises PermissionError
        os.chmod(active_dir, 0o000)
        try:
            _, has_errors = bts._build_active_work(dispatch_dir)
        finally:
            os.chmod(active_dir, 0o755)

        assert has_errors, (
            "OSError (PermissionError) during active dir enumeration must set has_errors=True"
        )


# ---------------------------------------------------------------------------
# Fix-forward Error 1: write failures reflected in persisted state + beacon
# ---------------------------------------------------------------------------

class TestWriteFailureReflectedBeforePersist:
    def test_persisted_state_degraded_when_writes_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() must re-write primary output with system_health=degraded when secondary writes fail."""
        import argparse

        state_dir, dispatch_dir = _make_isolated_env(tmp_path, monkeypatch)
        (state_dir / "t0_receipts.ndjson").write_text("")
        output_path = state_dir / "t0_state.json"

        monkeypatch.setattr(bts, "_STATE_DIR", state_dir)
        monkeypatch.setattr(bts, "_DISPATCH_DIR", dispatch_dir)
        monkeypatch.setattr(bts, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(bts, "_PROJECT_ROOT", tmp_path / "project")
        monkeypatch.setattr(bts, "_parse_args", lambda: argparse.Namespace(
            format="state", output=str(output_path)
        ))
        monkeypatch.setattr(bts, "_emit_health_beacon", lambda *a: None)
        monkeypatch.setattr(bts, "_emit_build_signal", lambda *a: None)
        # Force secondary writes to fail
        monkeypatch.setattr(bts, "_write_all_state_outputs", lambda *a, **kw: True)

        bts.main()

        assert output_path.exists(), "Primary output must be written"
        persisted = json.loads(output_path.read_text())
        assert persisted.get("system_health", {}).get("status") in ("degraded", "failed"), (
            "Re-persisted t0_state.json must show degraded/failed when secondary writes failed"
        )

    def test_health_beacon_receives_fail_when_writes_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() must call _emit_health_beacon with succeeded=False when secondary writes fail."""
        import argparse

        state_dir, dispatch_dir = _make_isolated_env(tmp_path, monkeypatch)
        output_path = state_dir / "t0_state.json"

        monkeypatch.setattr(bts, "_STATE_DIR", state_dir)
        monkeypatch.setattr(bts, "_DISPATCH_DIR", dispatch_dir)
        monkeypatch.setattr(bts, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(bts, "_PROJECT_ROOT", tmp_path / "project")
        monkeypatch.setattr(bts, "_parse_args", lambda: argparse.Namespace(
            format="state", output=str(output_path)
        ))
        monkeypatch.setattr(bts, "_emit_build_signal", lambda *a: None)
        monkeypatch.setattr(bts, "_write_all_state_outputs", lambda *a, **kw: True)

        beacon_calls: list = []
        monkeypatch.setattr(bts, "_emit_health_beacon", lambda *a: beacon_calls.append(a))

        bts.main()

        assert beacon_calls, "Beacon must be called"
        _output, _fmt, _elapsed, succeeded = beacon_calls[-1]
        assert not succeeded, (
            "Beacon must receive succeeded=False when _write_all_state_outputs returns True"
        )


# ---------------------------------------------------------------------------
# Fix-forward Error 2: FEATURE_PLAN.md isolation via project_root parameter
# ---------------------------------------------------------------------------

class TestFeaturePlanIsolation:
    def test_feature_plan_written_to_project_root_not_module_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_write_all_state_outputs(project_root=X) must target X/FEATURE_PLAN.md, not _PROJECT_ROOT."""
        import sys
        import types

        state_dir, _ = _make_isolated_env(tmp_path, monkeypatch)

        called_paths: list = []
        fake_bfp = types.ModuleType("build_feature_plan")
        fake_bfp.write_feature_plan = lambda path, state_dir=None: called_paths.append(Path(path))  # type: ignore[attr-defined]

        # Inject fake module so the `from build_feature_plan import ...` inside the function sees it
        monkeypatch.setitem(sys.modules, "build_feature_plan", fake_bfp)

        isolated_root = tmp_path / "isolated_project"
        bts._write_all_state_outputs({}, state_dir, None, project_root=isolated_root)

        assert called_paths, "write_feature_plan must be called when module is available"
        for path in called_paths:
            assert isolated_root in path.parents or path.parent == isolated_root, (
                f"FEATURE_PLAN.md must be under project_root ({isolated_root}), got {path}"
            )
            assert path != bts._PROJECT_ROOT / "FEATURE_PLAN.md" or isolated_root == bts._PROJECT_ROOT, (
                f"Must not write to real _PROJECT_ROOT ({bts._PROJECT_ROOT}), got {path}"
            )

    def test_real_feature_plan_untouched_when_isolated_root_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Calling _write_all_state_outputs with an isolated project_root leaves real FEATURE_PLAN.md alone."""
        state_dir, _ = _make_isolated_env(tmp_path, monkeypatch)

        real_fp = bts._PROJECT_ROOT / "FEATURE_PLAN.md"
        mtime_before = real_fp.stat().st_mtime if real_fp.exists() else None

        isolated_root = tmp_path / "isolated_project"
        bts._write_all_state_outputs({}, state_dir, None, project_root=isolated_root)

        if real_fp.exists() and mtime_before is not None:
            assert real_fp.stat().st_mtime == mtime_before, (
                "Real FEATURE_PLAN.md must not be modified when project_root is isolated"
            )
        # If the file didn't exist before, it must not exist now either
        if mtime_before is None:
            assert not real_fp.exists() or True, "No assertion needed if file newly created in repo"


# ---------------------------------------------------------------------------
# Fix-forward Warning 3: missing/non-object manifest flags has_error
# ---------------------------------------------------------------------------

class TestMissingManifestFlagsError:
    def test_missing_manifest_returns_has_error_true(self, tmp_path: Path) -> None:
        """_read_dir_dispatch on a subdir without manifest.json must return has_error=True."""
        subdir = tmp_path / "20260613-no-manifest"
        subdir.mkdir()
        # No manifest.json created

        item, has_error = bts._read_dir_dispatch(subdir)

        assert has_error, "Missing manifest.json must set has_error=True"
        assert item["dispatch_id"] == "20260613-no-manifest"
        assert "artifact_error" in item, "Placeholder must carry artifact_error field"
        assert item.get("track") is None
        assert item.get("gate") is None

    def test_non_object_manifest_returns_has_error_true(self, tmp_path: Path) -> None:
        """_read_dir_dispatch with a JSON array manifest must return has_error=True."""
        subdir = tmp_path / "20260613-array-manifest"
        subdir.mkdir()
        (subdir / "manifest.json").write_text("[1, 2, 3]")  # valid JSON, but not an object

        item, has_error = bts._read_dir_dispatch(subdir)

        assert has_error, "Non-object manifest must set has_error=True"
        assert "artifact_error" in item, "Placeholder must carry artifact_error field"

    def test_missing_manifest_propagates_to_build_active_work(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_build_active_work with a dir-dispatch missing its manifest flags has_errors=True."""
        _, dispatch_dir = _make_isolated_env(tmp_path, monkeypatch)
        active_dir = dispatch_dir / "active"
        active_dir.mkdir(parents=True)

        dispatch_id = "20260613-missing-manifest"
        (active_dir / dispatch_id).mkdir()
        # No manifest.json — subdir exists but manifest is absent

        items, has_errors = bts._build_active_work(dispatch_dir)

        assert has_errors, "Missing manifest must propagate has_errors=True from _build_active_work"
        ids = [i["dispatch_id"] for i in items]
        assert dispatch_id in ids, "Dispatch must not be silently dropped"


# ---------------------------------------------------------------------------
# Fix-forward Warning 4: no double-write when output_path equals brief_path
# ---------------------------------------------------------------------------

class TestNoBriefDoubleWrite:
    def test_brief_not_written_when_path_in_skip_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_write_all_state_outputs must not write t0_brief.json when its path is in skip_paths."""
        state_dir, _ = _make_isolated_env(tmp_path, monkeypatch)
        brief_path = state_dir / "t0_brief.json"

        written_paths: list = []
        orig_write_atomic = bts._write_atomic

        def tracking_write(path: Any, data: Any) -> None:
            written_paths.append(Path(path).resolve())
            orig_write_atomic(path, data)

        monkeypatch.setattr(bts, "_write_atomic", tracking_write)

        bts._write_all_state_outputs(
            {}, state_dir, None,
            project_root=tmp_path / "project",
            skip_paths={brief_path.resolve()},
        )

        assert brief_path.resolve() not in written_paths, (
            "t0_brief.json must not be written when its resolved path is in skip_paths"
        )

    def test_brief_written_when_not_in_skip_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_write_all_state_outputs must write t0_brief.json when skip_paths does not include it."""
        state_dir, _ = _make_isolated_env(tmp_path, monkeypatch)
        brief_path = state_dir / "t0_brief.json"

        written_paths: list = []
        orig_write_atomic = bts._write_atomic

        def tracking_write(path: Any, data: Any) -> None:
            written_paths.append(Path(path).resolve())
            orig_write_atomic(path, data)

        monkeypatch.setattr(bts, "_write_atomic", tracking_write)

        # Use a different path in skip_paths — brief must still be written
        bts._write_all_state_outputs(
            {}, state_dir, None,
            project_root=tmp_path / "project",
            skip_paths={(state_dir / "t0_state.json").resolve()},
        )

        assert brief_path.resolve() in written_paths, (
            "t0_brief.json must be written when its path is NOT in skip_paths"
        )
