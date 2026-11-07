#!/usr/bin/env python3
"""kimi PreToolUse hook: deny `git stash` (OI-2587).
stdin: the hook payload from kimi_cli/hooks/runner.py:33; the shell tool is
``Shell`` and its command is ``tool_input.command``. Deny = stdout
hookSpecificOutput ``permissionDecision: "deny"`` with exit 0
(kimi_cli/hooks/runner.py:70-82). Exit 1 = malformed payload, not a deny.
The command is tokenized on shell boundaries and every simple command checked on
its own (a ``;`` cannot walk the guard past refs/stash); a shell ``-c`` body,
``eval``/substitutions and unparseable text mentioning stash are all checked.
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
_OPERATORS = frozenset({";", ";;", "&", "&&", "|", "||", "|&", "(", ")"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_STASH = " ".join(_PATTERNS[0])


def _denied(tokens):
    for index, token in enumerate(tokens):
        rest = tokens[index + 1:]
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in _VALUE_OPTS else rest[1:]
        for exe, sub in _PATTERNS:
            if token.rsplit("/", 1)[-1] == exe and rest[:1] == [sub]:
                return f"{exe} {sub}"
    return None


def _simple_commands(tokens):
    """Every simple command, split on shell operators, markers stripped."""
    commands, simple = [], []
    for token in tokens + [";"]:
        if token not in _OPERATORS:
            while token[:2] == "$(" or token[:1] == "`":
                token = token[2:] if token[:2] == "$(" else token[1:]
            token = token.rstrip(")`")
            if token and token not in {"{", "}"}:
                simple.append(token)
        elif simple:
            commands.append(simple)
            simple = []
    return commands


def _nested(command, depth):
    """The command a nested shell would run, or None (a ``-c`` argument, ``eval``)."""
    for index, token in enumerate(command):
        executable, arguments = token.rsplit("/", 1)[-1], command[index + 1:]
        if executable == "eval":
            return _check(" ".join(arguments), depth)
        if executable not in _SHELLS:
            continue
        for at, argument in enumerate(arguments):
            if argument[:1] == "-" and argument[:2] != "--" and "c" in argument[1:]:
                return _check(arguments[at + 1], depth) if at + 1 < len(arguments) else None
    return None


def _check(command, depth):
    """The denied pattern in ``command`` or None; past the nesting limit, and text
    shlex cannot parse that mentions stash, are denied (fail closed)."""
    if depth > 3:  # past the nesting limit: fail closed
        return _STASH
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return _STASH if "stash" in command else None
    for simple in _simple_commands(tokens):
        pattern = _denied(simple) or _nested(simple, depth + 1)
        if pattern:
            return pattern
    return None


def main(stdin_text):
    try:
        command = json.loads(stdin_text)["tool_input"]["command"]
        if not isinstance(command, str):
            raise TypeError(f"tool_input.command is {type(command).__name__}, not a str")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"kimi_deny_git_stash: malformed hook payload: {exc!r}", file=sys.stderr)
        return 1  # documented non-deny code for an unreadable payload
    pattern = _check(command, 0)
    if pattern:
        print(json.dumps({"hookSpecificOutput": {"permissionDecision": "deny",
            "permissionDecisionReason": f"`{pattern}` is denied in the kimi worker lane "
            "(OI-1447): refs/stash is shared by every dispatch worktree; commit instead."}}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.stdin.read()))
