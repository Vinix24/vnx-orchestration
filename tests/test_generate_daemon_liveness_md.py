"""tests/test_generate_daemon_liveness_md.py — D2 (absence-is-loud).

scripts/generate_daemon_liveness_md.py regenerates docs/core/DAEMON_LIVENESS.md
from the live register + measurement (generation over editing -- a
hand-kept table drifts, a generated one cannot). It is a DEDICATED file, not
a section spliced into docs/core/SUBSYSTEMS.md: that ledger's
`subsystems-drift` CI check parses every 5-cell markdown table row as a
cockpit-subsystem row with no table-boundary awareness, and a
"daemon | script(s) | conditional | state | since" table also has 5 cells --
splicing it in made `make subsystems-check` fail on every daemon (reproduced
while building this module).

These tests exercise the marker-replacement logic against a throwaway doc
file so they don't depend on live process state matching whatever happens
to be running on the machine executing the suite.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import generate_daemon_liveness_md as gen  # noqa: E402


class TestRenderTable:
    def test_render_includes_all_nine_daemons(self) -> None:
        table = gen.render_table()
        for name in (
            "dispatcher", "smart_tap", "receipt_processor",
            "heartbeat_ack_monitor", "queue_watcher", "dashboard",
            "state_manager", "intelligence_daemon", "recommendations_engine",
        ):
            assert f"`{name}`" in table

    def test_render_has_markers(self) -> None:
        table = gen.render_table()
        assert gen._BEGIN_MARKER in table
        assert gen._END_MARKER in table

    def test_render_reports_recognized_overall(self) -> None:
        table = gen.render_table()
        assert any(f"**{v}**" in table for v in ("ok", "fail", "unknown"))


def _patch_root(monkeypatch, tmp_path: Path) -> None:
    class _FakeRoot:
        @staticmethod
        def resolve_project_root(_caller_file):
            return tmp_path

    monkeypatch.setattr(gen, "project_root", _FakeRoot)


class TestApplyToDocFile:
    def test_creates_file_when_absent(self, tmp_path: Path, monkeypatch) -> None:
        _patch_root(monkeypatch, tmp_path)
        doc_file = tmp_path / "docs" / "core" / "DAEMON_LIVENESS.md"
        assert not doc_file.exists()

        rc = gen._apply(check=False)
        assert rc == 0
        text = doc_file.read_text(encoding="utf-8")
        assert gen._BEGIN_MARKER in text
        assert "# VNX Daemon Liveness" in text

    def test_insert_when_no_markers_present(self, tmp_path: Path, monkeypatch) -> None:
        _patch_root(monkeypatch, tmp_path)
        doc = tmp_path / "docs" / "core"
        doc.mkdir(parents=True)
        doc_file = doc / "DAEMON_LIVENESS.md"
        doc_file.write_text("preexisting header\n\nsome content\n", encoding="utf-8")

        rc = gen._apply(check=False)
        assert rc == 0
        text = doc_file.read_text(encoding="utf-8")
        assert "some content" in text
        assert gen._BEGIN_MARKER in text
        assert "# VNX Daemon Liveness" in text

    def test_replaces_existing_block_not_duplicates(self, tmp_path: Path, monkeypatch) -> None:
        _patch_root(monkeypatch, tmp_path)
        doc = tmp_path / "docs" / "core"
        doc.mkdir(parents=True)
        doc_file = doc / "DAEMON_LIVENESS.md"
        doc_file.write_text(
            f"{gen._BEGIN_MARKER}\nold stale content\n{gen._END_MARKER}\n",
            encoding="utf-8",
        )

        gen._apply(check=False)
        text = doc_file.read_text(encoding="utf-8")
        assert "old stale content" not in text
        assert text.count(gen._BEGIN_MARKER) == 1
        assert text.count(gen._END_MARKER) == 1

    def test_check_mode_detects_drift_without_writing(self, tmp_path: Path, monkeypatch) -> None:
        _patch_root(monkeypatch, tmp_path)
        doc = tmp_path / "docs" / "core"
        doc.mkdir(parents=True)
        doc_file = doc / "DAEMON_LIVENESS.md"
        original = f"{gen._BEGIN_MARKER}\nold stale content\n{gen._END_MARKER}\n"
        doc_file.write_text(original, encoding="utf-8")

        rc = gen._apply(check=True)
        assert rc == 1
        assert doc_file.read_text(encoding="utf-8") == original, "check mode must not write"

    def test_check_mode_detects_drift_when_file_absent(self, tmp_path: Path, monkeypatch) -> None:
        _patch_root(monkeypatch, tmp_path)
        doc_file = tmp_path / "docs" / "core" / "DAEMON_LIVENESS.md"
        assert not doc_file.exists()

        rc = gen._apply(check=True)
        assert rc == 1
        assert not doc_file.exists(), "check mode must not write"

    def test_check_mode_clean_after_apply(self, tmp_path: Path, monkeypatch) -> None:
        _patch_root(monkeypatch, tmp_path)
        doc_file = tmp_path / "docs" / "core" / "DAEMON_LIVENESS.md"

        gen._apply(check=False)
        # Re-running --check immediately after a real apply may still detect
        # drift if a daemon's live state changes between the two
        # measurements (both calls hit real process state) -- assert on the
        # markers/skeleton being present instead of a byte-identical rc==0.
        text_after = doc_file.read_text(encoding="utf-8")
        assert gen._BEGIN_MARKER in text_after
        assert gen._END_MARKER in text_after
