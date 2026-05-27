"""vnx pool — Pool management CLI for elastic worker pools.

Usage:
    vnx pool status [--project <id>] [--pool-id <pool>] [--json]
    vnx pool scale --project <id> --to <N> [--pool-id <pool>]
    vnx pool config --project <id> [--min N] [--max N] [--policy <name>] [--cooldown <s>]
    vnx pool reap --project <id> [--pool-id <pool>] [--force]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import List, Optional

# Bootstrap scripts/lib into path so pool_manager etc. are importable. Route
# through the shared engine bootstrap so the packaged
# (<site-packages>/vnx_orchestration) and dev-checkout layouts resolve
# identically (PR-PIP-REPACKAGE). Do not recompute scripts/lib inline.
from vnx_cli import _engine  # noqa: E402

_engine.ensure_engine_on_path()

from pool_manager import ExecResult, PoolManager  # noqa: E402
from pool_state_repo import PoolStateRepository  # noqa: E402


def cmd_status(args: argparse.Namespace) -> int:
    """Show current pool state for a project."""
    mgr = _make_manager(args.project, args.pool_id)
    config, state, members = mgr.load_state()

    active = [m for m in members if m.status == "active"]
    if args.json:
        print(json.dumps({
            "pool_id": config.pool_id,
            "project_id": args.project,
            "current": len(active),
            "min": config.min_workers,
            "max": config.max_workers,
            "policy": config.scaling_policy,
            "queue_depth": state.queue_depth,
            "members": [
                {"terminal_id": m.terminal_id, "provider": m.provider}
                for m in active
            ],
        }, indent=2))
    else:
        print(f"Pool: {config.pool_id}")
        print(f"Project: {args.project}")
        print(
            f"Current: {len(active)} / policy={config.scaling_policy}"
            f" (min={config.min_workers}, max={config.max_workers})"
        )
        print(f"Queue depth: {state.queue_depth}")
        if active:
            print("Active workers:")
            for m in active:
                print(f"  {m.terminal_id} ({m.provider}) joined={_fmt_age(m.joined_at)}")
    return 0


def cmd_scale(args: argparse.Namespace) -> int:
    """Force scale pool to a specific worker count."""
    from pool_decision_engine import PoolDecision  # noqa: E402

    mgr = _make_manager(args.project, args.pool_id)
    config, _, members = mgr.load_state()
    current = len([m for m in members if m.status == "active"])
    target = args.to

    if target < config.min_workers or target > config.max_workers:
        print(
            f"ERROR: target {target} outside [{config.min_workers}, {config.max_workers}]",
            file=sys.stderr,
        )
        return 1

    delta = target - current
    if delta == 0:
        print(f"Already at {target}; nothing to do")
        return 0

    action = "scale_up" if delta > 0 else "scale_down"
    decision = PoolDecision(
        action=action,
        delta=delta,
        reason=f"operator request --to {target}",
    )
    result: ExecResult = mgr.execute(decision)
    now = time.time()
    mgr.repo.record_decision(mgr.pool_id, decision, now)  # OI-1485: audit trail
    if result.spawned or result.reaped:
        mgr.repo.update_last_scaled_at(mgr.pool_id, now)  # OI-1485: cooldown anchor
    print(f"Decision: {decision.action} delta={delta}")
    print(f"Spawned: {len(result.spawned)} {result.spawned}")
    print(f"Reaped: {len(result.reaped)} {result.reaped}")
    if result.errors:
        for e in result.errors:
            print(f"  ERROR: {e}", file=sys.stderr)
    return 0 if not result.errors else 1


_VALID_POLICIES = frozenset({"fixed", "queue_depth_v1", "queue_aware", "cost_aware_v1"})


def cmd_config(args: argparse.Namespace) -> int:
    """Update pool config fields (min/max/policy/cooldown)."""
    mgr = _make_manager(args.project, args.pool_id)
    pool_id = args.pool_id or "default"
    config = mgr.repo.get_config(pool_id)
    if not config:
        print(f"ERROR: pool {pool_id!r} not found for project {args.project!r}", file=sys.stderr)
        return 1

    updates = {}
    if args.min is not None:
        if args.min < 0:
            print("ERROR: --min must be >= 0", file=sys.stderr)
            return 1
        updates["min_workers"] = args.min
    if args.max is not None:
        if args.max < 0:
            print("ERROR: --max must be >= 0", file=sys.stderr)
            return 1
        updates["max_workers"] = args.max
    if args.policy is not None:
        if args.policy not in _VALID_POLICIES:
            print(
                f"ERROR: invalid policy {args.policy!r}; valid: {sorted(_VALID_POLICIES)}",
                file=sys.stderr,
            )
            return 1
        updates["scaling_policy"] = args.policy
    if args.cooldown is not None:
        if args.cooldown < 0:
            print("ERROR: --cooldown must be >= 0", file=sys.stderr)
            return 1
        updates["cooldown_seconds"] = args.cooldown

    if not updates:
        print("No changes")
        return 0

    final_min = updates.get("min_workers", config.min_workers)
    final_max = updates.get("max_workers", config.max_workers)
    if final_min > final_max:
        print(
            f"ERROR: min ({final_min}) > max ({final_max}); invariant violated",
            file=sys.stderr,
        )
        return 1

    mgr.repo.update_config(pool_id, updates)
    print(f"Updated config for {pool_id!r}: {updates}")
    return 0


def cmd_reap(args: argparse.Namespace) -> int:
    """Reap stale workers; dry-run by default unless --force."""
    mgr = _make_manager(args.project, args.pool_id)

    if not args.force:
        from pool_reaper import ReapConfig, identify_reap_targets  # noqa: E402
        _config, _state, members = mgr.load_state()
        targets = identify_reap_targets(members, time.time(), ReapConfig())
        print("WARN: --force not specified. Reap candidates (dry-run):")
        for t in targets:
            print(f"  would reap: {t.terminal_id} reason={t.reason}")
        if not targets:
            print("  (no candidates)")
        return 0

    reaped = mgr.reap_dead()
    print(f"Reaped {len(reaped)} workers")
    for t in reaped:
        print(f"  {t.terminal_id} ({t.reason})")
    return 0


def _make_manager(project: str, pool_id: Optional[str]) -> PoolManager:
    return PoolManager(project_id=project, pool_id=pool_id or "default")


def _fmt_age(timestamp: float) -> str:
    age_s = time.time() - timestamp
    if age_s < 60:
        return f"{age_s:.0f}s ago"
    if age_s < 3600:
        return f"{age_s / 60:.0f}min ago"
    return f"{age_s / 3600:.1f}h ago"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="vnx pool — manage elastic worker pools")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="show pool state")
    p_status.add_argument("--project", default="default")
    p_status.add_argument("--pool-id", dest="pool_id", default=None)
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_scale = sub.add_parser("scale", help="force scale pool to N workers")
    p_scale.add_argument("--project", required=True)
    p_scale.add_argument("--to", type=int, required=True)
    p_scale.add_argument("--pool-id", dest="pool_id", default=None)
    p_scale.set_defaults(func=cmd_scale)

    p_config = sub.add_parser("config", help="update pool config")
    p_config.add_argument("--project", required=True)
    p_config.add_argument("--pool-id", dest="pool_id", default=None)
    p_config.add_argument("--min", type=int, dest="min")
    p_config.add_argument("--max", type=int, dest="max")
    p_config.add_argument("--policy", dest="policy")
    p_config.add_argument("--cooldown", type=float, dest="cooldown")
    p_config.set_defaults(func=cmd_config)

    p_reap = sub.add_parser("reap", help="reap stale workers (dry-run by default)")
    p_reap.add_argument("--project", required=True)
    p_reap.add_argument("--pool-id", dest="pool_id", default=None)
    p_reap.add_argument("--force", action="store_true", help="actually reap (default: dry-run)")
    p_reap.set_defaults(func=cmd_reap)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
