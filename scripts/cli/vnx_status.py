#!/usr/bin/env python3
"""vnx status CLI dashboard — W-UX-3.

Reads the strategic-state projection (current_state.md) and the
runtime-state snapshot (t0_state.json) from VNX_STATE_DIR / VNX_DATA_DIR
and renders a 1-screen human-readable dashboard, or JSON via --json.
With --live, live_work and queues are measured at the call instead of read
from the snapshot (OI-2268).
Read-only: no writes to runtime state.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))
# build_t0_state.py (the live measurement) lives in scripts/, not scripts/lib/.
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(_SCRIPTS_DIR))
from health_status import worst_status  # noqa: E402
from vnx_paths import ensure_env  # noqa: E402


# ── Color helpers ──────────────────────────────────────────────────────

_CODES: dict[str, str] = {
    "bold": "\033[1m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "dim": "\033[2m",
    "reset": "\033[0m",
}


def _use_color() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR", "")


def _c(code: str, text: str) -> str:
    if not _use_color():
        return text
    return f"{_CODES.get(code, '')}{text}{_CODES['reset']}"


def _header(title: str) -> None:
    print(_c("bold", _c("cyan", f"\n── {title} ")))


# ── Parsing current_state.md ──────────────────────────────────────────

def _parse_current_state(strategy_dir: Path) -> dict:
    """Parse sections from current_state.md into structured data."""
    cs_file = strategy_dir / "current_state.md"
    if not cs_file.exists():
        return {}

    text = cs_file.read_text()
    result: dict = {
        "focus": "",
        "waves": [],
        "prs": [],
        "decisions": [],
    }

    m = re.search(r"^\*\*Focus\*\*:\s*(.+)$", text, re.MULTILINE)
    if m:
        result["focus"] = m.group(1).strip()

    current_section: str | None = None
    for line in text.splitlines():
        if line.startswith("## Roadmap Waves"):
            current_section = "waves"
        elif line.startswith("## Open Pull Requests"):
            current_section = "prs"
        elif line.startswith("## Recent Decisions"):
            current_section = "decisions"
        elif line.startswith("## "):
            current_section = None
        elif current_section == "waves" and line.startswith("- ["):
            m2 = re.match(r"^- \[(.)\] `([^`]+)`:\s*(.+)$", line)
            if m2:
                badge, wid, title = m2.groups()
                result["waves"].append({
                    "badge": badge,
                    "id": wid,
                    "title": title.strip(),
                })
        elif current_section == "prs" and line.startswith("- PR #"):
            result["prs"].append(line[2:].strip())
        elif current_section == "decisions" and line.startswith("- "):
            result["decisions"].append(line[2:].strip())

    return result


# ── Loading t0_state.json ─────────────────────────────────────────────

def _load_t0_state(state_dir: Path) -> dict:
    f = state_dir / "t0_state.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


def _parse_iso(ts: str) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── Session-freshness refusal (D2) ─────────────────────────────────────
#
# T0 must refuse to orchestrate on a state older than the current session,
# not silently brief against stale numbers (the exact D1 failure: t0_state.json
# stalled 23 days because the SessionStart refresh hook stopped firing, and
# nothing noticed). "Session start" is approximated by panes.json's mtime --
# VNX_HYBRID_FINAL.sh (re)writes state_dir/panes.json on every launch (the
# same file _build_system_health already reads for uptime_seconds), so a
# generated_at OLDER than that means this session never refreshed the
# projection. No panes.json (no live session, e.g. a headless dispatch) can't
# prove staleness either way -- reported as not applicable rather than
# guessed. The refusal itself piggybacks on dispatch_guard.sh's EXISTING
# Check 1 (system_health.status degraded/failed -> WAIT/exit 2): folding
# staleness into system_health.status here means the guard (out of this
# worker's scripts/**-only write scope) needs no change to enforce it.

def _state_freshness(t0: dict, state_dir: Path) -> dict:
    generated_at = t0.get("generated_at")
    panes_path = state_dir / "panes.json"
    if not generated_at or not panes_path.exists():
        return {"applicable": False, "stale": False}

    gen_dt = _parse_iso(str(generated_at))
    if gen_dt is None:
        return {"applicable": False, "stale": False}

    try:
        session_started_at = datetime.fromtimestamp(panes_path.stat().st_mtime, tz=timezone.utc)
    except OSError as exc:
        # panes.json is there but its mtime cannot be read: freshness is
        # unmeasured, not fresh. _build_json_output degrades on it.
        return {
            "applicable": True,
            "stale": None,
            "unmeasured_reason": f"{type(exc).__name__}: {exc}",
            "generated_at": generated_at,
        }

    stale = gen_dt < session_started_at
    return {
        "applicable": True,
        "stale": stale,
        "generated_at": generated_at,
        "session_started_at": session_started_at.isoformat().replace("+00:00", "Z"),
    }


# ── Live measurement (--live, OI-2268) ────────────────────────────────
#
# t0_state.json is a snapshot: a dispatch row or a pending bundle that appears
# after the last build is not in it, and dispatch_guard.sh read GO from it.
# --live measures the two sections the guard decides on at the call, with
# build_t0_state's own functions (measure_live_sections), and stamps the answer
# with live_measured_at so a reader can prove it is fresh. The rest of the
# snapshot stays as built: a full build takes about 15 s on vnx-dev.

_LIVE_SECTIONS = ("live_work", "queues")


def _measure_live(t0: dict, state_dir: Path, dispatch_dir: Path) -> dict:
    """live_work and queues as they are now, with when and which reads failed.

    A builder that does not even import leaves both sections unmeasured with
    the reason: never an empty list, never a zero count.
    """
    measured_at = datetime.now(timezone.utc)
    started = time.monotonic()
    try:
        import build_t0_state

        sections = build_t0_state.measure_live_sections(state_dir, dispatch_dir, t0.get("pr_queue"))
    except Exception as exc:  # vnx-silent-except: the failure is returned as unmeasured sections with their reason, and the guard reads it as WAIT
        reason = f"live measurement raised: {type(exc).__name__}: {exc}"
        sections = {
            "live_work": {"available": False, "reason": reason, "read_error": True},
            "queues": {
                "pending_count": None,
                "conflict_count": None,
                "pending_unmeasured_reason": reason,
                "conflict_unmeasured_reason": reason,
            },
            "degraded_reasons": [f"live_work unavailable: {reason}", f"queues unmeasured: {reason}"],
        }
    return {
        "live_work": sections["live_work"],
        "queues": sections["queues"],
        "unmeasured": list(sections["degraded_reasons"]),
        "measured_at": measured_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def _live_marker(live: dict | None) -> dict:
    """How live_work and queues in this answer were obtained."""
    if live is None:
        return {
            "mode": "snapshot",
            "sections": [],
            "reason": "live_work and queues are read from t0_state.json as built; --live measures them at the call",
        }
    return {
        "mode": "live",
        "sections": list(_LIVE_SECTIONS),
        "duration_seconds": live["duration_seconds"],
        "unmeasured": live["unmeasured"],
    }


# ── Dashboard sections ────────────────────────────────────────────────

def _active_waves(waves: list[dict], n: int = 3) -> list[dict]:
    """Return up to n in-progress/blocked waves, falling back to first n."""
    active = [w for w in waves if w["badge"] in ("~", "!")]
    return (active or waves)[:n]


def _print_focus(cs: dict) -> None:
    _header("Current Focus")
    focus = cs.get("focus", "")
    print(f"  {_c('bold', focus) if focus else '(no focus data)'}")


def _print_waves(cs: dict) -> None:
    _header("Active Waves  (top 3)")
    waves = _active_waves(cs.get("waves", []))
    if not waves:
        print("  (no wave data)")
        return
    for w in waves:
        b = w["badge"]
        color = "yellow" if b == "~" else "red" if b == "!" else "dim"
        print(f"  [{_c(color, b)}] {_c('bold', w['id'])}: {w['title']}")


def _print_prs(cs: dict) -> None:
    _header("Open PRs  (top 3)")
    prs = cs.get("prs", [])[:3]
    if not prs:
        print("  (no open PRs found in current_state.md)")
        return
    for pr in prs:
        print(f"  {_c('dim', chr(8226))} {pr}")


def _print_live_work(t0: dict, live: dict | None = None) -> None:
    # D5: what runs comes from the dispatches table plus the occupancy flock
    # (build_t0_state._build_live_work). The headless lane uses no terminals.
    _header("Live Work")
    if live is not None:
        print(f"  {_c('dim', 'measured live at ' + live['measured_at'])}")
        live_work = live["live_work"] or {}
    else:
        generated_at = str(t0.get("generated_at") or "unknown")
        print(f"  {_c('dim', 'snapshot of t0_state.json, generated_at ' + generated_at + ' (--live measures now)')}")
        live_work = t0.get("live_work") or {}
    if not live_work.get("available"):
        reason = live_work.get("reason") or "no live_work in t0_state.json"
        print(f"  {_c('yellow', 'unavailable')}: {reason}")
        return
    running = (live_work.get("live") or []) + (live_work.get("starting") or [])
    stale = live_work.get("stale") or []
    # A lock probe that raised: in flight, but whether it runs is unknown.
    # Shown so "nothing in flight" can never hide a measurement failure.
    unmeasured = live_work.get("unmeasured") or []
    if not running and not stale and not unmeasured:
        print("  (nothing in flight)")
    for item in running:
        pr = f"  PR #{item['pr']}" if item.get("pr") else ""
        print(
            f"  {_c('green', item.get('state', '?'))} "
            f"{str(item.get('dispatch_id', '?'))[:45]}  "
            f"{_c('dim', str((item.get('age_seconds') or 0) // 60) + ' min')}{pr}"
        )
    for item in stale:
        print(
            f"  {_c('yellow', 'stale')} "
            f"{str(item.get('dispatch_id', '?'))[:45]}  "
            f"{_c('dim', str(item.get('state', '?')) + ', lock ' + str(item.get('lock', '?')))}"
        )
    for item in unmeasured:
        print(
            f"  {_c('red', 'unmeasured')} "
            f"{str(item.get('dispatch_id', '?'))[:45]}  "
            f"{_c('dim', str(item.get('state', '?')) + ', ' + str(item.get('lock', '?')))}"
        )


def _print_decisions(cs: dict) -> None:
    _header("Recent Decisions  (last 3)")
    decisions = cs.get("decisions", [])[:3]
    if not decisions:
        print("  (no decisions found in current_state.md)")
        return
    for d in decisions:
        print(f"  {_c('dim', chr(8226))} {d}")


# ── JSON output ───────────────────────────────────────────────────────

def _build_json_output(cs: dict, t0: dict, state_dir: Path, live: dict | None = None) -> dict:
    """The vnx_status/1.0 answer. *live* (from _measure_live) replaces the
    snapshot's live_work and queues; a read in it that failed degrades
    system_health the way it would in a build."""
    freshness = _state_freshness(t0, state_dir)
    system_health = dict(t0.get("system_health", {}))
    if live is not None and live["unmeasured"]:
        system_health["status"] = worst_status(system_health.get("status", "healthy"), "fail")
        system_health["degraded_reasons"] = list(system_health.get("degraded_reasons") or []) + [
            f"live measurement: {reason}" for reason in live["unmeasured"]
        ]
    if freshness.get("stale"):
        # "fail" here is the nested-signal severity floor (worst_status maps
        # it to "degraded", one tier below "failed" reserved for db-level
        # corruption) -- state built before this session started is a real,
        # measured problem, not an unmeasured one.
        system_health["status"] = worst_status(system_health.get("status", "healthy"), "fail")
    elif freshness.get("unmeasured_reason"):
        # Freshness that could not be measured must not read as fresh: the
        # guard's Check 1 turns this into WAIT.
        system_health["status"] = worst_status(system_health.get("status", "healthy"), "fail")
        system_health["degraded_reasons"] = list(system_health.get("degraded_reasons") or []) + [
            f"state_freshness unmeasured: {freshness['unmeasured_reason']}"
        ]

    return {
        "schema": "vnx_status/1.0",
        "focus": cs.get("focus", ""),
        "active_waves": _active_waves(cs.get("waves", [])),
        "open_prs": cs.get("prs", [])[:3],
        "live_work": live["live_work"] if live is not None else t0.get("live_work", {}),
        "recent_decisions": cs.get("decisions", [])[:3],
        "queues": live["queues"] if live is not None else t0.get("queues", {}),
        "system_health": system_health,
        "state_freshness": freshness,
        # OI-2268: proof of when live_work and queues were measured. null and
        # mode "snapshot" mean they are t0_state.json's, as built.
        "live_measured_at": live["measured_at"] if live is not None else None,
        "live_measurement": _live_marker(live),
        "strategy_available": bool(cs),
        "t0_state_available": bool(t0),
    }


# ── Entry point ───────────────────────────────────────────────────────

def main(argv: list[str] | None = None, data_dir: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vnx status",
        description="VNX operator status dashboard (W-UX-3)",
    )
    parser.add_argument(
        "--json",
        dest="emit_json",
        action="store_true",
        help="Emit parseable JSON (stable schema: vnx_status/1.0)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "Measure live_work and queues now (dispatches table + occupancy flock, "
            "dispatches/pending and conflicts) instead of reading t0_state.json; "
            "the answer carries live_measured_at"
        ),
    )
    args, _extra = parser.parse_known_args(argv)

    if data_dir is None:
        # Resolve VNX_DATA_DIR the same way `vnx doctor` does (vnx_paths.ensure_env →
        # the per-project central store ~/.vnx-data/<project_id>), not via
        # resolve_data_dir(__file__) which, under a central install, resolves relative to
        # the install dir and misses the project's store entirely (issue #225).
        paths = ensure_env()
        data_dir = Path(paths["VNX_DATA_DIR"])
    else:
        paths = None

    strategy_dir = data_dir / "strategy"
    state_dir = data_dir / "state"

    strategy_ok = strategy_dir.exists()
    t0_ok = (state_dir / "t0_state.json").exists()

    if not strategy_ok and not t0_ok:
        if args.emit_json:
            print(json.dumps({
                "schema": "vnx_status/1.0",
                "error": "not_initialised",
                "strategy_available": False,
                "t0_state_available": False,
            }))
        else:
            print("vnx status: not initialised — run 'vnx init' to set up .vnx-data/")
        return 0

    cs = _parse_current_state(strategy_dir) if strategy_ok else {}
    t0 = _load_t0_state(state_dir) if t0_ok else {}
    live = None
    if args.live:
        # The pending queue the build counts: VNX_DISPATCH_DIR, as build_t0_state reads it.
        dispatch_dir = Path(paths["VNX_DISPATCH_DIR"]) if paths is not None else data_dir / "dispatches"
        live = _measure_live(t0, state_dir, dispatch_dir)

    if args.emit_json:
        print(json.dumps(_build_json_output(cs, t0, state_dir, live=live), indent=2))
        return 0

    if not strategy_ok:
        print(
            f"{_c('yellow', 'warn')} strategy/ missing — "
            "run: python3 scripts/build_current_state.py"
        )
    if not t0_ok:
        print(
            f"{_c('yellow', 'warn')} t0_state.json missing — "
            "run: python3 scripts/build_t0_state.py"
        )

    _print_focus(cs)
    _print_waves(cs)
    _print_prs(cs)
    _print_live_work(t0, live)
    _print_decisions(cs)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
