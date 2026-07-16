#!/usr/bin/env python3
"""VNX CLI — governance-first multi-agent orchestration."""

import argparse
import sys

from vnx_cli import __version__


def _register_doctor_subparser(subparsers: argparse.Action) -> None:
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="validate prerequisites and project structure",
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="emit results as JSON",
    )
    doctor_parser.add_argument(
        "--strict",
        action="store_true",
        help="fail (exit 1) on any warning or failure, not just failures",
    )
    doctor_parser.add_argument(
        "--project-dir",
        default=".",
        metavar="DIR",
        help="project directory to validate (default: current directory)",
    )


def _register_fabric_audit_subparser(subparsers: argparse.Action) -> None:
    fa_parser = subparsers.add_parser(
        "fabric-audit",
        help="audit the governance fabric: no shared-store fork, per-project ledgers, hash-chain integrity",
    )
    fa_parser.add_argument(
        "--data-home",
        default=None,
        metavar="DIR",
        help="central data-home root (default: $VNX_DATA_HOME or ~/.vnx-data)",
    )
    fa_parser.add_argument(
        "--registry",
        default=None,
        metavar="FILE",
        help="project registry JSON (default: ~/.vnx/projects.json)",
    )
    fa_parser.add_argument(
        "--json",
        action="store_true",
        help="emit results as JSON",
    )


def _register_version_subparser(subparsers: argparse.Action) -> None:
    version_parser = subparsers.add_parser(
        "version",
        help="print VNX version, commit, VNX_HOME, pin, and Python info",
    )
    version_parser.add_argument(
        "--project-dir",
        default=".",
        metavar="DIR",
        help="project directory to check for .vnx-version pin (default: current directory)",
    )


def _register_status_subparser(subparsers: argparse.Action) -> None:
    status_parser = subparsers.add_parser(
        "status",
        help="show current dispatch and agent status",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="emit results as JSON",
    )
    status_parser.add_argument(
        "--tracks",
        action="store_true",
        help="include a compact feature-tracks table (phase, derived_status, open OI count)",
    )
    status_parser.add_argument(
        "--project-id",
        default=None,
        metavar="PROJECT_ID",
        help="project_id to filter tracks (default: resolved from VNX_PROJECT_ID or git remote)",
    )
    status_parser.add_argument(
        "--project-dir",
        default=".",
        metavar="DIR",
        help="project directory to inspect (default: current directory)",
    )


def _register_subsystems_subparser(subparsers: argparse.Action) -> None:
    subsystems_parser = subparsers.add_parser(
        "subsystems",
        help="render the live subsystem cockpit SSOT (MAP + ON/OFF + HEALTH)",
    )
    subsystems_parser.add_argument(
        "--json",
        action="store_true",
        help="emit results as JSON",
    )
    subsystems_parser.add_argument(
        "--md",
        action="store_true",
        help="emit the docs/core/SUBSYSTEMS.md ledger table verbatim",
    )
    subsystems_parser.add_argument(
        "--probe",
        action="store_true",
        help="run registered effectiveness probes for live health (unknown for unregistered subsystems)",
    )
    subsystems_parser.add_argument(
        "--project-id",
        default=None,
        metavar="PROJECT_ID",
        help="project_id to resolve flag values for (default: registry default / env)",
    )
    subsystems_parser.add_argument(
        "--project-dir",
        default=".",
        metavar="DIR",
        help="project directory to resolve the data root from (default: current directory)",
    )


def _register_init_subparser(subparsers: argparse.Action) -> None:
    init_parser = subparsers.add_parser(
        "init",
        help="scaffold a new VNX project in the current directory",
    )
    init_parser.add_argument(
        "project_path",
        nargs="?",
        default=None,
        metavar="PROJECT_PATH",
        help="target directory (default: current directory; overrides --project-dir)",
    )
    init_parser.add_argument(
        "--project-dir",
        default=".",
        metavar="DIR",
        help="target directory (default: current directory)",
    )
    init_parser.add_argument(
        "--project-id",
        default=None,
        metavar="ID",
        help="explicit project_id (default: derived from the directory name); "
             "must match ^[a-z][a-z0-9-]{1,31}$",
    )
    init_parser.add_argument(
        "--template",
        default="default",
        choices=["default", "minimal"],
        metavar="TEMPLATE",
        help="scaffold template: 'default' (full T0-T3) or 'minimal' (T0 only). Default: default",
    )
    init_parser.add_argument(
        "--non-interactive",
        dest="non_interactive",
        action="store_true",
        help="skip all interactive prompts (for CI / scripted use)",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing scaffold files (allows reinitialisation)",
    )


def _register_pool_subparser(subparsers: argparse.Action) -> None:
    pool_parser = subparsers.add_parser(
        "pool",
        help="manage elastic worker pools (status/scale/config/reap)",
    )
    pool_parser.add_argument(
        "pool_args",
        nargs=argparse.REMAINDER,
        help="pool subcommand and arguments",
    )


