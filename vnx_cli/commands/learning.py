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


def vnx_learning(args) -> int:
    sub = getattr(args, "learning_subcommand", None)
    dispatch = {
        "run": _cmd_run,
        "status": _cmd_status,
        "review": _cmd_review,
    }
    if sub in dispatch:
        return dispatch[sub](args)
    print("Usage: vnx learning {run|status|review}")
    return 1
