#!/usr/bin/env python3
"""Measure how far the fleet has fallen behind the canonical T0 role.

OI-1478 closed with nine role files propagated by hand across three consumer
repos. What it did NOT close is the reason that took a hand: **nothing
measures whether the fleet is behind.** ``vnx role sync --dry-run`` answers
the question for ONE repo, in prose, when a human happens to run it. This
answers it for the whole fleet, as a state, with an exit code.

Three axes, because the propagation can fail in three independent ways and a
tool that reads only one of them reports a green fleet that is not green:

1. **CONTENT** — is the project's copy of the role identical to the canon?
   Measured as a DIFF against the canon, never as a search for a known
   sentence. A search finds only the drift you already knew about: OI-1478's
   own sweep looked for "NEVER claude -p" and would have walked straight past
   the stale ``_FAKE_DEFAULT_ROLE`` sentinel rule that the same nine files
   also carried. A diff finds both, plus the next one, which nobody has named
   yet. Covers role-orchestrator.md (Claude) and the marked
   ``<!-- VNX:BEGIN T0-ROLE -->`` block in AGENTS.md (Codex) and GEMINI.md
   (Gemini/Kimi). Those two are gitignored, locally-generated artefacts, so
   their ABSENCE is reported as a coverage warning and their DRIFT as
   drift — a fresh checkout has neither and is not thereby behind.

2. **REACH** — does any session actually LOAD that file? Claude Code finds
   ``.claude/terminals/T0/role-orchestrator.md`` only through an
   ``@role-orchestrator.md`` import in the sibling CLAUDE.md. Without it the
   sync writes a perfectly current file that nothing ever reads (OI-1480,
   measured in SEOcrawler_v2). A file that is up to date and unread is not a
   propagated role; it is a propagated artefact.

3. **STARTUP** — can the role's own Mandatory Startup step complete? It
   requires the t0-orchestrator playbook to arrive either through the
   SessionStart hook or through a model-invocable Skill fallback. When both
   are dead the delivered role prescribes STOP, so a T0 there cannot legally
   start at all (OI-1481, measured in sales-copilot).

Plus one axis about the meter's own input: is the canon this vnx would
PROPAGATE itself current with the fabric source? Vincent's 2026-08-28
operator decision — canon on main first, propagate second — exists because
syncing from a stale install pushes yesterday's role to the whole fleet in
one command.

Read-only by default. ``--write-state`` is opt-in for exactly the reason this
tool exists to catch: a measurement that writes to the live store is itself a
write, and an unasked-for write is how evidence gets overwritten.

**Why the opt-in write emits no NDJSON ledger event** (codex advisory on
#1704, answered rather than deferred). ADR-005 makes the ledger canonical for
four named classes: dispatch lifecycle events, receipts, gate outcomes, and
lease/heartbeat transitions. A component health beacon is none of those — the
"heartbeat" in that list is the runtime_coordination lease row, not a
``health/<component>.json`` file. Measured on 53973d6a: of the ELEVEN
``HealthBeacon(...)`` call sites in this tree, ZERO pair the beacon with a
ledger event. The one that a proximity grep flags,
``producer_freshness.write_heartbeat``, sits ~10 lines below an unrelated
function that appends ``producer_freshness_finding`` records; the beacon
itself emits nothing. The beacon file IS the audit carrier for component
health here, and adding a second trail only in this tool would make this tool
the outlier rather than close a gap.

What the advisory is right about, and what the boundary therefore is: a
beacon is CURRENT-VERDICT state, replaced on every run, so it carries no
history — you can read that the fleet is behind today, not that it was also
behind yesterday. That is a deliberate limit of this surface, not an
oversight. A fleet-drift TIMELINE is a different artefact with a different
owner, and it belongs in the ledger the day someone needs to answer "how long
has seocrawler-v2 been unreachable" rather than "is it unreachable now".

Exit codes:
    0  every measured project is current on every axis
    1  at least one project is behind on at least one axis
    2  the meter could not measure (canon absent, registry unreadable)
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
for _p in (_HERE / "lib", _HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

ROLE_MARKER_BEGIN = "<!-- VNX:BEGIN T0-ROLE -->"
ROLE_MARKER_END = "<!-- VNX:END T0-ROLE -->"
ROLE_BASENAME = "role-orchestrator.md"
PROVIDER_FILES = ("AGENTS.md", "GEMINI.md")
T0_REL = Path(".claude/terminals/T0")
SKILL_REL = Path(".claude/skills/t0-orchestrator/SKILL.md")
DEFAULT_REGISTRY = Path.home() / ".vnx" / "projects.json"

# Statuses, ordered worst-first for reporting.
CURRENT = "current"
STALE = "stale"
ABSENT = "absent"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _extract_marked_block(text: str) -> Optional[str]:
    """Return the canonical-role block between the T0-ROLE markers, or None.

    Mirrors what ``vnx_upsert_marked_block`` writes: the markers sit on their
    own lines and the payload is everything strictly between them.
    """
    begin = text.find(ROLE_MARKER_BEGIN)
    if begin == -1:
        return None
    end = text.find(ROLE_MARKER_END, begin)
    if end == -1:
        return None
    return text[begin + len(ROLE_MARKER_BEGIN):end].strip("\n")


def _drift(canon: str, local: Optional[str]) -> Dict[str, Any]:
    """Compare one surface against the canon and describe the difference.

    ``sample`` carries the first few differing lines so an operator can see
    WHAT drifted without opening the file — the string this tool refuses to
    search for in advance is exactly the string it should hand back here.
    """
    if local is None:
        return {"status": ABSENT, "drift_lines": None, "local_sha": None, "sample": []}
    if local.strip() == canon.strip():
        return {"status": CURRENT, "drift_lines": 0, "local_sha": _sha256(local), "sample": []}

    canon_lines = canon.strip().splitlines()
    local_lines = local.strip().splitlines()
    diff = [
        ln for ln in difflib.unified_diff(local_lines, canon_lines, lineterm="", n=0)
        if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
    ]
    sample = [ln.strip()[:160] for ln in diff[:6]]
    return {
        "status": STALE,
        "drift_lines": len(diff),
        "local_sha": _sha256(local),
        "sample": sample,
    }


def _frontmatter_disables_invocation(text: str) -> bool:
    """True when the FIRST frontmatter block carries the disable key.

    Anchored at line start and skipping comments, matching
    ``t0_role_audit.sh``'s check exactly: unanchored, a ``description:`` that
    merely MENTIONS the flag false-positived as unloadable (codex finding 3,
    2026-07-16). The two implementations must not diverge.
    """
    seen = 0
    for line in text.splitlines():
        if re.match(r"^---[ \t]*$", line):
            seen += 1
            if seen == 2:
                return False
            continue
        if seen != 1 or line.lstrip().startswith("#"):
            continue
        if re.match(r"^disable-model-invocation:[ \t]*true([ \t]|$)", line):
            return True
    return False


def _measure_reach(t0_dir: Path) -> Dict[str, Any]:
    """Axis 2: would a Claude T0 session in this repo actually load the role?"""
    claude_md = t0_dir / "CLAUDE.md"
    text = _read(claude_md)
    if text is None:
        return {
            "ok": False,
            "reason": f"no {claude_md.name} in {t0_dir} — nothing imports the role",
        }
    if "@role-orchestrator" not in text:
        return {
            "ok": False,
            "reason": (
                f"{claude_md.name} does not import @role-orchestrator.md — role sync "
                "refreshes a file no session reads (OI-1480)"
            ),
        }
    return {"ok": True, "reason": "CLAUDE.md imports @role-orchestrator.md"}


def _hook_injects_skill(project_root: Path) -> Tuple[bool, str]:
    """Is the t0-orchestrator playbook wired into a SessionStart hook?

    Static, by grep, deliberately: the hook is a shell script and executing
    it to find out would be a side effect, not a measurement.
    """
    settings = project_root / ".claude" / "settings.json"
    settings_text = _read(settings)
    if settings_text is None or "SessionStart" not in settings_text:
        return False, "no SessionStart entry in .claude/settings.json"
    for hook in (
        project_root / ".claude" / "hooks" / "sessionstart.sh",
        project_root / "hooks" / "sessionstart.sh",
    ):
        hook_text = _read(hook)
        if hook_text and "skills/t0-orchestrator/SKILL.md" in hook_text:
            return True, f"{hook.relative_to(project_root)} injects the skill body"
    return False, "SessionStart is wired but no hook script references t0-orchestrator/SKILL.md"


def _measure_startup(project_root: Path, t0_dir: Path) -> Dict[str, Any]:
    """Axis 3: can Mandatory Startup complete by either of its two routes?"""
    routes: Dict[str, Any] = {}

    claude_text = _read(t0_dir / "CLAUDE.md") or ""
    routes["claude_md_import"] = {
        "ok": "skills/t0-orchestrator/SKILL.md" in claude_text,
        "reason": "CLAUDE.md @-imports the SKILL.md body" if
        "skills/t0-orchestrator/SKILL.md" in claude_text else "CLAUDE.md does not import SKILL.md",
    }

    hook_ok, hook_reason = _hook_injects_skill(project_root)
    routes["sessionstart_hook"] = {"ok": hook_ok, "reason": hook_reason}

    skill_text = _read(project_root / SKILL_REL)
    if skill_text is None:
        routes["skill_tool_fallback"] = {"ok": False, "reason": f"{SKILL_REL} missing"}
    elif _frontmatter_disables_invocation(skill_text):
        routes["skill_tool_fallback"] = {
            "ok": False,
            "reason": "SKILL.md carries disable-model-invocation: true (operator-only by design)",
        }
    else:
        routes["skill_tool_fallback"] = {"ok": True, "reason": "SKILL.md is model-invocable"}

    ok = any(r["ok"] for r in routes.values())
    return {
        "ok": ok,
        "routes": routes,
        "reason": "at least one delivery route is live" if ok else (
            "every delivery route is dead — the delivered role prescribes STOP, so a "
            "T0 here cannot legally start (OI-1481)"
        ),
    }


def measure_project(project_root: Path, canon: str, *, is_canon_source: bool = False) -> Dict[str, Any]:
    """All three axes for one repo."""
    t0_dir = project_root / T0_REL
    surfaces: Dict[str, Any] = {}

    surfaces[ROLE_BASENAME] = _drift(canon, _read(t0_dir / ROLE_BASENAME))
    for name in PROVIDER_FILES:
        text = _read(t0_dir / name)
        block = _extract_marked_block(text) if text is not None else None
        result = _drift(canon, block)
        if text is not None and block is None:
            result["reason"] = f"{name} exists but carries no {ROLE_MARKER_BEGIN} block"
        surfaces[name] = result

    reach = _measure_reach(t0_dir)
    startup = _measure_startup(project_root, t0_dir)

    # An ABSENT provider mirror is a coverage gap, not drift, and the two must
    # not share a verdict. AGENTS.md and GEMINI.md are GITIGNORED
    # (.gitignore:154 in the fabric repo) — `vnx role sync` generates them
    # locally, so a fresh checkout legitimately has neither, and counting that
    # as "behind the canon" makes the meter red on a repo that is perfectly in
    # sync. Measured on this very branch's worktree, which is exactly that
    # case. A provider mirror that EXISTS and has drifted is unambiguous:
    # something generated it and the canon has moved since.
    drifted = {n: s for n, s in surfaces.items() if s["status"] == STALE}
    missing_provider = [n for n in PROVIDER_FILES if surfaces[n]["status"] == ABSENT]
    role_absent = surfaces[ROLE_BASENAME]["status"] == ABSENT

    warnings: List[str] = []
    for name in missing_provider:
        warnings.append(
            f"{name} carries no canonical-role block — the "
            f"{'Codex' if name == 'AGENTS.md' else 'Gemini/Kimi'} route has no role here "
            f"(gitignored artefact; run `vnx role sync --apply` if that route is used)"
        )

    return {
        "path": str(project_root),
        "t0_dir_exists": t0_dir.is_dir(),
        "is_canon_source": is_canon_source,
        "content": {"ok": not drifted and not role_absent, "surfaces": surfaces},
        "reach": reach,
        "startup": startup,
        "warnings": warnings,
        "behind": bool(drifted) or role_absent or not reach["ok"] or not startup["ok"],
    }


def load_registry(path: Path) -> List[Dict[str, Any]]:
    text = _read(path)
    if text is None:
        raise FileNotFoundError(f"registry not readable: {path}")
    data = json.loads(text)
    projects = data.get("projects") if isinstance(data, dict) else data
    if not isinstance(projects, list):
        raise ValueError(f"registry has no project list: {path}")
    return projects


def _resolve_canon_path() -> Path:
    """The role file THIS vnx would propagate (what `vnx role sync` copies)."""
    home = os.environ.get("VNX_HOME")
    if home:
        return Path(home) / T0_REL / ROLE_BASENAME
    try:
        from vnx_paths import resolve_paths  # noqa: PLC0415
        return Path(resolve_paths()["VNX_HOME"]) / T0_REL / ROLE_BASENAME
    except Exception:  # noqa: BLE001 — a resolver failure must not mask the measurement
        return _HERE.parent / T0_REL / ROLE_BASENAME


def _measure_canon_freshness(canon_path: Path, canon: str, projects: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Is the canon this vnx would propagate itself current with the source?

    Vincent's operator decision of 2026-08-28 (canon on main first, propagate
    second) exists because propagating from a stale install pushes yesterday's
    role to the whole fleet in a single command — and every consumer then
    reports "current" against a canon that is itself behind.
    """
    source = next((p for p in projects if p.get("project_id") == "vnx-dev"), None)
    if source is None:
        return {"ok": None, "reason": "fabric source (project_id=vnx-dev) not in the registry"}
    source_role = Path(source["path"]) / T0_REL / ROLE_BASENAME
    try:
        same_file = source_role.resolve() == canon_path.resolve()
    except OSError:
        same_file = False
    if same_file:
        return {"ok": True, "reason": "canon IS the fabric source checkout", "source": str(source_role)}
    source_text = _read(source_role)
    if source_text is None:
        return {"ok": None, "reason": f"fabric source role unreadable: {source_role}"}
    result = _drift(source_text, canon)
    return {
        "ok": result["status"] == CURRENT,
        "reason": (
            "canon matches the fabric source" if result["status"] == CURRENT else
            f"canon is {result['drift_lines']} lines away from the fabric source — "
            "syncing now would propagate a stale role"
        ),
        "source": str(source_role),
        "drift": result,
    }