def _register_role_subparser(subparsers: argparse.Action) -> None:
    role_parser = subparsers.add_parser(
        "role",
        help="sync the canonical, fleet-wide T0 orchestrator role (Claude/Codex/Kimi)",
    )
    role_parser.add_argument(
        "--project-dir",
        default=".",
        metavar="DIR",
        help="target repo directory (default: current directory's git root)",
    )
    role_subs = role_parser.add_subparsers(dest="role_subcommand", metavar="SUBCOMMAND")

    rs_parser = role_subs.add_parser(
        "sync",
        help="refresh role-orchestrator.md + AGENTS.md/GEMINI.md role block from canonical VNX",
    )
    rs_parser.add_argument(
        "--project-dir",
        default=".",
        metavar="DIR",
        help="target repo directory (default: current directory's git root)",
    )
    rs_apply_group = rs_parser.add_mutually_exclusive_group()
    rs_apply_group.add_argument(
        "--apply", action="store_true", dest="apply",
        help="write changes, with a timestamped backup first (default: dry-run preview)",
    )
    rs_apply_group.add_argument(
        "--dry-run", action="store_false", dest="apply",
        help="preview only, no writes (default)",
    )
    rs_parser.set_defaults(apply=False)


def _register_update_subparser(subparsers: argparse.Action) -> None:
    update_parser = subparsers.add_parser(
        "update",
        help="flip active VNX version in central install (pre-central-install scaffolding)",
    )
    update_parser.add_argument(
        "--to",
        dest="to_version",
        metavar="VERSION",
        help="target version (e.g. '1.0.0' or 'edge')",
    )
    update_parser.add_argument(
        "--keep-last",
        type=int,
        default=3,
        metavar="N",
        help="number of old versions to retain (default: 3)",
    )
    update_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print planned actions without making filesystem changes",
    )
    update_parser.add_argument(
        "--rollback",
        action="store_true",
        help="revert current symlink to the previous version",
    )


def _register_dispatch_agent_subparser(subparsers: argparse.Action) -> None:
    dispatch_parser = subparsers.add_parser(
        "dispatch-agent",
        help="dispatch a task to a named agent",
    )
    dispatch_parser.add_argument(
        "--agent",
        required=True,
        metavar="NAME",
        help="agent name (must have agents/<NAME>/CLAUDE.md)",
    )
    dispatch_parser.add_argument(
        "--instruction",
        required=False,
        default=None,
        metavar="TEXT",
        help="instruction text to send to the agent (optional for agents with default_instruction in config.yaml)",
    )
    dispatch_parser.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="model to use (default: sonnet, or the agent's config.yaml model when VNX_AGENT_FOLDERS is enabled)",
    )
    dispatch_parser.add_argument(
        "--project-dir",
        default=".",
        metavar="DIR",
        help="project directory (default: current directory)",
    )
    dispatch_parser.add_argument(
        "--deadline-seconds",
        type=int,
        default=None,
        dest="deadline_seconds",
        metavar="SECONDS",
        help=(
            "dispatch deadline in seconds, must be 300-14400 (default: unset, "
            "which preserves the lane's current 3600s default)"
        ),
    )


