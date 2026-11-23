"""vnx rework-attribution — nightly rework attribution job (ADR-071): run|install|uninstall|status.

Routed from bin/vnx like `vnx pool`: ``python -m vnx_cli.commands.rework_attribution <verb> [...]``.
"""
from __future__ import annotations

import sys

from vnx_cli import _engine

_engine.ensure_engine_on_path()


def main(argv: "list[str] | None" = None) -> int:
    import rework_attribution_job  # type: ignore[import]
    return rework_attribution_job.main(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
