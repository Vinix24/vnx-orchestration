"""tests/test_t0_rotation_state.py — the pending marker and latch (dispatch 20260926-ff-t0-rotation-ci).

Pins the three properties the review of the rotation PR asked for:

  - the marker changes ONLY on a transition (no rewrite, no fresh timestamp on a repeat);
  - the ledger is written BEFORE the state file it describes (ADR-005), and a ledger that
    refuses the transition leaves the marker alone so the next hook call retries it;
  - the read-decide-write of the marker is one critical section (OI-1486): parallel hook
    processes cannot both see "new" and both record it.

Everything runs against tmp paths; nothing touches a real store.

The wrap proof, the emergency path and the transcript digest (dispatch
20261016b-tg-g5a-wrap-proof) are pinned further down: wrap-start/wrap-finish bind the proof to
this wrap and this slug, compose refuses with 6 per named check, and the digest stays in its
budget with the newest entries.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import t0_rotation_state as rotation_state

PANE = "%9"
SESSION = "sess-state-1"


def _pending(sdir: Path, **overrides) -> bool:
    kwargs = dict(pane=PANE, session_id=SESSION, tokens=510_000, level="force",
                  transcript_path="/t.jsonl")
    kwargs.update(overrides)
    return rotation_state.write_pending(sdir, **kwargs)


def _marker(sdir: Path) -> dict:
    return json.loads(rotation_state.pending_path(sdir, PANE).read_text(encoding="utf-8"))


def _scratch_files(sdir: Path) -> List[str]:
    return sorted(p.name for p in sdir.iterdir() if p.name.endswith(".tmp"))


# ── the marker changes only on a transition ──────────────────────────────────


def test_first_crossing_writes_the_marker_and_reports_a_transition(tmp_path):
    assert _pending(tmp_path) is True
    marker = _marker(tmp_path)
    assert marker["session_id"] == SESSION and marker["level"] == "force"
    assert marker["first_seen"] and marker["transitioned"]
    assert rotation_state.pending_session_id(tmp_path, PANE) == SESSION


def test_a_repeat_in_the_same_band_leaves_the_marker_byte_for_byte_untouched(tmp_path):
    assert _pending(tmp_path) is True
    path = rotation_state.pending_path(tmp_path, PANE)
    before = path.read_bytes()
    os.utime(path, (1_000_000_000, 1_000_000_000))
    inode = path.stat().st_ino

    assert _pending(tmp_path, tokens=560_000, transcript_path="/other.jsonl") is False

    assert path.read_bytes() == before
    assert path.stat().st_mtime == 1_000_000_000
    assert path.stat().st_ino == inode, "the marker was replaced although nothing transitioned"


def test_a_band_change_is_a_transition_and_keeps_first_seen(tmp_path):
    _pending(tmp_path)
    first = _marker(tmp_path)

    assert _pending(tmp_path, level="hard", tokens=610_000) is True

    marker = _marker(tmp_path)
    assert marker["level"] == "hard" and marker["tokens"] == 610_000
    assert marker["first_seen"] == first["first_seen"]


def test_a_new_session_in_the_same_pane_is_a_transition(tmp_path):
    _pending(tmp_path)
    assert _pending(tmp_path, session_id="sess-successor") is True
    assert rotation_state.pending_session_id(tmp_path, PANE) == "sess-successor"


# ── ledger first (ADR-005) ───────────────────────────────────────────────────


def test_the_ledger_append_runs_before_the_marker_is_created(tmp_path):
    path = rotation_state.pending_path(tmp_path, PANE)
    seen = []

    def record() -> bool:
        seen.append(path.exists())
        return True

    assert _pending(tmp_path, record_transition=record) is True
    assert seen == [False]
    assert path.exists()


def test_the_ledger_append_runs_before_a_band_change_touches_the_marker(tmp_path):
    _pending(tmp_path)
    seen = []

    def record() -> bool:
        seen.append(_marker(tmp_path)["level"])
        return True

    assert _pending(tmp_path, level="hard", record_transition=record) is True
    assert seen == ["force"], "the marker already said hard while the ledger was being written"
    assert _marker(tmp_path)["level"] == "hard"


def test_no_ledger_append_when_nothing_transitions(tmp_path):
    _pending(tmp_path)
    calls = []
    assert _pending(tmp_path, record_transition=lambda: calls.append(1) or True) is False
    assert calls == []


def test_a_ledger_that_refuses_leaves_the_marker_alone_and_the_next_call_retries(tmp_path):
    path = rotation_state.pending_path(tmp_path, PANE)

    assert _pending(tmp_path, record_transition=lambda: False) is False
    assert not path.exists()

    recorded = []
    assert _pending(tmp_path, record_transition=lambda: recorded.append(1) or True) is True
    assert recorded == [1] and path.exists()


def test_a_refused_band_change_keeps_the_old_marker(tmp_path):
    _pending(tmp_path)
    before = rotation_state.pending_path(tmp_path, PANE).read_bytes()

    assert _pending(tmp_path, level="hard", record_transition=lambda: False) is False

    assert rotation_state.pending_path(tmp_path, PANE).read_bytes() == before


# ── one critical section (OI-1486) ───────────────────────────────────────────


def test_parallel_crossings_record_exactly_one_transition(tmp_path):
    """Parallel tool calls each fire a PreToolUse hook process. Without the lock, every one of
    them reads "no marker", decides "new", and records the transition."""
    workers = 12
    barrier = threading.Barrier(workers)
    recorded: List[int] = []
    results: List[bool] = []
    errors: List[BaseException] = []

    def record() -> bool:
        recorded.append(1)
        return True

    def cross() -> None:
        try:
            barrier.wait(timeout=30)
            results.append(_pending(tmp_path, record_transition=record))
        except BaseException as exc:  # surfaced in the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=cross) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    assert results.count(True) == 1 and results.count(False) == workers - 1
    assert len(recorded) == 1
    assert _marker(tmp_path)["session_id"] == SESSION


# ── scratch names: per writer, and none left behind ──────────────────────────


def test_marker_and_latch_writes_leave_no_scratch_file_behind(tmp_path):
    _pending(tmp_path)
    _pending(tmp_path, level="hard")
    rotation_state.write_latch(tmp_path, session_id=SESSION, pane=PANE, old_window="@1",
                               new_window="@2")
    assert _scratch_files(tmp_path) == []


def test_latch_payload_and_freshness(tmp_path):
    written = rotation_state.write_latch(tmp_path, session_id=SESSION, pane=PANE,
                                         old_window="@1", new_window="@2")
    assert sorted(p.name for p in written) == sorted([
        rotation_state.latch_path_for_session(tmp_path, SESSION).name,
        rotation_state.latch_path_for_pane(tmp_path, PANE).name,
    ])
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["old_window"] == "@1" and payload["new_window"] == "@2"
    assert rotation_state.is_latched(tmp_path, session_id=SESSION, pane="")
    assert rotation_state.is_latched(tmp_path, session_id="", pane=PANE)


# ── the latch command: ledger first ──────────────────────────────────────────


def test_latch_command_appends_to_the_ledger_before_the_latch_exists(tmp_path, monkeypatch):
    monkeypatch.setenv(rotation_state.STATE_DIR_ENV, str(tmp_path))
    session_latch = rotation_state.latch_path_for_session(tmp_path, SESSION)
    pane_latch = rotation_state.latch_path_for_pane(tmp_path, PANE)
    events = []

    def fake_emit(trigger, *, file, **fields):
        events.append({"trigger": trigger, "file": file,
                       "latches_present": [session_latch.exists(), pane_latch.exists()]})
        return True

    monkeypatch.setattr(rotation_state, "emit_event", fake_emit)

    rc = rotation_state.main(["latch", "--pane", PANE, "--session-id", SESSION,
                              "--old-window", "@1", "--new-window", "@2"])

    assert rc == 0
    assert events == [{"trigger": "t0_context_rotation_started", "file": str(session_latch),
                       "latches_present": [False, False]}]
    assert session_latch.exists() and pane_latch.exists()


def test_latch_command_still_latches_when_the_ledger_refuses(tmp_path, monkeypatch, capsys):
    """The successor is already up when the latch is written: withholding the latch would only
    make the old session nag, and nothing can retry the transition. The failure is reported."""
    monkeypatch.setenv(rotation_state.STATE_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(rotation_state, "emit_event", lambda trigger, *, file, **f: False)

    rc = rotation_state.main(["latch", "--pane", PANE, "--session-id", SESSION,
                              "--old-window", "@1", "--new-window", "@2"])

    assert rc == 0
    assert rotation_state.is_latched(tmp_path, session_id=SESSION, pane=PANE)
    assert "ledger" in capsys.readouterr().err


def test_latch_command_without_any_session_id_latches_by_pane_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(rotation_state.STATE_DIR_ENV, str(tmp_path))
    events = []
    monkeypatch.setattr(rotation_state, "emit_event",
                        lambda trigger, *, file, **f: events.append(file) or True)

    assert rotation_state.main(["latch", "--pane", PANE, "--old-window", "@1",
                                "--new-window", "@2"]) == 0

    assert events == [str(rotation_state.latch_path_for_pane(tmp_path, PANE))]
    assert "latching by pane only" in capsys.readouterr().err
    assert not rotation_state.latch_path_for_session(tmp_path, SESSION).exists()


@pytest.mark.parametrize("missing", ["--old-window", "--new-window"])
def test_latch_command_rejects_a_missing_window(tmp_path, monkeypatch, missing):
    monkeypatch.setenv(rotation_state.STATE_DIR_ENV, str(tmp_path))
    argv = ["latch", "--pane", PANE, "--old-window", "@1", "--new-window", "@2"]
    idx = argv.index(missing)
    del argv[idx:idx + 2]
    with pytest.raises(SystemExit) as exc:
        rotation_state.main(argv)
    assert exc.value.code == 2
    assert not list(tmp_path.glob("latch-*"))


# ── wrap proof: wrap-start / wrap-finish ─────────────────────────────────────

SLUG = "vnx-orchestration"
STATE_PY = REPO_ROOT / "scripts" / "lib" / "t0_rotation_state.py"
DAILY = "daily-log/2026-10-16.md"
HANDOFF_BODY = """# Handoff — vnx-orchestration — 2026-10-16

