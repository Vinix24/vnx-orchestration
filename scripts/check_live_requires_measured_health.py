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


# Health cells read "<category> — <detail>" (e.g. "works — CI green"). These
# are the categories this check has verified mean "a probe or seed actually
# produced a reading" (OI-1593): `works`, `degraded`, and `produces-crap` are
# real probe/seed outcomes; `stale` means the beacon aged out but still holds
# a prior real reading (health_beacon.py). None of the four says whether
# LIVE+degraded/produces-crap/stale SHOULD be legal — that is a separate
# design question this check does not take a position on. It only asserts
# they are genuinely measured, so they are exempt from the "unmeasured"
# violation this check exists to catch.
#
# Everything else falls through to "unmeasured": `unknown` (explicit "no
# probe yet"), an empty cell, a missing `health` key, and any category this
# check has never seen before. That last case is deliberate fail-closed
# behavior — a new beacon category (e.g. a future `absent`) is unmeasured
# until this allowlist is updated to say otherwise, not until this check
# happens to not recognize it as a violation.
_MEASURED_HEALTH_CATEGORIES = frozenset({
    "works",
    "degraded",
    "produces-crap",
    "stale",
})


def _health_category(health: str) -> str:
    """First token of a health cell, e.g. 'works — CI green' -> 'works'.
    An empty or missing cell yields '', which is never in
    ``_MEASURED_HEALTH_CATEGORIES`` and therefore always a violation."""
    return (health or "").split("—", 1)[0].strip()


def violations_in_rows(rows: list[dict]) -> list[str]:
    """Pure filter: subsystem names that are status=LIVE with unmeasured health.

    Split out from ``find_live_unknown_violations`` so the invariant is
    unit-testable against synthetic rows, without needing a live beacon dir
    or the committed SUBSYSTEMS.md seed table.
    """
    return [
        row["subsystem"]
        for row in rows
        if row["status"] == "LIVE"
        and _health_category(row.get("health", "")) not in _MEASURED_HEALTH_CATEGORIES
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
