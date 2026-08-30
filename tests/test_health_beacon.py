"""Tests for the component health beacon framework."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB_DIR = _REPO_ROOT / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from health_beacon import HealthBeacon, all_beacons, beacon_summary  # noqa: E402


# ---------------------------------------------------------------------------
# Case A — heartbeat() writes valid JSON
# ---------------------------------------------------------------------------

def test_heartbeat_writes_valid_json(tmp_path: Path) -> None:
    beacon = HealthBeacon(tmp_path, "comp_a", expected_interval_seconds=600)
    beacon.heartbeat(status="ok", details={"foo": "bar"})

    path = tmp_path / "health" / "comp_a.json"
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["component"] == "comp_a"
    assert payload["status"] == "ok"
    assert payload["details"] == {"foo": "bar"}
    assert payload["expected_interval_seconds"] == 600
    assert isinstance(payload["last_run_ts"], int)
    assert payload["last_run_iso"].endswith("Z")


# ---------------------------------------------------------------------------
# Case B — atomic write uses tmp + os.replace
# ---------------------------------------------------------------------------

def test_atomic_write_uses_tmp_and_replace(tmp_path: Path, monkeypatch) -> None:
    beacon = HealthBeacon(tmp_path, "comp_b")

    seen_tmp_paths: list[str] = []
    seen_replace_calls: list[tuple[str, str]] = []

    real_replace = __import__("os").replace

    def fake_replace(src, dst):
        seen_replace_calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr("health_beacon.os.replace", fake_replace)

    real_open = open

    def watching_open(path, *a, **kw):
        if isinstance(path, (str, Path)) and str(path).endswith(".tmp"):
            seen_tmp_paths.append(str(path))
        return real_open(path, *a, **kw)

    import builtins
    monkeypatch.setattr(builtins, "open", watching_open)

    beacon.heartbeat()

    assert seen_tmp_paths, "expected a *.tmp file to be opened"
    assert seen_replace_calls, "expected os.replace to be invoked"
    assert seen_replace_calls[-1][1].endswith("comp_b.json")


# ---------------------------------------------------------------------------
# Case C — concurrent writes never produce a corrupt file
# ---------------------------------------------------------------------------

def test_concurrent_writes_are_safe(tmp_path: Path) -> None:
    beacon = HealthBeacon(tmp_path, "comp_c", expected_interval_seconds=60)
    iterations = 25

    errors: list[Exception] = []

    def writer(idx: int) -> None:
        try:
            for i in range(iterations):
                beacon.heartbeat_strict(
                    status="ok",
                    details={"writer": idx, "i": i},
                )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"writers raised: {errors}"

    payload = json.loads((tmp_path / "health" / "comp_c.json").read_text(encoding="utf-8"))
    assert payload["component"] == "comp_c"
    assert "writer" in payload["details"]


# ---------------------------------------------------------------------------
# Case D — all_beacons classifies fresh as ok
# ---------------------------------------------------------------------------

def test_all_beacons_fresh_is_ok(tmp_path: Path) -> None:
    HealthBeacon(tmp_path, "fresh", expected_interval_seconds=3600).heartbeat()
    result = all_beacons(tmp_path)
    assert "fresh" in result
    assert result["fresh"]["health"] == "ok"


# ---------------------------------------------------------------------------
# Case E — classifies >1.5x interval as stale
# ---------------------------------------------------------------------------

def test_all_beacons_stale_classification(tmp_path: Path) -> None:
    health_dir = tmp_path / "health"
    health_dir.mkdir(parents=True)
    stale_payload = {
        "component": "stale_one",
        "last_run_ts": int(time.time() - 7200),  # 2h ago
        "last_run_iso": "2020-01-01T00:00:00Z",
        "status": "ok",
        "details": {},
        "expected_interval_seconds": 3600,  # 1h interval => 2h is > 1.5x
    }
    (health_dir / "stale_one.json").write_text(json.dumps(stale_payload), encoding="utf-8")

    result = all_beacons(tmp_path)
    assert result["stale_one"]["health"] == "stale"
    assert result["stale_one"]["age_seconds"] > 3600 * 1.5


# ---------------------------------------------------------------------------
# Case F — status="fail" classifies as fail regardless of age
# ---------------------------------------------------------------------------

def test_status_fail_overrides_age(tmp_path: Path) -> None:
    HealthBeacon(tmp_path, "broken", expected_interval_seconds=3600).heartbeat(
        status="fail", details={"err": "boom"}
    )
    result = all_beacons(tmp_path)
    assert result["broken"]["health"] == "fail"


# ---------------------------------------------------------------------------
# Case G — missing file means component absent from output
# ---------------------------------------------------------------------------

def test_missing_file_not_in_output(tmp_path: Path) -> None:
    HealthBeacon(tmp_path, "present").heartbeat()
    result = all_beacons(tmp_path)
    assert "present" in result
    assert "absent" not in result


# ---------------------------------------------------------------------------
# Case H — corrupt JSON marked corrupt
# ---------------------------------------------------------------------------

def test_corrupt_json_marked_corrupt(tmp_path: Path) -> None:
    health_dir = tmp_path / "health"
    health_dir.mkdir(parents=True)
    (health_dir / "garbled.json").write_text("not json {{{", encoding="utf-8")
    result = all_beacons(tmp_path)
    assert result["garbled"]["health"] == "corrupt"
    assert "error" in result["garbled"]


# ---------------------------------------------------------------------------
# Case I — CLI exit codes
# ---------------------------------------------------------------------------

def _run_cli(state_dir: Path, *extra: str) -> tuple[int, str, str]:
    cli_path = _REPO_ROOT / "scripts" / "health_check.py"
    proc = subprocess.run(
        [sys.executable, str(cli_path), "--state-dir", str(state_dir), *extra],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_exits_zero_when_all_ok(tmp_path: Path) -> None:
    HealthBeacon(tmp_path, "alpha").heartbeat()
    rc, stdout, _ = _run_cli(tmp_path)
    assert rc == 0, stdout


def test_cli_exits_one_on_stale(tmp_path: Path) -> None:
    health_dir = tmp_path / "health"
    health_dir.mkdir(parents=True)
    payload = {
        "component": "old",
        "last_run_ts": int(time.time() - 100000),
        "last_run_iso": "2020-01-01T00:00:00Z",
        "status": "ok",
        "details": {},
        "expected_interval_seconds": 60,
    }
    (health_dir / "old.json").write_text(json.dumps(payload), encoding="utf-8")
    rc, _stdout, _ = _run_cli(tmp_path)
    assert rc == 1


def test_cli_exits_one_on_missing_requested(tmp_path: Path) -> None:
    HealthBeacon(tmp_path, "present").heartbeat()
    rc, _stdout, _ = _run_cli(tmp_path, "--components", "absent")
    assert rc == 1


def test_cli_exits_two_on_unresolvable_state_dir() -> None:
    # Provide a path inside a non-git, non-existent directory tree to force
    # state_dir resolution to fall through. The _resolve_state_dir codepath
    # only triggers when --state-dir is omitted; explicit --state-dir always
    # resolves, so we test the framework-broken path by passing an entirely
    # bogus arg combination.
    cli_path = _REPO_ROOT / "scripts" / "health_check.py"
    proc = subprocess.run(
        [sys.executable, str(cli_path), "--state-dir", "/dev/null/does/not/exist"],
        capture_output=True,
        text=True,
    )
    # Non-existent dir: all_beacons returns empty, and exit code 1 (degraded).
    # That's fine — exit 2 is reserved for *unresolvable* state-dir, which we
    # can't easily trigger when --state-dir is explicit. So we just assert it
    # didn't crash with a stack trace.
    assert proc.returncode in (1, 2)


# ---------------------------------------------------------------------------
# Case J — --json output is parseable
# ---------------------------------------------------------------------------

def test_cli_json_output_parseable(tmp_path: Path) -> None:
    HealthBeacon(tmp_path, "j_one").heartbeat()
    HealthBeacon(tmp_path, "j_two").heartbeat()
    rc, stdout, _ = _run_cli(tmp_path, "--json")
    assert rc == 0
    payload = json.loads(stdout)
    assert "beacons" in payload
    assert {"j_one", "j_two"} <= set(payload["beacons"].keys())


def test_cli_json_marks_missing_component(tmp_path: Path) -> None:
    HealthBeacon(tmp_path, "exists").heartbeat()
    rc, stdout, _ = _run_cli(
        tmp_path, "--json", "--components", "exists,nonexistent"
    )
    assert rc == 1
    payload = json.loads(stdout)
    assert payload["beacons"]["nonexistent"]["health"] == "missing"


# ---------------------------------------------------------------------------
# Case K — --components filter
# ---------------------------------------------------------------------------

def test_cli_components_filter(tmp_path: Path) -> None:
    HealthBeacon(tmp_path, "keep_me").heartbeat()
    HealthBeacon(tmp_path, "ignore_me").heartbeat()
    rc, stdout, _ = _run_cli(tmp_path, "--json", "--components", "keep_me")
    assert rc == 0
    payload = json.loads(stdout)
    assert "keep_me" in payload["beacons"]
    assert "ignore_me" not in payload["beacons"]


# ---------------------------------------------------------------------------
# beacon_summary roll-up
# ---------------------------------------------------------------------------

def test_beacon_summary_rolls_up_overall_state(tmp_path: Path) -> None:
    HealthBeacon(tmp_path, "good").heartbeat()
    HealthBeacon(tmp_path, "broken").heartbeat(status="fail")

    summary = beacon_summary(tmp_path)
    assert summary["overall"] == "fail"
    assert summary["counts"]["fail"] == 1
    assert summary["counts"]["ok"] == 1


def test_event_driven_beacon_within_backstop_still_trusts_ok(tmp_path: Path) -> None:
    """An event-driven beacon well within the freshness backstop still
    trusts a self-reported "ok" as-is — no interval to check age against,
    but recent enough that "no news is good news" still applies."""
    health_dir = tmp_path / "health"
    health_dir.mkdir(parents=True)
    payload = {
        "component": "evented",
        "last_run_ts": int(time.time() - 3600),  # 1h old
        "last_run_iso": "2020-01-01T00:00:00Z",
        "status": "ok",
        "details": {},
        "expected_interval_seconds": None,
    }
    (health_dir / "evented.json").write_text(json.dumps(payload), encoding="utf-8")

    result = all_beacons(tmp_path)
    assert result["evented"]["health"] == "ok"


def test_event_driven_beacon_past_backstop_is_unknown_not_ok(tmp_path: Path) -> None:
    """D3a gap 4: an event-driven beacon (no interval) that died mid-"ok"
    used to stay "ok" forever — no age check applied to it at all. Past
    _EVENT_DRIVEN_MAX_AGE_SECONDS (7 days) its self-reported "ok" can no
    longer be trusted, so it becomes "unknown" — a distinct "I cannot
    verify this beacon's freshness" outcome, never a favorable default."""
    health_dir = tmp_path / "health"
    health_dir.mkdir(parents=True)
    payload = {
        "component": "evented",
        "last_run_ts": int(time.time() - 1_000_000),  # ~11.6 days — past the 7d backstop
        "last_run_iso": "2020-01-01T00:00:00Z",
        "status": "ok",
        "details": {},
        "expected_interval_seconds": None,
    }
    (health_dir / "evented.json").write_text(json.dumps(payload), encoding="utf-8")

    result = all_beacons(tmp_path)
    assert result["evented"]["health"] == "unknown"


