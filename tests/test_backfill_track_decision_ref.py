"""tests/test_backfill_track_decision_ref.py — OI-1190 retroactive backfill.

Covers the idempotent backfill that maps historical plan-gate reports back onto
their tracks via tracks.decision_ref:
- report-filename parsing (plan-gate-<track>-<label>-<hash>.md)
- dedupe-by-label (latest mtime wins)
- payload reconstruction via plan_gate_panel.build_decision_ref (source="backfill")
- apply_backfill fills only EMPTY decision_ref, reports filled/already-filled/orphan
- a second run is a no-op (idempotent)

All tests use temporary DBs only — the live .vnx-data DB is never touched.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"
_MIGRATIONS = _ROOT / "schemas" / "migrations"

for p in (str(_LIB), str(_SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import schema_migration  # noqa: E402
import tracks as tracks_lib  # noqa: E402
import plan_gate_panel as pgp  # noqa: E402
import backfill_track_decision_ref as b  # noqa: E402

from fixtures.dispatches_schema_fixture import ensure_dispatches_columns  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _report(verdict: str, findings: list[str] | None = None, rationale: str = "ok") -> str:
    body = json.dumps({
        "verdict": verdict,
        "blocking_findings": findings or [],
        "rationale": rationale,
    })
    return f"# review\n\nsome prose\n\n```{pgp.VERDICT_FENCE}\n{body}\n```\n"


def _make_state_dir(tmp_path: Path) -> Path:
    """Build a tracks DB (22/24/27/28) + decision_ref (0033), ready for the backfill."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev', state TEXT NOT NULL DEFAULT 'queued',
            terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2', pr_ref TEXT,
            gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'dispatch', entity_id TEXT NOT NULL,
            from_state TEXT, to_state TEXT, actor TEXT NOT NULL DEFAULT 'runtime',
            reason TEXT, metadata_json TEXT DEFAULT '{}', occurred_at TEXT NOT NULL, project_id TEXT
        )
        """
    )
    conn.commit()
    for ver, fname in ((22, "0022_track_layer.sql"), (24, "0024_tracks_tenant_scoping.sql")):
        schema_migration.apply_script_if_below(conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8"))
        conn.commit()
    ensure_dispatches_columns(conn)
    conn.execute("PRAGMA user_version = 26")
    conn.commit()
    for ver, fname in ((27, "0027_planning_horizon_and_deliverable_view.sql"),
                       (28, "0028_tracks_derived_status.sql")):
        schema_migration.apply_script_if_below(conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8"))
        conn.commit()
    schema_migration.apply_script_if_below(
        conn, 33, (_MIGRATIONS / "0033_track_decision_ref.sql").read_text(encoding="utf-8")
    )
    conn.commit()
    conn.close()
    return state_dir


def _decision_ref(state_dir: Path, track_id: str, project_id: str = "proj-x") -> str | None:
    t = tracks_lib.get_track(state_dir, track_id, project_id)
    return (t or {}).get("decision_ref")


# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------

def test_parse_report_filename_parses_all_default_labels():
    labels = b._known_labels()
    cases = {
        "plan-gate-feat-opus-deadbeef.md": ("feat", "opus", "deadbeef"),
        "plan-gate-feat-kimi-00ff00aa.md": ("feat", "kimi", "00ff00aa"),
        "plan-gate-feat-glm-5.2-harness-1a2b3c4d.md": ("feat", "glm-5.2-harness", "1a2b3c4d"),
        "plan-gate-feat-deepseek-12345678.md": ("feat", "deepseek", "12345678"),
        "plan-gate-feat-codex-fedcba98.md": ("feat", "codex", "fedcba98"),
    }
    for filename, (track, label, hash_) in cases.items():
        r = b._parse_report_filename(filename, labels)
        assert r is not None, filename
        assert (r.track_id, r.label, r.hash) == (track, label, hash_)


def test_parse_report_filename_track_with_dashes():
    labels = b._known_labels()
    r = b._parse_report_filename("plan-gate-20260814m-a-track-decision-ref-opus-deadbeef.md", labels)
    assert r is not None
    assert r.track_id == "20260814m-a-track-decision-ref"
    assert r.label == "opus"


def test_parse_report_filename_rejects_unparseable():
    labels = b._known_labels()
    assert b._parse_report_filename("some-other-file.md", labels) is None
    assert b._parse_report_filename("plan-gate-nohash.md", labels) is None
    assert b._parse_report_filename("plan-gate--opus-deadbeef.md", labels) is None  # empty track
    assert b._parse_report_filename("plan-gate-x-unknownlabel-deadbeef.md", labels) is None


# ---------------------------------------------------------------------------
# Discovery + dedupe
# ---------------------------------------------------------------------------

def test_discover_reports_and_latest_per_label(tmp_path):
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True)
    (reports_dir / "plan-gate-feat-opus-aaaaaaaa.md").write_text(_report("pass"), encoding="utf-8")
    # A retried kimi seat: two files, the older one is noise.
    older = reports_dir / "plan-gate-feat-kimi-bbbbbbbb.md"
    older.write_text(_report("revise"), encoding="utf-8")
    newer = reports_dir / "plan-gate-feat-kimi-cccccccc.md"
    newer.write_text(_report("pass"), encoding="utf-8")
    import os
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))
    # An out-of-scope file is ignored.
    (reports_dir / "plan-gate-ignore-me.md").write_text("x", encoding="utf-8")

    found = b.discover_reports(reports_dir)
    by_track: dict[str, list[b.ReportFile]] = {}
    for r in found:
        by_track.setdefault(r.track_id, []).append(r)

    assert set(by_track) == {"feat"}
    latest = b._latest_per_label(by_track["feat"])
    assert {r.label for r in latest} == {"opus", "kimi"}
    kimi = next(r for r in latest if r.label == "kimi")
    assert kimi.hash == "cccccccc"  # latest mtime wins


# ---------------------------------------------------------------------------
# Payload reconstruction
# ---------------------------------------------------------------------------

def test_build_payload_for_track_reconstructs_decision_and_rejected(tmp_path):
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True)
    files = [
        b.ReportFile("plan-gate-feat-opus-aaaaaaaa.md", "feat", "opus", "aaaaaaaa", 1.0),
        b.ReportFile("plan-gate-feat-kimi-bbbbbbbb.md", "feat", "kimi", "bbbbbbbb", 1.0),
    ]
    (reports_dir / files[0].filename).write_text(
        _report("block", ["no rollback", "ssrf"], "unsafe"), encoding="utf-8"
    )
    (reports_dir / files[1].filename).write_text(_report("pass"), encoding="utf-8")

    payload = b.build_payload_for_track(
        reports_dir, "feat", files, source="backfill", set_at="2026-08-14T00:00:00Z"
    )
    data = json.loads(payload)
    assert data["source"] == "backfill"
    assert data["decision"] == "REVISE"  # one block -> revise
    assert len(data["reports"]) == 2
    assert set(data["reports"]) == {
        "unified_reports/plan-gate-feat-opus-aaaaaaaa.md",
        "unified_reports/plan-gate-feat-kimi-bbbbbbbb.md",
    }
    rejected = data["rejected_alternatives"]
    assert len(rejected) == 1
    assert rejected[0]["panelist"] == "opus"
    assert rejected[0]["verdict"] == "block"
    assert rejected[0]["findings"] == ["no rollback", "ssrf"]
    assert rejected[0]["rationale"] == "unsafe"


# ---------------------------------------------------------------------------
# apply_backfill end-to-end
# ---------------------------------------------------------------------------

def test_apply_backfill_fills_empty_skips_filled_and_counts(tmp_path):
    state_dir = _make_state_dir(tmp_path)
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True)

    tracks_lib.create_track(state_dir, "feat-a", "proj-x", "A", "shipped")
    tracks_lib.create_track(state_dir, "feat-b", "proj-x", "B", "shipped")
    tracks_lib.create_track(state_dir, "feat-c", "proj-x", "C", "shipped")

    (reports_dir / "plan-gate-feat-a-opus-aaaaaaaa.md").write_text(_report("pass"), encoding="utf-8")
    (reports_dir / "plan-gate-feat-b-kimi-bbbbbbbb.md").write_text(_report("pass"), encoding="utf-8")
    # feat-b already has a (live) decision_ref — the backfill must not overwrite it.
    tracks_lib.set_decision_ref(state_dir, "feat-b", "proj-x", '{"source":"plan-gate"}', actor="system")

    report = b.apply_backfill(
        state_dir, reports_dir, "proj-x", set_at="2026-08-14T00:00:00Z",
    )

    assert report["tracks_total"] == 3
    assert report["tracks_with_reports"] == 2
    assert report["filled"] == ["feat-a"]
    assert report["already_filled"] == ["feat-b"]
    assert report["orphan_reports"] == []

    # feat-a's decision_ref is a real payload pointing at the report file.
    payload = json.loads(_decision_ref(state_dir, "feat-a"))
    assert payload["source"] == "backfill"
    assert payload["reports"] == ["unified_reports/plan-gate-feat-a-opus-aaaaaaaa.md"]
    assert (tmp_path / "unified_reports" / "plan-gate-feat-a-opus-aaaaaaaa.md").exists()
    # feat-b's live decision_ref is untouched.
    assert _decision_ref(state_dir, "feat-b") == '{"source":"plan-gate"}'
    # feat-c has no report and no decision_ref.
    assert _decision_ref(state_dir, "feat-c") is None


def test_apply_backfill_reports_orphan_reports(tmp_path):
    state_dir = _make_state_dir(tmp_path)
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True)

    tracks_lib.create_track(state_dir, "feat-a", "proj-x", "A", "shipped")
    (reports_dir / "plan-gate-ghost-opus-aaaaaaaa.md").write_text(_report("pass"), encoding="utf-8")

    report = b.apply_backfill(
        state_dir, reports_dir, "proj-x", set_at="2026-08-14T00:00:00Z",
    )
    assert report["tracks_total"] == 1
    assert report["tracks_with_reports"] == 0
    assert report["filled"] == []
    assert len(report["orphan_reports"]) == 1
    assert report["orphan_reports"][0][0] == "ghost"


def test_print_report_without_reports_not_reduced_by_orphans(tmp_path, capsys):
    """Orphan REPORT FILES must not shrink the 'tracks without reports' count.

    A track_id that appears in a report filename but has no tracks row is an orphan
    REPORT, not a track — so 'tracks without plan-gate reports' is tracks-in-DB with
    no matching report, independent of how many orphan files exist.
    """
    state_dir = _make_state_dir(tmp_path)
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True)

    tracks_lib.create_track(state_dir, "feat-a", "proj-x", "A", "shipped")
    tracks_lib.create_track(state_dir, "feat-b", "proj-x", "B", "shipped")
    (reports_dir / "plan-gate-feat-a-opus-aaaaaaaa.md").write_text(_report("pass"), encoding="utf-8")
    for h in ("11111111", "22222222", "33333333"):
        (reports_dir / f"plan-gate-ghost-kimi-{h}.md").write_text(_report("pass"), encoding="utf-8")

    report = b.apply_backfill(state_dir, reports_dir, "proj-x", set_at="2026-08-14T00:00:00Z")
    assert report["tracks_total"] == 2
    assert report["tracks_with_reports"] == 1
    assert len(report["orphan_reports"]) == 3  # three ghost files, not a track

    b.print_report(report)
    out = capsys.readouterr().out
    assert "tracks without plan-gate reports     : 1" in out


# ---------------------------------------------------------------------------
# Tiebreak reports (OI-1618 follow-up)
# ---------------------------------------------------------------------------

_TIEBREAK_FIXTURE = _ROOT / "tests" / "fixtures" / "plan-tiebreak-review-gate-secure-04990c59.md"


def _tiebreak_report(outcome: str, required_change: str = "", rationale: str = "ok") -> str:
    body = json.dumps({
        "outcome": outcome, "required_change": required_change, "rationale": rationale,
    })
    return (
        "# tiebreak\n\n**Dispatch-ID**: plan-tiebreak-x\n**Model**: deepseek-v4-pro\n"
        "**Provider**: deepseek-harness\n\n## Summary\n\nok\n\n"
        f"```vnx-plan-tiebreak\n{body}\n```\n"
    )


def test_parse_tiebreak_report_filename():
    r = b._parse_tiebreak_report_filename("plan-tiebreak-review-gate-secure-04990c59.md")
    assert r is not None
    assert (r.track_id, r.hash) == ("review-gate-secure", "04990c59")


def test_parse_tiebreak_report_filename_rejects_unparseable():
    assert b._parse_tiebreak_report_filename("plan-gate-x-opus-deadbeef.md") is None
    assert b._parse_tiebreak_report_filename("plan-tiebreak-nohash.md") is None
    assert b._parse_tiebreak_report_filename("plan-tiebreak--deadbeef.md") is None  # empty track


def test_apply_backfill_proposes_tiebreak_when_youngest_report_is_tiebreak(tmp_path):
    """The real historical tiebreak report for review-gate-secure (byte-exact
    fixture) is YOUNGER than its two panel reports — the backfill must propose
    ``tiebreak:START`` with the tiebreak report first, the panel reports
    inherited behind it, and the panel round's rejected alternative preserved.
    """
    import os

    state_dir = _make_state_dir(tmp_path)
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True)

    tracks_lib.create_track(state_dir, "review-gate-secure", "proj-x", "T", "shipped")

    panel_a = reports_dir / "plan-gate-review-gate-secure-opus-11111111.md"
    panel_a.write_text(_report("revise", ["gap one"], "needs more"), encoding="utf-8")
    panel_b = reports_dir / "plan-gate-review-gate-secure-kimi-22222222.md"
    panel_b.write_text(_report("pass"), encoding="utf-8")
    os.utime(panel_a, (1_000_000, 1_000_000))
    os.utime(panel_b, (1_000_100, 1_000_100))

    tb_path = reports_dir / _TIEBREAK_FIXTURE.name
    tb_path.write_text(_TIEBREAK_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(tb_path, (2_000_000, 2_000_000))

    report = b.apply_backfill(state_dir, reports_dir, "proj-x", set_at="2026-09-06T00:00:00Z")
    assert report["filled"] == ["review-gate-secure"]
    assert report["errors"] == []

    payload = json.loads(_decision_ref(state_dir, "review-gate-secure"))
    assert payload["decision"] == "tiebreak:START"
    assert payload["source"] == "plan-gate-tiebreak"
    assert payload["superseded_decision"] == "REVISE"
    assert payload["tiebreaker_model"] == "deepseek-v4-pro"
    assert payload["reports"][0] == f"unified_reports/{_TIEBREAK_FIXTURE.name}"
    assert len(payload["reports"]) == 3  # tiebreak report + 2 panel reports
    assert len(payload["rejected_alternatives"]) == 1  # only opus (revise)
    assert payload["rejected_alternatives"][0]["panelist"] == "opus"


def test_apply_backfill_tiebreak_only_track_has_no_superseded_decision(tmp_path):
    """A track with ONLY a tiebreak report (no panel reports at all) is never
    guessed at: ``superseded_decision`` is ``None`` and ``reports`` carries
    just the tiebreak report itself.
    """
    state_dir = _make_state_dir(tmp_path)
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True)
    tracks_lib.create_track(state_dir, "feat-tbonly", "proj-x", "T", "shipped")
    (reports_dir / "plan-tiebreak-feat-tbonly-cafef00d.md").write_text(
        _tiebreak_report("STOP", "", "no panel history for this track"),
        encoding="utf-8",
    )

    report = b.apply_backfill(state_dir, reports_dir, "proj-x", set_at="2026-09-06T00:00:00Z")
    assert report["filled"] == ["feat-tbonly"]

    payload = json.loads(_decision_ref(state_dir, "feat-tbonly"))
    assert payload["decision"] == "tiebreak:STOP"
    assert payload["superseded_decision"] is None
    assert payload["reports"] == ["unified_reports/plan-tiebreak-feat-tbonly-cafef00d.md"]
    assert payload["rejected_alternatives"] == []


def test_backfill_dry_run_shows_tiebreak_proposal_without_writing(tmp_path, capsys):
    state_dir = _make_state_dir(tmp_path)
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True)
    tracks_lib.create_track(state_dir, "review-gate-secure", "proj-x", "T", "shipped")
    (reports_dir / _TIEBREAK_FIXTURE.name).write_text(
        _TIEBREAK_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    rc = b.dry_run(state_dir, reports_dir, "proj-x")
    assert rc == 0
    out = capsys.readouterr().out
    assert "review-gate-secure" in out
    # The live store was not touched by the dry-run.
    assert _decision_ref(state_dir, "review-gate-secure") is None


def test_apply_backfill_panel_stays_on_panel_path_when_report_is_youngest(tmp_path):
    """Regression: a track whose YOUNGEST report is a panel report is unaffected
    by the new tiebreak-awareness, even when an OLDER tiebreak report also
    exists for the same track (e.g. an earlier tie later re-reviewed by a
    fresh panel round).
    """
    import os

    state_dir = _make_state_dir(tmp_path)
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True)
    tracks_lib.create_track(state_dir, "feat-repanel", "proj-x", "T", "shipped")

    old_tb = reports_dir / "plan-tiebreak-feat-repanel-99999999.md"
    old_tb.write_text(_tiebreak_report("STOP", "x", "old"), encoding="utf-8")
    os.utime(old_tb, (1_000_000, 1_000_000))

    panel = reports_dir / "plan-gate-feat-repanel-opus-aaaaaaaa.md"
    panel.write_text(_report("pass"), encoding="utf-8")
    os.utime(panel, (2_000_000, 2_000_000))

    report = b.apply_backfill(state_dir, reports_dir, "proj-x", set_at="2026-09-06T00:00:00Z")
    assert report["filled"] == ["feat-repanel"]

    payload = json.loads(_decision_ref(state_dir, "feat-repanel"))
    assert payload["decision"] == "PASS"
    assert payload["source"] == "backfill"


def test_apply_backfill_unparseable_tiebreak_report_skipped_with_reason(tmp_path):
    state_dir = _make_state_dir(tmp_path)
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True)
    tracks_lib.create_track(state_dir, "feat-badtb", "proj-x", "T", "shipped")
    (reports_dir / "plan-tiebreak-feat-badtb-abcdef01.md").write_text(
        "# tiebreak\n\nno fence here at all.\n", encoding="utf-8",
    )

    report = b.apply_backfill(state_dir, reports_dir, "proj-x", set_at="2026-09-06T00:00:00Z")
    assert report["filled"] == []
    assert any("feat-badtb" in e for e in report["errors"])
    assert _decision_ref(state_dir, "feat-badtb") is None


def test_apply_backfill_is_idempotent_second_run_is_noop(tmp_path):
    state_dir = _make_state_dir(tmp_path)
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True)

    tracks_lib.create_track(state_dir, "feat-a", "proj-x", "A", "shipped")
    (reports_dir / "plan-gate-feat-a-opus-aaaaaaaa.md").write_text(_report("pass"), encoding="utf-8")

    first = b.apply_backfill(state_dir, reports_dir, "proj-x", set_at="2026-08-14T00:00:00Z")
    second = b.apply_backfill(state_dir, reports_dir, "proj-x", set_at="2026-08-14T00:00:01Z")

    assert first["filled"] == ["feat-a"]
    assert second["filled"] == []          # nothing left to fill
    assert second["already_filled"] == ["feat-a"]  # the first run's write is seen


# ---------------------------------------------------------------------------
# --track + --supersede-panel-with-tiebreak (PR #1801 fix-forward)
#
# Scoped, explicit override of the "never overwrite" rule: only for exactly
# one operator-named track, and only when its youngest report on disk is a
# parseable tiebreak report strictly younger (mtime) than the EXISTING
# decision_ref's own "set_at". Every other track — including one with an
# empty decision_ref sitting in the same store — must be provably untouched.
# ---------------------------------------------------------------------------

import datetime as _dt  # noqa: E402


def _iso_from_epoch(epoch: float) -> str:
    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).isoformat()


def _seed_revise_decision_ref(state_dir: Path, track_id: str, project_id: str, set_at_iso: str) -> str:
    payload = json.dumps({
        "reports": [f"unified_reports/plan-gate-{track_id}-opus-11111111.md"],
        "decision": "REVISE",
        "rejected_alternatives": [
            {"panelist": "opus", "verdict": "revise", "findings": ["gap one"], "rationale": "needs more"},
        ],
        "set_at": set_at_iso,
        "source": "plan-gate",
    }, sort_keys=True)
    tracks_lib.set_decision_ref(state_dir, track_id, project_id, payload, actor="system")
    return payload


def test_generic_run_never_supersedes_existing_panel_decision_ref_even_with_younger_tiebreak(tmp_path):
    """Regression on the pre-existing 'never overwrite' rule: a track that already
    carries a panel decision_ref (REVISE) must stay untouched by the GENERIC
    (unscoped) backfill even when a younger, parseable tiebreak report exists for
    it on disk. Only the scoped --track --supersede-panel-with-tiebreak mode may
    replace it.
    """
    import os

    state_dir = _make_state_dir(tmp_path)
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True)
    tracks_lib.create_track(state_dir, "review-gate-secure", "proj-x", "T", "shipped")

    existing = _seed_revise_decision_ref(
        state_dir, "review-gate-secure", "proj-x", _iso_from_epoch(500_000)
    )

    tb_path = reports_dir / _TIEBREAK_FIXTURE.name
    tb_path.write_text(_TIEBREAK_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(tb_path, (1_000_000, 1_000_000))

    report = b.apply_backfill(state_dir, reports_dir, "proj-x", set_at="2026-09-06T00:00:00Z")
    assert report["already_filled"] == ["review-gate-secure"]
    assert report["filled"] == []
    assert _decision_ref(state_dir, "review-gate-secure") == existing


def test_supersede_panel_with_tiebreak_dry_run_shows_replacement_without_writing(tmp_path, capsys):
    import os

    state_dir = _make_state_dir(tmp_path)
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True)
    tracks_lib.create_track(state_dir, "review-gate-secure", "proj-x", "T", "shipped")

    existing = _seed_revise_decision_ref(
        state_dir, "review-gate-secure", "proj-x", _iso_from_epoch(500_000)
    )
    tb_path = reports_dir / _TIEBREAK_FIXTURE.name
    tb_path.write_text(_TIEBREAK_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(tb_path, (1_000_000, 1_000_000))

    rc = b.dry_run_supersede(state_dir, reports_dir, "proj-x", "review-gate-secure")
    assert rc == 0
    out = capsys.readouterr().out
    assert "review-gate-secure" in out
    assert "REVISE" in out
    assert "tiebreak:START" in out
    # The live store is untouched by the dry-run.
    assert _decision_ref(state_dir, "review-gate-secure") == existing


def test_supersede_panel_with_tiebreak_apply_writes_and_second_apply_is_noop(tmp_path):
    import os

    state_dir = _make_state_dir(tmp_path)
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True)
    tracks_lib.create_track(state_dir, "review-gate-secure", "proj-x", "T", "shipped")

    _seed_revise_decision_ref(
        state_dir, "review-gate-secure", "proj-x", _iso_from_epoch(500_000)
    )
    tb_path = reports_dir / _TIEBREAK_FIXTURE.name
    tb_path.write_text(_TIEBREAK_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(tb_path, (1_000_000, 1_000_000))

    rc = b.apply_supersede_live(state_dir, reports_dir, "proj-x", "review-gate-secure")
    assert rc == 0
    payload = json.loads(_decision_ref(state_dir, "review-gate-secure"))
    assert payload["decision"] == "tiebreak:START"
    assert payload["superseded_decision"] == "REVISE"
    assert payload["source"] == "plan-gate-tiebreak"
    assert payload["reports"][0] == f"unified_reports/{_TIEBREAK_FIXTURE.name}"

    # A second --apply is a no-op: the tiebreak report is no longer younger
    # than the (now recent) decision_ref.set_at that the first apply wrote.
    after_first = _decision_ref(state_dir, "review-gate-secure")
    rc2 = b.apply_supersede_live(state_dir, reports_dir, "proj-x", "review-gate-secure")
    assert rc2 == 0
    assert _decision_ref(state_dir, "review-gate-secure") == after_first


def test_supersede_panel_with_tiebreak_older_report_no_replacement(tmp_path):
    import os

    state_dir = _make_state_dir(tmp_path)
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True)
    tracks_lib.create_track(state_dir, "review-gate-secure", "proj-x", "T", "shipped")

    existing = _seed_revise_decision_ref(
        state_dir, "review-gate-secure", "proj-x", _iso_from_epoch(500_000)
    )
    tb_path = reports_dir / _TIEBREAK_FIXTURE.name
    tb_path.write_text(_TIEBREAK_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(tb_path, (100_000, 100_000))  # older than the existing set_at (500_000)

    result = b.compute_supersede_panel_with_tiebreak(
        state_dir, reports_dir, "proj-x", "review-gate-secure", set_at="2026-09-06T00:00:00Z",
    )
    assert result.action == "skipped"
    assert "not newer" in result.reason
    assert _decision_ref(state_dir, "review-gate-secure") == existing


def test_supersede_panel_with_tiebreak_leaves_other_tracks_untouched(tmp_path):
    import os

    state_dir = _make_state_dir(tmp_path)
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir(parents=True)
    tracks_lib.create_track(state_dir, "review-gate-secure", "proj-x", "T", "shipped")
    tracks_lib.create_track(state_dir, "feat-untouched", "proj-x", "U", "shipped")

    _seed_revise_decision_ref(
        state_dir, "review-gate-secure", "proj-x", _iso_from_epoch(500_000)
    )
    tb_path = reports_dir / _TIEBREAK_FIXTURE.name
    tb_path.write_text(_TIEBREAK_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(tb_path, (1_000_000, 1_000_000))

    rc = b.apply_supersede_live(state_dir, reports_dir, "proj-x", "review-gate-secure")
    assert rc == 0
    assert _decision_ref(state_dir, "feat-untouched") is None


def test_main_supersede_without_track_is_an_error(capsys):
    rc = b.main(["--supersede-panel-with-tiebreak"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "--track" in err
