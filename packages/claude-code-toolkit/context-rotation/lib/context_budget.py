"""Context measurement, bands and settings for the context-rotation plugin.

Standard library only (Python 3.9+). Unknown sizes are None, never 0.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, Iterator, List, Optional, Tuple

import regular_file

BLOCK_SIZE = 64 * 1024
SETTINGS_FILE = "context-rotation.json"

DEFAULTS = {
    "enabled": True,
    "window_tokens": 200000,
    "warn_pct": 55,
    "force_pct": 65,
    "hard_pct": 75,
    "auto_clear": True,
    # Read by the context-meter plugin only; here so the guard knows the key.
    "show_meter": True,
    "handover_max_chars": 8000,
    "handover_max_age_minutes": 60,
    "hard_max_denials": 3,
    "archive_keep": 20,
    "min_work_tokens": 20000,
    # A0 rule 6 (pane_screen): a spinner line above the input box, and the prompt of the input
    # line, `❯` and a no-break space; a prompt shown in the history above has a plain space.
    "busy_pattern": r"^\S+ \S+… \(\d+s",
    "prompt_pattern": "^❯ ",
    "ack_timeout_seconds": 20,
    # Tool names, or fnmatch patterns of them, that the force band denies; none by default.
    "force_deny_tools": (),
}
_BOOLS = ("enabled", "auto_clear", "show_meter")
_POSITIVE_INTS = (
    "window_tokens", "handover_max_chars", "handover_max_age_minutes",
    "hard_max_denials", "archive_keep", "ack_timeout_seconds", "min_work_tokens",
)
# UTF-8 takes at most four bytes per character, so every handover the contract accepts can be read.
MAX_HANDOVER_CHARS = regular_file.MAX_READ_BYTES // 4
_MAXIMA = {"handover_max_chars": MAX_HANDOVER_CHARS}
_PATTERNS = ("busy_pattern", "prompt_pattern")
_TOOL_LISTS = ("force_deny_tools",)
_PCTS = ("warn_pct", "force_pct", "hard_pct")
# Absolute thresholds in tokens: all three set to positive integers in ascending order, else percentage mode.
_TOKENS = ("warn_tokens", "force_tokens", "hard_tokens")
_THRESHOLD_NAMES = ("warn", "force", "hard")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _warn(message: str) -> None:
    print("context-rotation: " + message, file=sys.stderr)


# --- measurement ---------------------------------------------------------

def reversed_lines(path: str, end: Optional[int] = None) -> Iterator[bytes]:
    """Yield the lines of a file from the last to the first, reading 64 KiB blocks.

    With `end`, reading starts at that byte offset instead of the end of the file.
    Only a regular file is read (a FIFO or device raises OSError).
    """
    with regular_file.open_regular(path) as handle:
        size = handle.seek(0, os.SEEK_END)
        position = size if end is None else max(0, min(end, size))
        carry = b""
        while position > 0:
            step = min(BLOCK_SIZE, position)
            position -= step
            handle.seek(position)
            pieces = (handle.read(step) + carry).split(b"\n")
            carry = pieces[0]
            for line in reversed(pieces[1:]):
                yield line
        yield carry


def _usage_total(usage: Any) -> Optional[int]:
    if not isinstance(usage, dict):
        return None
    fields = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    present = [usage[f] for f in fields if _is_int(usage.get(f))]
    return sum(present) if present else None


def _decide(entry: Any) -> Optional[Dict[str, Optional[int]]]:
    """Return {"size": n_or_None} when the entry decides the measurement, else None."""
    if not isinstance(entry, dict):
        return None
    if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
        meta = entry.get("compactMetadata")
        post = meta.get("postTokens") if isinstance(meta, dict) else None
        return {"size": post if _is_int(post) else None}
    message = entry.get("message")
    if entry.get("type") != "assistant" or not isinstance(message, dict):
        return None
    if entry.get("isSidechain") is True or message.get("model") == "<synthetic>":
        return None
    total = _usage_total(message.get("usage"))
    return None if total is None else {"size": total}


def measure(transcript_path: Optional[str]) -> Optional[int]:
    """Size of the session's context in tokens, or None when it cannot be told."""
    if not transcript_path or not isinstance(transcript_path, str):
        return None
    try:
        for raw in reversed_lines(transcript_path):
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            decided = _decide(entry)
            if decided is not None:
                return decided["size"]
    except OSError:
        return None
    return None


