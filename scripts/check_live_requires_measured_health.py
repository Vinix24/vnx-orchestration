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


# Health cells read "<category> — <detail>" (e.g. "works — CI green").
# ``subsystems._attach_health`` fills this column from ONE OF TWO vocabularies
# per row (a beacon wins if one exists for the subsystem, else the row falls
# back to seed prose — see its docstring), and this allowlist must recognize
# both or it silently only covers whichever one the original author had in
# view (OI-1596: a version of this list that covered only the seed
# vocabulary reported LIVE+beacon-`ok` as a VIOLATION with the message
# "health is unmeasured" — the exact opposite of what had been measured).
#
#   seed prose (docs/core/SUBSYSTEMS.md health column, parsed by
#   ``_parse_seed_health``; verified against the committed file on OI-1596):
#       works, degraded, produces-crap, unknown
#
#   beacon (``health_beacon.all_beacons()``'s derived ``health`` field, which
#   is what ``beacon.get('health', ...)`` in ``_attach_health`` actually
#   reads; verified against health_beacon.py's docstring and its
#   ``beacon_summary()`` counts dict, both of which enumerate the same six):
#       ok, stale, fail, corrupt, unknown, absent
#
# A component CAN self-report a raw ``status`` of "degraded" (e.g.
# ``learning_loop.py``), but ``all_beacons()`` only ever honors a
# self-reported ok/fail/stale/corrupt — anything else, including "degraded",
# collapses to "fail" before this check ever sees it (health_beacon.py
# ``_status_to_health``). So the beacon vocabulary this check actually
# receives never contains the word "degraded"; that spelling only ever
# arrives via a seed row.
#
# `works`, `degraded`, and `produces-crap` (seed) are real seed outcomes — a
# human recorded a measurement. `ok` and `fail` (beacon) are likewise
# genuinely measured: `fail` is measured-and-bad, a SEPARATE design question
# (may LIVE+fail be legal?) this check does not take a position on, same as
# it already declines to judge degraded/produces-crap — this check is about
# *unmeasured*, not about *bad*. `stale` (beacon) means the beacon aged out
# but still holds a prior real reading. None of these six says whether the
# LIVE+<category> combination SHOULD be legal — only that it is genuinely
# measured, so all six are exempt from the "unmeasured" violation this check
# exists to catch.
#
# Everything else falls through to "unmeasured": `unknown` (both
# vocabularies' explicit "no probe/no recent signal yet"), `absent` (beacon:
# an expected component that never once wrote), `corrupt` (beacon: the JSON
# could not be read at all — not a trustworthy reading, so it is treated the
# same as never having measured, not bundled in with `fail`), an empty cell,
# a missing `health` key, and any category this check has never seen before.
# That last case is deliberate fail-closed behavior — a new category is
# unmeasured until this allowlist is updated to say otherwise, not until
# this check happens to not recognize it as a violation.
#
# Not derived from a shared source: neither vocabulary has one. The seed
# words are free text in a markdown table, not a validated enum anywhere in
# code. The beacon side comes closest — ``health_beacon.beacon_summary()``
# has an inline ``counts`` dict naming the same six categories — but it is
# not exported as a reusable constant, and only covers the beacon half
# anyway. Exporting one would mean refactoring health_beacon.py's internals,
# which is out of scope for this one allowlist; if a shared constant is
# wanted, do it as its own change.
_MEASURED_HEALTH_CATEGORIES = frozenset({
    "works",
    "degraded",
    "produces-crap",
    "stale",
    "ok",
    "fail",
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