def render_text(report: Dict[str, Any]) -> str:
    out: List[str] = []
    out.append(f"canon: {report['canon']['path']}  sha={report['canon']['sha']}")
    fresh = report["canon"]["freshness"]
    flag = {True: "ok", False: "STALE", None: "?"}[fresh["ok"]]
    out.append(f"canon freshness: [{flag}] {fresh['reason']}")
    out.append("")
    for name, p in sorted(report["projects"].items()):
        if not p["t0_dir_exists"]:
            out.append(f"[skip ] {name}: no {T0_REL} — not a VNX-terminal repo")
            continue
        mark = "BEHIND" if p["behind"] else "ok    "
        out.append(f"[{mark}] {name}  ({p['path']})")
        for surface, s in p["content"]["surfaces"].items():
            if s["status"] == CURRENT:
                out.append(f"           content  {surface:20s} current")
            else:
                extra = f" ({s['drift_lines']} diff lines)" if s["drift_lines"] is not None else ""
                out.append(f"           content  {surface:20s} {s['status'].upper()}{extra}")
                for ln in s["sample"]:
                    out.append(f"                      | {ln}")
        for w in p["warnings"]:
            out.append(f"           warn     {w}")
        out.append(f"           reach    {'ok' if p['reach']['ok'] else 'BROKEN'}: {p['reach']['reason']}")
        out.append(f"           startup  {'ok' if p['startup']['ok'] else 'BROKEN'}: {p['startup']['reason']}")
        if not p["startup"]["ok"]:
            for route, r in p["startup"]["routes"].items():
                out.append(f"                      | {route}: {r['reason']}")
        out.append("")
    behind = report["summary"]["projects_behind"]
    total = report["summary"]["projects_measured"]
    warned = report["summary"]["projects_with_warnings"]
    out.append(f"fleet: {behind} of {total} measured projects behind the canon"
               + (f", {warned} with coverage warnings" if warned else ""))
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    ap.add_argument("--canon", type=Path, default=None,
                    help="role file to measure against (default: the one this vnx would propagate)")
    ap.add_argument("--project-dir", type=Path, action="append", default=[],
                    help="measure this repo instead of the registry (repeatable)")
    ap.add_argument("--json", action="store_true", help="emit the full report as JSON")
    ap.add_argument("--write-state", action="store_true",
                    help="also write a health beacon (opt-in: a measurement that writes is a write)")
    ap.add_argument("--state-dir", type=Path, default=None,
                    help="explicit state dir for --write-state (default: resolved VNX_STATE_DIR)")
    args = ap.parse_args(argv)

    canon_path = args.canon or _resolve_canon_path()
    canon = _read(canon_path)
    if canon is None:
        print(f"fleet_role_drift: canonical role not readable: {canon_path}", file=sys.stderr)
        return 2

    if args.project_dir:
        projects = [{"name": p.name, "path": str(p)} for p in args.project_dir]
        registry_projects = projects
    else:
        try:
            registry_projects = load_registry(args.registry)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"fleet_role_drift: {exc}", file=sys.stderr)
            return 2
        projects = registry_projects

    canon_source_path = str(canon_path.parent.parent.parent.parent)
    measured: Dict[str, Any] = {}
    for entry in projects:
        root = Path(entry["path"])
        measured[entry.get("name") or root.name] = measure_project(
            root, canon, is_canon_source=str(root) == canon_source_path,
        )

    considered = {k: v for k, v in measured.items() if v["t0_dir_exists"]}
    behind = [k for k, v in considered.items() if v["behind"]]
    freshness = _measure_canon_freshness(canon_path, canon, registry_projects)

    report = {
        "canon": {"path": str(canon_path), "sha": _sha256(canon), "freshness": freshness},
        "projects": measured,
        "summary": {
            "projects_measured": len(considered),
            "projects_behind": len(behind),
            "behind": sorted(behind),
            "projects_with_warnings": sum(1 for v in considered.values() if v["warnings"]),
            "canon_stale": freshness["ok"] is False,
        },
    }

    print(json.dumps(report, indent=2) if args.json else render_text(report))

    if args.write_state:
        try:
            from health_beacon import HealthBeacon  # noqa: PLC0415
            state_dir = args.state_dir
            if state_dir is None:
                from vnx_paths import resolve_paths  # noqa: PLC0415
                state_dir = Path(resolve_paths()["VNX_STATE_DIR"])
            HealthBeacon(state_dir, "fleet_role_drift", expected_interval_seconds=86400).heartbeat(
                status="ok" if not behind and freshness["ok"] is not False else "fail",
                details=report["summary"],
            )
        except Exception as exc:  # noqa: BLE001 — a beacon failure must not change the verdict
            print(f"fleet_role_drift: could not write state: {exc}", file=sys.stderr)

    return 1 if (behind or freshness["ok"] is False) else 0


if __name__ == "__main__":
    sys.exit(main())
