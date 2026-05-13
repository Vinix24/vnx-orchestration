"""vnx CLI entry point (Phase 0a placeholder)."""
from __future__ import annotations
import sys


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    print("vnx-orchestration package — Phase 0a skeleton.")
    print("Real CLI surface lands in Phase 1.")
    if args and args[0] in ("--version", "-v"):
        from vnx_core import __version__
        print(f"version: {__version__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