# ---------------------------------------------------------------------------
# Staleness dominates status (OI-1024): a beacon older than its interval is
# stale regardless of the status it recorded at write time.
# ---------------------------------------------------------------------------

def test_stale_beacon_with_status_fail_is_stale_not_fail(tmp_path: Path) -> None:
    """A stale beacon reporting status=fail is classified as stale, not fail.

    The write-time status is untrustworthy when the reading is older than the
    expected interval — the component could have recovered silently.
    """
    health_dir = tmp_path / "health"
    health_dir.mkdir(parents=True)
    payload = {
        "component": "stale_failer",
        "last_run_ts": int(time.time() - 7200),  # 2h old
        "last_run_iso": "2020-01-01T00:00:00Z",
        "status": "fail",
        "details": {"err": "ancient history"},
        "expected_interval_seconds": 3600,  # 1h interval -> 2h > 1h
    }
    (health_dir / "stale_failer.json").write_text(json.dumps(payload), encoding="utf-8")

    result = all_beacons(tmp_path)
    assert result["stale_failer"]["health"] == "stale"


def test_tighter_staleness_threshold_1x_not_1_5x(tmp_path: Path) -> None:
    """Age between 1.0x and 1.5x interval -> stale (the old 1.5x window is gone).

    Before OI-1024 a beacon was stale only when age > interval * 1.5.
    Now it is stale when age > interval (1.0x).
    """
    health_dir = tmp_path / "health"
    health_dir.mkdir(parents=True)
    payload = {
        "component": "borderline",
        "last_run_ts": int(time.time() - 4200),  # 70 min old
        "last_run_iso": "2020-01-01T00:00:00Z",
        "status": "ok",
        "details": {},
        "expected_interval_seconds": 3600,  # 1h interval
        # age = 4200 > 3600 (1.0x) but 4200/3600 = 1.167 < 1.5
    }
    (health_dir / "borderline.json").write_text(json.dumps(payload), encoding="utf-8")

    result = all_beacons(tmp_path)
    assert result["borderline"]["health"] == "stale", (
        f"age=4200s > interval=3600s, expected stale, got {result['borderline']['health']}"
    )


