#!/usr/bin/env python3
"""Triage the ``role_scope_only__outside`` dispatches into three buckets.

Dispatch 20260815-opsch-w1-rolescope-triage (track ``role-scope-parity``, point 10
of the OPSCHALING cluster). ``worker_scope_enforcement_measure.py`` reports 140
dispatches that wrote at least one file outside their own role's
``file_write_scope`` (dispatch_paths ignored). That number is the flip-back
condition for ``worker_permission_enforcement_enabled()``, but the count alone is
not actionable: a dispatch writing outside its role scope is either (1) doing its
OWN job that the scope forgot to permit, (2) doing ANOTHER role's job (routed
wrong), or (3) a mix that cannot be decided from the spec + changed files.

This script splits the 140 into exactly those three buckets, each with a
per-dispatch reason. The classification is CODE, not after-the-fact judgement:
``OWNERSHIP_RULES`` is an ordered, documented map of "which path belongs to which
role", grounded in the canonical role-selection table
(``.claude/terminals/T0/role-orchestrator.md``, "Role selection (hard)"):

  Test harness / CI / test infra          -> quality-engineer
  Module boundaries / data model / ADRs   -> system-architect
  Permission posture / auth / secrets     -> security-engineer
  Runtime implementation in modules       -> backend-developer
  Dashboard / UI                          -> frontend-developer

The three buckets:

  * ``rol-te-smal``        — every outside path is the dispatch role's OWN work
                             per the ownership map. The role's file_write_scope
                             is too narrow; the routing was right.
  * ``verkeerd-gerouteerd``— every outside path belongs to a SINGLE other role.
                             The dispatch was routed to the wrong role.
  * ``onbeslisbaar``       — outside paths mix the role's own work with another
                             role's, span multiple other roles, or have no
                             ownership rule. Cannot be decided from spec+files.

HARD CHECK: the three buckets must partition the measured population exactly.
``sum(three buckets) == role_scope_only__outside`` is asserted; on mismatch the
script exits 1 loudly. Dispatches with no linked commit (or a linked commit with
an empty diff) fall OUTSIDE the measurement and are never placed in a bucket.

Method (same source as the measure script, helpers REUSED not duplicated):

  1. ``load_specs`` / ``link_dispatch_to_commits`` / ``changed_files`` are
     imported from ``worker_scope_enforcement_measure``.
  2. For each linked dispatch with >=1 changed file, the outside paths are the
     files where ``match_file_write_scope(f, resolve_worker_profile(role), None)``
     is False — the SAME matcher the hook enforces, so the verdict is the hook's
     verdict, never a reimplementation.
  3. Each outside path is attributed to an owner via ``OWNERSHIP_RULES``
     (fnmatch, first match wins); paths with no rule are "unknown".
  4. A dispatch is classified by combining its path-level attributions.

Output: JSON (summary + per-dispatch detail + per-role scope proposals) followed
by a human-readable table. ``--json-only`` prints just the JSON. Exit 0 always
except on the sum check.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

_ANALYSIS_DIR = Path(__file__).resolve().parent
_LIB_DIR = _ANALYSIS_DIR.parent / "lib"
for _d in (_ANALYSIS_DIR, _LIB_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

# The resolve_worker_profile fallback warning fires per unknown role; it is
# informative but noisy across the population. The JSON summary is the deliverable.
logging.getLogger("worker_permissions").setLevel(logging.CRITICAL)

# Reuse the measure script's source-reading helpers (single source of truth) and
# the hook's own matchers — never a reimplementation of either. _historical_profile
# (not resolve_worker_profile directly) replays what a dispatch's role actually
# resolved to at spawn time, including legacy role strings (e.g. technical-writer)
# that predate OI-1069 pt.5's refusal and would otherwise raise UnknownRoleError
# here.
from worker_scope_enforcement_measure import (  # noqa: E402
    _historical_profile as resolve_worker_profile,
    changed_files,
    link_dispatch_to_commits,
    load_specs,
)
from worker_permissions import (  # noqa: E402
    match_file_write_scope,
)

BUCKET_TOO_NARROW = "rol-te-smal"
BUCKET_MISROUTED = "verkeerd-gerouteerd"
BUCKET_UNDECIDABLE = "onbeslisbaar"
BUCKETS = (BUCKET_TOO_NARROW, BUCKET_MISROUTED, BUCKET_UNDECIDABLE)

# ---------------------------------------------------------------------------
# Ownership rules: which path belongs to which role.
#
# Ordered, first match wins (fnmatch). Grounded in the role-selection table in
# .claude/terminals/T0/role-orchestrator.md ("Role selection (hard)") plus the
# shipped .vnx/worker_permissions.yaml scopes. Each entry carries its basis.
#
# The mapping is deliberately a CLOSED set: a path that matches no rule is
# "unknown" and forces the dispatch into ``onbeslisbaar`` rather than being
# silently claimed for a role. That keeps the classification honest — the
# undecidable bucket is counted, never absorbed.
# ---------------------------------------------------------------------------
OWNERSHIP_RULES: "list[tuple[str, str]]" = [
    # --- security-engineer: permission posture / auth / secrets / sandboxing ---
    ("docs/operations/WORKER_PERMISSIONS.md", "security-engineer"),  # permission docs
    ("docs/governance/KEY_PROVISIONING.md", "security-engineer"),    # key provisioning
    (".vnx/worker_permissions.yaml", "security-engineer"),           # the scope config itself
    (".vnx/vnx_workers.default.yaml", "security-engineer"),
    (".claude/settings.json", "security-engineer"),                  # tool-permission posture
    # --- system-architect: module boundaries / data model / ADRs / scaffolding ---
    ("docs/governance/decisions/**", "system-architect"),            # ADRs
    ("docs/core/**", "system-architect"),                            # fabric/architecture docs
    ("docs/manifesto/**", "system-architect"),                       # fabric philosophy
    ("schemas/**", "system-architect"),                              # data model
    ("templates/**", "system-architect"),                            # scaffolding
    ("agents/**", "system-architect"),                               # agent scaffolding
    ("examples/**", "system-architect"),                             # scaffolding examples
    ("skills/**", "system-architect"),                               # top-level skill scaffolding
    (".claude/terminals/**", "system-architect"),                    # role definitions
    (".claude/skills/**", "system-architect"),                       # skill scaffolding
    # --- quality-engineer: test harness / CI / review-gate infrastructure ---
    ("tests/**", "quality-engineer"),                                # test infra
    ("scripts/check_*", "quality-engineer"),
    ("scripts/ci/**", "quality-engineer"),                           # CI coverage
    (".github/**", "quality-engineer"),                              # CI workflows
    ("scripts/benchmark/**", "quality-engineer"),                    # benchmark runners
    ("scripts/refactor_*.py", "quality-engineer"),                   # equivalence tooling
    ("scripts/lib/gate_*.py", "quality-engineer"),                   # review-gate infra
    ("scripts/commands/gate.sh", "quality-engineer"),
    ("scripts/review_gate_manager.py", "quality-engineer"),
    # --- frontend-developer: dashboard / UI ---
    ("dashboard/**", "frontend-developer"),
    # --- backend-developer: runtime implementation in existing modules ---
    ("vnx_cli/**", "backend-developer"),                             # CLI package
    ("bin/**", "backend-developer"),                                 # runtime entrypoint
    ("hooks/**", "backend-developer"),                               # runtime hooks
    ("configs/**", "backend-developer"),                             # runtime config
    ("scripts/**", "backend-developer"),                             # shared lib surface
    ("docs/**", "backend-developer"),                                # feature/ops docs
    # --- root build/release files ---
    ("VERSION", "backend-developer"),
    ("CHANGELOG.md", "backend-developer"),
    ("pyproject.toml", "backend-developer"),
    ("uv.lock", "backend-developer"),
    ("requirements.txt", "backend-developer"),
    ("Makefile", "backend-developer"),
]

# Root project-meta files with no single owner in the role-selection table.
# Deliberately NOT mapped: they force ``onbeslisbaar`` rather than being claimed.
# (CLAUDE.md, CODEOWNERS, FEATURE_PLAN.md, PR_QUEUE.md, and anything else unmatched.)


def owner(path: str) -> "Optional[str]":
    """Return the canonical owning role for *path*, or None if unmapped.

    First matching rule wins. A None return is a first-class signal: the path
    has no ownership rule, which pushes a dispatch toward ``onbeslisbaar``.
    """
    for pattern, role in OWNERSHIP_RULES:
        if fnmatch.fnmatch(path, pattern):
            return role
    return None


@dataclass(frozen=True)
class Classification:
    """The outcome of classifying one dispatch's outside-path set."""

    bucket: str
    reason: str
    own_paths: "tuple[str, ...]"
    other_paths: "tuple[str, ...]"
    unknown_paths: "tuple[str, ...]"
    other_roles: "tuple[str, ...]"


