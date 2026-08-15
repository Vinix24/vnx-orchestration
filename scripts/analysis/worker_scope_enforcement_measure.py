#!/usr/bin/env python3
"""Measure worker write-boundary enforcement blast radius and compliance.

Dispatch 20260815-scope-dir-matching (fix-forward on PR #1511). Two things are
measured, both by replaying the hook's OWN matchers over real history:

  1. The ROLE-scope-only outside rate — how many dispatches wrote at least one
     file outside their own role's ``file_write_scope``, ignoring
     ``dispatch_paths`` entirely. This is the number that matters for the
     enforcement default: role scope is the layer that becomes hard when
     ``worker_permission_enforcement_enabled()`` is ON. The 15-08 flip to ON
     was reverted to OFF; this rate is the flip-back condition.
  2. The dispatch-scope-only compliance — of the dispatches that declared
     write-granting ``dispatch_paths``, how many wrote outside them, under BOTH
     the pre-fix literal matcher and the repaired directory-aware matcher
     (``resolve_dispatch_write_scope`` now expands a declared directory to its
     contents). The delta proves the directory-matching fix.

Method (honest, per-dispatch, no extrapolation):

  1. Read every real dispatch spec from the project's pending dir
     (``~/.vnx-data/vnx-dev/dispatches/pending/*/dispatch-spec.json``).
     Plan-gate panel seat dirs (``final_prompt.md`` only, no spec) are excluded
     — they are not dispatches.
  2. Link each spec to the commit(s) that landed its work via the
     ``Dispatch-ID: <id>`` line in the commit body (the fabric's provenance
     convention). A dispatch with no such commit is reported as *unlinked*,
     never guessed.
  3. For each linked dispatch, list the files its commit(s) actually changed
     (``git diff-tree --root --no-commit-id --name-only -r -m``).
  4. Replay the hook's own decision for each changed file, using the SAME
     matchers the hook imports (``worker_permissions.match_file_write_scope`` /
     ``resolve_dispatch_write_scope`` / ``resolve_worker_profile``), so the
     verdict is the hook's verdict, not a reimplementation.

Only the *linked* population is reported as measured; the unlinked remainder is
a count, not a percentage. Exit 0 always; results go to stdout (JSON summary +
human-readable detail).
"""
from __future__ import annotations

import fnmatch
import json
import logging
import subprocess
import sys
from pathlib import Path

PENDING_DIR = Path.home() / ".vnx-data" / "vnx-dev" / "dispatches" / "pending"
REPO_ROOT = Path(__file__).resolve().parents[2]

WRITE_ACCESS = frozenset({"read_write", "write", "create"})

# The resolve_worker_profile fallback warning fires per unknown role (legacy
# role names like technical-writer/test-engineer); it is informative but noisy
# across 347 dispatches. Keep stderr clean — the JSON summary is the deliverable.
logging.getLogger("worker_permissions").setLevel(logging.CRITICAL)

# Import the hook's own matchers so the measurement replays the real decision.
_LIB = REPO_ROOT / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
from worker_permissions import (  # noqa: E402
    match_file_write_scope,
    resolve_dispatch_write_scope,
    resolve_worker_profile,
)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True
    ).stdout