## Next steps (om koud op te pakken)

1. **#2080 mergen**: lees het rapport.
2. **G5b dispatchen**.
"""
GOAL = """
## Actief /goal
Directive: `/goal werk de track af`
Resterend:
- G5b
"""


def _h(slug: str, title: str) -> str:
    return f"### 2026-10-16 · {slug} · {title}"


def _write_bucket(path: Path, headings: List[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{h}\n- **wat:** iets\n- **zichtbaarheid:** abstract\n\n" for h in headings)
    path.write_text(f"# Build-Log Bucket — 2026-10\n\n---\n\n{body}", encoding="utf-8")
    return path


def _append_bucket(path: Path, headings: List[str]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write("".join(f"{h}\n- **wat:** nieuw\n\n" for h in headings))


def _project(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "project"
    (root / "daily-log").mkdir(parents=True)
    monkeypatch.setenv(rotation_state.STATE_DIR_ENV, str(tmp_path / "state"))
    return root


def _write_daily(root: Path, *, mtime: float = None, flag: str = "true", extra: str = "") -> Path:
    path = root / DAILY
    path.write_text(f"---\ndate: 2026-10-16\n{extra}handoff: {flag}\n---\n\n# Log\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _write_handoff(root: Path, text: str = HANDOFF_BODY) -> Path:
    path = root / "daily-log" / "handoff.md"
    path.write_text(text, encoding="utf-8")
    return path


def _compose(root: Path, handoff: Path, out: Path, *extra: str) -> int:
    return rotation_state.main(["compose", "--handoff", str(handoff),
                                "--out-dir", str(out), "--project-root", str(root), *extra])


def test_wrap_start_and_finish_count_exactly_this_slugs_new_headings(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, monkeypatch)
    bucket = _write_bucket(tmp_path / "bucket" / "2026-10.md",
                           [_h(SLUG, "al-van-vanochtend"), _h("seocrawler-v2", "ander-project")])
    assert rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG,
                                "--bucket", str(bucket)]) == 0
    start = json.loads((tmp_path / "state" / "wrap-start.json").read_text(encoding="utf-8"))
    assert start["project_slug"] == SLUG and start["bucket"] == str(bucket)
    assert start["headings"] == [_h(SLUG, "al-van-vanochtend"), _h("seocrawler-v2", "ander-project")]

    _append_bucket(bucket, [_h("seocrawler-v2", "ook-nieuw-maar-niet-van-mij"), _h(SLUG, "gate-loopt-vast"),
                            _h(SLUG, "ledger-eerst")])
    handoff = _write_handoff(root)
    _write_daily(root)
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 0

    block = rotation_state.read_frontmatter(handoff)["wrap"]
    assert block["nugget_headings"] == [_h(SLUG, "gate-loopt-vast"), _h(SLUG, "ledger-eerst")]
    assert block["nuggets"] == 2
    assert block["daily_log"] == DAILY and block["bucket"] == str(bucket)
    assert block["started_at"] == start["started_at"]
    assert block["start_id"] == start["start_id"] and len(start["start_id"]) == 16
    assert rotation_state._parse_utc(block["started_at"]) <= rotation_state._parse_utc(block["wrapped_at"])
    assert _compose(root, handoff, tmp_path / "run") == 0
    assert "wrap-finish:" in capsys.readouterr().out


def test_a_second_nugget_under_an_existing_title_still_counts():
    before = [_h(SLUG, "zelfde-titel")]
    after = [_h(SLUG, "zelfde-titel"), _h("other", "x"), _h(SLUG, "zelfde-titel")]
    assert rotation_state.new_nugget_headings(before, after, SLUG) == [_h(SLUG, "zelfde-titel")]
    assert rotation_state.new_nugget_headings(after, after, SLUG) == []


def test_a_bucket_created_during_the_wrap_counts_its_headings(tmp_path, monkeypatch):
    """The first nugget of a month creates the bucket file after wrap-start ran."""
    root = _project(tmp_path, monkeypatch)
    bucket = tmp_path / "bucket" / "2026-11.md"
    assert rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG,
                                "--bucket", str(bucket)]) == 0
    _write_bucket(bucket, [_h(SLUG, "eerste-van-de-maand")])
    handoff = _write_handoff(root)
    _write_daily(root)
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 0
    assert rotation_state.read_frontmatter(handoff)["wrap"]["nuggets"] == 1


def test_a_wrap_without_a_bucket_records_no_nuggets(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    assert rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG]) == 0
    handoff = _write_handoff(root)
    _write_daily(root)
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 0
    block = rotation_state.read_frontmatter(handoff)["wrap"]
    assert block["nuggets"] == 0 and block["nugget_headings"] == [] and block["bucket"] is None
    assert "  bucket: null\n" in handoff.read_text(encoding="utf-8")
    assert _compose(root, handoff, tmp_path / "run") == 0


def test_wrap_finish_keeps_every_other_frontmatter_key_and_the_body(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    front = ("---\ncontext: dev\nproject: vnx-orchestration\ntags:\n  - rotatie\n  - g5a\n"
             "wrap:\n  started_at: 2026-10-01T08:00:00Z\n  wrapped_at: 2026-10-01T08:05:00Z\n"
             "  nuggets: 0\n  nugget_headings: []\n  daily_log: \"daily-log/2026-10-01.md\"\n"
             "  bucket: null\nbranch: main\n---\n")
    body = "\n# Handoff\n\nTekst met --- erin en een regel wrap: die geen sleutel is.\n" + HANDOFF_BODY
    handoff = _write_handoff(root, front + body)
    rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG])
    _write_daily(root)
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 0

    text = handoff.read_text(encoding="utf-8")
    head, rest = text.split("\n---\n", 1)
    assert rest == body, "the body changed"
    kept = [ln for ln in head.splitlines()[1:] if not ln.startswith("  ") and ln != "wrap:"]
    assert kept == ["context: dev", "project: vnx-orchestration", "tags:", "branch: main"]
    assert "  - rotatie\n  - g5a\n" in head + "\n"
    assert head.count("wrap:") == 1 and "2026-10-01T08:05:00Z" not in head
    assert rotation_state.read_frontmatter(handoff)["wrap"]["daily_log"] == DAILY


def test_wrap_finish_gives_a_handoff_without_frontmatter_one(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    handoff = _write_handoff(root)
    rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG])
    _write_daily(root)
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 0
    text = handoff.read_text(encoding="utf-8")
    assert text.startswith("---\nwrap:\n") and text.endswith("---\n" + HANDOFF_BODY)
    assert rotation_state.parse_handoff(handoff).first_step == "**#2080 mergen**: lees het rapport."


def test_wrap_finish_without_wrap_start_refuses_and_writes_nothing(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, monkeypatch)
    handoff = _write_handoff(root)
    _write_daily(root)
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 3
    assert "run wrap-start" in capsys.readouterr().err
    assert handoff.read_text(encoding="utf-8") == HANDOFF_BODY


@pytest.mark.parametrize("missing", ["handoff", "daily_log"])
def test_wrap_finish_refuses_a_missing_handoff_or_daily_log(tmp_path, monkeypatch, missing):
    root = _project(tmp_path, monkeypatch)
    rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG])
    if missing != "handoff":
        _write_handoff(root)
    if missing != "daily_log":
        _write_daily(root)
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 3


def test_wrap_finish_says_when_the_spawner_will_refuse_its_proof(tmp_path, monkeypatch, capsys):
    """The check runs before anything is written (round 3): a refused block is never published."""
    root = _project(tmp_path, monkeypatch)
    handoff = _write_handoff(root)
    rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG])
    _write_daily(root, flag="false")
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 6
    err = capsys.readouterr().err
    assert "'daily_log_handoff'" in err and "wrote nothing" in err
    assert handoff.read_text(encoding="utf-8") == HANDOFF_BODY


@pytest.mark.parametrize("slug", ["", "  ", "a · b"])
def test_wrap_start_refuses_a_slug_it_cannot_match(tmp_path, monkeypatch, slug):
    root = _project(tmp_path, monkeypatch)
    assert rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", slug]) == 2
    assert not (tmp_path / "state" / "wrap-start.json").exists()


def test_wrap_start_refuses_a_bucket_it_cannot_read(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, monkeypatch)
    bucket = tmp_path / "kapot.md"
    bucket.write_bytes(b"\xff\xfe### kapot\n")
    assert rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG,
                                "--bucket", str(bucket)]) == 3
    assert "wrap-start: cannot record the start" in capsys.readouterr().err
    assert not (tmp_path / "state" / "wrap-start.json").exists()


def test_a_wrap_of_twenty_minutes_passes(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    sdir = tmp_path / "state"
    began = time.time() - 20 * 60 - 30
    rotation_state.start_wrap(sdir, project_root=str(root), slug=SLUG, bucket=None, now=began)
    handoff = _write_handoff(root)
    _write_daily(root, mtime=began + 12 * 60)
    rotation_state.finish_wrap(sdir, project_root=root, handoff=handoff, daily_log=root / DAILY,
                               now=time.time() - 30)
    assert _compose(root, handoff, tmp_path / "run") == 0
    block = rotation_state.verify_wrap_proof(handoff, project_root=root, sdir=sdir, max_age_minutes=15)
    span = rotation_state._parse_utc(block["wrapped_at"]) - rotation_state._parse_utc(block["started_at"])
    assert span >= 20 * 60


def test_wrap_start_and_finish_leave_no_scratch_file_behind(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG])
    _write_handoff(root)
    _write_daily(root)
    rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY])
    assert _scratch_files(tmp_path / "state") == []
    assert _scratch_files(root / "daily-log") == []


# ── one wrap-start proves one wrap ───────────────────────────────────────────


def _start_record(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "state" / "wrap-start.json").read_text(encoding="utf-8"))


def test_a_second_finish_without_a_new_start_cannot_refresh_an_old_proof(tmp_path, monkeypatch,
                                                                          capsys):
    """codex_gate on 3c2bdd24: a second finish stamped a fresh wrapped_at on a wrap of two hours
    ago, and the unchanged old daily log then passed the age check."""
    root = _project(tmp_path, monkeypatch)
    sdir = tmp_path / "state"
    now = time.time()
    rotation_state.start_wrap(sdir, project_root=str(root), slug=SLUG, bucket=None, now=now - 7200)
    handoff = _write_handoff(root)
    _write_daily(root, mtime=now - 7100)
    rotation_state.finish_wrap(sdir, project_root=root, handoff=handoff, daily_log=root / DAILY,
                               now=now - 7000)
    with pytest.raises(rotation_state.WrapProofError) as old:
        rotation_state.verify_wrap_proof(handoff, project_root=root, sdir=sdir, max_age_minutes=15, now=now)
    assert old.value.check == "wrapped_at_age"
    proof = handoff.read_text(encoding="utf-8")

    with pytest.raises(rotation_state.WrapHelperError, match="run wrap-start first"):
        rotation_state.finish_wrap(sdir, project_root=root, handoff=handoff,
                                   daily_log=root / DAILY, now=now)
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 3
    assert "run wrap-start first" in capsys.readouterr().err
    assert handoff.read_text(encoding="utf-8") == proof, "a refused finish rewrote the proof"
    assert _compose(root, handoff, tmp_path / "run") == 6
    assert "wrap proof check 'wrapped_at_age'" in capsys.readouterr().err


def test_each_new_wrap_start_proves_one_more_wrap(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    handoff = _write_handoff(root)
    for run in ("first", "second"):
        assert rotation_state.main(["wrap-start", "--project-root", str(root),
                                    "--project-slug", SLUG]) == 0
        assert "consumed_at" not in _start_record(tmp_path)
        _write_daily(root)
        assert rotation_state.main(["wrap-finish", "--project-root", str(root),
                                    "--daily-log", DAILY]) == 0, run
        block = rotation_state.read_frontmatter(handoff)["wrap"]
        assert _start_record(tmp_path)["consumed_at"] == block["wrapped_at"]
        assert _compose(root, handoff, tmp_path / f"run-{run}") == 0, run


def test_a_start_record_without_a_start_id_proves_nothing(tmp_path, monkeypatch, capsys):
    """A record an older fabric wrote: no block could bind to it, so the wrap starts again."""
    root = _project(tmp_path, monkeypatch)
    handoff = _write_handoff(root)
    assert rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG]) == 0
    record = _start_record(tmp_path)
    del record["start_id"]
    (tmp_path / "state" / "wrap-start.json").write_text(json.dumps(record), encoding="utf-8")
    _write_daily(root)
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 3
    assert "carries no start_id" in capsys.readouterr().err
    assert handoff.read_text(encoding="utf-8") == HANDOFF_BODY


@pytest.mark.parametrize("failure", ["daily_log_missing", "self_check"])
def test_a_failed_finish_leaves_the_start_usable(tmp_path, monkeypatch, failure):
    """The operator repairs the daily log and finishes again, without a new wrap-start."""
    root = _project(tmp_path, monkeypatch)
    handoff = _write_handoff(root)
    assert rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG]) == 0
    started = _start_record(tmp_path)
    if failure == "self_check":
        _write_daily(root, flag="false")
    expected = 3 if failure == "daily_log_missing" else 6
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == expected
    assert _start_record(tmp_path) == started, "a failed finish consumed the start"

    _write_daily(root)
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 0
    assert rotation_state.read_frontmatter(handoff)["wrap"]["started_at"] == started["started_at"]
    assert _compose(root, handoff, tmp_path / "run") == 0


def test_two_finishes_racing_on_one_start_let_exactly_one_through(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    sdir = tmp_path / "state"
    rotation_state.start_wrap(sdir, project_root=str(root), slug=SLUG, bucket=None)
    handoff = _write_handoff(root)
    _write_daily(root)
    barrier = threading.Barrier(2)
    outcomes: List[str] = []

    def finish() -> None:
        barrier.wait()
        try:
            rotation_state.finish_wrap(sdir, project_root=root, handoff=handoff, daily_log=root / DAILY)
            outcomes.append("passed")
        except rotation_state.WrapHelperError:
            outcomes.append("refused")

    threads = [threading.Thread(target=finish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["passed", "refused"]


# ── the old wrap block goes as a whole, and a rewrite that does not parse is refused ──
# OI-2116, codex_gate on cd126146.

OLD_WRAP_BLOCK = ("wrap:\n  started_at: 2026-10-01T08:00:00Z\n\n  wrapped_at: 2026-10-01T08:05:00Z\n"
                  "# een opmerking zonder inspringing\n  nuggets: 0\n  nugget_headings: []\n"
                  "  daily_log: \"daily-log/2026-10-01.md\"\n  bucket: null\n")
WRAP_FIELDS = {"started_at", "wrapped_at", "nuggets", "nugget_headings", "daily_log", "bucket"}


def _yaml_frontmatter(path: Path):
    """The frontmatter through PyYAML, the independent judge of "it parses"."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    return yaml.safe_load(text[len("---\n"):].split("\n---\n", 1)[0])


