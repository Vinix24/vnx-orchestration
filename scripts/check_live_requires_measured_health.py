#!/usr/bin/env python3
"""CI check: no cockpit subsystem may claim LIVE while its own health reads
"unknown" (D6b).

`LIVE` means, per ``docs/core/SUBSYSTEMS.md``'s legend, "running and expected
to stay on" -- a claim of fact. "unknown -- no probe yet" is an admission
that nobody has ever measured it. The two together said the same thing twice
with opposite truth values: six subsystems carried exactly that combination
(cheap-recon-scout, horizon-planning, headless-dispatch-routing,
central-db-routing, smart-router-staging, claude-tmux-serialization) until
this dispatch moved them to ``ACTIVATE-and-measure``. This script keeps the
combination illegal going forward -- it is the "controle die rood wordt"
this dispatch was asked to leave behind, not just the one-time fix.

Health is resolved the same way ``vnx subsystems`` does (live beacon, else
the seed value committed in ``docs/core/SUBSYSTEMS.md``) so this check is
deterministic in a clean CI checkout with no beacon files: it degrades to
pure seed-health, which is exactly the committed doc text.

Wired via ``make live-health-check`` and
``.github/workflows/subsystems-drift.yml``.

Exit codes: 0 = no LIVE+unknown rows, 1 = violation found, 2 = internal error.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
for sub in (_REPO_ROOT / "scripts", _REPO_ROOT / "scripts" / "lib"):
    s = str(sub)
    if s not in sys.path:
        sys.path.insert(0, s)
sys.path.insert(0, str(_REPO_ROOT))


def violations_in_rows(rows: list[dict]) -> list[str]:
    """Pure filter: subsystem names that are status=LIVE with unmeasured health.

    Split out from ``find_live_unknown_violations`` so the invariant is
    unit-testable against synthetic rows, without needing a live beacon dir
    or the committed SUBSYSTEMS.md seed table.
    """
    return [
        row["subsystem"]
        for row in rows
        if row["status"] == "LIVE" and row.get("health", "").startswith("unknown")
    ]


def find_live_unknown_violations(repo_root: Path) -> list[str]:
    """Return subsystem names that are status=LIVE with unmeasured health."""
    from vnx_cli import _engine
    from vnx_cli.commands import subsystems as sub_mod

    engine_root = _engine.engine_root()
    data_dir = _engine.resolve_data_root(repo_root)

    rows = sub_mod.build_rows()
    seed_health = sub_mod._parse_seed_health(engine_root)
    sub_mod._attach_health(rows, data_dir, seed_health, use_probe=False)

    return violations_in_rows(rows)


def main() -> int:
    try:
        violations = find_live_unknown_violations(_REPO_ROOT)
    except ImportError as exc:
        print(f"internal error: cannot import subsystems generator: {exc}", file=sys.stderr)
        return 2

    if not violations:
        print("OK — no LIVE subsystem claims 'unknown' health.")
        return 0

    print(
        "VIOLATION — LIVE status paired with unmeasured health is not a legal "
        "combination:",
        file=sys.stderr,
    )
    for name in violations:
        print(f"  {name}: status=LIVE, health=unknown", file=sys.stderr)
    print(
        "\nEither: (a) demote to ACTIVATE in scripts/lib/config_registry.py "
        "until a probe exists, or (b) register an effectiveness probe "
        "(scripts/lib/effectiveness_probe.py) that measures it.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
