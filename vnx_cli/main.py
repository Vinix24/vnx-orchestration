"""VNX CLI entry point — governance-first multi-agent orchestration."""

import argparse
import sys

from vnx_cli import __version__
from vnx_cli.commands.init_cmd import vnx_init
from vnx_cli.commands.doctor import vnx_doctor


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

    # status (stub — wired up in later PRs)
    p_status = subparsers.add_parser(
        "status", help="Show current dispatch and agent status"
    )
    p_status.set_defaults(func=_stub_status)

    # dispatch-agent (stub — wired up in later PRs)
    p_dispatch = subparsers.add_parser(
        "dispatch-agent", help="Send a dispatch to an agent terminal"
    )
    p_dispatch.set_defaults(func=_stub_dispatch)

    return parser


def _stub_status(args: argparse.Namespace) -> int:
    print("vnx status: not yet implemented — coming in a future PR.")
    return 0


def _stub_dispatch(args: argparse.Namespace) -> int:
    print("vnx dispatch-agent: not yet implemented — coming in a future PR.")
    return 0


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
