#!/usr/bin/env python3
"""Run a command as the leader of its own session and process group.

Usage: vnx_exec_new_group.py <cmd> [args...]

The nightly analyzer runner (``scripts/conversation_analyzer_nightly.sh``)
starts every phase through this launcher with ``&``. After ``os.setsid()`` the
pid the shell recorded in ``$!`` is also the process group id, so
``vnx_run_bounded_group`` in ``scripts/lib/vnx_run_bounded.sh`` can signal the
phase and everything it started, without touching the runner's own group.
macOS has no ``setsid(1)`` in the base system; this launcher is the portable
replacement, and it ``exec``s, so no extra process sits between the shell and
the command.

Exit status when the command never starts: 125 when the process could not
become the leader of its own group (the runner must then not signal that
group), 127 when the command cannot be executed, 2 on a usage error.
"""

import os
import sys


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: vnx_exec_new_group.py <cmd> [args...]\n")
        return 2
    try:
        os.setsid()
    except OSError:  # vnx-silent-except: setsid() refuses a process that already leads its group; the check below decides
        pass
    if os.getpgrp() != os.getpid():
        sys.stderr.write(
            "vnx_exec_new_group: not the leader of its own process group; "
            f"refusing to run {argv[1]!r}\n"
        )
        return 125
    try:
        os.execvp(argv[1], argv[1:])
    except OSError as exc:
        sys.stderr.write(f"vnx_exec_new_group: cannot execute {argv[1]!r}: {exc}\n")
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv))