def _register_track_subparser(subparsers: argparse.Action) -> None:
    track_parser = subparsers.add_parser(
        "track",
        help="manage feature-tracks (new/activate/park/unpark/done/oi-close/dispatch/list/show)",
    )
    track_parser.add_argument(
        "--project-dir",
        default=".",
        metavar="DIR",
        help="project directory (default: current directory)",
    )
    track_subs = track_parser.add_subparsers(dest="track_subcommand", metavar="SUBCOMMAND")

    tn_parser = track_subs.add_parser("new", help="create a new track")
    tn_parser.add_argument("track_id", metavar="TRACK_ID")
    tn_parser.add_argument("--project-id", required=True, metavar="PROJECT_ID",
                           help="project_id for this track (required; ADR-007)")
    tn_parser.add_argument("--title", required=True, metavar="TITLE")
    tn_parser.add_argument("--goal", required=True, metavar="GOAL")
    tn_parser.add_argument("--priority", choices=["high", "medium", "low"], metavar="PRIORITY")
    tn_parser.add_argument("--project-dir", default=".", metavar="DIR")

    ta_parser = track_subs.add_parser("activate", help="activate a queued track")
    ta_parser.add_argument("track_id", metavar="TRACK_ID")
    ta_parser.add_argument("--project-id", required=True, metavar="PROJECT_ID",
                           help="project_id for this track (required)")
    ta_parser.add_argument("--reason", metavar="REASON")
    ta_parser.add_argument("--project-dir", default=".", metavar="DIR")

    tp_parser = track_subs.add_parser("park", help="park an active track")
    tp_parser.add_argument("track_id", metavar="TRACK_ID")
    tp_parser.add_argument("--project-id", required=True, metavar="PROJECT_ID",
                           help="project_id for this track (required)")
    tp_parser.add_argument("--reason", required=True, metavar="REASON")
    tp_parser.add_argument("--project-dir", default=".", metavar="DIR")

    tu_parser = track_subs.add_parser("unpark", help="unpark a parked track to queued")
    tu_parser.add_argument("track_id", metavar="TRACK_ID")
    tu_parser.add_argument("--project-id", required=True, metavar="PROJECT_ID",
                           help="project_id for this track (required)")
    tu_parser.add_argument("--reason", metavar="REASON")
    tu_parser.add_argument("--project-dir", default=".", metavar="DIR")

    td_parser = track_subs.add_parser("dispatch", help="create a dispatch for a track")
    td_parser.add_argument("track_id", metavar="TRACK_ID")
    td_parser.add_argument("--project-id", required=True, metavar="PROJECT_ID",
                           help="project_id for this track (required)")
    td_parser.add_argument("--pr", required=True, metavar="PR-ID")
    td_parser.add_argument("--terminal", required=True, choices=["T1", "T2", "T3"], metavar="TERMINAL")
    td_parser.add_argument("--instruction-file", metavar="PATH")
    td_parser.add_argument("--project-dir", default=".", metavar="DIR")

    tl_parser = track_subs.add_parser("list", help="list tracks")
    tl_parser.add_argument("--phase", choices=["queued", "active", "parked", "done"], metavar="PHASE")
    tl_parser.add_argument("--project-id", default=None, metavar="PROJECT_ID",
                           help="filter by project_id (default: resolved from git remote / VNX_PROJECT_ID)")
    tl_parser.add_argument("--all-projects", action="store_true",
                           help="show tracks across all projects (central operator view)")
    tl_parser.add_argument("--project-dir", default=".", metavar="DIR")

    ts_parser = track_subs.add_parser("show", help="show track detail")
    ts_parser.add_argument("track_id", metavar="TRACK_ID")
    ts_parser.add_argument("--project-id", default=None, metavar="PROJECT_ID",
                           help="project_id (default: resolved from git remote / VNX_PROJECT_ID)")
    ts_parser.add_argument("--project-dir", default=".", metavar="DIR")

    tdone_parser = track_subs.add_parser("done", help="close a track (transition to phase=done)")
    tdone_parser.add_argument("track_id", metavar="TRACK_ID")
    tdone_parser.add_argument("--project-id", required=True, metavar="PROJECT_ID",
                              help="project_id for this track (required; ADR-007)")
    tdone_parser.add_argument("--reason", required=True, metavar="REASON",
                              help="closure reason (required; recorded in phase history)")
    tdone_parser.add_argument("--project-dir", default=".", metavar="DIR")

    toic_parser = track_subs.add_parser("oi-close", help="non-destructively close a track open-item")
    toic_parser.add_argument("track_id", metavar="TRACK_ID")
    toic_parser.add_argument("oi_id", metavar="OI_ID")
    toic_parser.add_argument("--project-id", required=True, metavar="PROJECT_ID",
                             help="project_id for this track (required; ADR-007)")
    toic_parser.add_argument("--link-type", required=True,
                             choices=["blocks", "warns", "related"], metavar="LINK_TYPE",
                             help="link_type of the OI to close (blocks/warns/related)")
    toic_parser.add_argument("--reason", required=True, metavar="REASON",
                             help="resolution reason (required; recorded in audit trail)")
    toic_parser.add_argument("--project-dir", default=".", metavar="DIR")


_HORIZON_CHOICES = ("now", "next", "later")
_PHASE_CHOICES = ("queued", "active", "parked", "done")
_OUTPUT_KIND_CHOICES = ("pr", "post", "deal", "doc")


def _common_horizon_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--project-id",
        default=None,
        metavar="PROJECT_ID",
        help="project_id (default: resolved from VNX_PROJECT_ID / .vnx-project-id / "
             "git remote; never silently 'vnx-dev' — ADR-007)",
    )
    p.add_argument("--project-dir", default=".", metavar="DIR")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")