@pytest.mark.parametrize("before, after", [
    ("context: dev\n", "branch: main\n"),
    ("context: dev\n", ""),
    ("", "context: dev\nbranch: main\n"),
], ids=["middle", "last", "first"])
def test_wrap_finish_removes_the_old_block_up_to_the_next_key_and_the_yaml_parses(
        tmp_path, monkeypatch, before, after):
    """The old block held a blank line and an unindented comment. It was removed only up to the
    first of them; its remaining fields survived without their key, and wrap-finish and compose
    both returned 0 on frontmatter that does not parse."""
    root = _project(tmp_path, monkeypatch)
    handoff = _write_handoff(root, f"---\n{before}{OLD_WRAP_BLOCK}{after}---\n{HANDOFF_BODY}")
    assert set(_yaml_frontmatter(handoff)) >= {"wrap"}, "the fixture itself must parse"
    assert rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG]) == 0
    _write_daily(root)
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 0

    front = _yaml_frontmatter(handoff)
    assert list(front) == [k for k in ("context", "branch") if k in before + after] + ["wrap"]
    assert set(front["wrap"]) >= WRAP_FIELDS and front["wrap"]["daily_log"] == DAILY
    text = handoff.read_text(encoding="utf-8")
    assert "2026-10-01" not in text and "een opmerking" not in text, "the old block left lines behind"
    assert text.endswith("---\n" + HANDOFF_BODY)
    assert _compose(root, handoff, tmp_path / "run") == 0


