#!/usr/bin/env python3
"""build_current_state.py — Auto-project current_state.md from strategy/ sources.

Seven-section schema per PROJECT_STATE_DESIGN §4.2. Idempotent: timestamps are
derived from input-file mtime, never from datetime.now(). Two consecutive runs
on unchanged inputs produce byte-identical output.
"""
from __future__ import annotations

import datetime
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from project_root import resolve_data_dir  # noqa: E402
from strategy.roadmap import Roadmap, Phase, Wave, load_roadmap, next_actionable_wave, RoadmapValidationError  # noqa: E402
from strategy.decisions import recent_decisions, Decision  # noqa: E402

MAX_PRS = 5
SECTION_HEADERS = [
    "# Mission",
    "## Current focus",
    "## Roadmap snapshot",
    "## In flight",
    "## Last 3 decisions",
    "## Recommended next move",
    "## Resume hints",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _latest_mtime_iso(paths: list[Path]) -> str:
    mtimes = [_mtime(p) for p in paths if p.exists()]
    if not mtimes:
        return "unknown"
    dt = datetime.datetime.fromtimestamp(max(mtimes), tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_roadmap(strategy_dir: Path) -> Roadmap | None:
    rmap = strategy_dir / "roadmap.yaml"
    if not rmap.exists():
        return None
    try:
        return load_roadmap(rmap, strict=False)
    except Exception:
        return None


def _load_t0_state(state_dir: Path) -> dict:
    f = state_dir / "t0_state.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fetch_prs(n: int = MAX_PRS) -> list[dict]:
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--limit", str(n),
             "--json", "number,title,state,headRefName"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return json.loads(result.stdout) or []
    except Exception:
        pass
    return []


def _status_badge(status: str) -> str:
    return {
        "in_progress": "[~]",
        "planned": "[ ]",
        "completed": "[x]",
        "blocked": "[!]",
        "deferred": "[d]",
        "cancelled": "[c]",
    }.get(status, f"[{status}]")


def _phase_status(phase: Phase, roadmap: Roadmap) -> str:
    waves_in_phase = [w for w in roadmap.waves if w.phase_id == phase.phase_id]
    if not waves_in_phase:
        return "empty"
    statuses = {w.status for w in waves_in_phase}
    if "in_progress" in statuses:
        return "in_progress"
    if all(s == "completed" for s in statuses):
        return "completed"
    if "blocked" in statuses:
        return "blocked"
    return "planned"


def _sort_decisions(decisions: list[Decision]) -> list[Decision]:
    return sorted(decisions, key=lambda d: (d.ts, d.decision_id))


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

def _render_mission(roadmap: Roadmap | None) -> list[str]:
    title = roadmap.title if (roadmap and roadmap.title) else "_No mission set._"
    return ["# Mission", "", title, ""]


def _render_current_focus(roadmap: Roadmap | None) -> list[str]:
    lines = ["## Current focus", ""]
    if not roadmap:
        return lines + ["_No roadmap available._", ""]

    active_wave = next((w for w in roadmap.waves if w.status in ("in_progress", "blocked")), None)
    next_wave = next_actionable_wave(roadmap)
    phase_map = {p.phase_id: p for p in roadmap.phases}

    if active_wave:
        phase = phase_map.get(active_wave.phase_id)
        phase_title = phase.title if phase else "?"
        lines.append(f"**Phase {active_wave.phase_id}**: {phase_title}")
        lines.append(
            f"**Active wave**: `{active_wave.wave_id}` — {active_wave.title} "
            f"{_status_badge(active_wave.status)}"
        )
    elif next_wave:
        phase = phase_map.get(next_wave.phase_id)
        phase_title = phase.title if phase else "?"
        lines.append(f"**Phase {next_wave.phase_id}**: {phase_title}")
        lines.append(
            f"**Next wave**: `{next_wave.wave_id}` — {next_wave.title} "
            f"{_status_badge(next_wave.status)}"
        )
    else:
        lines.append("_No active phase. All waves completed or roadmap empty._")

    lines.append("")
    return lines


def _render_roadmap_snapshot(roadmap: Roadmap | None) -> list[str]:
    lines = ["## Roadmap snapshot", ""]
    if not roadmap or not roadmap.phases:
        return lines + ["_No roadmap data._", ""]

    lines.append("| Phase | Title | Status | Blockers |")
    lines.append("|-------|-------|--------|----------|")
    for phase in roadmap.phases:
        status = _phase_status(phase, roadmap)
        badge = _status_badge(status)
        blockers = ", ".join(phase.blocked_on) if phase.blocked_on else "—"
        lines.append(f"| {phase.phase_id} | {phase.title} | {badge} {status} | {blockers} |")

    lines.append("")
    return lines


def _render_in_flight(prs: list[dict], t0_state: dict) -> list[str]:
    lines = ["## In flight", ""]

    if prs:
        lines.append("**Open PRs:**")
        for pr in prs:
            lines.append(
                f"- PR #{pr.get('number', '?')}: {pr.get('title', '')} "
                f"(`{pr.get('headRefName', '')}`)"
            )
    else:
        lines.append("_No open PRs or gh CLI unavailable._")

    lines.append("")

    tracks = t0_state.get("tracks", {})
    active = [(tid, t) for tid, t in tracks.items() if t.get("active_dispatch_id")]
    if active:
        lines.append("**Active dispatches:**")
        for tid, t in sorted(active):
            dispatch_id = t.get("active_dispatch_id", "?")
            status = t.get("status", "?")
            gate = t.get("current_gate", "?")
            lines.append(f"- {tid}: `{dispatch_id}` ({status}, gate: {gate})")
    else:
        lines.append("_No active dispatches._")

    lines.append("")
    return lines


def _render_last_3_decisions(decisions: list[Decision]) -> list[str]:
    lines = ["## Last 3 decisions", ""]
    if not decisions:
        return lines + ["_No decisions recorded._", ""]

    for d in decisions:
        ts = str(d.ts)[:10]
        lines.append(f"- **{d.decision_id}** ({ts}): [{d.scope}] {d.rationale}")

    lines.append("")
    return lines


def _render_recommended_next_move(roadmap: Roadmap | None) -> list[str]:
    lines = ["## Recommended next move", ""]
    if not roadmap:
        return lines + ["_No roadmap available._", ""]

    wave = next_actionable_wave(roadmap)
    if wave is None:
        planned = [w for w in roadmap.waves if w.status == "planned"]
        if planned:
            first = planned[0]
            wave_status = {w.wave_id: w.status for w in roadmap.waves}
            unresolved = [d for d in first.depends_on if wave_status.get(d) != "completed"]
            lines.append(f"No immediately actionable wave. Next planned: `{first.wave_id}`")
            if unresolved:
                lines.append(f"Blocked on: {', '.join(f'`{d}`' for d in unresolved)}")
        else:
            lines.append("_All waves completed or no roadmap. Nothing to do._")
        lines.append("")
        return lines

    lines.append(f"**Wave**: `{wave.wave_id}` — {wave.title}")
    lines.append(f"**Status**: {wave.status} → ready to start")
    if wave.risk_class:
        lines.append(f"**Risk**: {wave.risk_class}")
    if wave.branch_name:
        lines.append(f"**Branch**: `{wave.branch_name}`")
    if wave.depends_on:
        lines.append(f"**Depends on**: {', '.join(f'`{d}`' for d in wave.depends_on)} (all resolved)")
    if wave.blocked_on:
        lines.append(f"**Blocked on**: {', '.join(wave.blocked_on)}")
    if wave.rationale:
        lines.append(f"**Rationale**: {wave.rationale}")
    if wave.notes:
        lines.append(f"**Notes**: {wave.notes}")

    lines.append("")
    return lines


def _render_resume_hints(roadmap: Roadmap | None, last_updated: str) -> list[str]:
    lines = ["## Resume hints", ""]
    lines.append(f"Last updated: {last_updated}")
    lines.append("")
    lines.append("After `/clear`, T0 should:")
    lines.append("1. Check `## Recommended next move` above for the actionable wave.")
    lines.append("2. Run `python3 scripts/build_current_state.py` to refresh this file.")
    lines.append("3. Check `.vnx-data/state/t0_state.json` for terminal availability.")
    lines.append("4. Review `## Last 3 decisions` to restore decision context.")
    if roadmap:
        lines.append(
            f"5. Roadmap has {len(roadmap.waves)} wave(s) across {len(roadmap.phases)} phase(s)."
        )
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build(data_dir: Path | None = None) -> str:
    if data_dir is None:
        data_dir = resolve_data_dir(__file__)

    strategy_dir = data_dir / "strategy"
    state_dir = data_dir / "state"

    roadmap = _load_roadmap(strategy_dir)
    prs = _fetch_prs()
    t0_state = _load_t0_state(state_dir)
    raw_decisions = recent_decisions(n=3, path=strategy_dir / "decisions.ndjson")
    decisions = _sort_decisions(raw_decisions)

    last_updated = _latest_mtime_iso([
        strategy_dir / "roadmap.yaml",
        state_dir / "t0_state.json",
        state_dir / "open_items_digest.json",
        strategy_dir / "decisions.ndjson",
    ])

    body: list[str] = []
    body += _render_mission(roadmap)
    body += _render_current_focus(roadmap)
    body += _render_roadmap_snapshot(roadmap)
    body += _render_in_flight(prs, t0_state)
    body += _render_last_3_decisions(decisions)
    body += _render_recommended_next_move(roadmap)
    body += _render_resume_hints(roadmap, last_updated)

    if len(body) > 200:
        body = body[:199] + ["_[truncated to 200 lines]_"]

    return "\n".join(body) + "\n"


def main() -> None:
    data_dir = resolve_data_dir(__file__)
    strategy_dir = data_dir / "strategy"
    strategy_dir.mkdir(parents=True, exist_ok=True)

    content = build(data_dir)
    out = strategy_dir / "current_state.md"
    out.write_text(content, encoding="utf-8")
    line_count = len(content.splitlines())
    print(f"[ok] current_state.md written ({line_count} lines) → {out}")


if __name__ == "__main__":
    main()
