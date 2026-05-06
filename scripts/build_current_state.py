#!/usr/bin/env python3
"""build_current_state.py — Auto-project current_state.md from strategy/ sources.

Idempotent: "Last updated:" is derived from the latest input-file mtime,
never from datetime.now(). Run twice on unchanged inputs → byte-identical output.
"""
from __future__ import annotations

import datetime
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from project_root import resolve_data_dir  # noqa: E402

MAX_RECEIPTS = 5
MAX_PRS = 5
MAX_OI = 5
MAX_DECISIONS = 3


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _latest_mtime_iso(paths: list[Path]) -> str:
    """Return ISO-8601 string of the latest mtime among existing paths."""
    mtimes = [_mtime(p) for p in paths if p.exists()]
    if not mtimes:
        return "unknown"
    dt = datetime.datetime.fromtimestamp(max(mtimes), tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_roadmap(strategy_dir: Path) -> dict:
    rmap = strategy_dir / "roadmap.yaml"
    if not rmap.exists():
        return {}
    try:
        import yaml
        return yaml.safe_load(rmap.read_text()) or {}
    except Exception:
        return {}


def _load_open_items(state_dir: Path) -> dict:
    oi = state_dir / "open_items_digest.json"
    if not oi.exists():
        return {}
    try:
        return json.loads(oi.read_text())
    except Exception:
        return {}


def _load_receipts(state_dir: Path, n: int = MAX_RECEIPTS) -> list[dict]:
    receipts_file = state_dir / "t0_receipts.ndjson"
    if not receipts_file.exists():
        return []
    lines: list[dict] = []
    try:
        for raw in receipts_file.read_text().splitlines():
            raw = raw.strip()
            if raw:
                try:
                    lines.append(json.loads(raw))
                except Exception:
                    pass
    except Exception:
        pass
    return lines[-n:]


def _load_decisions(strategy_dir: Path, n: int = MAX_DECISIONS) -> list[dict]:
    dec = strategy_dir / "decisions.ndjson"
    if not dec.exists():
        return []
    lines: list[dict] = []
    try:
        for raw in dec.read_text().splitlines():
            raw = raw.strip()
            if raw:
                try:
                    lines.append(json.loads(raw))
                except Exception:
                    pass
    except Exception:
        pass
    return lines[-n:]


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


def _wave_badge(status: str) -> str:
    return {"in_progress": "[~]", "planned": "[ ]", "completed": "[x]",
            "blocked": "[!]"}.get(status, f"[{status}]")


def _render_roadmap(roadmap: dict) -> list[str]:
    lines = ["## Roadmap Waves", ""]
    phases = roadmap.get("phases", [])
    waves_by_id = {w["wave_id"]: w for w in roadmap.get("waves", [])}

    if not phases and not waves_by_id:
        return lines + ["_No roadmap data._", ""]

    for phase in phases:
        pid = phase.get("phase_id", "?")
        title = phase.get("title", "")
        lines.append(f"### Phase {pid}: {title}")
        for wid in phase.get("waves", []):
            w = waves_by_id.get(wid)
            if w is None:
                continue
            status = w.get("status", "planned")
            lines.append(f"- {_wave_badge(status)} `{wid}`: {w.get('title', wid)}")
        lines.append("")
    return lines


def _render_prs(prs: list[dict]) -> list[str]:
    lines = ["## Open Pull Requests", ""]
    if not prs:
        return lines + ["_No open PRs or gh CLI unavailable._", ""]
    for pr in prs:
        lines.append(
            f"- PR #{pr.get('number', '?')}: {pr.get('title', '')} "
            f"(`{pr.get('headRefName', '')}`)"
        )
    return lines + [""]


def _render_open_items(oi: dict) -> list[str]:
    lines = ["## Open Items", ""]
    summary = oi.get("summary", {})
    open_count = summary.get("open_count", 0)
    blocker_count = summary.get("blocker_count", 0)

    if not oi:
        return lines + ["_No open items data._", ""]

    lines.append(
        f"**{open_count} open** ({blocker_count} blocking, "
        f"{summary.get('warn_count', 0)} warnings)"
    )
    lines.append("")

    blockers = oi.get("top_blockers", [])
    if blockers:
        lines.append("**Blockers:**")
        for item in blockers[:MAX_OI]:
            lines.append(f"- [{item.get('id', '?')}] {item.get('title', '')}")
        lines.append("")

    open_items = oi.get("open_items", [])
    non_blockers = [i for i in open_items if i.get("severity") != "blocking"][:MAX_OI]
    if non_blockers:
        lines.append("**Top open items:**")
        for item in non_blockers:
            sev = item.get("severity", "")
            lines.append(f"- [{item.get('id', '?')}] ({sev}) {item.get('title', '')}")
        if open_count > MAX_OI + len(blockers):
            lines.append(f"- … and {open_count - MAX_OI - len(blockers)} more")
        lines.append("")

    return lines


def _render_receipts(receipts: list[dict]) -> list[str]:
    lines = ["## Recent Receipts", ""]
    if not receipts:
        return lines + ["_No receipts found._", ""]
    for r in reversed(receipts):
        ts = str(r.get("timestamp", ""))[:10]
        event = r.get("event_type", r.get("event", "?"))
        status = r.get("status", "")
        terminal = r.get("terminal", "?")
        dispatch = str(r.get("dispatch_id", "?"))[:30]
        badge = "[ok]" if status == "success" else "[x]" if status in ("failed", "error") else "[~]"
        lines.append(f"- {badge} {ts} {terminal} `{dispatch}` ({event})")
    return lines + [""]


def _render_decisions(decisions: list[dict]) -> list[str]:
    if not decisions:
        return []
    lines = ["## Recent Decisions", ""]
    for d in reversed(decisions):
        ts = str(d.get("timestamp", d.get("decided_at", "")))[:10]
        title = d.get("title", d.get("decision_id", "?"))
        decision = d.get("decision", "")
        lines.append(f"- {ts}: **{title}** → {decision}")
    return lines + [""]


def _find_focus(roadmap: dict) -> str:
    waves_by_id = {w["wave_id"]: w for w in roadmap.get("waves", [])}
    for phase in roadmap.get("phases", []):
        for wid in phase.get("waves", []):
            w = waves_by_id.get(wid)
            if w and w.get("status") in ("in_progress", "blocked"):
                return f"Phase {phase.get('phase_id')}: {phase.get('title', '')}"
    return "No active phase"


def build(data_dir: Path | None = None) -> str:
    if data_dir is None:
        data_dir = resolve_data_dir(__file__)

    strategy_dir = data_dir / "strategy"
    state_dir = data_dir / "state"

    roadmap = _load_roadmap(strategy_dir)
    prs = _fetch_prs()
    receipts = _load_receipts(state_dir)
    oi = _load_open_items(state_dir)
    decisions = _load_decisions(strategy_dir)

    last_updated = _latest_mtime_iso([
        strategy_dir / "roadmap.yaml",
        state_dir / "t0_receipts.ndjson",
        state_dir / "open_items_digest.json",
        strategy_dir / "decisions.ndjson",
    ])

    body: list[str] = [
        "# VNX Project State",
        f"Last updated: {last_updated}",
        "",
        f"**Focus**: {_find_focus(roadmap)}",
        "",
    ]
    body += _render_roadmap(roadmap)
    body += _render_prs(prs)
    body += _render_open_items(oi)
    body += _render_receipts(receipts)
    body += _render_decisions(decisions)

    if len(body) > 200:
        body = body[:199] + ["_[truncated to 200 lines]_"]

    return "\n".join(body) + "\n"


def main() -> None:
    data_dir = resolve_data_dir(__file__)
    strategy_dir = data_dir / "strategy"
    strategy_dir.mkdir(parents=True, exist_ok=True)

    content = build(data_dir)
    out = strategy_dir / "current_state.md"
    out.write_text(content)
    line_count = len(content.splitlines())
    print(f"[ok] current_state.md written ({line_count} lines) → {out}")


if __name__ == "__main__":
    main()