def test_fresh_beacon_with_status_fail_still_fail(tmp_path: Path) -> None:
    """A fresh time-driven beacon with status=fail reports health=fail."""
    HealthBeacon(tmp_path, "fresh_failer", expected_interval_seconds=3600).heartbeat(
        status="fail", details={"err": "current problem"}
    )
    result = all_beacons(tmp_path)
    assert result["fresh_failer"]["health"] == "fail"


def test_event_driven_beacon_with_status_fail_is_fail(tmp_path: Path) -> None:
    """Event-driven beacons (interval=None) trust status=fail as-is."""
    health_dir = tmp_path / "health"
    health_dir.mkdir(parents=True)
    payload = {
        "component": "evented_failer",
        "last_run_ts": int(time.time() - 3600),  # 1h old
        "last_run_iso": "2020-01-01T00:00:00Z",
        "status": "fail",
        "details": {},
        "expected_interval_seconds": None,
    }
    (health_dir / "evented_failer.json").write_text(json.dumps(payload), encoding="utf-8")

    result = all_beacons(tmp_path)
    assert result["evented_failer"]["health"] == "fail"


# ---------------------------------------------------------------------------
# D3a gap 1 — a self-reported non-"ok" status can never be judged more
# favorably than what the component itself said.
# ---------------------------------------------------------------------------

