#!/usr/bin/env python3
"""Canonical state projections for dashboard, T0 brief, and receipt enrichment."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

from terminal_state_reconciler import ReconcilerConfig, reconcile_terminal_state
from terminal_state_shadow import validate_terminal_state_document

try:
    import yaml
except Exception:  # pragma: no cover - optional dependency fallback
    yaml = None  # type: ignore[assignment]


TERMINALS = ("T1", "T2", "T3")
TRACK_BY_TERMINAL = {"T1": "A", "T2": "B", "T3": "C"}
MODEL_BY_TERMINAL = {"T0": "unknown", "T1": "sonnet", "T2": "sonnet", "T3": "opus"}

CONTEXT_PRESSURE_THRESHOLD = 80  # usage % above which context-pressure attention fires
STALE_WORKING_SECONDS = 180      # working terminal with no update triggers stale attention

ATTENTION_PRIORITY: Dict[str, int] = {
    "blocked": 4,
    "review-needed": 3,
    "stale": 2,
    "context-pressure": 1,
}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _status_age_seconds(last_update: str | None) -> int | None:
    parsed = _parse_iso(last_update)
    if parsed is None:
        return None
    return max(0, int((_now_utc() - parsed).total_seconds()))


def _normalize_status(status: Any) -> str:
    value = str(status or "").strip().lower()
    if value in {"claimed", "active", "working", "busy", "in_progress"}:
        return "working"
    if value in {"idle", "ready", "complete", "completed"}:
        return "idle"
    if value in {"blocked", "error", "failed", "timeout"}:
        return "blocked"
    if value in {"offline", "down", "disconnected"}:
        return "offline"
    return "unknown"


def _safe_load_json(path: Path) -> Dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
    except Exception:
        return None
    return None


def _load_context_pressure(state_dir: Path, terminal: str) -> Tuple[int | None, int | None]:
    """Return (usage_pct, ts) for terminal. usage_pct = 100 - remaining_pct."""
    path = state_dir / f"context_window_{terminal}.json"
    data = _safe_load_json(path)
    if data is None:
        return None, None
    remaining = data.get("remaining_pct")
    ts = data.get("ts")
    if not isinstance(remaining, (int, float)):
        return None, None
    usage = max(0, min(100, 100 - int(remaining)))
    return usage, (int(ts) if isinstance(ts, (int, float)) else None)


def _count_staging_dispatches(dispatch_dir: Path) -> int:
    """Count .md files in dispatches/staging/."""
    staging_dir = dispatch_dir / "staging"
    if not staging_dir.is_dir():
        return 0
    try:
        return sum(1 for f in staging_dir.iterdir() if f.suffix == ".md")
    except Exception:
        return 0


def _compute_terminal_attention(
    terminal: str,
    status: str,
    last_update: str,
    status_age_seconds: int | None,
    context_usage_pct: int | None,
    context_ts: int | None,
    stale_after_seconds: int = STALE_WORKING_SECONDS,
) -> Dict[str, Any]:
    """Compute attention state for a worker terminal (T1-T3).

    Priority order: blocked > stale > context-pressure.
    Returns dict with 'needs_human' bool and 'attention' object or None.
    """
    since_last = last_update if last_update and last_update != "never" else _now_iso()

    if status == "blocked":
        return {
            "needs_human": True,
            "attention": {
                "type": "blocked",
                "reason": f"{terminal} is blocked and requires operator intervention",
                "since": since_last,
                "jump_target": terminal,
            },
        }

    if status == "working" and status_age_seconds is not None and status_age_seconds > stale_after_seconds:
        return {
            "needs_human": True,
            "attention": {
                "type": "stale",
                "reason": f"{terminal} has not reported activity for {status_age_seconds}s while working",
                "since": since_last,
                "jump_target": terminal,
            },
        }

    if context_usage_pct is not None and context_usage_pct > CONTEXT_PRESSURE_THRESHOLD:
        since_ctx = (
            datetime.fromtimestamp(context_ts, tz=timezone.utc).isoformat()
            if context_ts is not None
            else _now_iso()
        )
        return {
            "needs_human": True,
            "attention": {
                "type": "context-pressure",
                "reason": f"{terminal} context window at {context_usage_pct}% capacity",
                "since": since_ctx,
                "jump_target": terminal,
            },
        }

    return {"needs_human": False, "attention": None}


def _load_progress_dispatch_map(state_dir: Path) -> Dict[str, str]:
    if yaml is None:
        return {}

    path = state_dir / "progress_state.yaml"
    if not path.exists():
        return {}

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}

    tracks = payload.get("tracks") or {}
    mapping: Dict[str, str] = {}
    for terminal, track in TRACK_BY_TERMINAL.items():
        entry = tracks.get(track) or {}
        status = str(entry.get("status") or "").strip().lower()
        dispatch_id = str(entry.get("active_dispatch_id") or "").strip()
        if status == "working" and dispatch_id:
            mapping[terminal] = dispatch_id
    return mapping


def _load_primary_terminal_state(
    state_dir: Path, stale_after_seconds: int
) -> Tuple[Dict[str, Any] | None, bool, str | None, int | None]:
    primary_path = state_dir / "terminal_state.json"
    if not primary_path.exists():
        return None, False, "primary_missing", None

    age_seconds = int((_now_utc() - datetime.fromtimestamp(primary_path.stat().st_mtime, tz=timezone.utc)).total_seconds())

    payload = _safe_load_json(primary_path)
    if payload is None:
        return None, False, "primary_corrupt:JSONDecodeError", age_seconds

    try:
        validate_terminal_state_document(payload)
    except Exception as exc:
        return None, False, f"primary_corrupt:{exc.__class__.__name__}", age_seconds

    if age_seconds > stale_after_seconds:
        return payload, False, "primary_stale", age_seconds
    return payload, True, None, age_seconds


def build_terminal_snapshot(
    state_dir: str | Path,
    *,
    stale_after_seconds: int = 180,
    receipt_scan_lines: int = 600,
    active_window_seconds: int = 240,
    allow_tmux_probe: bool = True,
) -> Dict[str, Any]:
    state_root = Path(state_dir)
    dispatch_dir = state_root.parent / "dispatches"
    progress_dispatch_map = _load_progress_dispatch_map(state_root)

    primary_doc, primary_ok, primary_error, primary_age_seconds = _load_primary_terminal_state(
        state_root, stale_after_seconds
    )

    raw_records: Dict[str, Dict[str, Any]] = {}
    tmux_runtime: Dict[str, Dict[str, Any]] = {}
    degraded = False
    degraded_reasons: list[str] = []
    source = "terminal_state" if primary_ok else "reconciler"
    reconciled_records: Dict[str, Dict[str, Any]] = {}
    stale_terminals: set[str] = set()

    if primary_ok and primary_doc is not None:
        raw_records = dict((primary_doc.get("terminals") or {}))
        for terminal in TERMINALS:
            record = raw_records.get(terminal) or {}
            last_update = record.get("last_activity")
            age_seconds = _status_age_seconds(str(last_update) if last_update is not None else None)
            if age_seconds is None or age_seconds > stale_after_seconds:
                stale_terminals.add(terminal)
        if stale_terminals:
            reconcile = reconcile_terminal_state(
                state_root,
                config=ReconcilerConfig(
                    stale_after_seconds=stale_after_seconds,
                    receipt_scan_lines=receipt_scan_lines,
                    active_window_seconds=active_window_seconds,
                    allow_tmux_probe=allow_tmux_probe,
                ),
                repair=False,
            )
            reconciled_records = dict((reconcile.get("terminals") or {}))
            tmux_runtime = dict((reconcile.get("evidence") or {}).get("tmux") or {})
            degraded = bool(reconcile.get("degraded"))
            degraded_reasons = [str(item) for item in (reconcile.get("degraded_reasons") or []) if item]
            degraded_reasons.append("terminal_state_per_terminal_stale")
    else:
        reconcile = reconcile_terminal_state(
            state_root,
            config=ReconcilerConfig(
                stale_after_seconds=stale_after_seconds,
                receipt_scan_lines=receipt_scan_lines,
                active_window_seconds=active_window_seconds,
                allow_tmux_probe=allow_tmux_probe,
            ),
            repair=False,
        )
        raw_records = dict((reconcile.get("terminals") or {}))
        tmux_runtime = dict((reconcile.get("evidence") or {}).get("tmux") or {})
        degraded = bool(reconcile.get("degraded"))
        degraded_reasons = [str(item) for item in (reconcile.get("degraded_reasons") or []) if item]
        if primary_error and primary_error not in degraded_reasons:
            degraded_reasons.insert(0, primary_error)

    terminals: Dict[str, Dict[str, Any]] = {}
    for terminal in TERMINALS:
        record = raw_records.get(terminal) or {}
        override_source = False
        if terminal in stale_terminals and reconciled_records.get(terminal):
            reconciled = reconciled_records.get(terminal) or {}
            reconciled_status = _normalize_status(reconciled.get("status"))
            primary_status = _normalize_status(record.get("status"))
            # Prevent stale idle terminals from being promoted to working from
            # tmux/runtime noise alone. Require an explicit claim/dispatch hint.
            has_claim_signal = bool(reconciled.get("claimed_by")) or bool(progress_dispatch_map.get(terminal))
            if (reconciled_status in {"working", "blocked"} and has_claim_signal) or primary_status in {"unknown"}:
                record = reconciled
                override_source = True
        normalized_status = _normalize_status(record.get("status"))
        last_update = str(record.get("last_activity") or "never")
        claimed_by = record.get("claimed_by")
        fallback_task = progress_dispatch_map.get(terminal)
        # Reconciler-only snapshots can infer "active/working" from runtime noise
        # (e.g. pane command still "node") even when there is no live claim.
        # Keep dashboard terminal semantics claim/dispatch-driven.
        if source == "reconciler" and normalized_status == "working" and not claimed_by and not fallback_task:
            normalized_status = "idle"
        current_task = claimed_by if claimed_by else fallback_task
        if normalized_status == "idle":
            current_task = None
        current_command = (
            (tmux_runtime.get(terminal) or {}).get("current_command")
            or (tmux_runtime.get(terminal) or {}).get("command")
            or "claude"
        )

        terminals[terminal] = {
            "status": normalized_status,
            "track": TRACK_BY_TERMINAL[terminal],
            "ready": normalized_status == "idle",
            "last_update": last_update,
            "current_task": current_task,
            "source": source,
            "status_age_seconds": _status_age_seconds(last_update),
            "model": MODEL_BY_TERMINAL[terminal],
            "is_active": normalized_status == "working",
            "current_command": current_command,
            "directory": "vnx-terminal",
            "record_source": (
                "reconciler"
                if override_source
                else (record.get("reconciled_source") if isinstance(record, dict) else None)
            ),
        }

        # Compute attention state
        ctx_usage, ctx_ts = _load_context_pressure(state_root, terminal)
        attention_state = _compute_terminal_attention(
            terminal=terminal,
            status=normalized_status,
            last_update=last_update,
            status_age_seconds=_status_age_seconds(last_update),
            context_usage_pct=ctx_usage,
            context_ts=ctx_ts,
            stale_after_seconds=stale_after_seconds,
        )
        terminals[terminal]["needs_human"] = attention_state["needs_human"]
        terminals[terminal]["attention"] = attention_state["attention"]
        terminals[terminal]["context_usage_pct"] = ctx_usage

    return {
        "generated_at": _now_iso(),
        "source": source,
        "primary_state_ok": primary_ok,
        "primary_error": primary_error,
        "primary_age_seconds": primary_age_seconds,
        "degraded": degraded,
        "degraded_reasons": degraded_reasons,
        "terminals": terminals,
    }


def _dashboard_terminals(snapshot: Dict[str, Any], state_dir: Path | None = None) -> Dict[str, Any]:
    # Load panes.json to get provider information
    panes_data: Dict[str, Any] = {}
    if state_dir:
        panes_file = state_dir / "panes.json"
        panes_data = _safe_load_json(panes_file) or {}

    terminals = {
        "T0": {
            "status": "unknown",
            "model": MODEL_BY_TERMINAL["T0"],
            "provider": (panes_data.get("T0") or {}).get("provider", "claude_code"),
            "is_active": False,
            "current_command": "unknown",
            "directory": "unknown",
            "last_update": "never",
            "needs_human": False,
            "attention": None,
            "context_usage_pct": None,
        }
    }

    # Compute T0 attention: needs review if dispatches are in staging awaiting T0 promotion
    if state_dir is not None:
        ctx_usage_t0, ctx_ts_t0 = _load_context_pressure(state_dir, "T0")
        dispatch_dir = state_dir.parent / "dispatches"
        staging_count = _count_staging_dispatches(dispatch_dir)
        t0_attention: Dict[str, Any] = {"needs_human": False, "attention": None}
        if ctx_usage_t0 is not None and ctx_usage_t0 > CONTEXT_PRESSURE_THRESHOLD:
            since_ctx_t0 = (
                datetime.fromtimestamp(ctx_ts_t0, tz=timezone.utc).isoformat()
                if ctx_ts_t0 is not None
                else _now_iso()
            )
            t0_attention = {
                "needs_human": True,
                "attention": {
                    "type": "context-pressure",
                    "reason": f"T0 context window at {ctx_usage_t0}% capacity",
                    "since": since_ctx_t0,
                    "jump_target": "T0",
                },
            }
        elif staging_count > 0:
            t0_attention = {
                "needs_human": True,
                "attention": {
                    "type": "review-needed",
                    "reason": f"{staging_count} dispatch(es) in staging awaiting review",
                    "since": _now_iso(),
                    "jump_target": "T0",
                },
            }
        terminals["T0"]["needs_human"] = t0_attention["needs_human"]
        terminals["T0"]["attention"] = t0_attention["attention"]
        terminals["T0"]["context_usage_pct"] = ctx_usage_t0

    for terminal in TERMINALS:
        info = (snapshot.get("terminals") or {}).get(terminal) or {}
        pane_info = panes_data.get(terminal) or {}
        payload = {
            "status": info.get("status", "unknown"),
            "model": info.get("model", MODEL_BY_TERMINAL[terminal]),
            "provider": pane_info.get("provider", "claude_code"),
            "is_active": bool(info.get("is_active", False)),
            "current_command": info.get("current_command", "claude"),
            "directory": info.get("directory", "vnx-terminal"),
            "last_update": info.get("last_update", "never"),
        }
        current_task = info.get("current_task")
        if current_task:
            payload["current_task"] = current_task
        payload["needs_human"] = info.get("needs_human", False)
        payload["attention"] = info.get("attention")
        payload["context_usage_pct"] = info.get("context_usage_pct")
        terminals[terminal] = payload
    return terminals


def _brief_terminals(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    brief: Dict[str, Any] = {}
    for terminal in TERMINALS:
        info = (snapshot.get("terminals") or {}).get(terminal) or {}
        brief[terminal] = {
            "status": info.get("status", "unknown"),
            "track": TRACK_BY_TERMINAL[terminal],
            "ready": bool(info.get("ready", False)),
            "last_update": info.get("last_update", "never"),
            "current_task": info.get("current_task"),
            "source": info.get("source", "terminal_state"),
            "status_age_seconds": info.get("status_age_seconds"),
        }
    return brief


def _coerce_brief_terminals(terminals: Any) -> Dict[str, Any]:
    if not isinstance(terminals, dict):
        return {}

    coerced: Dict[str, Any] = {}
    for terminal in TERMINALS:
        entry = terminals.get(terminal)
        if not isinstance(entry, dict):
            continue
        status = _normalize_status(entry.get("status"))
        last_update = entry.get("last_update") or "never"
        coerced[terminal] = {
            "status": status,
            "track": TRACK_BY_TERMINAL[terminal],
            "ready": status == "idle",
            "last_update": last_update,
            "current_task": entry.get("current_task"),
            "source": "t0_brief",
            "status_age_seconds": _status_age_seconds(str(last_update)),
        }
    return coerced


def _load_recommendations_list(state_dir: Path) -> list[Any]:
    recs_file = state_dir / "t0_recommendations.json"
    if not recs_file.exists():
        return []
    payload = _safe_load_json(recs_file)
    if not payload:
        return []
    recommendations = payload.get("recommendations")
    if isinstance(recommendations, list):
        return recommendations[:5]
    return []


def build_notifier_system_state(
    state_dir: str | Path,
    *,
    stale_after_seconds: int = 180,
) -> Dict[str, Any]:
    state_root = Path(state_dir)
    snapshot = build_terminal_snapshot(
        state_root,
        stale_after_seconds=stale_after_seconds,
        allow_tmux_probe=False,
    )

    brief_path = state_root / "t0_brief.json"
    brief_payload = _safe_load_json(brief_path) if brief_path.exists() else None
    brief_payload = brief_payload or {}

    brief_terminals = _coerce_brief_terminals(brief_payload.get("terminals"))
    if snapshot.get("primary_state_ok"):
        terminals = _brief_terminals(snapshot)
    elif brief_terminals:
        terminals = brief_terminals
    else:
        terminals = _brief_terminals(snapshot)

    queues = brief_payload.get("queues") if isinstance(brief_payload.get("queues"), dict) else {}
    next_gates = brief_payload.get("next_gates") if isinstance(brief_payload.get("next_gates"), dict) else {}
    recommendations = brief_payload.get("recommendations")
    if not isinstance(recommendations, list) or not recommendations:
        recommendations = _load_recommendations_list(state_root)

    return {
        "terminals": terminals,
        "queues": queues,
        "next_gates": next_gates,
        "recommendations": recommendations if isinstance(recommendations, list) else [],
        "enriched_at": _now_utc().strftime("%Y-%m-%d %H:%M:%S"),
    }


def build_attention_summary(
    state_dir: str | Path,
    *,
    stale_after_seconds: int = STALE_WORKING_SECONDS,
) -> Dict[str, Any]:
    """Return terminals with attention state for jump command and dashboard."""
    state_root = Path(state_dir)
    snapshot = build_terminal_snapshot(
        state_root,
        stale_after_seconds=stale_after_seconds,
        allow_tmux_probe=False,
    )
    terminals_summary: Dict[str, Any] = _dashboard_terminals(snapshot, state_root)

    needs_attention = [
        tid for tid, info in terminals_summary.items()
        if info.get("needs_human")
    ]
    highest = None
    best_priority = 0
    for tid in needs_attention:
        att = (terminals_summary[tid].get("attention") or {})
        p = ATTENTION_PRIORITY.get(att.get("type", ""), 0)
        if p > best_priority:
            best_priority = p
            highest = tid

    return {
        "terminals": terminals_summary,
        "needs_attention_count": len(needs_attention),
        "needs_attention": needs_attention,
        "highest_priority": highest,
        "generated_at": _now_iso(),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Canonical state projections for VNX scripts")
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("VNX_STATE_DIR", ""),
        help="Path to state directory (defaults to VNX_STATE_DIR)",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("terminal-snapshot", help="Emit full terminal snapshot metadata")
    sub.add_parser("dashboard-terminals", help="Emit dashboard terminals object")
    sub.add_parser("brief-terminals", help="Emit terminal block for t0_brief generation")
    sub.add_parser("notifier-system-state", help="Emit notifier system_state payload")
    sub.add_parser("attention-summary", help="Emit terminal attention state for jump command")

    return parser


def main() -> int:
    args = _build_parser().parse_args()
    state_dir = args.state_dir.strip() or os.environ.get("VNX_STATE_DIR", "").strip()
    if not state_dir:
        print("{}")
        return 0

    state_root = Path(state_dir)
    if args.command == "notifier-system-state":
        payload = build_notifier_system_state(state_root)
    elif args.command == "attention-summary":
        payload = build_attention_summary(state_root)
    else:
        snapshot = build_terminal_snapshot(state_root)
        if args.command == "terminal-snapshot":
            payload = snapshot
        elif args.command == "dashboard-terminals":
            payload = _dashboard_terminals(snapshot, state_root)
        elif args.command == "brief-terminals":
            payload = {
                "terminals": _brief_terminals(snapshot),
                "degraded": snapshot.get("degraded", False),
                "degraded_reasons": snapshot.get("degraded_reasons", []),
                "source": snapshot.get("source"),
                "primary_state_ok": snapshot.get("primary_state_ok", False),
                "primary_error": snapshot.get("primary_error"),
            }
        else:  # pragma: no cover
            payload = {}

    print(json.dumps(payload, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