def _register_objective_verbs(subs: argparse.Action) -> None:
    """Register the objective-domain verbs shared by `vnx horizon` and the
    `vnx objective` backward-compat alias — same flags, same handlers."""
    p_add = subs.add_parser("add", help="add an ad-hoc objective/track (no ROADMAP edit)")
    _common_horizon_args(p_add)
    p_add.add_argument("track_id", metavar="TRACK_ID")
    p_add.add_argument("title", metavar="TITLE")
    p_add.add_argument("goal_state", metavar="GOAL_STATE", help="what 'done' looks like")
    p_add.add_argument("--horizon", choices=_HORIZON_CHOICES, default=None)
    p_add.add_argument("--priority", default=None)

    p_list = subs.add_parser("list", help="list objectives grouped by horizon")
    _common_horizon_args(p_list)
    p_list.add_argument("--horizon", choices=_HORIZON_CHOICES, default=None)
    p_list.add_argument("--phase", choices=_PHASE_CHOICES, default=None)
    p_list.add_argument("--all", action="store_true",
                        help="include done tracks (hidden by default)")

    p_show = subs.add_parser("show", help="show one objective")
    _common_horizon_args(p_show)
    p_show.add_argument("track_id", metavar="TRACK_ID")

    p_sync = subs.add_parser(
        "sync", help="re-project ROADMAP.yaml -> tracks (CHECK by default; --apply to write)"
    )
    _common_horizon_args(p_sync)
    p_sync.add_argument("--apply", action="store_true",
                        help="apply the idempotent projection (default: dry-run check)")
    p_sync.add_argument("--roadmap", default=None, help="path to ROADMAP.yaml (default: project root)")

    p_drift = subs.add_parser(
        "drift",
        help="advisory drift-gate: report declared-vs-derived divergence (exit 0)",
    )
    _common_horizon_args(p_drift)
    p_drift.add_argument(
        "--repo-root", default="", dest="repo_root", metavar="PATH",
        help="project repo root for the ROADMAP.yaml (Source-3) evidence "
             "(default: resolved --project-dir)",
    )

    p_reconcile = subs.add_parser(
        "reconcile",
        help="batch git-grounded auto-close: verify PR merge state via gh and close done tracks",
    )
    _common_horizon_args(p_reconcile)
    p_reconcile.add_argument(
        "--apply", action="store_true",
        help="close CONFIRMED tracks (default: check only — no phase writes)",
    )
    p_reconcile.add_argument(
        "--allow-closed-siblings", action="store_true", dest="allow_closed_siblings",
        help="CONFIRMED if >=1 PR merged even when siblings are CLOSED-unmerged",
    )
    p_reconcile.add_argument(
        "--max-gh-calls", type=int, default=50, dest="max_gh_calls", metavar="N",
        help="cap live gh pr view calls per run (default 50; excess -> deferred)",
    )
    p_reconcile.add_argument(
        "--repo-root", default="", dest="repo_root", metavar="PATH",
        help="git repo root for gh calls (default: resolved --project-dir)",
    )

    p_rec_review = subs.add_parser(
        "reconcile-review",
        help="record an operator review of a reconcile run (ok or false-candidate)",
    )
    _common_horizon_args(p_rec_review)
    p_rec_review.add_argument("run_id", help="run_id from reconcile_history.ndjson")
    p_rec_review.add_argument("--reviewer", required=True, help="reviewer name or id")
    p_rec_review.add_argument(
        "--verdict", required=True, choices=["ok", "false-candidate"],
        help="ok = candidate is correct; false-candidate = reconcile over-nominated this track",
    )
    p_rec_review.add_argument("--note", default="", help="optional review note")

    p_rec_streak = subs.add_parser(
        "reconcile-streak",
        help="compute the consecutive clean-run streak for the VNX_AUTO_CLOSE flip decision",
    )
    _common_horizon_args(p_rec_streak)

    p_close = subs.add_parser(
        "close",
        help="close-the-loop: advance declared phase to a terminal derived_status (human-gated)",
    )
    _common_horizon_args(p_close)
    p_close.add_argument("track_id")
    p_close.add_argument("--apply", action="store_true",
                         help="write the transition (default: dry-run check)")
    p_close.add_argument("--approval-id", default="",
                         help="operator approval token (REQUIRED with --apply)")
    p_close.add_argument("--include-parked", action="store_true",
                         help="allow closing a PARKED track (un-parks it; off by default)")
    p_close.add_argument(
        "--repo-root", default="", dest="repo_root", metavar="PATH",
        help="project repo root for the ROADMAP.yaml (Source-3) evidence "
             "(default: resolved --project-dir)",
    )

    p_reopen = subs.add_parser(
        "reopen", help="reopen a done track: done -> active (operator-gated, audited)",
    )
    _common_horizon_args(p_reopen)
    p_reopen.add_argument("track_id")
    p_reopen.add_argument("--approval-id", default="", dest="approval_id",
                          help="operator approval token (REQUIRED)")
    p_reopen.add_argument("--reason", default="",
                          help="reason for reopening (REQUIRED)")


def _register_deliverable_verbs(subs: argparse.Action) -> None:
    """Register the deliverable-domain verbs shared by `vnx horizon deliverable`
    and the top-level `vnx deliverable` backward-compat alias."""
    p_dadd = subs.add_parser("add", help="plan a deliverable (proposed dispatch)")
    _common_horizon_args(p_dadd)
    p_dadd.add_argument("--objective", required=True, metavar="TRACK_ID",
                        help="track/objective this deliverable belongs to")
    p_dadd.add_argument("--output-kind", required=True, choices=_OUTPUT_KIND_CHOICES,
                        metavar="KIND", dest="output_kind")
    p_dadd.add_argument("--title", required=True)

    p_dlist = subs.add_parser("list", help="list deliverables grouped by objective")
    _common_horizon_args(p_dlist)
    p_dlist.add_argument("--objective", default=None, metavar="TRACK_ID")

    p_dpromote = subs.add_parser(
        "promote", help="human gate: promote deliverable from proposed -> ready",
    )
    _common_horizon_args(p_dpromote)
    p_dpromote.add_argument("dispatch_id")


def _register_plan_gate_verbs(subs: argparse.Action) -> None:
    """Register the plan-gate-domain verbs under `vnx horizon plan-gate`."""
    p_pseed = subs.add_parser(
        "seed", help="seed the OI-PLAN blocker (track stays blocked until the gate passes)",
    )
    _common_horizon_args(p_pseed)
    p_pseed.add_argument("track_id")

    p_prun = subs.add_parser(
        "run", help="run the panel over a plan doc; on PASS, resolve the blocker",
    )
    _common_horizon_args(p_prun)
    p_prun.add_argument("track_id")
    p_prun.add_argument("--doc", required=True, help="path to the plan doc under review")

    p_pstat = subs.add_parser(
        "status", help="show a track's plan-gate state + derived_status",
    )
    _common_horizon_args(p_pstat)
    p_pstat.add_argument("track_id")

    p_patt = subs.add_parser(
        "attest",
        help="operator escape-hatch: attest the plan gate as passed without re-running the panel",
    )
    _common_horizon_args(p_patt)
    p_patt.add_argument("track_id")
    p_patt.add_argument("--reason", default=None,
                        help="operator attestation reason (REQUIRED)")
    p_patt.add_argument("--approval-id", dest="approval_id", default=None,
                        help="operator approval token (REQUIRED)")


