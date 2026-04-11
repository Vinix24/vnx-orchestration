"""VNX CLI entry point — governance-first multi-agent orchestration."""

import argparse
import sys

from vnx_cli import __version__
from vnx_cli.commands.init_cmd import vnx_init
from vnx_cli.commands.doctor import vnx_doctor
from vnx_cli.commands.status import vnx_status
from vnx_cli.commands.dispatch_agent import vnx_dispatch_agent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vnx",
        description="VNX — governance-first multi-agent orchestration for AI CLI workers",
    )
    parser.add_argument(
        "--version", action="version", version=f"vnx {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.required = False

    # init
    p_init = subparsers.add_parser(
        "init", help="Scaffold a new VNX project in the current directory"
    )
    p_init.add_argument(
        "--project-dir",
        default=".",
        metavar="DIR",
        help="Target directory for scaffolding (default: current dir)",
    )
    p_init.set_defaults(func=vnx_init)

    # doctor
    p_doctor = subparsers.add_parser(
        "doctor", help="Check prerequisites and project health"
    )
    p_doctor.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Emit results as JSON instead of human-readable text"
    )
    p_doctor.add_argument(
        "--project-dir",
        default=".",
        metavar="DIR",
        help="Project directory to validate (default: current dir)",
    )
    p_doctor.set_defaults(func=vnx_doctor)

    # status
    p_status = subparsers.add_parser(
        "status", help="Show current dispatch and agent status"
    )
    p_status.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Emit results as JSON instead of human-readable text"
    )
    p_status.add_argument(
        "--project-dir",
        default=".",
        metavar="DIR",
        help="Project directory to inspect (default: current dir)",
    )
    p_status.set_defaults(func=vnx_status)

    # dispatch-agent
    p_dispatch = subparsers.add_parser(
        "dispatch-agent", help="Send a dispatch to a named agent terminal"
    )
    p_dispatch.add_argument(
        "--agent", required=True, metavar="NAME",
        help="Agent name (must match a directory under agents/)",
    )
    p_dispatch.add_argument(
        "--instruction", required=True, metavar="TEXT",
        help="Dispatch instruction to send to the agent",
    )
    p_dispatch.add_argument(
        "--model", default="sonnet", metavar="MODEL",
        help="Model alias to use (default: sonnet)",
    )
    p_dispatch.add_argument(
        "--project-dir",
        default=".",
        metavar="DIR",
        help="Project directory (default: current dir)",
    )
    p_dispatch.set_defaults(func=vnx_dispatch_agent)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    result = args.func(args)
    sys.exit(result if isinstance(result, int) else 0)


if __name__ == "__main__":
    main()
