#!/usr/bin/env python3
"""kimi PreToolUse hook: deny `git stash` (OI-2587).
stdin: the hook payload from kimi_cli/hooks/runner.py:33; the shell tool is
``Shell`` and its command is ``tool_input.command``. Deny = stdout
hookSpecificOutput ``permissionDecision: "deny"`` with exit 0
(kimi_cli/hooks/runner.py:70-82). Exit 1 = malformed payload, not a deny.
Patterns: worker_permissions.WORKER_SHELL_DENIED_PATTERNS (OI-1447).
"""
import json
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from worker_permissions import WORKER_SHELL_DENIED_PATTERNS  # noqa: E402

_VALUE_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})
_PATTERNS = tuple(tuple(shlex.split(p)) for p in WORKER_SHELL_DENIED_PATTERNS)


def _denied(tokens):
    for index, token in enumerate(tokens):
        rest = tokens[index + 1:]
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in _VALUE_OPTS else rest[1:]
        for exe, sub in _PATTERNS:
            if token.rsplit("/", 1)[-1] == exe and rest[:1] == [sub]:
                return f"{exe} {sub}"
    return None


def main(stdin_text):
    try:
        tokens = shlex.split(json.loads(stdin_text)["tool_input"]["command"])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"kimi_deny_git_stash: malformed hook payload: {exc!r}", file=sys.stderr)
        return 1  # documented non-deny code for an unreadable payload
    pattern = _denied(tokens)
    if pattern:
        print(json.dumps({"hookSpecificOutput": {"permissionDecision": "deny",
            "permissionDecisionReason": f"`{pattern}` is denied in the kimi worker lane "
            "(OI-1447): refs/stash is shared by every dispatch worktree; commit instead."}}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.stdin.read()))
