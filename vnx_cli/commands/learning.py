"""vnx learning — operator-gated proposal tier for the intelligence self-learning loop."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from vnx_cli import _engine
_engine.ensure_engine_on_path()


def _resolve_state_dir(project_dir: Path) -> Path:
    """Return the canonical VNX state directory anchored on project_dir."""
    return _engine.resolve_data_root(project_dir) / "state"


def _cmd_run(args) -> int:
    """Run the daily learning cycle and write pending proposals for operator review."""
    project_dir = Path(getattr(args, "project_dir", "."))
    from_history = getattr(args, "from_history", False)

    # Validate project
    if not (_engine.resolve_data_root(project_dir).exists()):
        print("error: VNX project not initialized. Run `vnx init` first.", file=sys.stderr)
        return 1

    import learning_loop as ll  # type: ignore[import]
    loop = ll.LearningLoop()
    try:
        report = loop.daily_learning_cycle(from_history=from_history)
    finally:
        try:
            loop.conn.close()
        except Exception:
            pass

    proposal_count = report.get("statistics", {}).get("proposal_count", 0)
    print(f"\nProposals (pending rules): {proposal_count}")
    print("\nSummary:")
    print(json.dumps(report.get("statistics", {}), indent=2))
    return 0


def _cmd_status(args) -> int:
    """Show pending proposals and archival candidates."""
    project_dir = Path(getattr(args, "project_dir", "."))
    state_dir = _resolve_state_dir(project_dir)

    pending_rules_path = state_dir / "pending_rules.json"
    pending_archival_path = state_dir / "pending_archival.json"

    print("Learning loop status")
    print("=" * 40)

    # Pending rules
    pending_rules = 0
    approved_rules = 0
    if pending_rules_path.exists():
        try:
            data = json.loads(pending_rules_path.read_text(encoding="utf-8"))
            rules = data.get("pending_rules", [])
            pending_rules = sum(1 for r in rules if r.get("status") == "pending")
            approved_rules = sum(1 for r in rules if r.get("status") == "approved")
        except (json.JSONDecodeError, OSError):
            pass
    print(f"  Pending rules (awaiting operator approval): {pending_rules}")
    print(f"  Approved rules (ready for ingest):          {approved_rules}")

    # Pending archival / supersede
    pending_archival = 0
    pending_supersede = 0
    if pending_archival_path.exists():
        try:
            data = json.loads(pending_archival_path.read_text(encoding="utf-8"))
            candidates = data.get("pending_archival", [])
            for c in candidates:
                if c.get("status") != "pending":
                    continue
                if c.get("action") == "supersede":
                    pending_supersede += 1
                else:
                    pending_archival += 1
        except (json.JSONDecodeError, OSError):
            pass
    print(f"  Pending archival candidates:                {pending_archival}")
    print(f"  Pending supersede candidates (G-L4 gated): {pending_supersede}")
    print()
    print("To review proposals: vnx learning review")
    return 0


def _cmd_review(args) -> int:
    """Show pending proposals for operator review."""
    project_dir = Path(getattr(args, "project_dir", "."))
    state_dir = _resolve_state_dir(project_dir)
    mode = getattr(args, "mode", "all")

    show_rules = mode in ("all", "rules")
    show_archival = mode in ("all", "archival")

    if show_rules:
        pending_rules_path = state_dir / "pending_rules.json"
        if not pending_rules_path.exists():
            print("No pending_rules.json found. Run `vnx learning run` first.")
        else:
            try:
                data = json.loads(pending_rules_path.read_text(encoding="utf-8"))
                rules = [r for r in data.get("pending_rules", []) if r.get("status") == "pending"]
                if not rules:
                    print("No pending prevention rules.")
                else:
                    print(f"Pending prevention rules ({len(rules)}):")
                    for r in rules:
                        print(f"  [{r.get('id', '?')}] {r.get('pattern', '')[:80]}")
                        print(f"    Prevention: {r.get('prevention', '')[:80]}")
                        print(f"    Confidence: {r.get('confidence', '?')}  "
                              f"Occurrences: {r.get('occurrence_count', '?')}")
                        print()
            except (json.JSONDecodeError, OSError) as exc:
                print(f"error reading pending_rules.json: {exc}", file=sys.stderr)
                return 1

    if show_archival:
        pending_archival_path = state_dir / "pending_archival.json"
        if not pending_archival_path.exists():
            print("No pending_archival.json found.")
        else:
            try:
                data = json.loads(pending_archival_path.read_text(encoding="utf-8"))
                candidates = [c for c in data.get("pending_archival", []) if c.get("status") == "pending"]
                if not candidates:
                    print("No pending archival/supersede candidates.")
                else:
                    print(f"Pending archival/supersede candidates ({len(candidates)}):")
                    for c in candidates:
                        action = c.get("action", "archive")
                        print(f"  [{c.get('source_table', '?')}:{c.get('pattern_id', '?')}] "
                              f"action={action}  conf={c.get('confidence', '?')}")
                        print(f"    Title: {(c.get('title') or '')[:80]}")
                        print(f"    Reason: {c.get('reason', '')}")
                        print()
            except (json.JSONDecodeError, OSError) as exc:
                print(f"error reading pending_archival.json: {exc}", file=sys.stderr)
                return 1

    return 0


def _cmd_grounding_shadow(args) -> int:
    """Compare V1 (substring-join) vs V2 (junction) grounding — read-only, no DB writes."""
    import sqlite3 as _sqlite3
    project_dir = Path(getattr(args, "project_dir", "."))
    limit = int(getattr(args, "limit", 50))

    state_dir = _resolve_state_dir(project_dir)
    db_path = state_dir / "quality_intelligence.db"

    if not db_path.exists():
        print(f"error: quality_intelligence.db not found at {db_path}", file=sys.stderr)
        print("Run `vnx init` to initialise the project first.", file=sys.stderr)
        return 1

    try:
        conn = _sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = _sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT dispatch_id, outcome_status FROM dispatch_metadata "
                "WHERE outcome_status IN ('success', 'failure') "
                "ORDER BY dispatched_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except _sqlite3.OperationalError:
            rows = []
        finally:
            conn.close()
    except Exception as exc:
        print(f"error: could not read dispatch_metadata: {exc}", file=sys.stderr)
        return 1

    if not rows:
        print("No completed dispatches found in dispatch_metadata.")
        print("Run some dispatches first to populate outcome data.")
        return 0

    dispatches = [
        {"dispatch_id": r["dispatch_id"], "status": r["outcome_status"]}
        for r in rows
    ]

    import intelligence_persist as _ip  # type: ignore[import]
    report = _ip.shadow_grounding_compare(db_path, dispatches)

    summary = report["summary"]
    print("\nVNX Learning — Outcome Grounding Shadow (V1 vs V2)")
    print("=" * 52)
    print(f"Dispatches analysed : {summary['total_dispatches']}")
    print(f"Junction available  : {'yes' if summary['junction_available'] else 'no'}")

    if not summary["junction_available"]:
        print()
        print("No dispatch_pattern_offered junction table found.")
        print("V2 grounding requires the junction — run `vnx migrate` to create it.")
        return 0

    diverged_entries = [e for e in report["dispatches"] if e["has_divergence"]]
    if diverged_entries:
        print()
        for entry in diverged_entries:
            n_v2_only = len(entry["v2_only"])
            n_v1_only = len(entry["v1_only"])
            tag = f"[{entry['status']}]"
            print(f"  {entry['dispatch_id']} {tag}")
            if n_v2_only:
                print(f"    V2-only grounded (V1 missed): {n_v2_only} pattern(s)")
            if n_v1_only:
                print(f"    V1-only grounded (V2 skips) : {n_v1_only} pattern(s)")

    print()
    print("Divergence summary:")
    print(f"  Diverged dispatches             : {summary['diverged_dispatches']}/{summary['total_dispatches']}")
    print(f"  Patterns V2 grounds / V1 misses : {summary['v2_only_grounded']}")
    print(f"  Patterns V1 grounds / V2 skips  : {summary['v1_only_grounded']}")

    if summary["diverged_dispatches"] == 0:
        print("\nNo divergence — V1 and V2 agree on all dispatches.")
    else:
        print()
        print("To flip the default to V2 once shadow validates on real data:")
        print("  Set VNX_OUTCOME_GROUNDING_V2=1 in your environment, or")
        print("  flip the config toggle in the dashboard (requires operator approval).")

    return 0


def vnx_learning(args) -> int:
    sub = getattr(args, "learning_subcommand", None)
    dispatch = {
        "run": _cmd_run,
        "status": _cmd_status,
        "review": _cmd_review,
        "grounding-shadow": _cmd_grounding_shadow,
    }
    if sub in dispatch:
        return dispatch[sub](args)
    print("Usage: vnx learning {run|status|review|grounding-shadow}")
    return 1