BROKEN_FRONTMATTER = {
    "orphan_under_scalar": "context: dev\n  verdwaald: 1\n",
    "orphan_after_blank": "context: dev\n\n  verdwaald: 1\n",
    "orphan_after_quoted": "title: \"klaar\"\n  verdwaald: 1\n",
    "text_after_quoted": "title: \"klaar\"\n  verder\n",
    "orphan_after_flow": "tags: [a, b]\n  verdwaald: 1\n",
    "list_then_key": "tags:\n  - a\n  verdwaald: 1\n",
    "key_then_list": "meta:\n  a: 1\n  - b\n",
    "dedent": "meta:\n    a: 1\n  b: 2\n",
    "indented_first": "  verdwaald: 1\ncontext: dev\n",
    "not_a_key": "context: dev\nzomaar tekst\n",
    "tab_indent": "meta:\n\ta: 1\n",
    "list_after_scalar": "context: dev\n- a\n",
    "text_at_child_level": "meta:\n  a: 1\n  zomaar\n",
    "colon_in_plain_value": "note: zie daily-log 2026-09-27: 22,4M tokens\n",
    "colon_in_child_value": "meta:\n  note: a: b\n",
    "value_ends_on_colon": "note: waarde:\n",
}
VALID_FRONTMATTER = {
    "compact_list": "tags:\n- rotatie\n- g5a\nbranch: main\n",
    "nested_list": "tags:\n  - rotatie\n  - g5a\n",
    "block_scalar": "notes: |\n  stap: een\n\n  stap: twee\nbranch: main\n",
    "folded_scalar": "notes: >-\n  lang\n  verhaal\n",
    "multiline_plain": "title: een lange\n  titel\n",
    "multiline_quoted": "title: \"een: lange\n  titel: ja\"\n",
    "open_flow": "tags: [rotatie,\n  g5a]\n",
    "nested_map": "meta:\n  owner: vincent\n  sub:\n    diep: 1\n",
    "anchor": "base: &b\n  a: 1\n",
    "comments_and_blanks": "# kop\ncontext: dev\n\n# tussen\nbranch: main  # staart\n",
    "empty_value": "leeg:\nbranch: main\n",
    "quoted_and_date_keys": "\"sleutel met spatie\": 1\n2026-10-16: datum\n",
    "url_continuation": "title: zie\n  https://x.nl/a\n",
    "colon_without_space": "time: 12:30\nurl: https://x.nl # om: 12 uur\n",
    "quoted_colon": "note: \"a: b\"\nmeta:\n  note: 'c: d'\n",
    "compact_list_under_child": "meta:\n  tags:\n  - a\n  - b\n  owner: x\n",
}


@pytest.mark.parametrize("case", sorted(BROKEN_FRONTMATTER))
def test_a_rewrite_that_would_not_parse_is_refused_and_the_handoff_stays_untouched(
        tmp_path, monkeypatch, capsys, case):
    root = _project(tmp_path, monkeypatch)
    original = f"---\n{BROKEN_FRONTMATTER[case]}---\n{HANDOFF_BODY}"
    handoff = _write_handoff(root, original)
    with pytest.raises(yaml.YAMLError):
        _yaml_frontmatter(handoff)
    assert rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG]) == 0
    started = _start_record(tmp_path)
    _write_daily(root)

    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 3
    err = capsys.readouterr().err
    assert "does not parse" in err and "nothing written" in err, err
    assert handoff.read_text(encoding="utf-8") == original
    assert _start_record(tmp_path) == started, "a refused rewrite consumed the start"
    with pytest.raises(ValueError, match="does not parse"):
        rotation_state.write_wrap_block(handoff, _block())
    assert handoff.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("case", sorted(VALID_FRONTMATTER))
def test_a_frontmatter_yaml_reads_is_rewritten_and_keeps_its_keys(tmp_path, monkeypatch, case):
    """The other side of the refusal: valid YAML that a handoff may carry is never refused."""
    root = _project(tmp_path, monkeypatch)
    handoff = _write_handoff(root, f"---\n{VALID_FRONTMATTER[case]}---\n{HANDOFF_BODY}")
    keys = list(_yaml_frontmatter(handoff))
    assert rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG]) == 0
    _write_daily(root)
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 0
    front = _yaml_frontmatter(handoff)
    assert list(front) == keys + ["wrap"]
    assert _compose(root, handoff, tmp_path / "run") == 0


def test_compose_refuses_a_proof_in_frontmatter_that_does_not_parse(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, monkeypatch)
    handoff = _write_handoff(root, f"---\ncontext: dev\n---\n{HANDOFF_BODY}")
    assert rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG]) == 0
    _write_daily(root)
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 0
    text = handoff.read_text(encoding="utf-8")
    handoff.write_text(text.replace("context: dev\n", "context: dev\n  verdwaald: 1\n", 1), encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        _yaml_frontmatter(handoff)

    assert _compose(root, handoff, tmp_path / "run") == 6
    err = capsys.readouterr().err
    assert "wrap proof check 'wrap_block'" in err and "does not parse" in err


def test_a_daily_log_with_an_odd_line_still_shows_its_handoff_flag(tmp_path, monkeypatch):
    """Only the handoff is read strictly. The daily log stays lenient: one odd line in it never
    hides the `handoff: true` around it."""
    root = _project(tmp_path, monkeypatch)
    handoff = _write_handoff(root)
    assert rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG]) == 0
    _write_daily(root, extra="  verdwaald: 1\n")
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 0
    assert _compose(root, handoff, tmp_path / "run") == 0


# ── consumption and publication are one step (codex_gate advisory on cd126146) ──


class _Crash(BaseException):
    """The process dies mid-finish: nothing in finish_wrap may catch this."""


def _inject(monkeypatch, name: str, target: Path, *, when: str):
    """Make the module's ``name`` (atomic_write_json or atomic_write_text) fail for ``target``:
    ``before`` raises OSError and writes nothing, ``after`` writes and then dies. Returns the
    real function, so the test can put it back."""
    real = getattr(rotation_state, name)

    def write(path, *args, **kwargs):
        if Path(path) != target:
            return real(path, *args, **kwargs)
        if when == "before":
            raise OSError(28, "No space left on device")
        real(path, *args, **kwargs)
        raise _Crash(f"died after writing {path}")

    monkeypatch.setattr(rotation_state, name, write)
    return real


def _wrap_until_finish(tmp_path: Path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    handoff = _write_handoff(root)
    assert rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG]) == 0
    _write_daily(root)
    return root, handoff, tmp_path / "state" / "wrap-start.json"


def test_a_failed_consumption_write_publishes_no_proof(tmp_path, monkeypatch, capsys):
    """codex_gate on cd126146 injected exactly this: the proof was already published when the
    consumption write failed, so wrap-finish reported a failure while compose accepted the proof,
    and the start stayed reusable beside it."""
    root, handoff, record = _wrap_until_finish(tmp_path, monkeypatch)
    started = _start_record(tmp_path)
    real = _inject(monkeypatch, "atomic_write_json", record, when="before")

    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 3
    err = capsys.readouterr().err
    assert _start_record(tmp_path) == started
    assert _compose(root, handoff, tmp_path / "run-a") == 6, "compose accepted a proof beside an unconsumed start"
    assert handoff.read_text(encoding="utf-8") == HANDOFF_BODY
    assert "nothing published" in err

    monkeypatch.setattr(rotation_state, "atomic_write_json", real)
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 0
    assert _compose(root, handoff, tmp_path / "run-b") == 0
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 3


@pytest.mark.parametrize("when", ["after", "before"], ids=["crash", "error"])
def test_between_consumption_and_publication_the_wrap_fails_and_a_new_wrap_start_repairs_it(
        tmp_path, monkeypatch, capsys, when):
    """Crash point 1: the start is consumed (first write), the block is not published (the second
    write died or failed). That is a failed wrap, never a reusable start beside a valid proof."""
    root, handoff, record = _wrap_until_finish(tmp_path, monkeypatch)
    if when == "after":
        _inject(monkeypatch, "atomic_write_json", record, when="after")
        with pytest.raises(_Crash):
            rotation_state.finish_wrap(tmp_path / "state", project_root=root.resolve(),
                                       handoff=handoff, daily_log=root / DAILY)
        monkeypatch.undo()
        monkeypatch.setenv(rotation_state.STATE_DIR_ENV, str(tmp_path / "state"))
    else:
        real = _inject(monkeypatch, "atomic_write_text", handoff, when="before")
        assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 3
        monkeypatch.setattr(rotation_state, "atomic_write_text", real)
    err = capsys.readouterr().err

    assert "consumed_at" in _start_record(tmp_path), "the start stayed reusable"
    assert handoff.read_text(encoding="utf-8") == HANDOFF_BODY
    assert _compose(root, handoff, tmp_path / "run-a") == 6
    assert when == "after" or "run wrap-start" in err
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 3
    assert "run wrap-start first" in capsys.readouterr().err

    assert rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG]) == 0
    _write_daily(root)
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 0
    assert _compose(root, handoff, tmp_path / "run-b") == 0


