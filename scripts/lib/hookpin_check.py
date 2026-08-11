#!/usr/bin/env python3
"""Verify every configured Claude Code hook command path resolves to a real file.

OI-1123: a hook pinned to a fabric version/path that no longer exists fails
SILENTLY. Claude Code prints "Stop hook error: ... No such file or directory"
to the pane and nothing else — the hook simply never runs, no report is
written, no receipt lands, and (for a PreToolUse guard) no protection is
applied. Nothing today verifies that a deployed hook path actually resolves;
this module is that check.

Only a hook path that is CONFIGURED in .claude/settings.json is in scope. A
hook event with no entries at all is absent by design — not a defect — and
produces no findings.

Path resolution handles every idiom observed across the live fleet
(vnx-orchestration, mission-control, SEOcrawler_v2, sales-copilot):
  - literal absolute paths                          (mission-control, SEOcrawler, sales-copilot)
  - ``~/...``                                        (home-relative)
  - ``$(git rev-parse --show-toplevel ...)/rel``      (this repo's own settings.json)
  - ``$CLAUDE_PROJECT_DIR/rel`` / ``$PROJECT_ROOT/rel``  (Claude Code / VNX project-root vars)
  - ``${VNX_HOME}/rel`` / ``$VNX_HOME/rel``           (engine-relative)

A token that cannot be deterministically resolved (references an unrecognized
env var, or ``VNX_HOME`` with no candidate base found) is reported as
"unresolved", never "missing" — guessing wrong would produce a false FAIL,
and a check that cries wolf on healthy projects is worse than no check.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_UNRESOLVED = "unresolved"

_GITROOT_SENTINEL = "\x00GITROOT\x00"
_GIT_TOPLEVEL_RE = re.compile(r"\$\(git rev-parse --show-toplevel[^)]*\)")
_PATH_TOKEN_RE = re.compile(
    r"(?:"
    + re.escape(_GITROOT_SENTINEL)
    + r"|\$\{[A-Z_][A-Z0-9_]*\}|\$[A-Z_][A-Z0-9_]*|~|/)"
    r'[^\s"\'\)]*\.(?:sh|py|mjs|js)'
)
_VAR_RE = re.compile(r"^\$\{?([A-Z_][A-Z0-9_]*)\}?(.*)$")

# Vars that mean "this project's own root" — resolved directly, deterministically.
_PROJECT_RELATIVE_VARS = {"CLAUDE_PROJECT_DIR", "PROJECT_ROOT"}

# Observed fleet convention (SEOcrawler_v2): a matcher value engineered to never
# match a real Claude Code tool name is a deliberate off-switch, not a live
# hook. Absent-by-design — skip it, don't check its path. A differently-named
# disable sentinel would not be caught by this; see module docstring.
_DISABLED_MATCHER_MARKERS = ("disabled", "never_match")


def _matcher_is_disabled(matcher: str) -> bool:
    lowered = matcher.lower()
    return any(marker in lowered for marker in _DISABLED_MATCHER_MARKERS)


@dataclass
class HookPinFinding:
    event: str
    matcher: str
    raw_path: str
    resolved_path: Optional[str]
    status: str
    detail: str = ""


def iter_hook_commands(settings: dict) -> Iterator[Tuple[str, str, str]]:
    """Yield (event, matcher, command) for every configured command-type hook."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            matcher = entry.get("matcher", "") or ""
            if _matcher_is_disabled(matcher):
                continue
            sub_hooks = entry.get("hooks", [])
            if not isinstance(sub_hooks, list):
                continue
            for h in sub_hooks:
                if not isinstance(h, dict) or h.get("type") != "command":
                    continue
                command = h.get("command")
                if isinstance(command, str) and command.strip():
                    yield event, matcher, command


def extract_path_tokens(command: str) -> List[str]:
    """Extract candidate script-path tokens from a hook command string, deduped."""
    masked = _GIT_TOPLEVEL_RE.sub(_GITROOT_SENTINEL, command)
    seen: List[str] = []
    for token in _PATH_TOKEN_RE.findall(masked):
        if token not in seen:
            seen.append(token)
    return seen


