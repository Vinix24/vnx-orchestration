#!/usr/bin/env python3
"""VNX CLI — governance-first multi-agent orchestration."""

import argparse
import sys

from vnx_cli import __version__


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

    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