def test_a_crash_after_publication_leaves_a_valid_proof_and_a_consumed_start(tmp_path, monkeypatch, capsys):
    """Crash point 2: both writes landed and the process died before it returned."""
    root, handoff, _ = _wrap_until_finish(tmp_path, monkeypatch)
    _inject(monkeypatch, "atomic_write_text", handoff, when="after")
    with pytest.raises(_Crash):
        rotation_state.finish_wrap(tmp_path / "state", project_root=root.resolve(),
                                   handoff=handoff, daily_log=root / DAILY)
    monkeypatch.undo()
    monkeypatch.setenv(rotation_state.STATE_DIR_ENV, str(tmp_path / "state"))

    block = rotation_state.read_frontmatter(handoff)["wrap"]
    assert _start_record(tmp_path).get("consumed_at") == block["wrapped_at"]
    assert _compose(root, handoff, tmp_path / "run") == 0
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 3
    assert "run wrap-start first" in capsys.readouterr().err


@pytest.mark.parametrize("case", ["no_record", "newer_start", "unconsumed", "consumed_by_another",
                                  "no_start_id"])
def test_compose_accepts_a_block_only_when_its_start_record_was_consumed_by_it(
        tmp_path, monkeypatch, capsys, case):
    root, handoff, record = _wrap_until_finish(tmp_path, monkeypatch)
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 0
    data = _start_record(tmp_path)
    if case == "no_record":
        record.unlink()
    elif case == "newer_start":
        assert rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG]) == 0
    elif case == "unconsumed":
        data.pop("consumed_at", None)
        record.write_text(json.dumps(data), encoding="utf-8")
    elif case == "consumed_by_another":
        data["consumed_at"] = rotation_state._iso_utc(time.time() - 100)
        record.write_text(json.dumps(data), encoding="utf-8")
    else:
        text = handoff.read_text(encoding="utf-8")
        handoff.write_text("".join(ln for ln in text.splitlines(keepends=True)
                                   if not ln.startswith("  start_id:")), encoding="utf-8")
    assert _compose(root, handoff, tmp_path / "run") == 6
    assert "wrap proof check 'wrap_start_binding'" in capsys.readouterr().err


# ── wrap proof: compose refuses with 6 per named check ───────────────────────


def _block(**overrides) -> dict:
    now = time.time()
    block = {"start_id": "0123456789abcdef", "started_at": rotation_state._iso_utc(now - 300),
             "wrapped_at": rotation_state._iso_utc(now - 10), "nuggets": 0,
             "nugget_headings": [], "daily_log": DAILY, "bucket": None}
    block.update(overrides)
    return block


def _publish(tmp_path: Path, handoff: Path, block: dict) -> None:
    """What a completed wrap-finish leaves: the start record the block names, consumed by it,
    and the block in the handoff. Each case below then fails on its own check only."""
    record = {"start_id": block["start_id"], "started_at": block["started_at"],
              "project_root": str(handoff.parent.parent), "project_slug": SLUG, "bucket": block["bucket"],
              "headings": [], "consumed_at": block["wrapped_at"]}
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "wrap-start.json").write_text(json.dumps(record), encoding="utf-8")
    rotation_state.write_wrap_block(handoff, block)


def _proof_case(tmp_path: Path, monkeypatch, case: str):
    root = _project(tmp_path, monkeypatch)
    handoff = _write_handoff(root)
    now = time.time()
    _write_daily(root, mtime=now - 60)
    bucket = _write_bucket(tmp_path / "bucket.md", [_h(SLUG, "echt")])
    blocks = {
        "wrap_times": _block(started_at="vanochtend"),
        "wrap_order": _block(started_at=rotation_state._iso_utc(now - 5),
                             wrapped_at=rotation_state._iso_utc(now - 300)),
        "wrapped_at_future": _block(wrapped_at=rotation_state._iso_utc(now + 300)),
        "wrapped_at_age": _block(started_at=rotation_state._iso_utc(now - 7500),
                                 wrapped_at=rotation_state._iso_utc(now - 7200)),
        "daily_log": _block(daily_log="daily-log/2026-10-15.md"),
        "nuggets_count": _block(nuggets=2, nugget_headings=[_h(SLUG, "echt")], bucket=str(bucket)),
        "nugget_in_bucket": _block(nuggets=1, nugget_headings=[_h(SLUG, "verzonnen")], bucket=str(bucket)),
        "bucket_missing": _block(nuggets=1, nugget_headings=[_h(SLUG, "echt")],
                                 bucket=str(tmp_path / "geen-bucket.md")),
        "bucket_not_utf8": _block(nuggets=1, nugget_headings=[_h(SLUG, "echt")],
                                  bucket=str(tmp_path / "kapot.md")),
    }
    (tmp_path / "kapot.md").write_bytes(b"\xff\xfe### kapot\n")
    if case in blocks:
        _publish(tmp_path, handoff, blocks[case])
    elif case == "unclosed":
        handoff.write_text("---\nwrap:\n  nuggets: 0\n" + HANDOFF_BODY, encoding="utf-8")
    elif case == "daily_log_handoff":
        _publish(tmp_path, handoff, _block())
        _write_daily(root, mtime=now - 60, flag="false")
    elif case in ("before_start", "after_wrap"):
        _publish(tmp_path, handoff, _block())
        _write_daily(root, mtime=now - 3600 if case == "before_start" else now + 120)
    return root, handoff


@pytest.mark.parametrize("case, check", [
    ("no_block", "wrap_block"),
    ("unclosed", "wrap_block"),
    ("wrap_times", "wrap_times"),
    ("wrap_order", "wrap_order"),
    ("wrapped_at_future", "wrapped_at_future"),
    ("wrapped_at_age", "wrapped_at_age"),
    ("daily_log", "daily_log"),
    ("daily_log_handoff", "daily_log_handoff"),
    ("before_start", "daily_log_window"),
    ("after_wrap", "daily_log_window"),
    ("nuggets_count", "nuggets_count"),
    ("nugget_in_bucket", "nugget_in_bucket"),
    ("bucket_missing", "nugget_in_bucket"),
    ("bucket_not_utf8", "nugget_in_bucket"),
])
def test_compose_refuses_a_failed_wrap_check_with_6_and_names_it(tmp_path, monkeypatch, capsys,
                                                                 case, check):
    root, handoff = _proof_case(tmp_path, monkeypatch, case)
    out = tmp_path / "run"
    assert _compose(root, handoff, out) == 6
    err = capsys.readouterr().err.strip()
    assert f"wrap proof check '{check}'" in err
    assert err.endswith("run /build-log wrap")
    assert not (out / "prompt.txt").exists()