def _register_horizon_subparser(subparsers: argparse.Action) -> None:
    horizon_parser = subparsers.add_parser(
        "horizon",
        help="planning layer: objectives, deliverables, plan-gate (aliases: objective, deliverable)",
    )
    horizon_parser.add_argument("--project-dir", default=".", metavar="DIR")
    horizon_subs = horizon_parser.add_subparsers(dest="horizon_verb", metavar="VERB")
    _register_objective_verbs(horizon_subs)

    dlv_parser = horizon_subs.add_parser(
        "deliverable", help="deliverable plane (proposed dispatches)"
    )
    dlv_parser.add_argument("--project-dir", default=".", metavar="DIR")
    dlv_subs = dlv_parser.add_subparsers(dest="deliverable_verb", metavar="VERB")
    _register_deliverable_verbs(dlv_subs)

    pg_parser = horizon_subs.add_parser(
        "plan-gate", help="plan-first gate: a multi-model panel reviews a plan before any build"
    )
    pg_parser.add_argument("--project-dir", default=".", metavar="DIR")
    pg_subs = pg_parser.add_subparsers(dest="plan_gate_verb", metavar="VERB")
    _register_plan_gate_verbs(pg_subs)


def _register_objective_subparser(subparsers: argparse.Action) -> None:
    """Backward-compat alias: `vnx objective <verb>` — same verbs/handlers as
    `vnx horizon <verb>` (bin/vnx parity)."""
    objective_parser = subparsers.add_parser(
        "objective", help="alias for `vnx horizon` (backward compat)",
    )
    objective_parser.add_argument("--project-dir", default=".", metavar="DIR")
    objective_subs = objective_parser.add_subparsers(dest="objective_verb", metavar="VERB")
    _register_objective_verbs(objective_subs)


def _register_deliverable_subparser(subparsers: argparse.Action) -> None:
    """Backward-compat alias: `vnx deliverable <verb>` — same handlers as
    `vnx horizon deliverable` (bin/vnx parity)."""
    deliverable_parser = subparsers.add_parser(
        "deliverable", help="alias for `vnx horizon deliverable` (backward compat)",
    )
    deliverable_parser.add_argument("--project-dir", default=".", metavar="DIR")
    deliverable_subs = deliverable_parser.add_subparsers(dest="deliverable_verb", metavar="VERB")
    _register_deliverable_verbs(deliverable_subs)


def _register_handoff_subparser(subparsers: argparse.Action) -> None:
    handoff_parser = subparsers.add_parser(
        "handoff",
        help="read/ack the T0 context-rotation handoff (show/mark-ready)",
    )
    handoff_parser.add_argument("--project-dir", default=".", metavar="DIR")
    handoff_parser.add_argument(
        "--project-id",
        default=None,
        metavar="PROJECT_ID",
        help="project_id (default: resolved from VNX_PROJECT_ID / .vnx-project-id / git remote)",
    )
    handoff_subs = handoff_parser.add_subparsers(dest="handoff_subcommand", metavar="SUBCOMMAND")

    show_parser = handoff_subs.add_parser("show", help="print the resume briefing parsed from handoff.md")
    show_parser.add_argument(
        "--logdir",
        default=None,
        metavar="DIR",
        help="directory containing handoff.md (default: the project_id+terminal-scoped rotation dir)",
    )
    show_parser.add_argument("--terminal", default="T0", metavar="TERMINAL")
    show_parser.add_argument(
        "--mark-ready",
        action="store_true",
        dest="mark_ready",
        help="also write the .ready signal after printing (requires --rotation-id)",
    )
    show_parser.add_argument("--rotation-id", default=None, metavar="ROTATION_ID")
    # SUPPRESS (not "." / None): the subparsers action parses each subcommand
    # into a FRESH namespace and then copies every attribute from it back
    # onto the parent namespace — including untouched defaults. Without
    # SUPPRESS, that copy silently clobbers a --project-dir/--project-id
    # already set at the parent `handoff` level (e.g. `vnx handoff
    # --project-id foo show`) back to this subparser's own default. SUPPRESS
    # means "don't set this attribute at all unless the user passed it here",
    # so an unset flag on the subcommand never overwrites a value the parent
    # already resolved.
    show_parser.add_argument("--project-dir", default=argparse.SUPPRESS, metavar="DIR")
    show_parser.add_argument("--project-id", default=argparse.SUPPRESS, metavar="PROJECT_ID")

    mark_ready_parser = handoff_subs.add_parser("mark-ready", help="write the rotation_id-stamped .ready signal")
    mark_ready_parser.add_argument("--rotation-id", required=True, metavar="ROTATION_ID")
    mark_ready_parser.add_argument("--terminal", default="T0", metavar="TERMINAL")
    # See the show_parser comment above — same duplicate-dest clobber risk.
    mark_ready_parser.add_argument("--project-dir", default=argparse.SUPPRESS, metavar="DIR")
    mark_ready_parser.add_argument("--project-id", default=argparse.SUPPRESS, metavar="PROJECT_ID")


def _register_migrate_subparser(subparsers: argparse.Action) -> None:
    migrate_parser = subparsers.add_parser(
        "migrate",
        help="apply runtime DB migrations (tracks, pool, dispatch tables)",
    )
    migrate_parser.add_argument(
        "--project-dir",
        default=".",
        metavar="DIR",
        help="project directory to resolve data root from (default: current directory)",
    )