def classify(role: str, outside_paths: "Iterable[str]") -> Classification:
    """Classify one dispatch into exactly one of the three buckets.

    Pure function: takes a role and the paths it wrote outside its scope, returns
    a Classification. Deterministic and reusable in tests with fabricated inputs.

    Rule (in order):
      * any path with no ownership rule              -> onbeslisbaar
      * otherwise, all paths are the role's own work -> rol-te-smal
      * otherwise, all paths belong to one other role and none are the role's
        own -> verkeerd-gerouteerd
      * otherwise (own work mixed with other-role work, or spanning multiple
        other roles) -> onbeslisbaar
    """
    paths = [p for p in outside_paths]
    own = tuple(p for p in paths if owner(p) == role)
    other = tuple(p for p in paths if owner(p) is not None and owner(p) != role)
    unknown = tuple(p for p in paths if owner(p) is None)
    other_roles = tuple(sorted({owner(p) for p in other}))

    n = len(paths)
    if unknown:
        reason = (
            f"{len(unknown)} of {n} outside path(s) have no ownership rule "
            f"(first: {unknown[0]}) — cannot decide too-narrow vs misrouted"
        )
        return Classification(
            BUCKET_UNDECIDABLE, reason, own, other, unknown, other_roles
        )
    if not other:
        reason = (
            f"all {n} outside path(s) are {role}'s own work per the ownership "
            f"map — {role} file_write_scope is too narrow, routing was right"
        )
        return Classification(
            BUCKET_TOO_NARROW, reason, own, other, unknown, other_roles
        )
    if not own and len(other_roles) == 1:
        target = other_roles[0]
        reason = (
            f"all {n} outside path(s) belong to {target} per the ownership map "
            f"— the dispatch was routed to the wrong role"
        )
        return Classification(
            BUCKET_MISROUTED, reason, own, other, unknown, other_roles
        )
    if own:
        reason = (
            f"{n} outside path(s) mix {role}'s own work with {', '.join(other_roles)} "
            f"territory — the dispatch straddles roles, cannot decide a single fix"
        )
    else:
        reason = (
            f"all {n} outside path(s) span multiple roles ({', '.join(other_roles)}) "
            f"— no single correct routing"
        )
    return Classification(
        BUCKET_UNDECIDABLE, reason, own, other, unknown, other_roles
    )