def test_compose_honours_max_age_minutes(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    handoff = _write_handoff(root)
    now = time.time()
    _write_daily(root, mtime=now - 20 * 60 - 30)
    _publish(tmp_path, handoff, _block(started_at=rotation_state._iso_utc(now - 21 * 60),
                                       wrapped_at=rotation_state._iso_utc(now - 20 * 60)))
    assert _compose(root, handoff, tmp_path / "a") == 6
    assert _compose(root, handoff, tmp_path / "b", "--max-age-minutes", "30") == 0


def test_the_frontmatter_reader_tolerates_values_it_does_not_parse(tmp_path):
    path = tmp_path / "log.md"
    path.write_text('---\ntags: [rotatie, g5a]\ntitle: "open quote\nhandoff: true  # gezet door wrap\n'
                    "sessions:\n  - een\n  - twee\n---\nbody\n", encoding="utf-8")
    front = rotation_state.read_frontmatter(path)
    assert front["handoff"] is True
    assert front["tags"] == "[rotatie, g5a]" and front["title"] == '"open quote'


# ── emergency path: compose in a skip run ────────────────────────────────────


def _good_transcript(tmp_path: Path) -> Path:
    path = tmp_path / "transcripts" / "old session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "user", "message": {"content": "hoi"}}) + "\n", encoding="utf-8")
    return path


def test_a_skip_run_composes_without_a_wrap_block_and_puts_the_catch_up_first(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    handoff = _write_handoff(root)
    transcript = _good_transcript(tmp_path)
    out = tmp_path / "run"
    assert _compose(root, handoff, out, "--skip-wrap-reason", "context\nop 610K",
                    "--transcript", str(transcript)) == 0
    prompt = (out / "prompt.txt").read_text(encoding="utf-8")
    assert prompt.startswith("Eerst een inhaal-wrap: deze rotatie sloeg /build-log wrap over "
                             "(reden: context op 610K).")
    quoted = f"'{transcript}'"
    assert f"/build-log wrap --from-transcript {quoted}" in prompt
    assert f"transcript-digest --transcript {quoted}" in prompt
    assert "kill-window" not in prompt and "Voer daarna de kickoff-skill uit" in prompt
    assert "\n" not in prompt
    assert (out / "first_step.txt").read_text(encoding="utf-8") == "**#2080 mergen**: lees het rapport."


@pytest.mark.parametrize("kind", ["missing", "directory", "empty", "not_json", "array"])
def test_a_skip_run_refuses_a_transcript_that_is_not_jsonl(tmp_path, monkeypatch, capsys, kind):
    root = _project(tmp_path, monkeypatch)
    handoff = _write_handoff(root)
    target = tmp_path / "t.jsonl"
    if kind == "directory":
        target.mkdir()
    elif kind != "missing":
        target.write_text({"empty": "", "not_json": "hallo\n", "array": "[1]\n"}[kind], encoding="utf-8")
    out = tmp_path / "run"
    assert _compose(root, handoff, out, "--skip-wrap-reason", "noodrem", "--transcript", str(target)) == 6
    assert "readable JSONL" in capsys.readouterr().err
    assert not (out / "prompt.txt").exists()


def test_compose_usage_errors_of_the_emergency_path(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    handoff = _write_handoff(root)
    transcript = str(_good_transcript(tmp_path))
    assert _compose(root, handoff, tmp_path / "a", "--skip-wrap-reason", " \n ", "--transcript", transcript) == 2
    assert _compose(root, handoff, tmp_path / "b", "--transcript", transcript) == 2
    assert _compose(root, handoff, tmp_path / "c", "--skip-wrap-reason", "noodrem") == 6


@pytest.mark.parametrize("first, second, expected", [
    (GOAL, "", None),
    ("", GOAL, "/goal werk de track af, hervat na context-rotatie; resterend: G5b"),
    (GOAL, GOAL.replace("de track", "golf twee"),
     "/goal werk golf twee af, hervat na context-rotatie; resterend: G5b"),
])
def test_a_rerun_of_compose_follows_the_goal_of_the_handoff_it_reads(tmp_path, monkeypatch,
                                                                     first, second, expected):
    """compose's side of "continuation is frozen after recovery": a re-run into the same run dir
    rewrites first_step.txt and writes or removes goal.txt."""
    root = _project(tmp_path, monkeypatch)
    out = tmp_path / "run"
    transcript = str(_good_transcript(tmp_path))
    handoff = _write_handoff(root, HANDOFF_BODY + first)
    assert _compose(root, handoff, out, "--skip-wrap-reason", "noodrem", "--transcript", transcript) == 0
    handoff = _write_handoff(root, HANDOFF_BODY.replace("#2080 mergen", "#2081 mergen") + second)
    rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG])
    _write_daily(root)
    rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY])
    assert _compose(root, handoff, out) == 0
    goal = out / "goal.txt"
    assert (goal.read_text(encoding="utf-8") if goal.exists() else None) == expected
    assert (out / "first_step.txt").read_text(encoding="utf-8") == "**#2081 mergen**: lees het rapport."


# ── emergency path: record-skip, pending-transcript, skips ───────────────────


def _record_argv(**overrides) -> List[str]:
    values = {"--reason": "noodrem", "--transcript": "/t.jsonl", "--handoff": "/h.md",
              "--run-dir": "/run", "--pane": PANE, "--old-window": "@1"}
    values.update(overrides)
    argv = ["record-skip"]
    for key, value in values.items():
        argv += [key, value]
    return argv


def test_record_skip_books_the_receipt_with_reason_and_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv(rotation_state.STATE_DIR_ENV, str(tmp_path))
    _pending(tmp_path, transcript_path="/t.jsonl")
    events = []
    monkeypatch.setattr(rotation_state, "emit_event",
                        lambda trigger, *, file, **f: events.append((trigger, file, f)) or True)
    assert rotation_state.main(_record_argv()) == 0
    [(trigger, file, fields)] = events
    assert trigger == rotation_state.WRAP_SKIPPED_TRIGGER and file == "/run"
    assert fields["reason"] == "noodrem" and fields["transcript"] == "/t.jsonl"
    assert fields["session_id"] == SESSION and fields["old_window"] == "@1"


def test_record_skip_refuses_when_the_ledger_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(rotation_state.STATE_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(rotation_state, "emit_event", lambda trigger, *, file, **f: False)
    assert rotation_state.main(_record_argv()) == 6
    assert "unrecorded skip" in capsys.readouterr().err


def test_record_skip_refuses_an_empty_reason(tmp_path, monkeypatch):
    monkeypatch.setenv(rotation_state.STATE_DIR_ENV, str(tmp_path))
    events = []
    monkeypatch.setattr(rotation_state, "emit_event",
                        lambda trigger, *, file, **f: events.append(1) or True)
    assert rotation_state.main(_record_argv(**{"--reason": "  "})) == 2
    assert events == []


def test_pending_transcript_reads_the_marker_of_this_session_only(tmp_path):
    assert rotation_state.pending_transcript(tmp_path, PANE) is None
    _pending(tmp_path, transcript_path="/sessions/a.jsonl")
    assert rotation_state.pending_transcript(tmp_path, PANE) == "/sessions/a.jsonl"
    assert rotation_state.pending_transcript(tmp_path, PANE, SESSION) == "/sessions/a.jsonl"
    assert rotation_state.pending_transcript(tmp_path, PANE, "sess-other") is None
    assert rotation_state.pending_transcript(tmp_path, "%77") is None


def _ledger_line(trigger: str, days_ago: float, **fields) -> str:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime(time.time() - days_ago * 86400))
    return json.dumps({"timestamp": stamp, "event_type": "state_mutation", "trigger": trigger,
                       **fields}) + "\n"


def test_skips_lists_the_skip_receipts_of_the_window(tmp_path, monkeypatch, capsys):
    ledger = tmp_path / "t0_receipts.ndjson"
    ledger.write_text(
        _ledger_line(rotation_state.WRAP_SKIPPED_TRIGGER, 10, reason="oud", transcript="/a.jsonl")
        + _ledger_line("t0_context_rotation_started", 1)
        + "{niet: json, t0_context_rotation_wrap_skipped}\n"
        + _ledger_line(rotation_state.WRAP_SKIPPED_TRIGGER, 1, reason="vers", transcript="/b.jsonl",
                       run_dir="/run-b"),
        encoding="utf-8")
    monkeypatch.setenv(rotation_state.RECEIPTS_FILE_ENV, str(ledger))

    assert rotation_state.main(["skips"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert 'reason="vers"' in lines[0] and "transcript=/b.jsonl" in lines[0] and "run_dir=/run-b" in lines[0]
    assert lines[1].startswith("1 wrap skip(s) in the last 7 day(s)")

    assert rotation_state.main(["skips", "--since", "30"]) == 0
    out = capsys.readouterr().out
    assert 'reason="oud"' in out and out.strip().splitlines()[-1].startswith("2 wrap skip(s)")


def test_skips_on_a_ledger_that_does_not_exist_yet_lists_none(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(rotation_state.RECEIPTS_FILE_ENV, str(tmp_path / "nog-niet.ndjson"))
    assert rotation_state.main(["skips"]) == 0
    assert capsys.readouterr().out.startswith("0 wrap skip(s)")


# ── transcript digest (risk T15) ─────────────────────────────────────────────


def _user(text, **extra):
    return {"type": "user", "timestamp": extra.pop("ts", "t"), "message": {"role": "user", "content": text}, **extra}


def _assistant(blocks, **extra):
    return {"type": "assistant", "timestamp": extra.pop("ts", "t"),
            "message": {"model": extra.pop("model", "claude-opus-5-5"), "content": blocks}, **extra}


BOUNDARY = {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted",
            "compactMetadata": {"trigger": "auto", "preTokens": 900_000, "postTokens": 21_000}}


def _write_jsonl(path: Path, entries) -> Path:
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries) + "half-written",
                    encoding="utf-8")
    return path


def test_the_digest_keeps_only_post_boundary_user_and_assistant_text(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", [
        _user("VOOR-DE-GRENS-user"),
        _assistant([{"type": "text", "text": "VOOR-DE-GRENS-assistant"}]),
        BOUNDARY,
        _user("SAMENVATTING-NA-COMPACTIE", isCompactSummary=True, ts="t1"),
        {"type": "attachment", "attachment": {"type": "hook_output", "content": "BIJLAGE"}},
        _user("eerste vraag", ts="t2"),
        _assistant([{"type": "thinking", "thinking": "DENKWERK"},
                    {"type": "text", "text": "eerste antwoord"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "TOOLAANROEP"}}], ts="t3"),
        _user([{"type": "tool_result", "tool_use_id": "x", "content": "TOOLRESULTAAT"}]),
        _user([{"type": "text", "text": "SKILL-INHOUD"}], isMeta=True),
        _assistant([{"type": "text", "text": "ZIJSPOOR"}], isSidechain=True),
        _assistant([{"type": "text", "text": "SYNTHETISCH"}], model="<synthetic>"),
        _user([{"type": "text", "text": "tweede vraag"}, {"type": "image", "source": {"data": "BEELD"}}], ts="t4"),
        _assistant([{"type": "text", "text": "tweede antwoord"}], ts="t5"),
    ])
    text, stats = rotation_state.transcript_digest(path, max_bytes=200_000)
    for absent in ("VOOR-DE-GRENS", "BIJLAGE", "DENKWERK", "TOOLAANROEP", "TOOLRESULTAAT",
                   "SKILL-INHOUD", "ZIJSPOOR", "SYNTHETISCH", "BEELD"):
        assert absent not in text, absent
    order = ["SAMENVATTING-NA-COMPACTIE", "eerste vraag", "eerste antwoord", "tweede vraag", "tweede antwoord"]
    positions = [text.index(item) for item in order]
    assert positions == sorted(positions)
    assert "## user · t2\neerste vraag\n" in text and "## assistant · t5\ntweede antwoord\n" in text
    assert stats == {"entries": 5, "kept": 5, "boundary": True, "bytes": len(text.encode("utf-8"))}


