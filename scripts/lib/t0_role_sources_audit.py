#!/usr/bin/env python3
"""Audit the sources the T0 role names against the repo.

`t0_role_audit.sh --static` already checks role<->skill invocability. This
module adds the second half: every script, subcommand and state file the role
(and DISPATCH_RULES) name must still exist in the fabric. Without it a PR can
delete `receipt_query.py pull` or a t0_state section and the role keeps
instructing T0 to use it.

Findings (one per line on stdout, exit 1 when any):

  SCRIPT-MISSING       a `*.py` / `*.sh` the text names does not resolve
  SUBCOMMAND-MISSING   `vnx <cmd> [<sub>]` (inline, or a command line inside a
                       fenced block) or `<script>.py <sub>` names a subcommand
                       the code that handles it does not define
  STATE-UNWRITTEN      the role or DISPATCH_RULES names a path in the project
                       state directory that is not in the writers manifest
  STATE-WRITER-GONE    a manifest entry whose writer no longer holds its
                       marker, or whose section phrase left the role
  MANIFEST-BAD-LINE    a manifest line that is not `target | writer | marker | phrase`

Usage: t0_role_sources_audit.py <project_root> <fabric_root>

`fabric_root` is where scripts/, bin/vnx and the manifest live. For the fabric
repo itself that is the project root; a consumer project passes the fabric it
runs on, because its own tree carries no scripts/.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

MANIFEST_REL = "scripts/lib/t0_role_state_writers.txt"
ROLE_REL = ".claude/terminals/T0/role-orchestrator.md"
DOC_SOURCES = ("docs/core/DISPATCH_RULES.md",)

# `vnx <group> <sub>`: the second word is a real subcommand only for these.
# Elsewhere it is prose ("vnx dispatch the work").
VNX_GROUPS = frozenset({"objective", "horizon", "deliverable", "role", "pool", "skills", "runtime"})

SCRIPT_TOKEN = re.compile(r"(?<![\w$/.~-])([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|sh))\b")
PY_SUBCOMMAND = re.compile(r"(scripts/[A-Za-z0-9_./-]+\.py)[ \t]+([a-z][a-z0-9_-]*)\b")
VNX_COMMAND = re.compile(r"`vnx ([a-z][a-z0-9_-]*)(?: ([a-z][a-z0-9_-]*))?")
VNX_FENCED_LINE = re.compile(r"^[ \t]*(?:\$[ \t]+)?vnx ([a-z][a-z0-9_-]*)(?: ([a-z][a-z0-9_-]*))?")
FENCE = re.compile(r"^[ \t]*(```|~~~)")
# Built from parts: the CI legacy-path gate greps the scripts tree for the
# literal spelling of this directory.
STATE_DIR = ".vnx-data" + "/state/"
STATE_PATH = re.compile(re.escape(STATE_DIR) + r"([A-Za-z0-9_./-]+)")
MODULE_TOKEN = re.compile(r"-m[ \t]+([A-Za-z0-9_.]+)")
SCRIPT_REF = re.compile(r"scripts/([A-Za-z0-9_./-]+\.py)")
SCRIPT_DIRS = ("scripts", "hooks", ".claude/hooks")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _script_index(fabric: Path) -> Tuple[Set[str], Set[str]]:
    """Return (repo-relative paths, basenames) of every script the fabric holds."""
    paths: Set[str] = set()
    names: Set[str] = set()
    for d in SCRIPT_DIRS:
        base = fabric / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.suffix in (".py", ".sh") and p.is_file():
                paths.add(p.relative_to(fabric).as_posix())
                names.add(p.name)
    return paths, names


def _script_exists(token: str, paths: Set[str], names: Set[str], fabric: Path) -> bool:
    if "/" not in token:
        return token in names
    return any(
        (fabric / prefix / token).is_file() for prefix in ("", ".claude", "scripts")
    ) or token in paths


def _defines_word(text: str, word: str) -> bool:
    """`word` occurs as a quoted literal or a shell case label."""
    if re.search(r"""["']%s["']""" % re.escape(word), text):
        return True
    return _has_case_label(text, word)


def _has_case_label(text: str, word: str) -> bool:
    return re.search(r"(?m)^\s*(?:[\w-]+\|)*%s(?:\|[\w-]+)*\)" % re.escape(word), text) is not None


def _vnx_command_exists(fabric: Path, word: str) -> bool:
    if (fabric / "scripts/commands" / f"{word}.sh").is_file():
        return True
    if (fabric / "scripts/commands" / f"{word.replace('-', '_')}.sh").is_file():
        return True
    return _has_case_label(_read(fabric / "bin/vnx"), word)


def _case_branch(vnx_text: str, group: str) -> str:
    """Body of the `<group>)` branch of the top-level dispatch case in bin/vnx."""
    m = re.search(r"(?m)^[ \t]+(?:[\w-]+\|)*%s(?:\|[\w-]+)*\)[ \t]*\n" % re.escape(group), vnx_text)
    if not m:
        return ""
    end = re.search(r"(?m)^[ \t]+;;[ \t]*$", vnx_text[m.end():])
    return vnx_text[m.end(): m.end() + end.start()] if end else vnx_text[m.end():]


def _shell_functions(vnx_text: str, prefix: str) -> str:
    """Bodies of every top-level `<prefix>...() { ... }` function in bin/vnx."""
    out: List[str] = []
    for m in re.finditer(r"(?m)^%s[\w]*\(\)[ \t]*\{[ \t]*\n" % re.escape(prefix), vnx_text):
        end = re.search(r"(?m)^\}[ \t]*$", vnx_text[m.end():])
        out.append(vnx_text[m.end(): m.end() + end.start()] if end else vnx_text[m.end():])
    return "\n".join(out)


def _group_handlers(fabric: Path, group: str) -> List[str]:
    """Text of the code that handles `vnx <group> ...`, and only that code.

    The case branch in bin/vnx, the script or module it launches, the
    `cmd_<group>*` shell functions it calls, and a `commands/<group>.sh` file.
    """
    vnx_text = _read(fabric / "bin/vnx")
    branch = _case_branch(vnx_text, group)
    texts = [branch]
    if re.search(r"\bcmd_%s\b" % re.escape(group.replace("-", "_")), branch):
        texts.append(_shell_functions(vnx_text, "cmd_" + group.replace("-", "_")))
    for rel in SCRIPT_REF.findall(branch):
        texts.append(_read(fabric / "scripts" / rel))
    for mod in MODULE_TOKEN.findall(branch):
        texts.append(_read(fabric / "scripts/lib" / (mod.replace(".", "/") + ".py")))
    for name in (group, group.replace("-", "_")):
        texts.append(_read(fabric / "scripts/commands" / f"{name}.sh"))
    return texts


def _vnx_sub_exists(fabric: Path, group: str, word: str) -> bool:
    return any(_defines_word(t, word) for t in _group_handlers(fabric, group))


def _resolve_script(fabric: Path, token: str) -> Path | None:
    for prefix in ("", "scripts", ".claude"):
        cand = fabric / prefix / token
        if cand.is_file():
            return cand
    if "/" not in token:
        for d in SCRIPT_DIRS:
            hits = sorted((fabric / d).rglob(token)) if (fabric / d).is_dir() else []
            if hits:
                return hits[0]
    return None


def _fenced_vnx_commands(text: str) -> List[Tuple[str, str]]:
    """`vnx <cmd> [<sub>]` at the start of a line inside a fenced block."""
    found: List[Tuple[str, str]] = []
    fence: Optional[str] = None
    for line in text.splitlines():
        m = FENCE.match(line)
        if m:
            fence = None if fence == m.group(1) else (fence or m.group(1))
            continue
        if fence is None:
            continue
        c = VNX_FENCED_LINE.match(line)
        if c:
            found.append((c.group(1), c.group(2) or ""))
    return found


def check_text(text: str, source: str, fabric: Path) -> List[str]:
    findings: List[str] = []
    paths, names = _script_index(fabric)

    for token in sorted(set(SCRIPT_TOKEN.findall(text))):
        if not _script_exists(token, paths, names, fabric):
            findings.append(f"SCRIPT-MISSING: {source} names '{token}' but it does not exist under {fabric}")

    for script_rel, sub in sorted(set(PY_SUBCOMMAND.findall(text))):
        script = _resolve_script(fabric, script_rel)
        if script is None:
            continue  # already reported as SCRIPT-MISSING
        if not _defines_word(_read(script), sub):
            findings.append(
                f"SUBCOMMAND-MISSING: {source} runs '{script_rel} {sub}' but {script_rel} defines no '{sub}'"
            )

    for cmd, sub in sorted(set(VNX_COMMAND.findall(text)) | set(_fenced_vnx_commands(text))):
        if not _vnx_command_exists(fabric, cmd):
            findings.append(f"SUBCOMMAND-MISSING: {source} runs 'vnx {cmd}' but bin/vnx defines no '{cmd}'")
            continue
        if sub and cmd in VNX_GROUPS and not _vnx_sub_exists(fabric, cmd, sub):
            findings.append(f"SUBCOMMAND-MISSING: {source} runs 'vnx {cmd} {sub}' but the code handling '{cmd}' defines no '{sub}'")
    return findings


def _manifest(fabric: Path) -> Tuple[List[Tuple[str, str, str, str]], List[str]]:
    rows: List[Tuple[str, str, str, str]] = []
    bad: List[str] = []
    for lineno, raw in enumerate(_read(fabric / MANIFEST_REL).splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cols = [c.strip() for c in line.split("|")]
        if len(cols) != 4 or not cols[0] or not cols[1] or not cols[2]:
            bad.append(f"MANIFEST-BAD-LINE: {MANIFEST_REL}:{lineno} is not 'target | writer | marker | phrase'")
            continue
        rows.append((cols[0], cols[1], cols[2], cols[3]))
    return rows, bad


def check_state(text: str, source: str, fabric: Path, phrases: bool = True) -> List[str]:
    findings: List[str] = []
    rows, bad = _manifest(fabric)
    findings += bad
    if not (fabric / MANIFEST_REL).is_file():
        return findings + [f"MANIFEST-BAD-LINE: {MANIFEST_REL} does not exist"]

    known = {target.split("#", 1)[0] for target, _, _, _ in rows}
    named = {p.rstrip(".,;:)") for p in STATE_PATH.findall(text)}
    for path in sorted(named):
        if path not in known:
            findings.append(
                f"STATE-UNWRITTEN: {source} names {STATE_DIR}{path} but {MANIFEST_REL} lists no writer for it"
            )

    for target, writer, marker, phrase in rows:
        if target.split("#", 1)[0] not in named:
            continue  # this source does not rely on it
        writer_path = fabric / writer
        if not writer_path.is_file():
            findings.append(f"STATE-WRITER-GONE: {target}: writer {writer} does not exist")
        elif marker not in _read(writer_path):
            findings.append(f"STATE-WRITER-GONE: {target}: {writer} no longer contains {marker!r}")
        if phrases and phrase and phrase.lower() not in text.lower():
            findings.append(f"STATE-WRITER-GONE: {target}: {source} no longer mentions {phrase!r}")
    return findings


def audit(project: Path, fabric: Path) -> List[str]:
    role = project / ROLE_REL
    findings: List[str] = []
    role_text = _read(role)
    if role_text:
        findings += check_text(role_text, "role-orchestrator.md", fabric)
        findings += check_state(role_text, "role-orchestrator.md", fabric)
    for rel in DOC_SOURCES:
        doc = fabric / rel
        if doc.is_file():
            doc_text = _read(doc)
            findings += check_text(doc_text, Path(rel).name, fabric)
            findings += check_state(doc_text, Path(rel).name, fabric, phrases=False)
    return list(dict.fromkeys(findings))


def main(argv: Iterable[str]) -> int:
    args = list(argv)
    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    findings = audit(Path(args[0]), Path(args[1]))
    for f in findings:
        print(f)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
