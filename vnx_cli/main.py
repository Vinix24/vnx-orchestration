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
    subparsers.add_parser(
        "status",
        help="show current dispatch and terminal status",
    )

    # dispatch-agent subcommand
    dispatch_parser = subparsers.add_parser(
        "dispatch-agent",
        help="dispatch a task to a specific agent terminal",
    )
    dispatch_parser.add_argument("terminal", help="target terminal (e.g. T1, T2, T3)")
    dispatch_parser.add_argument("task_file", help="path to dispatch instruction file")

    args = parser.parse_args()

    if args.command == "init":
        from vnx_cli.commands.init_cmd import vnx_init
        sys.exit(vnx_init(args))

    elif args.command == "doctor":
        from vnx_cli.commands.doctor import vnx_doctor
        sys.exit(vnx_doctor(args))

    elif args.command == "status":
        print("vnx status: not yet implemented")
        sys.exit(0)

    elif args.command == "dispatch-agent":
        print("vnx dispatch-agent: not yet implemented")
        sys.exit(0)

    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