def test_the_digest_reads_after_the_last_of_several_boundaries(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", [_user("een"), BOUNDARY, _user("twee"), BOUNDARY,
                                               _user("drie")])
    text, _ = rotation_state.transcript_digest(path, max_bytes=1000)
    assert "drie" in text and "twee" not in text and "een" not in text


def test_the_digest_without_a_boundary_reads_from_the_start(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", [_user("een"), _assistant([{"type": "text", "text": "twee"}])])
    text, stats = rotation_state.transcript_digest(path, max_bytes=1000)
    assert text.index("een") < text.index("twee") and stats["boundary"] is False


def test_the_digest_of_a_megabyte_keeps_the_newest_entries_within_the_budget(tmp_path):
    """1 MB of text after the boundary plus tool results of the same size: the CLI prints at
    most 200,000 bytes, only text, the newest entries, oldest first."""
    entries = [_user("OUDE-SESSIE " * 5000), BOUNDARY]
    for idx in range(100):
        filler = f"<{idx:03d}>" + ("ä" * 5000)
        if idx % 2:
            entries.append(_assistant([{"type": "text", "text": filler},
                                       {"type": "tool_use", "name": "Read", "input": {"p": "x" * 9000}}], ts=f"e{idx:03d}"))
        else:
            entries.append(_user(filler, ts=f"e{idx:03d}"))
            entries.append(_user([{"type": "tool_result", "tool_use_id": str(idx), "content": "R" * 10_000}]))
    path = _write_jsonl(tmp_path / "big.jsonl", entries)
    assert path.stat().st_size > 2_000_000

    proc = subprocess.run([sys.executable, str(STATE_PY), "transcript-digest", "--transcript", str(path)],
                          capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert 190_000 < len(out) <= 200_000
    text = out.decode("utf-8")
    seen = [int(text[i + 1:i + 4]) for i in range(len(text)) if text.startswith("<", i) and text[i + 4:i + 5] == ">"]
    assert seen == sorted(seen) and seen[-1] == 99 and 0 not in seen
    assert "RRRR" not in text and "xxxx" not in text and "OUDE-SESSIE" not in text
    assert b"after the last compact_boundary" in proc.stderr


def test_the_digest_cuts_the_oldest_kept_entry_and_keeps_its_end(tmp_path):
    path = _write_jsonl(tmp_path / "t.jsonl", [BOUNDARY, _user("BEGIN" + "x" * 3000 + "EINDE", ts="t1")])
    text, stats = rotation_state.transcript_digest(path, max_bytes=500)
    assert len(text.encode("utf-8")) <= 500
    assert text.startswith("## user · t1\n[... the start of this entry was cut")
    assert text.rstrip().endswith("EINDE") and "BEGIN" not in text
    assert stats["kept"] == 1


def test_the_digest_of_an_unreadable_transcript_is_exit_3(tmp_path, capsys):
    assert rotation_state.main(["transcript-digest", "--transcript", str(tmp_path / "nope.jsonl")]) == 3
    assert "unreadable" in capsys.readouterr().err


# ── the interface the G7a skills call (build-log, kickoff; patched 2026-10-16) ──
# Each skill block first runs `<cmd> --help` and skips the step when that exits non-zero, so a
# renamed command or flag would silently skip the proof. These run the CLI the way the skills do.

SKILL_FLAGS = {
    "wrap-start": ["--project-root", "--project-slug", "--bucket"],
    "wrap-finish": ["--project-root", "--daily-log"],
    "transcript-digest": ["--transcript", "--max-bytes"],
    "skips": ["--since"],
    "state-dir": ["--project-root"],
}


def _cli(*argv: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(STATE_PY), *argv], capture_output=True, text=True,
                          env=env, timeout=60)


@pytest.mark.parametrize("cmd", sorted(SKILL_FLAGS))
def test_every_command_a_skill_calls_answers_help_with_the_flags_it_passes(cmd):
    proc = _cli(cmd, "--help", env=dict(os.environ))
    assert proc.returncode == 0, proc.stderr
    for flag in SKILL_FLAGS[cmd]:
        assert flag in proc.stdout, f"{cmd} --help does not list {flag}"


def test_the_skills_argv_runs_a_wrap_a_digest_and_the_skip_list(tmp_path):
    """Exactly the argv of the patched skills, on fixtures: build-log's wrap-start, wrap-finish
    (handoff defaulted to <root>/daily-log/handoff.md) and transcript-digest, and kickoff's
    state-dir and `skips --since 7`, whose lines must carry transcript= and run_dir=<path>."""
    root = tmp_path / "project"
    (root / "daily-log").mkdir(parents=True)
    ledger = tmp_path / "t0_receipts.ndjson"
    env = {**os.environ, rotation_state.STATE_DIR_ENV: str(tmp_path / "state"),
           rotation_state.RECEIPTS_FILE_ENV: str(ledger)}
    bucket = _write_bucket(tmp_path / "bucket" / "2026-10.md", [_h("seocrawler-v2", "ander-project")])

    start = _cli("wrap-start", "--project-root", str(root), "--project-slug", SLUG, "--bucket", str(bucket), env=env)
    assert start.returncode == 0, start.stderr
    _append_bucket(bucket, [_h(SLUG, "nieuw-moment")])
    handoff = _write_handoff(root)
    _write_daily(root)
    finish = _cli("wrap-finish", "--project-root", str(root), "--daily-log", DAILY, env=env)
    assert finish.returncode == 0, finish.stderr
    assert rotation_state.read_frontmatter(handoff)["wrap"]["nugget_headings"] == [_h(SLUG, "nieuw-moment")]

    state = _cli("state-dir", "--project-root", str(root), env=env)
    assert state.returncode == 0 and state.stdout.strip() == str(tmp_path / "state")

    transcript = _write_jsonl(tmp_path / "voorganger.jsonl", [_user("wat deed ik net")])
    digest = _cli("transcript-digest", "--transcript", str(transcript), "--max-bytes", "200000", env=env)
    assert digest.returncode == 0 and "wat deed ik net" in digest.stdout, digest.stderr

    run_dir = tmp_path / "run-1"
    ledger.write_text(_ledger_line(rotation_state.WRAP_SKIPPED_TRIGGER, 1, reason="noodrem",
                                   transcript=str(transcript), run_dir=str(run_dir)), encoding="utf-8")
    skips = _cli("skips", "--since", "7", env=env)
    assert skips.returncode == 0, skips.stderr
    line = skips.stdout.splitlines()[0]
    assert f"transcript={transcript}" in line and f"run_dir={run_dir}" in line


# ── run-dir, kickoff-done, continuation.json (dispatch 20261021-rot-prompt-als-argument) ──


def _handoff_with_wrap(root: Path, tmp_path: Path, extra: str = "") -> Path:
    """A handoff with a real wrap: block, made the way /build-log wrap makes it."""
    assert rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG]) == 0
    handoff = _write_handoff(root, HANDOFF_BODY + extra)
    _write_daily(root)
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 0
    return handoff


def _run(tmp_path: Path, root: Path, handoff: Path, name: str = "rotation-20261021-070000-1") -> Path:
    out = tmp_path / "state" / name
    assert _compose(root, handoff, out) == 0
    return out


