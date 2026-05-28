#!/usr/bin/env python3
"""VNX CLI — governance-first multi-agent orchestration."""

import argparse
import sys

from vnx_cli import __version__


def _register_track_subparser(subparsers: argparse.Action) -> None:
    track_parser = subparsers.add_parser(
        "track",
        help="manage feature-tracks (new/activate/park/unpark/dispatch/list/show)",
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
    tn_parser.add_argument("--title", required=True, metavar="TITLE")
    tn_parser.add_argument("--goal", required=True, metavar="GOAL")
    tn_parser.add_argument("--priority", choices=["high", "medium", "low"], metavar="PRIORITY")
    tn_parser.add_argument("--project-dir", default=".", metavar="DIR")

    ta_parser = track_subs.add_parser("activate", help="activate a queued track")
    ta_parser.add_argument("track_id", metavar="TRACK_ID")
    ta_parser.add_argument("--reason", metavar="REASON")
    ta_parser.add_argument("--project-dir", default=".", metavar="DIR")

    tp_parser = track_subs.add_parser("park", help="park an active track")
    tp_parser.add_argument("track_id", metavar="TRACK_ID")
    tp_parser.add_argument("--reason", required=True, metavar="REASON")
    tp_parser.add_argument("--project-dir", default=".", metavar="DIR")

    tu_parser = track_subs.add_parser("unpark", help="unpark a parked track to queued")
    tu_parser.add_argument("track_id", metavar="TRACK_ID")
    tu_parser.add_argument("--reason", metavar="REASON")
    tu_parser.add_argument("--project-dir", default=".", metavar="DIR")

    td_parser = track_subs.add_parser("dispatch", help="create a dispatch for a track")
    td_parser.add_argument("track_id", metavar="TRACK_ID")
    td_parser.add_argument("--pr", required=True, metavar="PR-ID")
    td_parser.add_argument("--terminal", required=True, choices=["T1", "T2", "T3"], metavar="TERMINAL")
    td_parser.add_argument("--instruction-file", metavar="PATH")
    td_parser.add_argument("--project-dir", default=".", metavar="DIR")

    tl_parser = track_subs.add_parser("list", help="list tracks")
    tl_parser.add_argument("--phase", choices=["queued", "active", "parked", "done"], metavar="PHASE")
    tl_parser.add_argument("--project-id", default="vnx-dev", metavar="PROJECT_ID")
    tl_parser.add_argument("--project-dir", default=".", metavar="DIR")

    ts_parser = track_subs.add_parser("show", help="show track detail")
    ts_parser.add_argument("track_id", metavar="TRACK_ID")
    ts_parser.add_argument("--project-dir", default=".", metavar="DIR")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="vnx",
        description="VNX — governance-first multi-agent orchestration for AI CLI workers",
    )
    parser.add_argument(
        "--version", action="version", version=f"vnx {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # init subcommand
    init_parser = subparsers.add_parser(
        "init",
        help="scaffold a new VNX project in the current directory",
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

    # doctor subcommand
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

    # status subcommand
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
        "--project-dir",
        default=".",
        metavar="DIR",
        help="project directory to inspect (default: current directory)",
    )

    # dispatch-agent subcommand
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
        required=True,
        metavar="TEXT",
        help="instruction text to send to the agent",
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

    # pool subcommand — delegates to vnx_cli.commands.pool for sub-subcommand parsing
    pool_parser = subparsers.add_parser(
        "pool",
        help="manage elastic worker pools (status/scale/config/reap)",
    )
    pool_parser.add_argument(
        "pool_args",
        nargs=argparse.REMAINDER,
        help="pool subcommand and arguments",
    )

    # version subcommand
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

    # track subcommand
    _register_track_subparser(subparsers)

    # update subcommand
    update_parser = subparsers.add_parser(
        "update",
        help="flip active VNX version in central install (pre-central-install scaffolding)",
    )
    update_parser.add_argument(
        "--to",
        dest="to_version",
        metavar="VERSION",
        help="target version (e.g. '1.0.0-rc3' or 'edge')",
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

    args = parser.parse_args()

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

    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
