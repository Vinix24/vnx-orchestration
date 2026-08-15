#!/usr/bin/env python3
"""Measure whether historical dispatches wrote outside their declared dispatch_paths.

Purpose (operator directive 2026-08-15, "de schrijfbegrenzing gaat default aan"):
before flipping ``worker_permission_enforcement_enabled()`` to default-ON, measure
what a fleet-wide flip would have blocked. This script answers the dispatch-level
question only: for every dispatch that DECLARED write-granting ``dispatch_paths``,
did the files it actually changed stay inside that declaration?

Linkage (measured reliable, not estimated):
  dispatch-spec.json ``dispatch_id``  ->  git commit body ``Dispatch-ID: <id>``
  (the worker commit convention)      ->  ``git diff-tree`` changed files.

A changed file is "outside" when it does not fnmatch any of the dispatch's
write-granting declared paths — the exact predicate
``worker_permissions.match_file_write_scope`` applies at the dispatch layer
(``any(fnmatch.fnmatch(file_path, scope) for scope in write_scope)``), WITHOUT the
role-scope intersection this measurement deliberately ignores.

Honest limits (see report):
  * git changed-files is an UPPER BOUND on "would be blocked": the enforcement
    hook gates only Write/Edit/MultiEdit tool calls, not Bash, so a file changed
    via ``git apply``/``cat >``/a python script would not have been blocked.
  * dispatches whose commits are unreachable (deleted-without-merge branches,
    foreign repos) are counted separately as "no commits found", never guessed.

Run:
  python3 scripts/analysis/dispatch_paths_compliance_measure.py [--pending-dir PATH]
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path

# PathAccess values that grant WRITE — mirror of dispatch_spec.WRITE_GRANTING_PATH_ACCESS.
WRITE_GRANTING_ACCESS = frozenset({"write", "read_write", "create"})

# Matches the two dispatch-id conventions seen in commit bodies:
#   "Dispatch-ID: <id>" (current worker convention) and
#   "Dispatch <id>"     (older prose convention, e.g. "Dispatch 20260724-... .").
# The captured token is stripped of trailing punctuation (".", ",", ";").
_DISPATCH_ID_RE = re.compile(r"\bDispatch(?:-ID)?:?\s+(\S+)", re.MULTILINE)


def _repo_root() -> Path:
    """scripts/analysis/<this>.py -> repo root (parents[2])."""
    return Path(__file__).resolve().parents[2]


def load_specs(pending_dir: Path) -> list[dict]:
    """Load every dispatch-spec.json under *pending_dir* (one per dispatch subdir)."""
    specs: list[dict] = []
    for sp in sorted(pending_dir.glob("*/dispatch-spec.json")):
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"skip unreadable {sp}: {exc}")
            continue
        if not isinstance(data, dict):
            continue
        specs.append(data)
    return specs


def write_scope_paths(spec: dict) -> list[str]:
    """The write-granting declared paths of *spec*, in declaration order."""
    raw = spec.get("dispatch_paths") or []
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        access = entry.get("access")
        if isinstance(path, str) and access in WRITE_GRANTING_ACCESS:
            out.append(path)
    return out


def build_dispatch_id_to_commits(repo: Path) -> dict[str, list[str]]:
    """Map Dispatch-ID -> [commit sha] from commit bodies across all refs.

    One ``git log`` pass, parsed locally. A dispatch id can map to several
    commits (the worker commit(s) plus the squash-merge commit on main).
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "log", "--all", "--format=%x1e%H%x00%B"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git log failed: {proc.stderr}")
    mapping: dict[str, list[str]] = {}
    for chunk in proc.stdout.split("\x1e"):
        chunk = chunk.lstrip("\n")
        if not chunk.strip():
            continue
        if "\x00" not in chunk:
            continue
        sha, body = chunk.split("\x00", 1)
        m = _DISPATCH_ID_RE.search(body)
        if m:
            did = m.group(1).rstrip(".,;:")
            mapping.setdefault(did, []).append(sha)
    return mapping