def test_compose_writes_continuation_json_and_a_prompt_without_the_kill_window_sentence(
        tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    handoff = _handoff_with_wrap(root, tmp_path)
    out = _run(tmp_path, root, handoff)
    continuation = json.loads((out / "continuation.json").read_text(encoding="utf-8"))
    assert continuation == {"run_id": out.name, "first_step": (out / "first_step.txt").read_text(),
                            "goal": None, "handoff": str(handoff)}
    prompt = (out / "prompt.txt").read_text(encoding="utf-8")
    assert "kill-window" not in prompt and "Sluit eerst" not in prompt
    assert prompt.startswith("Je bent de verse T0 na een context-rotatie. Voer eerst de kickoff-skill uit")


def test_continuation_json_carries_the_goal_line_when_the_handoff_has_one(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    handoff = _handoff_with_wrap(root, tmp_path, extra="\n## Actief /goal\nDirective: `/goal werk de track af`\n")
    out = _run(tmp_path, root, handoff)
    continuation = json.loads((out / "continuation.json").read_text(encoding="utf-8"))
    assert continuation["goal"] == (out / "goal.txt").read_text(encoding="utf-8")
    assert continuation["goal"].startswith("/goal werk de track af")


def test_run_dir_prints_the_absolute_run_dir_and_refuses_what_is_not_a_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(rotation_state.STATE_DIR_ENV, str(tmp_path / "state"))
    (tmp_path / "state" / "rotation-1").mkdir(parents=True)
    assert rotation_state.main(["run-dir", "rotation-1"]) == 0
    assert capsys.readouterr().out.strip() == str((tmp_path / "state" / "rotation-1").resolve())
    assert rotation_state.main(["run-dir", "rotation-2"]) == 1
    assert capsys.readouterr().out == ""
    for bad in ("..", "../state", "a/b"):
        assert rotation_state.main(["run-dir", bad]) == 1


def test_kickoff_done_writes_the_marker_and_books_the_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv(rotation_state.RECEIPTS_FILE_ENV, str(tmp_path / "r.ndjson"))
    run = tmp_path / "rotation-9"
    run.mkdir()
    assert rotation_state.main(["kickoff-done", "--run-dir", str(run)]) == 0
    marker = json.loads((run / "kickoff_done.json").read_text(encoding="utf-8"))
    assert marker["run_id"] == "rotation-9" and marker["catch_up_done"] is False
    receipts = [json.loads(ln) for ln in (tmp_path / "r.ndjson").read_text().splitlines()]
    assert [r["trigger"] for r in receipts] == ["t0_context_rotation_kickoff_done"]
    assert not list(run.glob("*.tmp"))


def test_kickoff_done_on_a_missing_run_dir_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv(rotation_state.RECEIPTS_FILE_ENV, str(tmp_path / "r.ndjson"))
    assert rotation_state.main(["kickoff-done", "--run-dir", str(tmp_path / "nope")]) == 1
    assert not (tmp_path / "r.ndjson").exists()


def _skip_receipt(tmp_path: Path, monkeypatch, run: Path, at: float) -> None:
    ledger = tmp_path / "r.ndjson"
    monkeypatch.setenv(rotation_state.RECEIPTS_FILE_ENV, str(ledger))
    ledger.write_text(json.dumps({
        "trigger": rotation_state.WRAP_SKIPPED_TRIGGER, "run_dir": str(run),
        "timestamp": rotation_state._iso_utc(at)}) + "\n", encoding="utf-8")


def test_kickoff_done_catch_up_needs_a_wrap_newer_than_the_skip(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, monkeypatch)
    handoff = _handoff_with_wrap(root, tmp_path)
    run = _run(tmp_path, root, handoff)
    wrapped = rotation_state._parse_utc(rotation_state.read_frontmatter(handoff)["wrap"]["wrapped_at"])

    # skip receipt newer than the wrap: the catch-up wrap has not happened, exit 3, nothing written
    _skip_receipt(tmp_path, monkeypatch, run, wrapped + 600)
    assert rotation_state.main(["kickoff-done", "--run-dir", str(run), "--catch-up-done"]) == 3
    assert not (run / "kickoff_done.json").exists()
    assert "not newer than the skip receipt" in capsys.readouterr().err

    # the wrap is newer than the skip receipt: accepted
    _skip_receipt(tmp_path, monkeypatch, run, wrapped - 600)
    assert rotation_state.main(["kickoff-done", "--run-dir", str(run), "--catch-up-done"]) == 0
    assert json.loads((run / "kickoff_done.json").read_text())["catch_up_done"] is True


def test_kickoff_done_catch_up_without_a_skip_receipt_for_this_run_exits_3(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    run = _run(tmp_path, root, _handoff_with_wrap(root, tmp_path))
    _skip_receipt(tmp_path, monkeypatch, tmp_path / "another-run", time.time() - 3600)
    assert rotation_state.main(["kickoff-done", "--run-dir", str(run), "--catch-up-done"]) == 3
    assert not (run / "kickoff_done.json").exists()


# ── round 2 on #2144 (dispatch 20261021-rot-prompt-r2, OI-2218) ───────────────


def _blocked_ledger(tmp_path: Path, monkeypatch) -> None:
    """A receipts path whose directory is a file: every append to it fails."""
    blocker = tmp_path / "ledger-blocker"
    blocker.write_text("a file where the ledger's directory should be", encoding="utf-8")
    monkeypatch.setenv(rotation_state.RECEIPTS_FILE_ENV, str(blocker / "t0_receipts.ndjson"))


def test_kickoff_done_with_a_refusing_ledger_exits_1_and_writes_no_marker(tmp_path, monkeypatch, capsys):
    """Red run on e7ccb56c: the marker was written first and the command returned 0."""
    _blocked_ledger(tmp_path, monkeypatch)
    run = tmp_path / "rotation-9"
    run.mkdir()
    assert rotation_state.main(["kickoff-done", "--run-dir", str(run)]) == 1
    assert not (run / "kickoff_done.json").exists()
    assert "ledger did not take t0_context_rotation_kickoff_done" in capsys.readouterr().err


def test_kickoff_done_books_the_receipt_before_the_marker_exists(tmp_path, monkeypatch):
    ledger = tmp_path / "r.ndjson"
    monkeypatch.setenv(rotation_state.RECEIPTS_FILE_ENV, str(ledger))
    run = tmp_path / "rotation-9"
    run.mkdir()
    seen = {}
    real = rotation_state.emit_event

    def spy(trigger, **kwargs):
        seen["marker_existed"] = (run / "kickoff_done.json").exists()
        return real(trigger, **kwargs)

    monkeypatch.setattr(rotation_state, "emit_event", spy)
    assert rotation_state.main(["kickoff-done", "--run-dir", str(run)]) == 0
    assert seen == {"marker_existed": False}
    assert (run / "kickoff_done.json").exists()


def test_the_event_command_exits_non_zero_when_the_append_failed(tmp_path, monkeypatch, capsys):
    """Red run on e7ccb56c: `event` returned 0 after a failed append."""
    _blocked_ledger(tmp_path, monkeypatch)
    assert rotation_state.main(["event", "--trigger", "t0_context_rotation_delivery",
                                "--file", str(tmp_path / "delivery.json"),
                                "--field", "outcome=confirmed"]) == 1
    assert "ledger did not take t0_context_rotation_delivery" in capsys.readouterr().err


def test_the_event_command_exits_0_when_the_ledger_took_it(tmp_path, monkeypatch):
    monkeypatch.setenv(rotation_state.RECEIPTS_FILE_ENV, str(tmp_path / "r.ndjson"))
    assert rotation_state.main(["event", "--trigger", "t0_context_rotation_delivery",
                                "--file", str(tmp_path / "delivery.json"),
                                "--field", "outcome=confirmed"]) == 0
    assert (tmp_path / "r.ndjson").read_text().count("t0_context_rotation_delivery") == 1


def test_compose_stores_a_relative_handoff_as_an_absolute_path(tmp_path, monkeypatch):
    """Red run on e7ccb56c: continuation.json held the path as given, so a catch-up check run
    from another folder could not find the handoff."""
    root = _project(tmp_path, monkeypatch)
    handoff = _handoff_with_wrap(root, tmp_path)
    out = tmp_path / "state" / "rotation-rel"
    monkeypatch.chdir(root)
    assert rotation_state.main(["compose", "--handoff", str(handoff.relative_to(root)),
                                "--out-dir", str(out), "--project-root", str(root)]) == 0
    stored = json.loads((out / "continuation.json").read_text(encoding="utf-8"))["handoff"]
    assert Path(stored).is_absolute() and Path(stored).samefile(handoff)
    assert stored in (out / "prompt.txt").read_text(encoding="utf-8")


def test_kickoff_done_catch_up_resolves_the_handoff_from_any_folder(tmp_path, monkeypatch):
    root = _project(tmp_path, monkeypatch)
    handoff = _handoff_with_wrap(root, tmp_path)
    out = tmp_path / "state" / "rotation-rel2"
    monkeypatch.chdir(root)
    assert rotation_state.main(["compose", "--handoff", str(handoff.relative_to(root)),
                                "--out-dir", str(out), "--project-root", str(root)]) == 0
    wrapped = rotation_state._parse_utc(rotation_state.read_frontmatter(handoff)["wrap"]["wrapped_at"])
    _skip_receipt(tmp_path, monkeypatch, out, wrapped - 600)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert rotation_state.main(["kickoff-done", "--run-dir", str(out), "--catch-up-done"]) == 0