def _vnx_home_candidates(project_root: Path) -> List[Path]:
    """Best-effort bases for $VNX_HOME, tried in the order a live session would."""
    candidates: List[Path] = []
    env_home = os.environ.get("VNX_HOME")
    if env_home:
        candidates.append(Path(env_home).expanduser())
    current_symlink = Path.home() / ".vnx-system" / "current"
    if current_symlink.exists():
        candidates.append(current_symlink)
    candidates.append(project_root / ".vnx")
    candidates.append(project_root / ".claude" / "vnx-system")
    candidates.append(project_root)  # standalone-dev checkout: VNX_HOME == PROJECT_ROOT
    return candidates


def resolve_token(token: str, project_root: Path) -> Tuple[Optional[Path], str]:
    """Resolve one extracted token to (path, status). Never raises."""
    if token.startswith(_GITROOT_SENTINEL):
        rel = token[len(_GITROOT_SENTINEL):].lstrip("/")
        path = (project_root / rel) if rel else project_root
        return path, (STATUS_OK if path.exists() else STATUS_MISSING)

    if token.startswith("$"):
        m = _VAR_RE.match(token)
        if not m:
            return None, STATUS_UNRESOLVED
        var, rel = m.group(1), m.group(2).lstrip("/")
        if var in _PROJECT_RELATIVE_VARS:
            path = (project_root / rel) if rel else project_root
            return path, (STATUS_OK if path.exists() else STATUS_MISSING)
        if var == "VNX_HOME":
            for base in _vnx_home_candidates(project_root):
                path = (base / rel) if rel else base
                if path.exists():
                    return path, STATUS_OK
            return None, STATUS_UNRESOLVED
        return None, STATUS_UNRESOLVED

    if token.startswith("~"):
        path = Path(os.path.expanduser(token))
        return path, (STATUS_OK if path.exists() else STATUS_MISSING)

    if token.startswith("/"):
        path = Path(token)
        return path, (STATUS_OK if path.exists() else STATUS_MISSING)

    return None, STATUS_UNRESOLVED


def check_project_hook_pins(project_root: Path) -> List[HookPinFinding]:
    """Check every hook path configured in <project_root>/.claude/settings.json.

    Returns an empty list when there is no settings.json or no hooks section —
    that is "absent by design", not a defect, and produces no findings.
    """
    settings_file = project_root / ".claude" / "settings.json"
    if not settings_file.is_file():
        return []

    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [HookPinFinding(
            event="<settings.json>", matcher="", raw_path=str(settings_file),
            resolved_path=None, status=STATUS_UNRESOLVED,
            detail=f"Cannot parse settings.json: {exc}",
        )]

    findings: List[HookPinFinding] = []
    seen = set()
    for event, matcher, command in iter_hook_commands(settings):
        for token in extract_path_tokens(command):
            key = (event, matcher, token)
            if key in seen:
                continue
            seen.add(key)
            resolved, status = resolve_token(token, project_root)
            display = token.replace(_GITROOT_SENTINEL, "$(git rev-parse --show-toplevel)")
            findings.append(HookPinFinding(
                event=event, matcher=matcher, raw_path=display,
                resolved_path=str(resolved) if resolved is not None else None,
                status=status,
            ))
    return findings


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Verify every configured Claude Code hook path resolves to a real file."
    )
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    project_root = Path(args.project_root).expanduser().resolve()
    findings = check_project_hook_pins(project_root)
    missing = [f for f in findings if f.status == STATUS_MISSING]

    if args.json:
        print(json.dumps([asdict(f) for f in findings], indent=2))
        return 1 if missing else 0

    if not findings:
        print(f"[hookpin-check] {project_root}: no hooks configured — nothing to check")
        return 0

    for f in findings:
        icon = {STATUS_OK: "OK  ", STATUS_MISSING: "DEAD", STATUS_UNRESOLVED: "SKIP"}[f.status]
        print(f"  [{icon}] {f.event} ({f.matcher or '*'}): {f.raw_path} -> {f.resolved_path or '?'}")

    if missing:
        print(f"\n{len(missing)} dead hook pin(s) in {project_root}/.claude/settings.json")
    else:
        print(f"\nAll configured hook pins resolve in {project_root}/.claude/settings.json")

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
