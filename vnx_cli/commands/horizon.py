#!/usr/bin/env python3
"""vnx horizon — the planning-layer command group (Horizon).

Wraps the existing planning engine (``scripts/planning_cli.py`` cmd_* functions
+ ``objective_reconcile``) with the pip CLI's tenant-safe resolvers. No new
planning logic lives here — every verb below is a thin delegate.

Backward-compat aliases: ``vnx objective <verb>`` and ``vnx deliverable
<verb>`` dispatch to the SAME handler functions as ``vnx horizon <verb>`` /
``vnx horizon deliverable <verb>`` (parity with ``bin/vnx``'s top-level
``objective``/``deliverable`` commands, which shell out to the same
``scripts/planning_cli.py``). One implementation, three entry names.

State-dir resolution (critical): ``planning_cli._resolve_state_dir`` resolves
the REPO-LOCAL ``<git-root>/.vnx-data/state`` — the degraded path. This module
NEVER calls it. Instead every verb resolves the CENTRAL data root via
``_engine.resolve_data_root`` (the same resolver ``vnx track``/``vnx status``
use) and passes the result as ``args.state_dir``, which the delegated cmd_*
functions treat as an explicit override.

Project-id resolution (ADR-007): ``--project-id`` defaults to None at the
argparse layer (never ``'vnx-dev'``). When omitted, it is resolved from
``VNX_PROJECT_ID`` / a ``.vnx-project-id`` marker / the git remote (see
``project_root.resolve_project_id``); if none of those are unambiguous, the
command refuses with exit code 2 instead of silently defaulting.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

from vnx_cli import _engine


def resolve_state_dir(project_dir: str | Path) -> Path:
    """Central data root for planning state — matches ``vnx track``/``vnx status``.

    NOT ``planning_cli._resolve_state_dir`` (that resolves the repo-local
    ``.vnx-data/state`` degraded path).
    """
    return _engine.resolve_data_root(Path(project_dir).resolve()) / "state"


def resolve_project_id(args: Any) -> str:
    """Resolve ``--project-id``: explicit > env/marker/git-remote > hard refusal.

    Never falls back to ``'vnx-dev'`` silently (ADR-007).
    """
    pid = getattr(args, "project_id", None)
    if pid:
        return pid

    _engine.ensure_engine_on_path()
    from project_root import resolve_project_id as _resolve

    try:
        return _resolve(getattr(args, "project_dir", "."))
    except RuntimeError as exc:
        print(
            f"  Error: --project-id not supplied and auto-resolution failed: {exc}",
            file=sys.stderr,
        )
        sys.exit(2)


def _require_planning_cli():
    _engine.ensure_engine_on_path()
    import planning_cli

    return planning_cli


def _prep(args: Any) -> None:
    """Bind the tenant-safe state_dir + resolved project_id onto args, in place."""
    args.state_dir = str(resolve_state_dir(args.project_dir))
    args.project_id = resolve_project_id(args)


# ---------------------------------------------------------------------------
# objective-domain verbs
# ---------------------------------------------------------------------------

def _cmd_add(args: Any) -> int:
    pc = _require_planning_cli()
    _prep(args)
    return pc.cmd_objective_add(args)


def _cmd_list(args: Any) -> int:
    pc = _require_planning_cli()
    _prep(args)
    return pc.cmd_objective_list(args)


def _cmd_show(args: Any) -> int:
    pc = _require_planning_cli()
    _prep(args)
    return pc.cmd_objective_show(args)


def _cmd_sync(args: Any) -> int:
    pc = _require_planning_cli()
    _prep(args)
    return pc.cmd_objective_sync(args)


def _cmd_drift(args: Any) -> int:
    pc = _require_planning_cli()
    _prep(args)
    return pc.cmd_objective_drift(args)


def _cmd_reconcile(args: Any) -> int:
    pc = _require_planning_cli()
    _prep(args)
    # Default repo_root to the resolved project_dir (not the packaged engine's
    # own location) so `gh` PR lookups run against the caller's actual repo.
    if not getattr(args, "repo_root", ""):
        args.repo_root = str(Path(args.project_dir).resolve())
    return pc.cmd_objective_reconcile(args)


def _cmd_reconcile_review(args: Any) -> int:
    pc = _require_planning_cli()
    _prep(args)
    return pc.cmd_objective_reconcile_review(args)


def _cmd_reconcile_streak(args: Any) -> int:
    pc = _require_planning_cli()
    _prep(args)
    return pc.cmd_objective_reconcile_streak(args)


def _cmd_close(args: Any) -> int:
    pc = _require_planning_cli()
    _prep(args)
    return pc.cmd_objective_close(args)


def _cmd_reopen(args: Any) -> int:
    pc = _require_planning_cli()
    _prep(args)
    return pc.cmd_objective_reopen(args)


_VERB_DISPATCH: dict[str, Callable[[Any], int]] = {
    "add": _cmd_add,
    "list": _cmd_list,
    "show": _cmd_show,
    "sync": _cmd_sync,
    "drift": _cmd_drift,
    "reconcile": _cmd_reconcile,
    "reconcile-review": _cmd_reconcile_review,
    "reconcile-streak": _cmd_reconcile_streak,
    "close": _cmd_close,
    "reopen": _cmd_reopen,
}


# ---------------------------------------------------------------------------
# deliverable-domain verbs
# ---------------------------------------------------------------------------

def _cmd_deliverable_add(args: Any) -> int:
    pc = _require_planning_cli()
    _prep(args)
    return pc.cmd_deliverable_add(args)


def _cmd_deliverable_list(args: Any) -> int:
    pc = _require_planning_cli()
    _prep(args)
    return pc.cmd_deliverable_list(args)


def _cmd_deliverable_promote(args: Any) -> int:
    pc = _require_planning_cli()
    _prep(args)
    return pc.cmd_deliverable_promote(args)


_DELIVERABLE_DISPATCH: dict[str, Callable[[Any], int]] = {
    "add": _cmd_deliverable_add,
    "list": _cmd_deliverable_list,
    "promote": _cmd_deliverable_promote,
}


# ---------------------------------------------------------------------------
# plan-gate-domain verbs
# ---------------------------------------------------------------------------

def _cmd_plan_gate_seed(args: Any) -> int:
    pc = _require_planning_cli()
    _prep(args)
    return pc.cmd_plan_gate_seed(args)


def _cmd_plan_gate_run(args: Any) -> int:
    pc = _require_planning_cli()
    _prep(args)
    return pc.cmd_plan_gate_run(args)


def _cmd_plan_gate_status(args: Any) -> int:
    pc = _require_planning_cli()
    _prep(args)
    return pc.cmd_plan_gate_status(args)


_PLAN_GATE_DISPATCH: dict[str, Callable[[Any], int]] = {
    "seed": _cmd_plan_gate_seed,
    "run": _cmd_plan_gate_run,
    "status": _cmd_plan_gate_status,
}


# ---------------------------------------------------------------------------
# entry points (wired from vnx_cli/main.py)
# ---------------------------------------------------------------------------

def _dispatch_deliverable(args: Any) -> int:
    sub = getattr(args, "deliverable_verb", None)
    fn = _DELIVERABLE_DISPATCH.get(sub)
    if fn is None:
        print(
            "  vnx horizon deliverable: missing subcommand. "
            "See `vnx horizon deliverable --help`",
            file=sys.stderr,
        )
        return 1
    return fn(args)


def _dispatch_plan_gate(args: Any) -> int:
    sub = getattr(args, "plan_gate_verb", None)
    fn = _PLAN_GATE_DISPATCH.get(sub)
    if fn is None:
        print(
            "  vnx horizon plan-gate: missing subcommand. "
            "See `vnx horizon plan-gate --help`",
            file=sys.stderr,
        )
        return 1
    return fn(args)


def vnx_horizon(args: Any) -> int:
    """Entry point for ``vnx horizon`` — the full surface (objective verbs +
    nested ``deliverable``/``plan-gate`` groups)."""
    verb = getattr(args, "horizon_verb", None)
    if verb == "deliverable":
        return _dispatch_deliverable(args)
    if verb == "plan-gate":
        return _dispatch_plan_gate(args)
    fn = _VERB_DISPATCH.get(verb)
    if fn is None:
        print("  vnx horizon: missing subcommand. See `vnx horizon --help`", file=sys.stderr)
        return 1
    return fn(args)


def vnx_objective(args: Any) -> int:
    """Entry point for the top-level ``vnx objective`` alias.

    Same verbs, same handler functions as ``vnx horizon`` (excluding the
    nested ``deliverable``/``plan-gate`` groups, which are their own
    top-level surfaces — see ``vnx_deliverable`` — per ``bin/vnx`` parity).
    """
    verb = getattr(args, "objective_verb", None)
    fn = _VERB_DISPATCH.get(verb)
    if fn is None:
        print("  vnx objective: missing subcommand. See `vnx objective --help`", file=sys.stderr)
        return 1
    return fn(args)


def vnx_deliverable(args: Any) -> int:
    """Entry point for the top-level ``vnx deliverable`` alias.

    Same handler functions as ``vnx horizon deliverable``.
    """
    sub = getattr(args, "deliverable_verb", None)
    fn = _DELIVERABLE_DISPATCH.get(sub)
    if fn is None:
        print("  vnx deliverable: missing subcommand. See `vnx deliverable --help`", file=sys.stderr)
        return 1
    return fn(args)