def commit_changed_files(repo: Path, sha: str) -> list[str]:
    """Changed files of one commit (first-parent diff; works for merge + root)."""
    proc = subprocess.run(
        [
            "git", "-C", str(repo), "diff-tree",
            "-r", "--no-commit-id", "--name-only",
            "-m", "--first-parent", sha,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def files_within_scope(changed: list[str], scope: list[str]) -> tuple[list[str], list[str]]:
    """Split *changed* into (inside, outside) against the dispatch write *scope*.

    Matches the dispatch-layer predicate of
    ``worker_permissions.match_file_write_scope``: a path is inside when it
    fnmatch-matches any declared glob (fnmatch ``*`` matches ``/`` on Unix,
    exactly as the enforcement does).
    """
    inside: list[str] = []
    outside: list[str] = []
    for f in changed:
        if any(fnmatch.fnmatch(f, s) for s in scope):
            inside.append(f)
        else:
            outside.append(f)
    return inside, outside


def outside_is_nested_under_declared_dir(file_path: str, scope: list[str]) -> bool:
    """True when *file_path* sits under a declared path used as a directory.

    fnmatch("tests", "tests/test_x.py") is False, but a T0 that declared a bare
    "tests" (or "scripts/commands") almost certainly meant "that directory and
    everything under it". This split distinguishes that directory-name ambiguity
    from a genuinely unrelated write, because the two argue for different fixes.
    """
    return any(file_path.startswith(s + "/") for s in scope)


def measure(pending_dir: Path, repo: Path) -> dict:
    specs = load_specs(pending_dir)
    id_to_commits = build_dispatch_id_to_commits(repo)

    declared = []          # specs with >=1 write-granting dispatch_path
    no_paths = []          # specs with no write-granting dispatch_path (empty/absent/read-only)
    for spec in specs:
        scope = write_scope_paths(spec)
        if scope:
            declared.append((spec, scope))
        else:
            no_paths.append(spec)

    within: list[dict] = []
    outside: list[dict] = []
    no_commits: list[dict] = []
    for spec, scope in declared:
        did = spec.get("dispatch_id", "")
        commits = id_to_commits.get(did, [])
        changed: list[str] = []
        for sha in commits:
            changed.extend(commit_changed_files(repo, sha))
        # Dedupe, preserving order.
        seen: set[str] = set()
        uniq = [f for f in changed if not (f in seen or seen.add(f))]
        if not uniq:
            no_commits.append({
                "dispatch_id": did,
                "role": spec.get("role"),
                "scope": scope,
                "commit_count": len(commits),
            })
            continue
        inside, out = files_within_scope(uniq, scope)
        nested = [f for f in out if outside_is_nested_under_declared_dir(f, scope)]
        unrelated = [f for f in out if f not in nested]
        record = {
            "dispatch_id": did,
            "role": spec.get("role"),
            "scope": scope,
            "changed": uniq,
            "outside": out,
            "outside_nested_under_declared_dir": nested,
            "outside_unrelated": unrelated,
            "commit_count": len(commits),
        }
        if out:
            outside.append(record)
        else:
            within.append(record)

    return {
        "total_specs": len(specs),
        "declared_write_paths": len(declared),
        "no_write_paths": len(no_paths),
        "within": within,
        "outside": outside,
        "no_commits": no_commits,
    }


def _print_summary(result: dict) -> None:
    total = result["total_specs"]
    declared = result["declared_write_paths"]
    no_paths = result["no_write_paths"]
    within = result["within"]
    outside = result["outside"]
    no_commits = result["no_commits"]

    print("=" * 72)
    print("dispatch_paths write-scope compliance (would the default-ON flip block?)")
    print("=" * 72)
    print(f"specs with dispatch-spec.json          : {total}")
    print(f"  declared >=1 write-granting path     : {declared}")
    print(f"  declared no write-granting path      : {no_paths}  (fall back to role scope)")
    print()
    measured = within + outside
    print(f"declared dispatches with git commits   : {len(measured)}")
    print(f"  stayed within declaration            : {len(within)}")
    print(f"  wrote OUTSIDE declaration            : {len(outside)}")
    print(f"  no commits found (not measurable)    : {len(no_commits)}")
    if measured:
        print(f"  outside-rate over measurable         : {len(outside)}/{len(measured)} = "
              f"{100.0 * len(outside) / len(measured):.1f}%")
        print(f"  outside-rate over ALL declared       : {len(outside)}/{declared} = "
              f"{100.0 * len(outside) / declared:.1f}%")
    # Characterize the "outside" population: how much of it is the
    # directory-name ambiguity (declared "tests", wrote "tests/x.py") vs a
    # genuinely unrelated write. This determines the shape of the operator's
    # decision, so it is measured, not guessed.
    nested_only = [
        r for r in outside if r["outside_unrelated"] == []
    ]
    any_unrelated = [
        r for r in outside if r["outside_unrelated"] != []
    ]
    print(f"  outside, every file nested under a declared dir-name : {len(nested_only)}")
    print(f"  outside, >=1 file genuinely unrelated               : {len(any_unrelated)}")

    print()
    print("Concrete examples of dispatches that wrote outside their declaration:")
    for rec in outside[:10]:
        did = rec["dispatch_id"]
        role = rec["role"]
        print(f"\n  {did}  (role={role})")
        print(f"    declared: {rec['scope']}")
        print(f"    outside writes: {rec['outside'][:8]}")
        rest = rec["outside"][8:]
        if rest:
            print(f"      (+{len(rest)} more)")
        if rec["outside_nested_under_declared_dir"]:
            print(f"    -> nested under a declared dir-name: {rec['outside_nested_under_declared_dir'][:4]}")
        if rec["outside_unrelated"]:
            print(f"    -> unrelated to declaration: {rec['outside_unrelated'][:4]}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--pending-dir",
        default=os.path.expanduser("~/.vnx-data/vnx-dev/dispatches/pending"),
        help="directory of dispatch-spec subdirs (default: ~/.vnx-data/vnx-dev/dispatches/pending)",
    )
    ap.add_argument("--repo", default=None, help="git repo root (default: repo containing this script)")
    args = ap.parse_args()

    repo = Path(args.repo) if args.repo else _repo_root()
    result = measure(Path(args.pending_dir), repo)
    _print_summary(result)


if __name__ == "__main__":
    main()
