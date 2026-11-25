"""Shared exit contract for the launchd jobs ledger-health, door-audit and fleet-role-drift.

A job that finishes normally exits 0 (clean), 1 (finding) or 2 (unmeasurable /
config error). An uncaught exception is **70** (EX_SOFTWARE), never 1, so a crash
cannot read as "ran fine, found something". 78 stays the launcher's config error.

``run_job`` also prints one closing line as the LAST stdout line of a text run::

    <name>: <status> (exit <code>)

``status`` comes from the job's own ``statuses`` map, else ``crash`` for 70,
``usage_error`` for an argparse ``SystemExit(2)``, else ``exit_<code>``.
A job whose stdout is JSON passes ``closing_line=False``; a crash still gives 70.
"""

from __future__ import annotations

import sys
import traceback
from typing import Callable, Mapping, Optional, Sequence

EXIT_CRASH = 70


def argv_has_flag(argv: Sequence[str], *flags: str) -> bool:
    """True when ``argv`` carries one of ``flags`` (also as ``--flag=value``)."""
    return any(a == f or a.startswith(f + "=") for a in argv for f in flags)


def run_job(
    name: str,
    main: Callable[[], Optional[int]],
    statuses: Mapping[int, str],
    closing_line: bool = True,
) -> int:
    usage_error = False
    try:
        result = main()
        code = 0 if result is None else int(result)
    except SystemExit as exc:
        if exc.code is None:
            code = 0
        elif isinstance(exc.code, int):
            code = exc.code
            usage_error = code == 2  # the jobs return their codes; only argparse raises SystemExit(2)
        else:
            # SystemExit("message"): the message goes to stderr, exit code 1 like Python does.
            print(exc.code, file=sys.stderr)
            code = 1
    except Exception:  # vnx-silent-except: the traceback goes to stderr and the exit code is 70
        traceback.print_exc()
        code = EXIT_CRASH
    if closing_line:
        if usage_error:
            status = "usage_error"
        elif code in statuses:
            status = statuses[code]
        elif code == EXIT_CRASH:
            status = "crash"
        else:
            status = f"exit_{code}"
        sys.stdout.flush()
        print(f"{name}: {status} (exit {code})")
        sys.stdout.flush()
    return code