def load_specs() -> "dict[str, dict]":
    specs: dict[str, dict] = {}
    if not PENDING_DIR.is_dir():
        return specs
    for entry in PENDING_DIR.iterdir():
        if not entry.is_dir():
            continue
        spec_file = entry / "dispatch-spec.json"
        if not spec_file.exists():
            continue  # plan-gate panel seat dir, not a dispatch
        try:
            spec = json.loads(spec_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if spec.get("dispatch_id"):
            specs[spec["dispatch_id"]] = spec
    return specs


def link_dispatch_to_commits(spec_ids: "set[str]") -> "dict[str, list[str]]":
    """Map dispatch_id -> commit SHAs via the ``Dispatch-ID:`` body line."""
    out = _git("log", "--all", "--format=%H%x00%B%x00")
    did2shas: dict[str, list[str]] = {}
    parts = out.split("\x00")
    for i in range(0, len(parts) - 1, 2):
        sha = parts[i].strip()
        body = parts[i + 1]
        for line in body.splitlines():
            if line.strip().startswith("Dispatch-ID:"):
                did = line.split(":", 1)[1].strip()
                if did in spec_ids:
                    did2shas.setdefault(did, []).append(sha)
    return did2shas


def changed_files(sha: str) -> "set[str]":
    out = _git(
        "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-m", sha
    )
    return {ln for ln in out.splitlines() if ln.strip()}


def write_granting_paths(spec: dict) -> "list[str]":
    out: list[str] = []
    for entry in spec.get("dispatch_paths") or []:
        if entry.get("access") in WRITE_ACCESS:
            out.append(entry["path"])
    return out


def declared_directory_paths(declared: "list[str]") -> "list[str]":
    """Declared entries that are bare directory paths (no wildcard), i.e. the
    literal-fnmatch gap: ``fnmatch('tests/test_x.py', 'tests')`` is False, so a
    dispatch declaring a directory can never write a file inside it under the
    shipped hook."""
    return [
        s for s in declared
        if not any(c in s for c in "*?[") and not s.endswith((".py", ".md", ".sh", ".json", ".yaml", ".yml", ".toml", ".sql", ".ts", ".js", ".tsx", ".txt"))
    ]


def main() -> int:
    specs = load_specs()
    spec_ids = set(specs)
    did2shas = link_dispatch_to_commits(spec_ids)

    linked = [did for did in specs if did in did2shas]
    unlinked = [did for did in specs if did not in did2shas]

    # ---- per-dispatch verdict over the linked population with real diffs ----
    no_dispatch_paths = 0        # declared none -> role scope alone binds
    with_dispatch_paths = 0      # declared >=1 write-granting path
    no_changed_files = 0         # linked but empty diff (merge-only)
    blocked = 0                  # the flip would block >=1 of this dispatch's writes
    not_blocked = 0
    blocked_no_paths = 0         # blocked purely by role scope (no dispatch_paths)
    blocked_by_dispatch_narrow = 0  # in role scope but outside declared dispatch_paths
    blocked_by_role_scope_with_paths = 0  # declared paths but also wrote outside role scope

    blocked_examples: list[dict] = []

    for did in linked:
        spec = specs[did]
        role = spec.get("role") or ""
        declared = write_granting_paths(spec)
        files: set[str] = set()
        for sha in did2shas[did]:
            files |= changed_files(sha)
        if not files:
            no_changed_files += 1
            continue

        profile = resolve_worker_profile(role)
        # ``resolve_dispatch_write_scope`` parses raw CLI path strings; the spec
        # stores typed DispatchPath dicts, so pass the already-write-granting
        # plain ``path`` strings (each parses as READ_WRITE, the default).
        dispatch_scope = resolve_dispatch_write_scope(declared)

        if not declared:
            no_dispatch_paths += 1
        else:
            with_dispatch_paths += 1

        # Which files would the hook block?
        blocked_files: list[dict] = []
        for f in sorted(files):
            if match_file_write_scope(f, profile, dispatch_scope):
                continue
            # attribute the block: role scope, dispatch narrowing, or both
            role_ok = match_file_write_scope(f, profile, None)
            blocked_files.append(
                {"file": f, "role_ok": role_ok}
            )

        if blocked_files:
            blocked += 1
            # attribute the dispatch's block reason
            if not declared:
                blocked_no_paths += 1
            else:
                any_role_fail = any(not bf["role_ok"] for bf in blocked_files)
                any_dispatch_fail = any(bf["role_ok"] for bf in blocked_files)
                if any_dispatch_fail:
                    blocked_by_dispatch_narrow += 1
                if any_role_fail:
                    blocked_by_role_scope_with_paths += 1
            if len(blocked_examples) < 40:
                blocked_examples.append(
                    {
                        "dispatch_id": did,
                        "role": role,
                        "declared": declared,
                        "blocked": blocked_files[:6],
                    }
                )
        else:
            not_blocked += 1

    # directory-vs-fnmatch gap, over dispatches that declared paths
    dir_declared = 0
    for did in linked:
        spec = specs[did]
        declared = write_granting_paths(spec)
        if declared_directory_paths(declared):
            dir_declared += 1

    # ---- role-scope-only outside rate (step 3 of the fix-forward) ----
    # Ignore dispatch_paths entirely: does any changed file fall outside the
    # dispatch's ROLE file_write_scope? This is the number that matters most
    # for the flip, because the role scope is the layer that becomes hard.
    role_outside = 0
    role_inside = 0
    role_outside_examples: list[dict] = []
    for did in linked:
        spec = specs[did]
        role = spec.get("role") or ""
        files: set[str] = set()
        for sha in did2shas[did]:
            files |= changed_files(sha)
        if not files:
            continue
        profile = resolve_worker_profile(role)
        outside = [f for f in sorted(files) if not match_file_write_scope(f, profile, None)]
        if outside:
            role_outside += 1
            if len(role_outside_examples) < 20:
                role_outside_examples.append(
                    {"dispatch_id": did, "role": role, "files": outside[:4]}
                )
        else:
            role_inside += 1

    # ---- dispatch-scope-only compliance: outside-declaration rate, under the
    # literal (pre-fix) and dir-aware (post-fix) matchers. Proves the
    # directory-matching fix: a dispatch that declared a bare directory (e.g.
    # ``tests``) and wrote inside it was "outside-declaration" under the literal
    # fnmatch matcher but is "inside" once the directory expands to ``tests/**``.
    dispatch_scope_total = 0
    outside_declaration_literal = 0
    outside_declaration_dir_aware = 0
    dir_fix_rescued: list[str] = []
    for did in linked:
        spec = specs[did]
        declared = write_granting_paths(spec)
        if not declared:
            continue
        files: set[str] = set()
        for sha in did2shas[did]:
            files |= changed_files(sha)
        if not files:
            continue
        dispatch_scope_total += 1
        dir_aware = resolve_dispatch_write_scope(declared)
        literal_out = any(
            not any(fnmatch.fnmatch(f, d) for d in declared) for f in files
        )
        diraware_out = any(
            not any(fnmatch.fnmatch(f, s) for s in dir_aware) for f in files
        )
        if literal_out:
            outside_declaration_literal += 1
        if diraware_out:
            outside_declaration_dir_aware += 1
        if literal_out and not diraware_out:
            dir_fix_rescued.append(did)

    summary = {
        "dispatch_specs_total": len(specs),
        "linked_to_commit": len(linked),
        "unlinked": len(unlinked),
        "linked_with_changed_files": len(linked) - no_changed_files,
        "no_dispatch_paths": no_dispatch_paths,
        "with_dispatch_paths": with_dispatch_paths,
        "would_be_blocked_by_flip": blocked,
        "not_blocked": not_blocked,
        "blocked_no_dispatch_paths__role_scope_only": blocked_no_paths,
        "blocked_with_paths__dispatch_narrowing": blocked_by_dispatch_narrow,
        "blocked_with_paths__also_outside_role_scope": blocked_by_role_scope_with_paths,
        "dispatches_declaring_a_directory_path": dir_declared,
        "role_scope_only__outside": role_outside,
        "role_scope_only__inside": role_inside,
        "dispatch_scope_only__declared_total": dispatch_scope_total,
        "dispatch_scope_only__outside_literal": outside_declaration_literal,
        "dispatch_scope_only__outside_dir_aware": outside_declaration_dir_aware,
        "dispatch_scope_only__dir_fix_rescued": len(dir_fix_rescued),
    }
    print(json.dumps(summary, indent=2))
    print()
    print("== concrete examples: dispatches the flip would block ==")
    for e in blocked_examples:
        files = ", ".join(b["file"] for b in e["blocked"])
        print(f"  {e['dispatch_id']} [{e['role']}] declared={e['declared']!r}")
        for b in e["blocked"]:
            tag = "role-scope-fail" if not b["role_ok"] else "dispatch-narrow-fail"
            print(f"      {b['file']}  <{tag}>")
    print()
    print("== concrete examples: dispatches outside their ROLE scope (dispatch_paths ignored) ==")
    for e in role_outside_examples:
        files = ", ".join(e["files"])
        print(f"  {e['dispatch_id']} [{e['role']}] -> {files}")
    print()
    print("== dispatches the directory-matching fix rescues (outside-declaration under literal, inside under dir-aware) ==")
    for did in dir_fix_rescued:
        print(f"  {did}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