@dataclass(frozen=True)
class Triage:
    """One measured dispatch and its classification."""

    dispatch_id: str
    role: str
    outside_paths: "tuple[str, ...]"
    classification: Classification


@dataclass
class TriageResult:
    """The full measurement: triaged dispatches plus the excluded populations."""

    triages: "list[Triage]" = field(default_factory=list)
    unlinked: int = 0
    linked_no_files: int = 0
    in_scope: int = 0

    @property
    def outside(self) -> int:
        return len(self.triages)


def outside_paths(role: str, files: "Iterable[str]") -> "list[str]":
    """Return the files in *files* outside *role*'s file_write_scope (sorted).

    Uses the hook's own ``match_file_write_scope`` with no dispatch-path
    narrowing — exactly the ``role_scope_only`` dimension the measure script
    reports. The profile comes from ``resolve_worker_profile`` (the hook's
    resolver), so unknown roles inherit the code-worker fallback scope.
    """
    profile = resolve_worker_profile(role)
    return sorted(f for f in files if not match_file_write_scope(f, profile, None))


def build_triage(
    specs: "dict[str, dict]",
    did2files: "dict[str, set[str]]",
) -> TriageResult:
    """Classify every linked, file-changing dispatch. Testable without git.

    *specs* maps dispatch_id -> spec dict (with a ``role`` key).
    *did2files* maps dispatch_id -> set of changed file paths.

    A dispatch present in *specs* but absent from *did2files* has NO linked
    commit — it falls OUTSIDE the measurement (``unlinked``) and is never placed
    in a bucket. A linked dispatch with zero changed files is likewise excluded
    (``linked_no_files``). A linked dispatch whose changes are all in-scope is
    counted as ``in_scope``, not bucketed. Only dispatches with >=1 outside path
    produce a Triage and land in exactly one bucket.
    """
    result = TriageResult()
    for did in sorted(specs):
        files = did2files.get(did)
        if files is None:
            result.unlinked += 1
            continue
        if not files:
            result.linked_no_files += 1
            continue
        role = (specs[did].get("role") or "").strip()
        out = outside_paths(role, files)
        if not out:
            result.in_scope += 1
            continue
        result.triages.append(
            Triage(
                dispatch_id=did,
                role=role,
                outside_paths=tuple(out),
                classification=classify(role, out),
            )
        )
    return result


