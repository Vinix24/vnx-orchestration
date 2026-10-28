"""ADR-045 K-2b: the shell readers of an end line read `dispatch_finished` too.

ADR-045 §2 replaces the ledger's single end line type with a pair: the legacy
``task_complete``/``task_failed``/``task_timeout`` (still written for legacy
dispatches) and ``dispatch_finished``, which carries ``end_state`` out of a
closed four-value set. §10 makes readers fix-forward: every reader keeps the
legacy branch and accepts the new line next to it.

Every test here drives the REAL shell function through a subprocess against a
sandboxed state dir (the same ``_RP_LIB_MODE=1`` sourcing harness that
tests/test_receipt_processor_deadletter.py uses) — no reimplemented logic, and
only paths/env are patched, never a production script.

The two ledgers carry the same dispatches: one as ``dispatch_finished`` lines,
one as the legacy types. Per record the decisions must match, and the
``dispatch_finished`` line whose ``end_state`` is missing (while its raw
``status`` says ``success``) must never be read as a success.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
RP_SCRIPT = VNX_ROOT / "scripts" / "receipt_processor.sh"
BRIEF_SCRIPT = VNX_ROOT / "scripts" / "generate_t0_brief.sh"

# Fixed activity timestamp: the readers under test never compare it to now.
STAMP = "2026-10-28T00:00:00Z"

# Records with a meaningful legacy counterpart — a decision taken for one must
# be taken for the other.
PAIRED = ("completed", "failed", "timed_out", "started", "ingested")

# The new ledger. `malformed` has no legacy counterpart on purpose: it is the
# line the write side refuses (missing end_state) and it carries a raw
# `status: success` that no reader may believe.
NEW_RECORDS = {
    "completed": {"event_type": "dispatch_finished", "end_state": "completed"},
    "failed": {"event_type": "dispatch_finished", "end_state": "failed"},
    "timed_out": {"event_type": "dispatch_finished", "end_state": "timed_out"},
    "malformed": {"event_type": "dispatch_finished", "status": "success"},
    "started": {"event_type": "dispatch_started"},
    "ingested": {"event_type": "report_ingested"},
}

# The same dispatches written the legacy way.
LEGACY_RECORDS = {
    "completed": {"event_type": "task_complete", "status": "success"},
    "failed": {"event_type": "task_failed"},
    "timed_out": {"event_type": "task_timeout", "status": "timeout"},
    "started": {"event_type": "task_started"},
    "ingested": {"event_type": "task_started"},
}

# Does the record end the dispatch? (move-to-completed, shadow state, lease)
EXPECTED_END_LINE = {
    "completed": True,
    "failed": True,
    "timed_out": True,
    "malformed": True,
    "started": False,
    "ingested": False,
}

# Is the record a governed success? (pattern credit, track close, hooks, brief)
EXPECTED_SUCCESS = {
    "completed": True,
    "failed": False,
    "timed_out": False,
    "malformed": False,
    "started": False,
    "ingested": False,
}

ENV_PREAMBLE = """
set -u
export _RP_LIB_MODE=1
export VNX_DATA_DIR="{data_dir}"
export VNX_STATE_DIR="{state}"
export VNX_DISPATCH_DIR="{dispatches}"
export VNX_PIDS_DIR="{pids}"
export VNX_LOCKS_DIR="{locks}"
export VNX_REPORTS_DIR="{unified}"
export VNX_HEADLESS_REPORTS_DIR="{headless}"
export VNX_DATA_DIR_GUARD=off
source "{rp_script}" || {{ echo "FATAL: source failed" >&2; exit 1; }}
"""


def _sandbox(root: Path) -> dict[str, Path]:
    data_dir = root / "data"
    dirs = {
        "data_dir": data_dir,
        "state": data_dir / "state",
        "dispatches": data_dir / "dispatches",
        "unified": root / "unified",
        "headless": root / "headless",
        "pids": root / "pids",
        "locks": root / "locks",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    for sub in ("active", "completed", "queue", "pending", "conflicts"):
        (dirs["dispatches"] / sub).mkdir(parents=True, exist_ok=True)
    return dirs


def _run(dirs: dict[str, Path], body: str) -> subprocess.CompletedProcess:
    cmd = ENV_PREAMBLE.format(rp_script=RP_SCRIPT, **{k: str(v) for k, v in dirs.items()}) + body
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=180)


def _write_record(dirs: dict[str, Path], record: dict) -> Path:
    path = dirs["state"] / "record.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _run_record(tmp_path: Path, tag: str, record: dict, body: str, prepare=None):
    dirs = _sandbox(tmp_path / tag)
    full = {"dispatch_id": f"dlv-{tag}", "timestamp": STAMP}
    full.update(record)
    record_path = _write_record(dirs, full)
    if prepare is not None:
        prepare(dirs, full)
    result = _run(dirs, body.format(record=record_path))
    assert result.returncode == 0, f"driver failed for {tag}: {result.stderr}"
    return dirs, full


def _decide_all(tmp_path: Path, body: str, observe, prepare=None) -> tuple[dict, dict]:
    """Run one reader over both ledgers, one sandboxed process per record."""
    new = {
        key: observe(*_run_record(tmp_path, f"new-{key}", record, body, prepare))
        for key, record in NEW_RECORDS.items()
    }
    legacy = {
        key: observe(*_run_record(tmp_path, f"legacy-{key}", record, body, prepare))
        for key, record in LEGACY_RECORDS.items()
    }
    return new, legacy


def _assert_paired(new: dict, legacy: dict) -> None:
    for key in PAIRED:
        assert new[key] == legacy[key], (
            f"{key}: a dispatch_finished decided {new[key]!r} where the same "
            f"dispatch written the legacy way decided {legacy[key]!r}")


def _log(dirs: dict[str, Path]) -> str:
    path = dirs["state"] / "receipt_processing.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ---------------------------------------------------------------------------
# rp_extract.sh — the status projection and the completion-hook predicate
# ---------------------------------------------------------------------------

_EXTRACT_BODY = """
records="{records}"
for f in "$records"/*.json; do
    extract_receipt_fields "$(cat "$f")"
    if rp_is_completion_hook "$_rf_event_type" "$_rf_status"; then hook=yes; else hook=no; fi
    printf '%s\\t%s\\t%s\\t%s\\n' "$(basename "$f" .json)" "$_rf_status" "$(rp_end_outcome "$(cat "$f")")" "$hook"
done
"""


def _extract_all(tmp_path: Path) -> dict[str, tuple[str, str, str]]:
    dirs = _sandbox(tmp_path / "extract")
    records_dir = dirs["state"] / "records"
    records_dir.mkdir()
    for key, record in list(NEW_RECORDS.items()) + [("legacy-" + k, v) for k, v in LEGACY_RECORDS.items()]:
        full = {"dispatch_id": f"dlv-{key}", "timestamp": STAMP}
        full.update(record)
        (records_dir / f"{key}.json").write_text(json.dumps(full), encoding="utf-8")
    result = _run(dirs, _EXTRACT_BODY.format(records=records_dir))
    assert result.returncode == 0, result.stderr
    decisions = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        key, status, outcome, hook = line.split("\t")
        decisions[key] = (status, outcome, hook)
    assert set(decisions) == set(NEW_RECORDS) | {"legacy-" + k for k in LEGACY_RECORDS}
    return decisions


def test_extract_receipt_fields_projects_end_state(tmp_path: Path):
    """A dispatch_finished's status comes from end_state, never from the line.

    The malformed record says `"status": "success"` and has no end_state: the
    reader must still report no status and no outcome for it.
    """
    decisions = _extract_all(tmp_path)

    assert decisions["completed"] == ("success", "success", "yes")
    assert decisions["failed"] == ("failure", "failure", "no")
    assert decisions["timed_out"] == ("timeout", "none", "no")
    assert decisions["malformed"] == ("", "none", "no")
    # Non-end records keep jq's literal "unknown" default — unchanged.
    assert decisions["started"] == ("unknown", "none", "no")
    assert decisions["ingested"] == ("unknown", "none", "no")

    # The legacy branch reads `.status` literally, exactly as before. (A
    # legacy task_timeout is a governed failure whatever its status carries;
    # a dispatch_finished end_state=timed_out projects to the ignorable
    # "timeout" literal. That difference is event_outcome_semantics' call, not
    # a reader's — no shell decision below turns on "failure".)
    assert decisions["legacy-completed"] == ("success", "success", "yes")
    assert decisions["legacy-failed"] == ("unknown", "failure", "no")
    assert decisions["legacy-timed_out"] == ("timeout", "failure", "no")

    for key in PAIRED:
        if key == "started":
            continue  # task_started has no end-line meaning in either ledger
        new_status, new_outcome, new_hook = decisions[key]
        legacy_status, legacy_outcome, legacy_hook = decisions["legacy-" + key]
        assert new_hook == legacy_hook, f"{key}: hook {new_hook} vs {legacy_hook}"
        assert (new_outcome == "success") == (legacy_outcome == "success"), (
            f"{key}: success reading {new_outcome!r} vs {legacy_outcome!r}")


# ---------------------------------------------------------------------------
# rp_end_line.sh — a failing helper is a failure, never an empty status
# ---------------------------------------------------------------------------

# The subject line the stub interpreter writes: it must survive as far as the
# processor log or stderr. Folding it into 2>/dev/null is exactly the defect.
_STUB_STDERR_LINE = "stub python3: forced failure"

# Reads both accessors, then the status extract_receipt_fields projects and the
# completion-hook verdict that follows from it. `status_rc`/`outcome_rc` keep
# the accessors' own exit status; the record comes from a file so JSON braces
# never reach str.format.
_END_LINE_BODY = """
JSON="$(cat "{record}")"
status_rc=0
outcome_rc=0
status_out=$(rp_end_status "$JSON") || status_rc=$?
outcome_out=$(rp_end_outcome "$JSON") || outcome_rc=$?
extract_receipt_fields "$JSON"
if rp_is_completion_hook "$_rf_event_type" "$_rf_status"; then hook=yes; else hook=no; fi
printf 'status_rc=%s\\nstatus_out=%s\\noutcome_rc=%s\\noutcome_out=%s\\nrf_status=%s\\nhook=%s\\n' \\
    "$status_rc" "$status_out" "$outcome_rc" "$outcome_out" "$_rf_status" "$hook"
"""


def _install_failing_python3(root: Path) -> Path:
    """A `python3` earlier on PATH that exits 7 and says why on stderr."""
    stub_dir = root / "stub-bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "python3"
    stub.write_text(
        "#!/bin/sh\n"
        f"echo '{_STUB_STDERR_LINE}' >&2\n"
        "exit 7\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub_dir


def _parse_end_line(stdout: str) -> dict[str, str]:
    got: dict[str, str] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition("=")
        got[key] = value
    return got


def test_end_line_accessors_fail_closed_on_helper_failure(tmp_path: Path):
    """The codex reproduction: python3 exits 7.

    Both accessors must return non-zero with nothing on stdout — today they
    return 0 with an empty status, which is indistinguishable from a valid line
    without an end_state. The helper's stderr line must reach the processor log
    (or stderr when not defined), and the completion hook must not fire.
    """
    dirs = _sandbox(tmp_path / "failing-helper")
    stub_dir = _install_failing_python3(tmp_path / "failing-helper")
    record = _write_record(
        dirs, {"event_type": "dispatch_finished", "end_state": "completed", "dispatch_id": "dlv-stub"})

    result = _run(dirs, f'export PATH="{stub_dir}:$PATH"\n' + _END_LINE_BODY.format(record=record))
    assert result.returncode == 0, result.stderr

    got = _parse_end_line(result.stdout)
    assert got["status_rc"] == "2", f"rp_end_status exited {got['status_rc']}, expected 2"
    assert got["status_out"] == "", f"rp_end_status printed {got['status_out']!r} on a failed helper"
    assert got["outcome_rc"] == "2", f"rp_end_outcome exited {got['outcome_rc']}, expected 2"
    assert got["outcome_out"] == "", f"rp_end_outcome printed {got['outcome_out']!r} on a failed helper"

    # No status projected into the caller's field, and no completion hook.
    assert got["rf_status"] == "", f"_rf_status is {got['rf_status']!r} after a failed helper"
    assert got["hook"] == "no", "the completion hook fired for a dispatch_finished with a failed helper"

    # The diagnostic survived: in the structured-failure log or on stderr.
    combined = result.stdout + result.stderr + _log(dirs)
    assert "end_line_helper_failed" in combined, f"no structured failure recorded: {combined!r}"
    assert _STUB_STDERR_LINE in combined, f"helper stderr was thrown away: {combined!r}"


def test_end_line_accessors_normal_path_unchanged(tmp_path: Path):
    """With the real python3 the accessors behave exactly as on 4ea92b5b."""
    dirs = _sandbox(tmp_path / "normal-helper")
    completed = _write_record(dirs, {"event_type": "dispatch_finished", "end_state": "completed"})

    result = _run(dirs, _END_LINE_BODY.format(record=completed))
    assert result.returncode == 0, result.stderr
    got = _parse_end_line(result.stdout)
    assert got["status_rc"] == "0"
    assert got["status_out"] == "success"
    assert got["outcome_rc"] == "0"
    assert got["outcome_out"] == "success"
    assert got["rf_status"] == "success"
    assert got["hook"] == "yes"

    missing = _write_record(dirs, {"event_type": "dispatch_finished"})
    result = _run(dirs, _END_LINE_BODY.format(record=missing))
    assert result.returncode == 0, result.stderr
    got = _parse_end_line(result.stdout)
    assert got["status_rc"] == "0", "a missing end_state is not a helper failure"
    assert got["status_out"] == ""
    assert got["outcome_rc"] == "0"
    assert got["outcome_out"] == "none"
    assert got["rf_status"] == ""
    assert got["hook"] == "no"


# ---------------------------------------------------------------------------
# rp_state.sh — the terminal_state shadow write
# ---------------------------------------------------------------------------

_SHADOW_BODY = """
JSON="$(cat "{record}")"
extract_receipt_fields "$JSON"
update_receipt_shadow_state T1
"""


def _shadow_status(dirs: dict[str, Path], record: dict):
    path = dirs["state"] / "terminal_state.json"
    if not path.exists():
        return None
    terminals = json.loads(path.read_text(encoding="utf-8")).get("terminals", {})
    entry = terminals.get("T1")
    return None if entry is None else entry.get("status")


def test_rp_state_shadow_writes_dispatch_finished(tmp_path: Path):
    new, legacy = _decide_all(tmp_path, _SHADOW_BODY, _shadow_status)

    _assert_paired(new, legacy)
    for key, expected in EXPECTED_END_LINE.items():
        want = "idle" if expected else None
        assert new[key] == want, f"{key}: shadow status {new[key]!r}, expected {want!r}"


# ---------------------------------------------------------------------------
# rp_dispatch.sh — move the dispatch out of active/
# ---------------------------------------------------------------------------

_MOVE_BODY = """
JSON="$(cat "{record}")"
extract_receipt_fields "$JSON"
_move_dispatch_to_completed
"""


def _prepare_active(dirs: dict[str, Path], record: dict) -> None:
    (dirs["dispatches"] / "active" / f"{record['dispatch_id']}.md").write_text("# dispatch\n")


def _move_decision(dirs: dict[str, Path], record: dict) -> bool:
    dispatch_id = record["dispatch_id"]
    moved = (dirs["dispatches"] / "completed" / f"{dispatch_id}.md").exists()
    still_active = (dirs["dispatches"] / "active" / f"{dispatch_id}.md").exists()
    assert moved != still_active, f"{dispatch_id} is in both places or in neither"
    return moved


def test_rp_dispatch_moves_dispatch_finished_to_completed(tmp_path: Path):
    new, legacy = _decide_all(tmp_path, _MOVE_BODY, _move_decision, _prepare_active)

    _assert_paired(new, legacy)
    for key, expected in EXPECTED_END_LINE.items():
        assert new[key] is expected, f"{key}: moved={new[key]}, expected {expected}"


# ---------------------------------------------------------------------------
# receipt_processor.sh — the lease release and the completion hooks
# ---------------------------------------------------------------------------

_RELEASE_BODY = """
JSON="$(cat "{record}")"
extract_receipt_fields "$JSON"
_psr_release_lease_on_event "$_rf_event_type" "$_rf_status" T1 "$_rf_dispatch_id"
"""


def _lease_released(dirs: dict[str, Path], record: dict) -> bool:
    return f"AUTO_LEASE_RELEASE: terminal=T1 dispatch={record['dispatch_id']}" in _log(dirs)


def test_receipt_processor_releases_lease_for_dispatch_finished(tmp_path: Path):
    new, legacy = _decide_all(tmp_path, _RELEASE_BODY, _lease_released)

    _assert_paired(new, legacy)
    for key, expected in EXPECTED_END_LINE.items():
        assert new[key] is expected, f"{key}: lease released={new[key]}, expected {expected}"


# ---------------------------------------------------------------------------
# rp_pattern.sh — pattern credit and the track close
# ---------------------------------------------------------------------------

_PATTERN_BODY = """
JSON="$(cat "{record}")"
_track_pattern_success_fallback "$JSON"
"""


def _naive_utc_now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _prepare_pattern_db(dirs: dict[str, Path], record: dict) -> None:
    conn = sqlite3.connect(dirs["state"] / "quality_intelligence.db")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pattern_usage ("
            "pattern_id TEXT PRIMARY KEY, success_count INTEGER NOT NULL DEFAULT 0, "
            "last_offered TEXT, updated_at TEXT)")
        conn.execute(
            "INSERT OR REPLACE INTO pattern_usage (pattern_id, success_count, last_offered) "
            "VALUES ('p-offered', 0, ?)", (_naive_utc_now(),))
        conn.commit()
    finally:
        conn.close()


def _pattern_credited(dirs: dict[str, Path], record: dict) -> bool:
    conn = sqlite3.connect(dirs["state"] / "quality_intelligence.db")
    try:
        row = conn.execute(
            "SELECT success_count FROM pattern_usage WHERE pattern_id = 'p-offered'").fetchone()
    finally:
        conn.close()
    assert row is not None
    return bool(row[0])


def test_rp_pattern_never_credits_a_missing_end_state(tmp_path: Path):
    """Pattern learning fires on task_complete+success and on a completed
    dispatch_finished, and on nothing else — the malformed line gives no credit."""
    new, legacy = _decide_all(tmp_path, _PATTERN_BODY, _pattern_credited, _prepare_pattern_db)

    _assert_paired(new, legacy)
    for key, expected in EXPECTED_SUCCESS.items():
        assert new[key] is expected, f"{key}: pattern credit={new[key]}, expected {expected}"


_TRACK_BODY = """
JSON="$(cat "{record}")"
extract_receipt_fields "$JSON"
update_track_progress "$JSON" T1
"""

_TRACK_CLOSED = "PROGRESS_STATE: Task completed → Track A idle"


def _track_closed(dirs: dict[str, Path], record: dict) -> bool:
    return _TRACK_CLOSED in _log(dirs)


def test_rp_pattern_closes_track_for_dispatch_finished(tmp_path: Path):
    new, legacy = _decide_all(tmp_path, _TRACK_BODY, _track_closed)

    _assert_paired(new, legacy)
    for key, expected in EXPECTED_SUCCESS.items():
        assert new[key] is expected, f"{key}: track closed={new[key]}, expected {expected}"


# ---------------------------------------------------------------------------
# generate_t0_brief.sh — the inline completion filter
# ---------------------------------------------------------------------------

def _brief_env(tmp_path: Path) -> dict:
    import os

    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    dispatch_dir = data_dir / "dispatches"
    for name in ("queue", "pending", "active", "conflicts"):
        (dispatch_dir / name).mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["VNX_HOME"] = str(VNX_ROOT)
    env["PROJECT_ROOT"] = str(VNX_ROOT)
    env["VNX_DATA_DIR"] = str(data_dir)
    env["VNX_STATE_DIR"] = str(state_dir)
    env["VNX_DISPATCH_DIR"] = str(dispatch_dir)
    env["VNX_DATA_DIR_GUARD"] = "off"
    return env


def _brief_completed_last_hour(tmp_path: Path, name: str, records: list) -> int:
    root = tmp_path / name
    root.mkdir()
    env = _brief_env(root)
    now = datetime.now(timezone.utc).isoformat()
    ledger = Path(env["VNX_STATE_DIR"]) / "t0_receipts.ndjson"
    with ledger.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(dict(record, timestamp=now)) + "\n")
    result = subprocess.run(
        ["bash", str(BRIEF_SCRIPT)], cwd=VNX_ROOT, env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, f"brief failed: {result.stderr}"
    brief = json.loads((Path(env["VNX_STATE_DIR"]) / "t0_brief.json").read_text(encoding="utf-8"))
    return brief["queues"]["completed_last_hour"]


def _gate_line() -> dict:
    return {"event_type": "quality_gate_verification", "dispatch_id": "dlv-gate"}


def test_generate_t0_brief_counts_dispatch_finished(tmp_path: Path):
    """The inline filter counts a completed dispatch_finished like a
    task_complete; a failed/timed-out/unreadable end line counts like its
    task_failed counterpart — not at all."""
    legacy = [
        {"event_type": "task_complete", "status": "success", "dispatch_id": "dlv-completed"},
        {"event_type": "task_failed", "dispatch_id": "dlv-failed"},
        {"event_type": "task_timeout", "status": "timeout", "dispatch_id": "dlv-timed-out"},
        {"event_type": "task_started", "dispatch_id": "dlv-started"},
        _gate_line(),
    ]
    new = [
        {"event_type": "dispatch_finished", "end_state": "completed", "dispatch_id": "dlv-completed"},
        {"event_type": "dispatch_finished", "end_state": "failed", "dispatch_id": "dlv-failed"},
        {"event_type": "dispatch_finished", "end_state": "timed_out", "dispatch_id": "dlv-timed-out"},
        {"event_type": "dispatch_started", "dispatch_id": "dlv-started"},
        {"event_type": "report_ingested", "dispatch_id": "dlv-ingested"},
        _gate_line(),
    ]
    malformed = {"event_type": "dispatch_finished", "status": "success", "dispatch_id": "dlv-malformed"}

    legacy_count = _brief_completed_last_hour(tmp_path, "legacy", legacy)
    new_count = _brief_completed_last_hour(tmp_path, "new", new)
    malformed_count = _brief_completed_last_hour(tmp_path, "malformed", new + [malformed])

    assert legacy_count == 2, f"legacy ledger counted {legacy_count}, expected 2"
    assert new_count == legacy_count, (
        f"a dispatch_finished ledger counted {new_count} where the same dispatches "
        f"written the legacy way counted {legacy_count}")
    assert malformed_count == new_count, (
        f"a dispatch_finished with no end_state added {malformed_count - new_count} completion(s)")
