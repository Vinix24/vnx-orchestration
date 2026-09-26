#!/usr/bin/env python3
"""t0_context_budget — how full is a T0 session's context window, and which rotation band is it in.

Operator decision 2026-09-25: a T0 orchestrator MUST rotate at 500K context. This module is
the measurement half of that rule; ``scripts/hooks/t0_context_guard.py`` is the enforcement
half and ``scripts/t0_rotate_spawn.sh`` the execution half.

MEASUREMENT. Claude Code writes the session transcript as JSONL at the ``transcript_path``
every hook receives. Each ``type: assistant`` line carries ``message.usage`` for the request
that produced it, and the prompt side of that request IS the context the model saw:

    input_tokens + cache_read_input_tokens + cache_creation_input_tokens

(measured by T0 on its own transcript on main 5c358f3e: 426.361). The LAST such line is the
current context size. Output tokens are not counted: they become input on the next request
and show up there.

Lines that do not describe a real request are skipped:
  - ``isSidechain: true`` — a subagent's own context, not T0's;
  - ``message.model == "<synthetic>"`` — harness-written messages (API errors, interrupts)
    that carry an all-zero usage block and would read as an empty context.

An unreadable, empty, or usage-less transcript yields ``None``, never a crash and never 0:
"cannot measure" and "empty context" are different facts and the guard treats the first as
"do nothing".

The transcript is read backwards in blocks, so a multi-megabyte transcript costs one or two
block reads per hook call instead of a full parse.

THRESHOLDS come from the config registry (``scripts/lib/config_registry.py``), so an operator
can move them per project without a code change:

    VNX_T0_ROTATE_WARN_TOKENS   400000  prepare the rotation
    VNX_T0_ROTATE_FORCE_TOKENS  500000  start nothing new, rotate at the next boundary
    VNX_T0_ROTATE_HARD_TOKENS   600000  rotate now, even with work in flight

A value that does not parse as a positive integer falls back to that key's registry default,
loudly on stderr, rather than disabling the band.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Union

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import config_registry

WARN_KEY = "VNX_T0_ROTATE_WARN_TOKENS"
FORCE_KEY = "VNX_T0_ROTATE_FORCE_TOKENS"
HARD_KEY = "VNX_T0_ROTATE_HARD_TOKENS"

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_FORCE = "force"
LEVEL_HARD = "hard"

_BLOCK_SIZE = 64 * 1024
_USAGE_FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


@dataclass(frozen=True)
class Thresholds:
    warn: int
    force: int
    hard: int


def _read_lines_backwards(path: Path) -> Iterator[bytes]:
    """Yield the file's lines last-first, reading fixed-size blocks from the end."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        position = fh.tell()
        remainder = b""
        while position > 0:
            step = min(_BLOCK_SIZE, position)
            position -= step
            fh.seek(position)
            chunk = fh.read(step) + remainder
            lines = chunk.split(b"\n")
            remainder = lines[0]
            for line in reversed(lines[1:]):
                if line.strip():
                    yield line
        if remainder.strip():
            yield remainder


def _usage_total(entry: object) -> Optional[int]:
    """Context size from one transcript entry, or None if it is not a real assistant request."""
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return None
    if entry.get("isSidechain") is True:
        return None
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("model") == "<synthetic>":
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    total = 0
    seen = False
    for field in _USAGE_FIELDS:
        value = usage.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        total += value
        seen = True
    return total if seen else None


def context_tokens(transcript_path: Union[str, Path, None]) -> Optional[int]:
    """Current context size of the session behind ``transcript_path``; None if unmeasurable."""
    if not transcript_path:
        return None
    path = Path(transcript_path).expanduser()
    try:
        for raw in _read_lines_backwards(path):
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            total = _usage_total(entry)
            if total is not None:
                return total
    except OSError:
        return None
    return None


def _threshold(key: str) -> int:
    default = int(config_registry.CONFIG_REGISTRY[key].default)
    raw = config_registry.get(key)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        sys.stderr.write(
            f"t0_context_budget: {key}={raw!r} is not a positive integer; using default {default}\n"
        )
        return default
    return value


def thresholds() -> Thresholds:
    return Thresholds(warn=_threshold(WARN_KEY), force=_threshold(FORCE_KEY), hard=_threshold(HARD_KEY))


def level(tokens: Optional[int], th: Thresholds) -> str:
    """Rotation band for ``tokens``. Unmeasurable (None) is always ``ok``: never act on no data."""
    if tokens is None:
        return LEVEL_OK
    if tokens >= th.hard:
        return LEVEL_HARD
    if tokens >= th.force:
        return LEVEL_FORCE
    if tokens >= th.warn:
        return LEVEL_WARN
    return LEVEL_OK


def main(argv: Optional[list] = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) != 1 or args[0] in ("-h", "--help"):
        sys.stderr.write("usage: t0_context_budget.py <transcript_path>\n")
        return 2
    th = thresholds()
    tokens = context_tokens(args[0])
    sys.stdout.write(json.dumps({
        "tokens": tokens,
        "level": level(tokens, th),
        "warn": th.warn,
        "force": th.force,
        "hard": th.hard,
    }) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
