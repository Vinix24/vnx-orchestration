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
        default="sonnet",
        metavar="MODEL",
        help="model to use (default: sonnet)",
    )
    dispatch_parser.add_argument(
        "--project-dir",
        default=".",
        metavar="DIR",
        help="project directory (default: current directory)",
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


def _dispatch_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.command == "init":
        from vnx_cli.commands.init_cmd import vnx_init
        sys.exit(vnx_init(args))

    elif args.command == "doctor":
        from vnx_cli.commands.doctor import vnx_doctor
        sys.exit(vnx_doctor(args))

    elif args.command == "status":
        from vnx_cli.commands.status import vnx_status
        sys.exit(vnx_status(args))

    elif args.command == "dispatch-agent":
        from vnx_cli.commands.dispatch_agent import vnx_dispatch_agent
        sys.exit(vnx_dispatch_agent(args))

    elif args.command == "pool":
        from vnx_cli.commands.pool import main as pool_main
        sys.exit(pool_main(argv=getattr(args, "pool_args", None) or None))

    elif args.command == "version":
        from vnx_cli.commands.version import vnx_version
        sys.exit(vnx_version(args))

    elif args.command == "update":
        from vnx_cli.commands.update import vnx_update
        sys.exit(vnx_update(args))

    elif args.command == "track":
        from vnx_cli.commands.track import vnx_track
        sys.exit(vnx_track(args))

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
    _register_version_subparser(subparsers)
    _register_status_subparser(subparsers)
    _register_init_subparser(subparsers)
    _register_pool_subparser(subparsers)
    _register_update_subparser(subparsers)
    _register_dispatch_agent_subparser(subparsers)
    _register_track_subparser(subparsers)
    _register_learning_subparser(subparsers)
    _register_dream_subparser(subparsers)
    _register_migrate_subparser(subparsers)
    _register_attest_subparser(subparsers)

    args = parser.parse_args()
    _dispatch_command(args, parser)


if __name__ == "__main__":
    main()