# --- settings ------------------------------------------------------------

def settings_paths(project_dir: Optional[str], home: Optional[str] = None) -> List[str]:
    """The settings files load_settings reads, in its order: the project's, then the user's in the home directory."""
    user_home = home if home is not None else os.path.expanduser("~")
    return [os.path.join(directory, ".claude", SETTINGS_FILE) for directory in (project_dir, user_home) if directory]


def _read_settings_file(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        _warn("cannot read %s (%s), using defaults" % (path, exc))
        return {}
    if not isinstance(data, dict):
        _warn("%s is not a JSON object, using defaults" % path)
        return {}
    return data


def _valid_value(key: str, value: Any) -> bool:
    if key in _BOOLS:
        return isinstance(value, bool)
    if key in _POSITIVE_INTS:
        return _is_int(value) and 0 < value <= _MAXIMA.get(key, value)
    if key in _TOOL_LISTS:
        return isinstance(value, list) and all(isinstance(item, str) and item and "\0" not in item for item in value)
    if key in _PATTERNS:
        if not isinstance(value, str):
            return False
        try:
            re.compile(value)
        except re.error:
            return False
        return True
    return True


def _valid_triple(warn: Any, force: Any, hard: Any) -> bool:
    if not all(_is_number(v) for v in (warn, force, hard)):
        return False
    return 0 < warn < force < hard <= 100


def _apply(raw: Dict[str, Any]) -> Dict[str, Any]:
    settings = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in _PCTS or key not in raw:
            continue
        if _valid_value(key, raw[key]):
            settings[key] = tuple(raw[key]) if key in _TOOL_LISTS else raw[key]
        else:
            _warn("bad value for %s, using the default" % key)
    if any(k in raw for k in _PCTS):
        triple = [raw.get(k, DEFAULTS[k]) for k in _PCTS]
        if _valid_triple(*triple):
            settings.update(zip(_PCTS, triple))
        else:
            _warn("warn_pct/force_pct/hard_pct must satisfy 0 < warn < force < hard <= 100, using the defaults")
    if any(k in raw for k in _TOKENS):
        triple = [raw.get(k) for k in _TOKENS]
        if all(_is_int(v) and v > 0 for v in triple) and triple[0] < triple[1] < triple[2]:
            settings.update(zip(_TOKENS, triple))
        else:
            _warn("warn_tokens/force_tokens/hard_tokens must all be set to positive integers with "
                  "0 < warn < force < hard, using the percentages")
    return settings


def load_settings(project_dir: Optional[str], home: Optional[str] = None) -> Dict[str, Any]:
    """Project file, else user file, else defaults (settings_paths). Bad keys fall back one by one."""
    for path in settings_paths(project_dir, home):
        raw = _read_settings_file(path)
        if raw is not None:
            return _apply(raw)
    return dict(DEFAULTS)


def thresholds(settings: Dict[str, Any]) -> Tuple[float, float, float]:
    """The warn, force and hard thresholds in tokens, as (warn, force, hard).

    The absolute values when all three are set (and thus valid), else the share of
    `window_tokens` the percentages give. When both are configured and valid the
    absolute values win: this is the one place percentages become tokens.
    """
    if all(key in settings for key in _TOKENS):
        return (settings["warn_tokens"], settings["force_tokens"], settings["hard_tokens"])
    window = settings["window_tokens"]
    return (window * settings["warn_pct"] / 100,
            window * settings["force_pct"] / 100,
            window * settings["hard_pct"] / 100)


def threshold_for(settings: Dict[str, Any], name: str) -> float:
    """The threshold of one band (warn, force or hard) in tokens."""
    warn, force, hard = thresholds(settings)
    return {"warn": warn, "force": force, "hard": hard}[name]


def token_text(number: float) -> str:
    """A token count as a message shows it: a whole number without a fraction."""
    whole = int(number)
    return str(whole) if whole == number else str(number)


def band(size: Optional[int], settings: Dict[str, Any]) -> Optional[str]:
    """below, warn, force or hard for a measured size. None for an unknown size."""
    if size is None:
        return None
    warn, force, hard = thresholds(settings)
    for name, threshold in (("hard", hard), ("force", force), ("warn", warn)):
        if size >= threshold:
            return name
    return "below"
