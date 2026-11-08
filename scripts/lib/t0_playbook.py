#!/usr/bin/env python3
"""T0 playbook source: resolution, pointer block, freshness (ADR-058). Stdlib only.

Sources, first existing file wins: (a) fabric `<scripts root>/../skills`, (b) vnx_home
`$VNX_HOME/skills`, (c) installed `~/.vnx-system/current/skills`, (d) project
`<project>/skills` (degraded), (e) legacy `<project>/.claude/skills` (degraded), all
ending in t0-orchestrator/SKILL.md. bin/vnx calls the CLI at the bottom, so the
resolver exists once and is not mirrored in bash.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atomic_io import atomic_write_text

BEGIN_MARKER = "<!-- VNX:BEGIN T0-PLAYBOOK -->"
END_MARKER = "<!-- VNX:END T0-PLAYBOOK -->"
SKILL_REL = Path("skills") / "t0-orchestrator" / "SKILL.md"
LEGACY_REL = Path(".claude") / SKILL_REL

STATE_OK = "ok"
STATE_STALE_SHA = "stale_sha"
STATE_STALE_SOURCE = "stale_source"
STATE_MISSING = "missing"
STATE_ABSENT = "absent"

EXIT_MISSING = 3
MISSING_LINE = (
    "T0 PLAYBOOK SOURCE MISSING: no playbook in sources (a)-(e); "
    "do not orchestrate from memory"
)

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_FIELD_RE = re.compile(r"^(path|source_id|sha256):[ \t]*(.*?)[ \t]*$")
_DEGRADED_IDS = frozenset({"project", "legacy"})


@dataclass(frozen=True)
class Resolution:
    """Outcome of the walk. `missing` is a value, not an error."""
    state: str
    path: Optional[str]
    source_id: Optional[str]
    sha256: Optional[str]
    degraded: bool
    probed: tuple


@dataclass(frozen=True)
class Pointer:
    """A parsed block. An absent or malformed field is None."""
    path: Optional[str]
    source_id: Optional[str]
    sha256: Optional[str]


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _scripts_root_candidates(vnx_home: Optional[str], home: str) -> list:
    # same candidates and order as the hook's _vnx_try_scripts_root
    cands = [Path(__file__).resolve().parent.parent]
    if vnx_home:
        cands.append(Path(vnx_home) / "scripts")
    cands.append(Path(home) / ".vnx-system" / "current" / "scripts")
    return cands


def _pick_scripts_root(scripts_root, vnx_home, home) -> Optional[Path]:
    if scripts_root is not None:
        return Path(scripts_root)
    for cand in _scripts_root_candidates(vnx_home, home):
        if (cand / "lib" / "vnx_paths.py").is_file():
            return cand
    return None


def _source_steps(project_root: Path, scripts_root, vnx_home, home) -> list:
    root = _pick_scripts_root(scripts_root, vnx_home, home)
    steps = [("fabric", root.parent / SKILL_REL if root else None)]
    steps.append(("vnx_home", Path(vnx_home) / SKILL_REL if vnx_home else None))
    steps.append(("installed", Path(home) / ".vnx-system" / "current" / SKILL_REL))
    steps.append(("project", project_root / SKILL_REL))
    steps.append(("legacy", project_root / LEGACY_REL))
    return steps


def resolve(
    project_root,
    *,
    scripts_root=None,
    vnx_home: Optional[str] = None,
    home: Optional[str] = None,
) -> Resolution:
    """First readable source in order. `scripts_root` overrides the step (a) search;
    `vnx_home` and `home` default to $VNX_HOME and the user's home."""
    vnx_home = vnx_home if vnx_home is not None else (os.environ.get("VNX_HOME") or None)
    home = home if home is not None else str(Path.home())
    probed = []
    for source_id, cand in _source_steps(Path(project_root), scripts_root, vnx_home, home):
        if cand is None:
            probed.append(f"{source_id}: not set")
            continue
        probed.append(f"{source_id}: {cand}")
        if not cand.is_file():
            continue
        real = cand.resolve()
        try:
            digest = sha256_of(real)
        except OSError:  # vnx-silent-except: unreadable source is skipped and stays in probed; missing is reported if none is readable
            continue
        return Resolution(STATE_OK, str(real), source_id, digest,
                          source_id in _DEGRADED_IDS, tuple(probed))
    return Resolution(STATE_MISSING, None, None, None, False, tuple(probed))


def render_block(result: Resolution) -> str:
    """The full marker block for an `ok` resolution; refuses anything else."""
    if result.state != STATE_OK or not (result.path and result.source_id and result.sha256):
        raise ValueError("cannot render a T0-PLAYBOOK block without a resolved source")
    return BEGIN_MARKER + "\n" + _block_body(result) + END_MARKER + "\n"


def _block_body(result: Resolution) -> str:
    return (
        "T0 playbook: read this file at session start. This block is a pointer, not a copy.\n"
        f"path: {result.path}\n"
        f"source_id: {result.source_id}\n"
        f"sha256: {result.sha256}\n"
    )


def parse_block(text: str) -> Optional[Pointer]:
    """First block read back; None without a block. Bad fields come back None."""
    lines = (text or "").splitlines()
    begin = next((i for i, ln in enumerate(lines) if BEGIN_MARKER in ln), None)
    if begin is None:
        return None
    fields: dict = {}
    for ln in lines[begin + 1:]:
        if END_MARKER in ln:
            break
        m = _FIELD_RE.match(ln)
        if m:
            fields[m.group(1)] = None if m.group(1) in fields else (m.group(2) or None)
    else:
        return Pointer(None, None, None)  # no END marker: the block is not trustworthy
    sha = fields.get("sha256")
    return Pointer(fields.get("path"), fields.get("source_id"),
                   sha if sha and _SHA_RE.match(sha) else None)


def pointer_state(pointer: Optional[Pointer], current: Resolution) -> str:
    """Only a pointer that matches today's source AND bytes is `ok`; every doubt is stale."""
    if current.state != STATE_OK:
        return STATE_MISSING
    if pointer is None:
        return STATE_ABSENT
    if not pointer.path or not pointer.source_id:
        return STATE_STALE_SOURCE
    if pointer.path != current.path or pointer.source_id != current.source_id:
        return STATE_STALE_SOURCE
    if pointer.sha256 is None or pointer.sha256 != current.sha256:
        return STATE_STALE_SHA
    try:
        recomputed = sha256_of(Path(pointer.path))
    except OSError:  # vnx-silent-except: an unreadable stored file is stale_sha, the fail-closed answer
        return STATE_STALE_SHA
    return STATE_OK if recomputed == pointer.sha256 else STATE_STALE_SHA


render_pointer, parse_pointer = render_block, parse_block  # ADR-058 names


def _cmd_body(args: argparse.Namespace) -> int:
    result = resolve(args.project_root, vnx_home=args.vnx_home or None)
    if result.state != STATE_OK:
        print(f"[role-sync] {MISSING_LINE}", file=sys.stderr)
        for line in result.probed:
            print(f"[role-sync]   tried {line}", file=sys.stderr)
        return EXIT_MISSING
    atomic_write_text(Path(args.out), _block_body(result))
    print(f"{result.source_id} {result.path}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="T0 playbook source (ADR-058)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    body = sub.add_parser("body", help="write the pointer block body (without markers) to --out")
    body.add_argument("--project-root", required=True)
    body.add_argument("--out", required=True)
    body.add_argument("--vnx-home", default="")
    body.set_defaults(func=_cmd_body)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