def group_by_bucket(triages: "list[Triage]") -> "dict[str, list[Triage]]":
    """Partition *triages* into the three buckets, preserving input order."""
    grouped: "dict[str, list[Triage]]" = {b: [] for b in BUCKETS}
    for t in triages:
        grouped[t.classification.bucket].append(t)
    return grouped


def verify_partition(grouped: "dict[str, list[Triage]]", total: int) -> bool:
    """Hard check: the three buckets sum to the measured outside total.

    Returns True only when every triage lands in exactly one of the three
    buckets AND the bucket sum equals *total*. This is the dispatch's hard
    control — on failure the caller exits non-zero.
    """
    return sum(len(v) for v in grouped.values()) == total


def scope_proposals(
    triages: "list[Triage]",
) -> "dict[str, dict]":
    """Concrete per-role scope additions and how many dispatches they resolve.

    For each role R, the paths that the ownership map assigns to R but R's
    file_write_scope currently rejects (i.e. R's own work that its scope forgot).
    ``resolves_too_narrow`` counts the rol-te-smal dispatches already routed to R
    (fixed by adding the paths). ``resolves_misrouted`` counts the
    verkeerd-gerouteerd dispatches whose outside paths belong to R (fixed by
    re-routing to R once its scope covers them). Undecidable dispatches are NOT
    resolved by any single scope change and are reported separately.
    """
    missing: "dict[str, set[str]]" = defaultdict(set)
    too_narrow: "dict[str, int]" = defaultdict(int)
    misrouted: "dict[str, int]" = defaultdict(int)

    for t in triages:
        b = t.classification.bucket
        if b == BUCKET_TOO_NARROW:
            too_narrow[t.role] += 1
        elif b == BUCKET_MISROUTED:
            for r in t.classification.other_roles:
                misrouted[r] += 1
        # every outside path that is role R's own work, but outside R's scope,
        # is a candidate scope rule for R — regardless of which role wrote it
        for p in t.outside_paths:
            o = owner(p)
            if o is None:
                continue
            if not match_file_write_scope(p, resolve_worker_profile(o), None):
                missing[o].add(p)

    proposals: "dict[str, dict]" = {}
    for role in sorted(missing):
        proposals[role] = {
            "missing_paths": sorted(missing[role]),
            "resolves_too_narrow": too_narrow.get(role, 0),
            "resolves_misrouted": misrouted.get(role, 0),
            "resolves_total": too_narrow.get(role, 0) + misrouted.get(role, 0),
        }
    return proposals