def _register_learning_subparser(subparsers: argparse.Action) -> None:
    learning_parser = subparsers.add_parser(
        "learning",
        help="operator-gated proposal tier for the intelligence self-learning loop",
    )
    learning_parser.add_argument(
        "--project-dir",
        default=".",
        metavar="DIR",
        help="project directory (default: current directory)",
    )
    learning_subs = learning_parser.add_subparsers(
        dest="learning_subcommand", metavar="SUBCOMMAND"
    )

    lr_run = learning_subs.add_parser(
        "run",
        help="run the daily learning cycle and queue proposals for operator review",
    )
    lr_run.add_argument("--project-dir", default=".", metavar="DIR")
    lr_run.add_argument(
        "--from-history",
        dest="from_history",
        action="store_true",
        help="mine full receipt history (all-time window) instead of the default 24h window",
    )

    lr_status = learning_subs.add_parser("status", help="show pending proposal counts")
    lr_status.add_argument("--project-dir", default=".", metavar="DIR")

    lr_review = learning_subs.add_parser(
        "review", help="show pending proposals for operator review"
    )
    lr_review.add_argument("--project-dir", default=".", metavar="DIR")
    lr_review.add_argument(
        "--mode",
        default="all",
        choices=["all", "rules", "archival"],
        help="which proposals to show: rules, archival, or all (default: all)",
    )

    lr_ab = learning_subs.add_parser(
        "tagger-ab",
        help=(
            "read-only A/B: tag-overlap precision WITH tagger (LLM) vs WITHOUT "
            "(deterministic derive_tags). Reports rescue-rate + cost. "
            "Does NOT set VNX_TAGGER_ENABLED."
        ),
    )
    lr_ab.add_argument("--project-dir", default=".", metavar="DIR")
    lr_ab.add_argument(
        "--sample",
        type=int,
        default=20,
        metavar="N",
        help="number of patterns to sample (default: 20; keep small — each makes one LLM call)",
    )
    lr_ab.add_argument(
        "--seed",
        type=int,
        default=42,
        metavar="SEED",
        help="random seed for deterministic sampling (default: 42)",
    )

    lr_gs = learning_subs.add_parser(
        "grounding-shadow",
        help="compare V1 (substring-join) vs V2 (junction) confidence grounding — read-only",
    )
    lr_gs.add_argument("--project-dir", default=".", metavar="DIR")
    lr_gs.add_argument(
        "--limit",
        type=int,
        default=50,
        metavar="N",
        help="max recent dispatches to analyse (default: 50)",
    )

    lr_sr = learning_subs.add_parser(
        "skill-refine",
        help=(
            "generate operator-gated skill-refinement proposals from rework attribution. "
            "Read-only w.r.t. skill files; writes pending_skill_refinements.json for review."
        ),
    )
    lr_sr.add_argument("--project-dir", default=".", metavar="DIR")
    lr_sr.add_argument(
        "--threshold",
        type=float,
        default=0.3,
        metavar="RATE",
        help="rework-rate threshold above which a role is considered rework-prone (default: 0.3)",
    )

    lr_srw = learning_subs.add_parser(
        "skill-review",
        help="show pending skill-refinement proposals for operator review",
    )
    lr_srw.add_argument("--project-dir", default=".", metavar="DIR")
    lr_srw.add_argument(
        "--show-diff",
        dest="show_diff",
        action="store_true",
        help="display the full unified diff for each proposal",
    )


def _register_dream_subparser(subparsers: argparse.Action) -> None:
    dream_parser = subparsers.add_parser(
        "dream",
        help="auto-dream memory consolidation (ADR-019)",
    )
    dream_subs = dream_parser.add_subparsers(dest="dream_subcommand", metavar="SUBCOMMAND")

    dream_run_p = dream_subs.add_parser("run", help="run a consolidation cycle")
    dream_run_p.add_argument("--project-id", default=None, metavar="ID")
    dream_run_p.add_argument("--project-dir", dest="project_dir", default=".", metavar="DIR")
    dream_run_p.add_argument("--dry-run", action="store_true", help="emit events but skip DB writes")

    dream_status_p = dream_subs.add_parser("status", help="show latest cycle results")
    dream_status_p.add_argument("--project-id", default=None, metavar="ID")
    dream_status_p.add_argument("--project-dir", dest="project_dir", default=".", metavar="DIR")

    dream_review_p = dream_subs.add_parser("review", help="approve or reject a pending cycle")
    dream_review_p.add_argument("cycle_id", metavar="CYCLE_ID")
    dream_review_p.add_argument("--project-id", default=None, metavar="ID")
    dream_review_p.add_argument("--project-dir", dest="project_dir", default=".", metavar="DIR")
    dream_review_p.add_argument("--approve", action="store_true", help="approve without prompting")
    dream_review_p.add_argument("--reject", action="store_true", help="reject without prompting")
    dream_review_p.add_argument("--reason", default="operator rejected", metavar="REASON")

    dream_history_p = dream_subs.add_parser("history", help="show recent cycles")
    dream_history_p.add_argument("--project-id", default=None, metavar="ID")
    dream_history_p.add_argument("--project-dir", dest="project_dir", default=".", metavar="DIR")
    dream_history_p.add_argument("--limit", type=int, default=10, metavar="N")

    dream_install_p = dream_subs.add_parser(
        "install-scheduler", help="install nightly auto-dream scheduler (macOS/Linux)"
    )
    dream_install_p.add_argument("--project-id", default=None, metavar="ID")
    dream_install_p.add_argument("--project-dir", dest="project_dir", default=".", metavar="DIR")

    dream_subs.add_parser(
        "uninstall-scheduler", help="remove nightly auto-dream scheduler"
    )