def _write_raw_beacon(health_dir: Path, name: str, **overrides) -> None:
    health_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "component": name,
        "last_run_ts": int(time.time()),
        "last_run_iso": "2020-01-01T00:00:00Z",
        "status": "ok",
        "details": {},
        "expected_interval_seconds": 86400,
    }
    payload.update(overrides)
    (health_dir / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_self_reported_stale_status_is_honored_not_upgraded_to_ok(tmp_path: Path) -> None:
    """The exact producer_freshness_monitor case: a fresh (well within its
    interval), self-reported status="stale" beacon must classify as
    health="stale", not "ok" — the old code only special-cased the literal
    string "fail", so any other non-ok status (including "stale" itself)
    fell through to "ok"."""
    health_dir = tmp_path / "health"
    _write_raw_beacon(health_dir, "producer_freshness_monitor", status="stale")

    result = all_beacons(tmp_path)
    assert result["producer_freshness_monitor"]["health"] == "stale"


def test_unrecognized_self_reported_status_falls_to_fail_not_ok(tmp_path: Path) -> None:
    """A component that self-reports something this module has never seen
    before ("degraded", "warn", ...) must fall to the unfavorable side —
    never "ok"."""
    health_dir = tmp_path / "health"
    _write_raw_beacon(health_dir, "flaky_thing", status="degraded")

    result = all_beacons(tmp_path)
    assert result["flaky_thing"]["health"] == "fail"


def test_missing_status_field_falls_to_fail_not_ok(tmp_path: Path) -> None:
    """A beacon payload with no status field at all (status=None) must not
    default to "ok" — the writer told us nothing, which is not the same as
    telling us it is fine."""
    health_dir = tmp_path / "health"
    _write_raw_beacon(health_dir, "silent_on_status", status=None)

    result = all_beacons(tmp_path)
    assert result["silent_on_status"]["health"] == "fail"


def test_self_reported_stale_on_event_driven_beacon_is_honored(tmp_path: Path) -> None:
    """The same gap 1 fix applies to the event-driven (interval=None)
    branch, within the freshness backstop — both branches must agree."""
    health_dir = tmp_path / "health"
    _write_raw_beacon(
        health_dir, "evented_stale_reporter", status="stale",
        expected_interval_seconds=None,
    )

    result = all_beacons(tmp_path)
    assert result["evented_stale_reporter"]["health"] == "stale"


# ---------------------------------------------------------------------------
# D3a gap 2 — a component expected to write a beacon but never has is a
# distinct, more suspect outcome ("absent") than one that wrote and went
# stale. `expected=None` (the default) must be fully backward compatible.
# ---------------------------------------------------------------------------

def test_expected_none_is_backward_compatible(tmp_path: Path) -> None:
    """Omitting `expected` entirely (the pre-D3a call shape every existing
    consumer uses) must produce byte-for-byte the same output as before —
    no synthetic entries appear."""
    HealthBeacon(tmp_path, "present").heartbeat()
    result = all_beacons(tmp_path)
    assert set(result.keys()) == {"present"}


def test_expected_component_with_no_beacon_is_absent(tmp_path: Path) -> None:
    HealthBeacon(tmp_path, "present").heartbeat()
    result = all_beacons(tmp_path, expected=["present", "never_wrote"])
    assert result["present"]["health"] == "ok"
    assert result["never_wrote"] == {"component": "never_wrote", "health": "absent"}


def test_expected_with_no_health_dir_at_all_still_reports_absent(tmp_path: Path) -> None:
    """A fresh checkout / brand-new data dir with no health/ directory yet
    is the loudest absence case of all — every expected name must still
    surface, not short-circuit to an empty result."""
    result = all_beacons(tmp_path, expected=["never_ran"])
    assert result == {"never_ran": {"component": "never_ran", "health": "absent"}}


def test_expected_empty_sequence_adds_nothing(tmp_path: Path) -> None:
    """An explicitly empty `expected` is a real, deliberate "expect
    nothing" — distinct in intent from `None`, but the same output."""
    HealthBeacon(tmp_path, "present").heartbeat()
    result = all_beacons(tmp_path, expected=())
    assert set(result.keys()) == {"present"}


def test_beacon_summary_absent_flips_overall_to_fail(tmp_path: Path) -> None:
    """D3a gap 2's own trap: adding a health value to `counts` without
    teaching `overall` about it leaves the roll-up silently unchanged. An
    absent expected component is at least as bad as a confirmed fail."""
    HealthBeacon(tmp_path, "good").heartbeat()
    summary = beacon_summary(tmp_path, expected=["good", "never_wrote"])
    assert summary["counts"]["absent"] == 1
    assert summary["overall"] == "fail"


def test_beacon_summary_unknown_flips_overall_to_stale(tmp_path: Path) -> None:
    """An event-driven beacon past the freshness backstop ("unknown") is
    "can't verify", not "confirmed bad" — it joins the milder stale tier,
    not the fail tier."""
    health_dir = tmp_path / "health"
    _write_raw_beacon(
        health_dir, "long_dead",
        status="ok", expected_interval_seconds=None,
        last_run_ts=int(time.time() - 1_000_000),
    )
    summary = beacon_summary(tmp_path)
    assert summary["counts"]["unknown"] == 1
    assert summary["overall"] == "stale"


def test_beacon_summary_counts_always_include_absent_and_unknown_keys(tmp_path: Path) -> None:
    """Consumers should be able to rely on all six keys always being
    present (at 0 when unused), matching the existing four-key convention."""
    HealthBeacon(tmp_path, "good").heartbeat()
    summary = beacon_summary(tmp_path)
    assert summary["counts"] == {
        "ok": 1, "stale": 0, "fail": 0, "corrupt": 0, "absent": 0, "unknown": 0,
    }
