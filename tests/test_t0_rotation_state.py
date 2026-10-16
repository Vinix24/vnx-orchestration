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
    return rotation_state.main(["compose", "--handoff", str(handoff), "--old-window", "@1",
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
    root = _project(tmp_path, monkeypatch)
    handoff = _write_handoff(root)
    rotation_state.main(["wrap-start", "--project-root", str(root), "--project-slug", SLUG])
    _write_daily(root, flag="false")
    assert rotation_state.main(["wrap-finish", "--project-root", str(root), "--daily-log", DAILY]) == 6
    assert "'daily_log_handoff'" in capsys.readouterr().err
    assert "wrap" in rotation_state.read_frontmatter(handoff)


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
    block = rotation_state.verify_wrap_proof(handoff, project_root=root, max_age_minutes=15)
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


# ── wrap proof: compose refuses with 6 per named check ───────────────────────


def _block(**overrides) -> dict:
    now = time.time()
    block = {"started_at": rotation_state._iso_utc(now - 300),
             "wrapped_at": rotation_state._iso_utc(now - 10), "nuggets": 0,
             "nugget_headings": [], "daily_log": DAILY, "bucket": None}
    block.update(overrides)
    return block


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
        rotation_state.write_wrap_block(handoff, blocks[case])
    elif case == "unclosed":
        handoff.write_text("---\nwrap:\n  nuggets: 0\n" + HANDOFF_BODY, encoding="utf-8")
    elif case == "daily_log_handoff":
        rotation_state.write_wrap_block(handoff, _block())
        _write_daily(root, mtime=now - 60, flag="false")
    elif case in ("before_start", "after_wrap"):
        rotation_state.write_wrap_block(handoff, _block())
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
    rotation_state.write_wrap_block(handoff, _block(started_at=rotation_state._iso_utc(now - 21 * 60),
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
    assert "Sluit daarna het vorige venster" in prompt and "Sluit eerst" not in prompt
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