def _register_attest_subparser(subparsers: argparse.Action) -> None:
    attest_parser = subparsers.add_parser(
        "attest",
        help="write and verify in-repo attestation records (governance D2)",
    )
    attest_subs = attest_parser.add_subparsers(dest="attest_subcommand", metavar="SUBCOMMAND")

    aw = attest_subs.add_parser("write", help="write attest record for the current branch")
    aw.add_argument("--dispatch-id", required=True, dest="dispatch_id", metavar="DISPATCH_ID",
                    help="Dispatch-ID of the governed build")
    aw.add_argument("--deliverable", required=True, metavar="DELIVERABLE",
                    help="deliverable being attested (e.g. D2)")
    aw.add_argument("--track", required=True, metavar="TRACK_ID",
                    help="track/objective ID")
    aw.add_argument("--gate-ref", dest="gate_ref", default="no-gate-ref", metavar="REF",
                    help="plan-gate pass reference")
    aw.add_argument("--signer", default="vnx@local", metavar="IDENTITY",
                    help="signer identity (must match an allowed_signers entry)")
    aw.add_argument("--key", default=None, metavar="KEY_PATH",
                    help="SSH private key for detached signature (optional; unsigned if omitted)")
    aw.add_argument("--base-ref", dest="base_ref", default="origin/main", metavar="REF",
                    help="base branch for merge-base (default: origin/main)")
    aw.add_argument("--no-commit", dest="no_commit", action="store_true",
                    help="write the record file only; skip git add + commit")
    aw.add_argument("--project-dir", default=".", metavar="DIR",
                    help="repository root (default: current directory)")

    av = attest_subs.add_parser("verify", help="verify a branch attest record")
    av.add_argument("--allowed-signers", dest="allowed_signers", default=None, metavar="PATH",
                    help="path to SSH allowed_signers file (auto-detected if omitted)")
    av.add_argument("--base-ref", dest="base_ref", default="origin/main", metavar="REF",
                    help="base branch for merge-base (default: origin/main)")
    av.add_argument("--project-dir", default=".", metavar="DIR",
                    help="repository root (default: current directory)")

    avp = attest_subs.add_parser(
        "verify-pr",
        help="verify attestation for a PR (D3 gate — used by GitHub Action)",
    )
    avp.add_argument("--base-ref", dest="base_ref", default="origin/main", metavar="REF",
                     help="base branch for merge-base (default: origin/main)")
    avp.add_argument("--head-ref", dest="head_ref", default="HEAD", metavar="REF",
                     help="PR head ref to verify (default: HEAD)")
    avp.add_argument("--allowed-signers", dest="allowed_signers", default=None, metavar="PATH",
                     help="override allowed_signers path (default: resolved from base branch)")
    avp.add_argument("--verbose", action="store_true",
                     help="emit diagnostic lines to stderr")
    avp.add_argument("--project-dir", default=".", metavar="DIR",
                     help="repository root (default: current directory)")

    # D4: signed, budgeted, audited gate override
    aov = attest_subs.add_parser(
        "override",
        help="record a signed, budgeted gate override (D4 — recorded deviation, never silent)",
    )
    aov.add_argument("--reason", required=True, metavar="REASON",
                     help="non-empty justification (permanent audit record)")
    aov.add_argument("--key", required=True, metavar="KEY_PATH",
                     help="SSH private key for signing (must match an allowed_signers entry)")
    aov.add_argument("--signer", default="vnx@local", metavar="IDENTITY",
                     help="signer identity (must match an allowed_signers entry at base branch)")
    aov.add_argument("--dispatch-id", dest="dispatch_id", default="override", metavar="ID",
                     help="dispatch ID or override slug for traceability")
    aov.add_argument("--base-ref", dest="base_ref", default="origin/main", metavar="REF",
                     help="base branch for merge-base (default: origin/main)")
    aov.add_argument("--head-ref", dest="head_ref", default="HEAD", metavar="REF",
                     help="PR head ref (default: HEAD)")
    aov.add_argument("--no-commit", dest="no_commit", action="store_true",
                     help="write record files only; skip git add + commit")
    aov.add_argument("--project-dir", default=".", metavar="DIR",
                     help="repository root (default: current directory)")

    # Signed delegation mandate (issue / revoke)
    am = attest_subs.add_parser(
        "mandate",
        help="issue or revoke a signed delegation mandate for governed dispatches",
    )
    am_subs = am.add_subparsers(dest="mandate_subcommand", metavar="SUBCOMMAND")

    ami = am_subs.add_parser("issue", help="sign and issue a delegation mandate")
    ami.add_argument("--key", required=True, metavar="KEY_PATH",
                     help="SSH private key for signing the mandate")
    ami.add_argument("--signer", default="vnx@local", metavar="IDENTITY",
                     help="signer identity (must match an allowed_signers entry)")
    ami.add_argument("--mandate-id", dest="mandate_id", default=None, metavar="ID",
                     help="stable mandate ID (default: generated)")
    ami.add_argument("--project-id", dest="project_id", default=None, metavar="ID",
                     help="project the mandate is bound to (default: VNX_PROJECT_ID)")
    ami.add_argument("--expires-at", dest="expires_at", required=True, metavar="ISO8601",
                     help="mandatory expiry timestamp, e.g. 2026-07-10T12:00:00Z")
    ami.add_argument("--session-id", dest="session_id", default=None, metavar="ID",
                     help="scope: exact session identifier")
    ami.add_argument("--task-class", dest="task_class", default=None, metavar="CLASS",
                     help="scope: comma-separated allowed task classes")
    ami.add_argument("--dispatch-id-glob", dest="dispatch_id_glob", default=None, metavar="GLOB",
                     help="scope: shell glob matching dispatch IDs")
    ami.add_argument("--project-dir", default=".", metavar="DIR",
                     help="repository root (default: current directory)")

    amr = am_subs.add_parser("revoke", help="revoke a previously issued mandate")
    amr.add_argument("--key", required=True, metavar="KEY_PATH",
                     help="SSH private key for signing the revocation")
    amr.add_argument("--signer", default="vnx@local", metavar="IDENTITY",
                     help="signer identity (must match an allowed_signers entry)")
    amr.add_argument("--mandate-id", dest="mandate_id", required=True, metavar="ID",
                     help="mandate ID to revoke")
    amr.add_argument("--project-dir", default=".", metavar="DIR",
                     help="repository root (default: current directory)")