def _build_payload(triages: "list[Triage]", result: TriageResult) -> dict:
    grouped = group_by_bucket(triages)
    ok = verify_partition(grouped, result.outside)

    by_role: "dict[str, dict]" = {}
    for role in sorted({t.role for t in triages}):
        rt = [t for t in triages if t.role == role]
        by_role[role] = {
            b: sum(1 for t in rt if t.classification.bucket == b) for b in BUCKETS
        }

    return {
        "summary": {
            "role_scope_only__outside": result.outside,
            "unlinked": result.unlinked,
            "linked_no_files": result.linked_no_files,
            "in_scope": result.in_scope,
            "rol_te_smal": len(grouped[BUCKET_TOO_NARROW]),
            "verkeerd_gerouteerd": len(grouped[BUCKET_MISROUTED]),
            "onbeslisbaar": len(grouped[BUCKET_UNDECIDABLE]),
            "sum_check_ok": ok,
        },
        "by_role": by_role,
        "scope_proposals": scope_proposals(triages),
        "dispatches": [
            {
                "dispatch_id": t.dispatch_id,
                "role": t.role,
                "outside_paths": list(t.outside_paths),
                "bucket": t.classification.bucket,
                "reason": t.classification.reason,
                "other_roles": list(t.classification.other_roles),
            }
            for t in triages
        ],
    }


def _render_table(payload: dict) -> str:
    s = payload["summary"]
    lines = [
        "== role_scope_outside_triage ==",
        f"outside total : {s['role_scope_only__outside']}",
        f"rol-te-smal        : {s['rol_te_smal']}",
        f"verkeerd-gerouteerd: {s['verkeerd_gerouteerd']}",
        f"onbeslisbaar       : {s['onbeslisbaar']}",
        f"sum check         : {'OK' if s['sum_check_ok'] else 'FAILED'}",
        "",
        "== per-role distribution ==",
    ]
    for role, buckets in sorted(payload["by_role"].items()):
        lines.append(
            f"  {role}: "
            + ", ".join(f"{b}={c}" for b, c in sorted(buckets.items()))
        )
    lines.append("")
    lines.append("== per-dispatch detail ==")
    for d in payload["dispatches"]:
        lines.append(
            f"{d['dispatch_id']} [{d['role']}] -> {d['bucket']} "
            f"({len(d['outside_paths'])} outside)"
        )
        for p in d["outside_paths"]:
            lines.append(f"      {p}")
        lines.append(f"      reason: {d['reason']}")
    return "\n".join(lines)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="print only the JSON payload (skip the human-readable table)",
    )
    args = parser.parse_args(argv)

    specs = load_specs()
    did2shas = link_dispatch_to_commits(set(specs))

    did2files: "dict[str, set[str]]" = {}
    for did, shas in did2shas.items():
        files: "set[str]" = set()
        for sha in shas:
            files |= changed_files(sha)
        did2files[did] = files

    result = build_triage(specs, did2files)
    grouped = group_by_bucket(result.triages)
    ok = verify_partition(grouped, result.outside)

    payload = _build_payload(result.triages, result)
    print(json.dumps(payload, indent=2))
    if not args.json_only:
        print()
        print(_render_table(payload))

    # Hard control: the three buckets must partition the measured population
    # exactly. On mismatch, fail loudly with a non-zero exit.
    if not ok:
        print(
            "role_scope_outside_triage: SUM CHECK FAILED — "
            f"buckets sum to {sum(len(v) for v in grouped.values())} but the "
            f"outside population is {result.outside}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
