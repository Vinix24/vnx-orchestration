#!/usr/bin/env python3
"""Permission-event scan (ADR-075, L2): B1, B2, B3 and B4a from the bound transcripts into ``permission_events.ndjson``.

Exit codes: 0 a completed scan (also when a source is unbound, absent, error or unknown: that is its coverage),
2 a refused binding, a foreign store or invalid arguments. Prints counts per source and besluit, never a command.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

import operator_steering_source as source
import permission_events as pe
import permission_events_prune as prune_mod
import permission_events_sources as sources
from vnx_paths import project_id_from_state_dir

BRONNEN = ("B1", "B2", "B3", "B4a")


def parse_since(text: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([dh])", text or "")
    if not m or int(m.group(1)) < 1:
        raise ValueError(f"--since {text!r}: use <N>d or <N>h")
    return timedelta(**{"days" if m.group(2) == "d" else "hours": int(m.group(1))})


def _aware(ts: object) -> Optional[datetime]:
    """The timestamp as an aware datetime, or None when it is malformed or carries no timezone."""
    try:
        when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def _refused_binding(state: Path) -> bool:
    """A binding file naming a forbidden slug (ADR-075 decision 5), judged with ADR-072's own list."""
    try:
        slugs = json.loads((state / source.BINDING_FILE).read_text(encoding="utf-8"))["transcript_dirs"]
        return any(p in str(s).lower() for s in slugs for p in source.FORBIDDEN_SLUG_PARTS)
    except (OSError, ValueError, KeyError, TypeError):  # vnx-silent-except: no readable binding is "unbound", judged later
        return False


def _coverage_args(result: source.SourceResult, read: int, unreadable: int) -> Dict[str, object]:
    """state, files_read, files_unreadable and reason for the four sources; a reason is a fixed code, never a slug."""
    if result.state == "ok":
        if unreadable:
            return dict(state="unknown", files_read=read, files_unreadable=unreadable, unreadable_reason="unreadable_file")
        if not read:
            return dict(state="absent", files_read=0, files_unreadable=0, unreadable_reason="no_transcript_files")
        return dict(state="ok", files_read=read, files_unreadable=0)
    if result.state == "error":
        if set(result.refused) == {"open_failed"}:
            return dict(state="unknown", files_read=read, files_unreadable=sum(result.refused.values()), unreadable_reason="unreadable_file")
        return dict(state="error", files_read=read, files_unreadable=sum(result.refused.values()), unreadable_reason="containment_refused")
    return dict(state=result.state, files_read=0, files_unreadable=0, unreadable_reason=result.reason.split(":")[0].strip()[:80] or result.state)


def run_scan(project_id: str, since: str, projects_root: Path, state_dir: Optional[Path] = None,
             account_configs=None, now: Optional[datetime] = None,
             out: Optional[TextIO] = None) -> int:
    out = out or sys.stdout
    account_configs = sources.default_account_configs() if account_configs is None else account_configs
    state = pe._state_dir(project_id, state_dir)  # the single gate: fleet project id, central store
    derived = project_id_from_state_dir(state)
    if derived and derived != project_id:
        raise ValueError(f"state dir belongs to the store of another project, not {project_id}")
    if _refused_binding(state):
        raise PermissionError("binding refused: a transcript dir with a forbidden slug part")
    cutoff = (now or datetime.now(timezone.utc)) - parse_since(since)
    events: List[Dict[str, str]] = []
    skipped = {b: Counter() for b in BRONNEN}
    files = unreadable = 0

    def consume(handle: TextIO, name: str, slug: str) -> None:
        nonlocal files, unreadable
        try:
            lines = handle.readlines()
        except (OSError, ValueError):  # vnx-silent-except: counted as an unreadable file in the coverage
            unreadable += 1
            return
        files += 1
        got = sources.scan_transcript(lines, project_id=project_id, slug=slug, stem=Path(name).stem, account_configs=account_configs)
        events.extend(got.events)
        for bron, counts in got.skipped.items():
            skipped[bron].update(counts)

    result = source.read_sources(state, projects_root, consume, with_slug=True)
    kept = []
    for e in events if result.state == "ok" else []:
        when = _aware(e["ts"])
        if when is None:  # no timezone or unparseable: counted, never compared (fail-closed, not a crash)
            skipped[e["bron"]]["bad_timestamp"] += 1
        elif when >= cutoff:
            kept.append(e)
    if kept:
        pe.write_events(kept, project_id, state)
    cov = _coverage_args(result, files, unreadable)
    counts = Counter((e["bron"], e["besluit"]) for e in kept)
    for bron in BRONNEN:
        total = sum(n for (b, _), n in counts.items() if b == bron)
        pe.write_coverage(project_id, bron, window=since, state_dir=state, skipped=dict(skipped[bron]),
                          count=total if cov["state"] == "ok" else None, **cov)
        print(f"{bron} {cov['state']}" + (f" ({cov['unreadable_reason']})" if cov.get("unreadable_reason") else ""), file=out)
        for (b, besluit), n in sorted(counts.items()):
            if b == bron:
                print(f"{bron} {besluit} {n}", file=out)
        for reason, n in sorted(skipped[bron].items()):
            print(f"{bron} skipped {reason} {n}", file=out)
    return 0


def main_prune(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="permission_events_scan.py prune", description="ADR-075 decision 6: lines older than 90 days move to the archive")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--state-dir", help="project state dir (default: central store of the project)")
    args = parser.parse_args(argv)
    try:
        entry = prune_mod.prune(args.project_id, Path(args.state_dir) if args.state_dir else None)
    except (ValueError, PermissionError) as exc:
        print(f"prune refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # the round restored both ledgers and wrote status error into the coverage
        print(f"prune failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    for name, rep in entry["ledgers"].items():
        print(f"{name} moved {rep['moved']} removed {rep['removed_archives']} ({rep['removed_reason']}) unparseable_ts {rep['unparseable_ts']}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["prune"]:
        return main_prune(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--since", default="7d", help="<N>d or <N>h (default 7d)")
    parser.add_argument("--projects-root", default=str(source.DEFAULT_PROJECTS_ROOT))
    parser.add_argument("--state-dir", help="project state dir (default: central store of the project)")
    parser.add_argument("--vnx-config", help="explicit config whose oauthAccount is slot vnx")
    parser.add_argument("--sm-config", help="explicit config whose oauthAccount is slot sm")
    args = parser.parse_args(argv)
    try:
        configs = sources.default_account_configs()
        configs.update({slot: Path(value) for slot, value in (("vnx", args.vnx_config), ("sm", args.sm_config)) if value})
        return run_scan(args.project_id, args.since, Path(args.projects_root), Path(args.state_dir) if args.state_dir else None,
                        configs)
    except (ValueError, PermissionError) as exc:
        print(f"scan refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