def _dispatch_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.command == "init":
        from vnx_cli.commands.init_cmd import vnx_init
        sys.exit(vnx_init(args))

    elif args.command == "doctor":
        from vnx_cli.commands.doctor import vnx_doctor
        sys.exit(vnx_doctor(args))

    elif args.command == "fabric-audit":
        from vnx_cli.commands.fabric_audit import vnx_fabric_audit
        sys.exit(vnx_fabric_audit(args))

    elif args.command == "status":
        from vnx_cli.commands.status import vnx_status
        sys.exit(vnx_status(args))

    elif args.command == "subsystems":
        from vnx_cli.commands.subsystems import vnx_subsystems
        sys.exit(vnx_subsystems(args))

    elif args.command == "dispatch-agent":
        from vnx_cli.commands.dispatch_agent import vnx_dispatch_agent
        sys.exit(vnx_dispatch_agent(args))

    elif args.command == "pool":
        from vnx_cli.commands.pool import main as pool_main
        sys.exit(pool_main(argv=getattr(args, "pool_args", None) or None))

    elif args.command == "role":
        from vnx_cli.commands.role import vnx_role
        sys.exit(vnx_role(args))

    elif args.command == "version":
        from vnx_cli.commands.version import vnx_version
        sys.exit(vnx_version(args))

    elif args.command == "update":
        from vnx_cli.commands.update import vnx_update
        sys.exit(vnx_update(args))

    elif args.command == "track":
        from vnx_cli.commands.track import vnx_track
        sys.exit(vnx_track(args))

    elif args.command == "handoff":
        from vnx_cli.commands.handoff import vnx_handoff
        sys.exit(vnx_handoff(args))

    elif args.command == "learning":
        from vnx_cli.commands.learning import vnx_learning
        sys.exit(vnx_learning(args))

    elif args.command == "dream":
        from vnx_cli.commands.dream import vnx_dream
        sys.exit(vnx_dream(args))

    elif args.command == "migrate":
        from vnx_cli.commands.migrate import vnx_migrate
        sys.exit(vnx_migrate(args))

    elif args.command == "attest":
        from vnx_cli.commands.attest import vnx_attest
        sys.exit(vnx_attest(args))

    elif args.command == "horizon":
        from vnx_cli.commands.horizon import vnx_horizon
        sys.exit(vnx_horizon(args))

    elif args.command == "objective":
        from vnx_cli.commands.horizon import vnx_objective
        sys.exit(vnx_objective(args))

    elif args.command == "deliverable":
        from vnx_cli.commands.horizon import vnx_deliverable
        sys.exit(vnx_deliverable(args))

    else:
        parser.print_help()
        sys.exit(0)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="vnx",
        description="VNX — governance-first multi-agent orchestration for AI CLI workers",
    )
    parser.add_argument(
        "--version", action="version", version=f"vnx {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    _register_doctor_subparser(subparsers)
    _register_fabric_audit_subparser(subparsers)
    _register_version_subparser(subparsers)
    _register_status_subparser(subparsers)
    _register_subsystems_subparser(subparsers)
    _register_init_subparser(subparsers)
    _register_pool_subparser(subparsers)
    _register_role_subparser(subparsers)
    _register_update_subparser(subparsers)
    _register_dispatch_agent_subparser(subparsers)
    _register_track_subparser(subparsers)
    _register_handoff_subparser(subparsers)
    _register_learning_subparser(subparsers)
    _register_dream_subparser(subparsers)
    _register_migrate_subparser(subparsers)
    _register_attest_subparser(subparsers)
    _register_horizon_subparser(subparsers)
    _register_objective_subparser(subparsers)
    _register_deliverable_subparser(subparsers)

    args = parser.parse_args()
    _dispatch_command(args, parser)


if __name__ == "__main__":
    main()
