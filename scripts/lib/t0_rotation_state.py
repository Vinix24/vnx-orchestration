#!/usr/bin/env python3
"""t0_rotation_state — the state the T0 context guard and the rotation spawner share.

Three producers/consumers meet here:
  - ``scripts/hooks/t0_context_guard.py`` writes a PENDING marker when a T0 session crosses
    the force threshold, and reads the LATCH to stop nagging once the rotation started;
  - ``scripts/t0_rotate_spawn.sh`` parses the handoff, composes the successor's start argument
    and prompt file, and writes the LATCH once the successor is up;
  - both log the transition as a ``state_mutation`` receipt.

STATE DIR. ``$VNX_T0_ROTATION_STATE_DIR`` when set (tests, debugging), else
``~/.vnx-data/<project_id>/state/t0_rotation`` — the project's central state dir, the same
root the receipt processor watches. The hook (resolving from the session cwd) and the spawner
(resolving from the project root) land on the same dir because both resolve the project id.

KEYS. The hook knows the Claude ``session_id`` and the tmux pane it runs in (``TMUX_PANE`` is
inherited by the hook process). The spawner runs as a Bash tool call inside the same session,
so it sees the same ``TMUX_PANE`` but not the session id. The pending marker is therefore keyed
by pane and records the session id; the spawner reads it back to write a latch per session id,
and also writes a pane latch so the guard is released even when no pending marker existed.

LATCH TTL. A latch older than ``LATCH_TTL_SECONDS`` no longer counts. The delivery job on the
tmux server closes the old window once the successor's first message is confirmed; an old session
that is still alive half an hour after its rotation started means the successor died during boot
(or the delivery was not confirmed), and the guard should re-arm.

LEDGER FIRST (ADR-005). A transition is a ``state_mutation`` receipt in ``t0_receipts.ndjson``,
the first canonical ledger of the ADR, and that append comes BEFORE the state file it describes
changes: the ledger wins, so a crash between the two may duplicate an event on the retry but can
never lose one. The marker changes only on a transition; a repeat call in the same band writes
nothing at all. The read-decide-write of the marker is one critical section under
``atomic_io.slot_lock``, because parallel tool calls each start their own hook process.

HANDOFF CONTRACT (docs/operations/T0_CONTEXT_ROTATION.md): numbered next steps under a
``## Next steps`` (or ``## Volgende stappen``) heading, and — when a /goal was running — a
``## Actief /goal`` section with a ``Directive:`` line and the remaining tasks as a list.

WRAP PROOF (operator decisions 2026-10-16 a, b). ``/build-log wrap`` does not type its own
proof: ``wrap-start`` (first step of the wrap) records the start time, a random ``start_id`` and
the nugget headings already in the month bucket, ``wrap-finish`` (last step) writes a ``wrap:``
block into the handoff frontmatter with the system clock. ``compose`` refuses with exit 6 unless
that block binds to this wrap: the daily log was written inside the wrap's window, every nugget
heading it lists is in the bucket, and the start record it names was consumed by exactly this
block. Frontmatter is read with a stdlib parser: the skills run this file with whatever python3
is on PATH, which need not have PyYAML.
One wrap-start proves one wrap: a ``wrap-finish`` whose block passes the check writes
``consumed_at`` into the start record, and a second finish without a new start refuses, so a
fresh ``wrapped_at`` can never be stamped on old evidence. ``compose`` reads the start record
and never writes it.

CONSUME, THEN PUBLISH (OI-2116). wrap-finish checks the block before it writes anything, then
consumes the start record (``consumed_at`` = the block's ``wrapped_at``), then publishes the
block (which carries the record's ``start_id``). compose accepts a block only when the record
with that ``start_id`` carries that ``consumed_at``. A crash between the two writes leaves a
consumed start and no block: a failed wrap that the next ``wrap-start`` repairs, never a
reusable start beside a valid proof.

HANDOFF FRONTMATTER (OI-2116). The old ``wrap:`` entry goes as a whole: its key line and every
line up to the next top-level key, blank lines and comments inside it included. The rewritten
frontmatter is parsed again, strictly, before it is written; a result that would not parse is
refused and the handoff stays as it was. compose reads the handoff strictly too. Only the daily
log is read leniently, so one odd line in it never hides its ``handoff: true``.

EMERGENCY PATH (operator decision 2026-10-16 c). ``compose --skip-wrap-reason`` skips the wrap
proof (never the first-step check), needs the predecessor's transcript as a readable JSONL
file, and puts a catch-up wrap at the head of the prompt. The spawner books a
``t0_context_rotation_wrap_skipped`` receipt (``record-skip``) before any window opens;
``skips`` lists them, and ``transcript-digest`` is the bounded read the catch-up wrap uses.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import secrets
import shlex
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_LIB_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _LIB_DIR.parent
for _p in (str(_LIB_DIR), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from atomic_io import atomic_write_json, atomic_write_text, slot_lock

STATE_DIR_ENV = "VNX_T0_ROTATION_STATE_DIR"
RECEIPTS_FILE_ENV = "VNX_T0_ROTATION_RECEIPTS_FILE"
LATCH_TTL_SECONDS = 30 * 60
# The hard limit Claude Code puts on a /goal directive (see the goal-prompting skill).
GOAL_MAX_CHARS = 4000

WRAP_START_FILE = "wrap-start.json"
WRAP_SKIPPED_TRIGGER = "t0_context_rotation_wrap_skipped"
# How far wrapped_at may lie in the future, and how long after wrapped_at the daily log may
# still have been touched: clock jitter and a last save, not a second wrap.
WRAP_TOLERANCE_SECONDS = 60
DEFAULT_MAX_AGE_MINUTES = 15
DIGEST_DEFAULT_MAX_BYTES = 200_000
EXIT_WRAP_PROOF = 6
EXIT_LEDGER_REFUSED = 1
KICKOFF_DONE_TRIGGER = "t0_context_rotation_kickoff_done"
CONTINUATION_FILE = "continuation.json"
KICKOFF_DONE_FILE = "kickoff_done.json"

_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_utc(epoch: float) -> str:
    """Whole seconds, UTC, ``Z``: the form the wrap block carries."""
    return dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: Any) -> Optional[float]:
    """Epoch seconds of an ISO-8601 time that names its zone; None for anything else."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo is not None else None


def _safe_key(value: str) -> str:
    return _SAFE_KEY_RE.sub("_", value.strip())


def state_dir(project_root: Optional[str] = None) -> Path:
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    from project_root import resolve_central_data_dir, resolve_project_id

    project_id = resolve_project_id(project_root or os.getcwd())
    return resolve_central_data_dir(project_id) / "state" / "t0_rotation"


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ── pending marker (hook -> spawner) ─────────────────────────────────────────


def pending_path(sdir: Path, pane: str) -> Path:
    return sdir / f"pending-pane-{_safe_key(pane)}.json"


def write_pending(sdir: Path, *, pane: str, session_id: str, tokens: int,
                  level: str, transcript_path: str,
                  record_transition: Optional[Callable[[], bool]] = None) -> bool:
    """Record that ``session_id`` in ``pane`` is rotation-pending. True when this call is a
    transition and the marker was written: the first crossing for that session, or a change
    of band (force -> hard). False when the marker already said the same, or when the ledger
    refused the transition.

    The marker changes ONLY on a transition. A repeat call in the same band leaves the file
    exactly as it was: no rewrite, no fresh timestamp, no event.

    ``record_transition`` is the ledger append (ADR-005: ledger first). It runs after the
    decision and before the marker changes, inside the marker's lock, so parallel hook
    processes cannot both read "new" and both record it. It returns True when the ledger holds
    the event. On False the marker stays as it was, and the next hook call decides again and
    retries the ledger: a transition the ledger did not take is not recorded as done.
    """
    path = pending_path(sdir, pane or "no-pane")
    with slot_lock(path):
        existing = _read_json(path) or {}
        is_new = not existing or existing.get("session_id") != session_id
        if not is_new and existing.get("level") == level:
            return False
        if record_transition is not None and not record_transition():
            return False
        now = _utc_now_iso()
        atomic_write_json(path, {
            "session_id": session_id,
            "pane": pane,
            "tokens": tokens,
            "level": level,
            "transcript_path": transcript_path,
            "first_seen": now if is_new else existing.get("first_seen"),
            "transitioned": now,
        })
    return True


def pending_session_id(sdir: Path, pane: str) -> Optional[str]:
    data = _read_json(pending_path(sdir, pane)) if pane else None
    value = (data or {}).get("session_id")
    return value if isinstance(value, str) and value else None


# ── latch (spawner -> hook) ──────────────────────────────────────────────────


def latch_path_for_session(sdir: Path, session_id: str) -> Path:
    return sdir / f"latch-session-{_safe_key(session_id)}.json"


def latch_path_for_pane(sdir: Path, pane: str) -> Path:
    return sdir / f"latch-pane-{_safe_key(pane)}.json"


def _fresh(path: Path, now: Optional[float] = None) -> bool:
    try:
        age = (now if now is not None else time.time()) - path.stat().st_mtime
    except OSError:
        return False
    return age <= LATCH_TTL_SECONDS


def is_latched(sdir: Path, *, session_id: str, pane: str) -> bool:
    if session_id and _fresh(latch_path_for_session(sdir, session_id)):
        return True
    return bool(pane) and _fresh(latch_path_for_pane(sdir, pane))


def latch_targets(sdir: Path, *, session_id: Optional[str], pane: str) -> List[Path]:
    """The latch files ``write_latch`` writes for this session/pane, session latch first."""
    targets: List[Path] = []
    if session_id:
        targets.append(latch_path_for_session(sdir, session_id))
    if pane:
        targets.append(latch_path_for_pane(sdir, pane))
    return targets


def write_latch(sdir: Path, *, session_id: Optional[str], pane: str,
                old_window: str, new_window: str) -> List[Path]:
    payload = {
        "session_id": session_id,
        "pane": pane,
        "old_window": old_window,
        "new_window": new_window,
        "started": _utc_now_iso(),
    }
    targets = latch_targets(sdir, session_id=session_id, pane=pane)
    for path in targets:
        atomic_write_json(path, payload)
    return targets


# ── receipts ─────────────────────────────────────────────────────────────────


def emit_event(trigger: str, *, file: str, **fields: Any) -> bool:
    """Append a ``state_mutation`` receipt for a rotation transition: the ADR-005 ledger append
    (``t0_receipts.ndjson``). True when the ledger holds the event (freshly appended, or already
    there under the same idempotency key). A failure is reported on stderr and returned as
    False; it never raises, so the guard and the rotation carry on. What a False means for the
    state file that follows is the caller's call: the pending marker waits for a retry, the
    latch is written anyway."""
    receipt: Dict[str, Any] = {
        "timestamp": _utc_now_iso(),
        "event_type": "state_mutation",
        "receipt_kind": "state_mutation",
        "terminal": "T0",
        "source": "t0_context_rotation",
        "file": file,
        "trigger": trigger,
    }
    receipt.update({k: v for k, v in fields.items() if v is not None})
    try:
        from append_receipt import append_receipt_payload

        append_receipt_payload(
            receipt,
            receipts_file=os.environ.get(RECEIPTS_FILE_ENV) or None,
            skip_enrichment=True,
        )
        return True
    except Exception as exc:  # vnx-silent-except: receipt write is best-effort, reported on stderr
        sys.stderr.write(f"t0_rotation_state: receipt for {trigger} not written: {exc}\n")
        return False


# ── handoff parsing ──────────────────────────────────────────────────────────

_NEXT_STEPS_RE = re.compile(r"^##\s+(next\s+steps|volgende\s+stappen)\b", re.IGNORECASE)
_GOAL_HEADING_RE = re.compile(r"^##\s+actief\s+/goal\b", re.IGNORECASE)
_HEADING_RE = re.compile(r"^#{1,6}\s")
# A section runs until the next level-1 or level-2 heading; ### subsections stay inside it.
_SECTION_END_RE = re.compile(r"^#{1,2}\s")
_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_DIRECTIVE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?(?:directive|directief)(?:\*\*)?\s*:\s*(?:\*\*)?\s*(.*)$",
    re.IGNORECASE,
)
_REMAINING_LABEL_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?(?:resterend(?:e taken)?|remaining(?: tasks)?)(?:\*\*)?\s*:\s*(?:\*\*)?\s*(.*)$",
    re.IGNORECASE,
)
_NO_GOAL_RE = re.compile(r"^\s*(?:geen|none|n\.?v\.?t\.?|-)\s*\.?\s*$", re.IGNORECASE)


@dataclass
class ActiveGoal:
    directive: str
    remaining: List[str] = field(default_factory=list)


@dataclass
class Handoff:
    first_step: Optional[str]
    goal: Optional[ActiveGoal]


def _one_line(text: str) -> str:
    """Collapse all whitespace to single spaces. Everything typed into the pane must be one
    line: a newline in ``send-keys -l`` submits the input box early."""
    return " ".join(text.split())


def _section(lines: List[str], heading_re: "re.Pattern[str]") -> Optional[List[str]]:
    for idx, line in enumerate(lines):
        if heading_re.match(line):
            body: List[str] = []
            for nxt in lines[idx + 1:]:
                if _SECTION_END_RE.match(nxt):
                    break
                body.append(nxt)
            return body
    return None


def _first_item(body: List[str]) -> Optional[str]:
    """The first numbered item (continuation lines joined); a bullet only when no numbered
    item exists at all."""
    for pattern in (_NUMBERED_RE, _BULLET_RE):
        for idx, line in enumerate(body):
            match = pattern.match(line)
            if not match:
                continue
            parts = [match.group(1)]
            for cont in body[idx + 1:]:
                if not cont.strip() or _NUMBERED_RE.match(cont) or _BULLET_RE.match(cont) \
                        or _HEADING_RE.match(cont):
                    break
                parts.append(cont)
            text = _one_line(" ".join(parts))
            if text:
                return text
    return None


def _strip_goal_prefix(text: str) -> str:
    text = text.strip().strip("`").strip()
    if text.lower().startswith("/goal"):
        text = text[len("/goal"):].strip()
    return text


def _parse_goal(body: List[str]) -> Optional[ActiveGoal]:
    meaningful = [line for line in body if line.strip()]
    if not meaningful or (len(meaningful) == 1 and _NO_GOAL_RE.match(meaningful[0])):
        return None
    directive: Optional[str] = None
    remaining: List[str] = []
    in_remaining = False
    for idx, line in enumerate(body):
        if directive is None:
            match = _DIRECTIVE_RE.match(line)
            if match:
                directive = _strip_goal_prefix(match.group(1))
                continue
            if line.strip().strip("`").lower().startswith("/goal "):
                directive = _strip_goal_prefix(line)
                continue
        label = _REMAINING_LABEL_RE.match(line)
        if label:
            in_remaining = True
            inline = label.group(1).strip()
            if inline:
                remaining.extend(p.strip() for p in inline.split(";") if p.strip())
            continue
        if in_remaining:
            item = _NUMBERED_RE.match(line) or _BULLET_RE.match(line)
            if item:
                remaining.append(_one_line(item.group(1)))
    if not directive:
        return None
    return ActiveGoal(directive=_one_line(directive), remaining=remaining)


def parse_handoff(path: Path) -> Handoff:
    lines = path.read_text(encoding="utf-8").splitlines()
    steps = _section(lines, _NEXT_STEPS_RE)
    goal_body = _section(lines, _GOAL_HEADING_RE)
    return Handoff(
        first_step=_first_item(steps) if steps is not None else None,
        goal=_parse_goal(goal_body) if goal_body is not None else None,
    )


def goal_followup_text(goal: ActiveGoal, handoff_path: str) -> str:
    """The /goal line the spawner types into the successor, within Claude Code's limit."""
    base = f"/goal {goal.directive}, hervat na context-rotatie"
    if goal.remaining:
        full = f"{base}; resterend: {'; '.join(goal.remaining)}"
        if len(full) <= GOAL_MAX_CHARS:
            return full
        shortened = f"{base}; resterend: zie ## Actief /goal in {handoff_path}"
        if len(shortened) <= GOAL_MAX_CHARS:
            return shortened
    elif len(base) <= GOAL_MAX_CHARS:
        return base
    return (f"/goal Hervat de /goal uit {handoff_path} (sectie ## Actief /goal) na "
            "context-rotatie en werk de resterende taken daar af.")


def kickoff_prompt(*, handoff_path: str, first_step: str, has_goal: bool,
                   catch_up: Optional[str] = None) -> str:
    """The full instruction the kickoff skill reads from ``prompt.txt``. The first message the
    successor receives is the short ``/kickoff rotatie <run_id>``; closing the old window is the
    delivery job's work, so this text carries no kill command. A skip run puts ``catch_up`` at its
    head, so the catch-up wrap comes before everything else."""
    order = "Voer daarna" if catch_up else "Voer eerst"
    prompt = (
        f"Je bent de verse T0 na een context-rotatie. {order} de kickoff-skill uit op de "
        f"handoff ({handoff_path}). Pak daarna stap 1 op: {first_step}"
    )
    if catch_up:
        prompt = f"{catch_up}. {prompt}"
    if has_goal:
        prompt += (" Na deze beurt stuur ik je automatisch het /goal-vervolg uit de handoff;"
                   " start zelf geen nieuwe /goal.")
    return _one_line(prompt)


# ── frontmatter (stdlib only) ────────────────────────────────────────────────

_FENCE_CLOSE = ("---", "...")
_TOP_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:(.*)$")
_NESTED_KEY_RE = re.compile(r"^[ \t]+([A-Za-z_][A-Za-z0-9_-]*)\s*:(.*)$")
_INT_RE = re.compile(r"^-?\d+$")
_TRAILING_COMMENT_RE = re.compile(r"\s+#.*$")
# The strict structure check. A mapping key as a frontmatter line carries one: plain or quoted,
# then a colon followed by a space or the line end (``url:http`` is a scalar, not a key).
_ANY_KEY_RE = re.compile(
    r"""^(?P<key>"[^"]*"|'[^']*'|[^\s#'"\[\]{},&*!|>%@`-][^#]*?)\s*:(?:\s+(?P<value>.*))?$""")
_BLOCK_SCALAR_RE = re.compile(r"^[|>][1-9+-]*$")
_NODE_PROPERTIES_RE = re.compile(r"^(?:[&!]\S*\s*)+$")
_MAPPING_INDICATOR_RE = re.compile(r":(?:\s|$)")


class FrontmatterError(ValueError):
    """A frontmatter YAML would not read; the message names the line and why."""


def _bad_line(number: int, bare: str, why: str) -> FrontmatterError:
    return FrontmatterError(f"the frontmatter does not parse at line {number} ({bare.strip()!r}): {why}")


@dataclass
class _Entry:
    """A top-level key as the structure check follows it. ``kind``: ``collection`` (no value on
    its line: a list or map follows), ``text`` (a block scalar, or a quoted or flow value still
    open at the line end), ``closed`` (a quoted or flow value that ended on its line) or
    ``plain`` (a plain scalar)."""
    key: str
    kind: str
    child_indent: Optional[int] = None
    child_kind: Optional[str] = None
    compact_open: bool = False


def _closes_on_its_line(text: str) -> bool:
    """Whether the quoted or flow value that opens ``text`` also ends on this line."""
    depth, quote, idx = 0, "", 0
    while idx < len(text):
        char = text[idx]
        if quote:
            if quote == '"' and char == "\\" or quote == "'" and text[idx:idx + 2] == "''":
                idx += 1
            elif char == quote:
                quote = ""
                if depth == 0:
                    return True
        elif char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth <= 0:
                return True
        idx += 1
    return False


def _without_comment(value: Optional[str]) -> str:
    return _TRAILING_COMMENT_RE.sub("", " " + (value or "")).strip()


def _value_kind(value: Optional[str]) -> str:
    text = _without_comment(value)
    if not text or _NODE_PROPERTIES_RE.match(text):
        return "collection"
    if _BLOCK_SCALAR_RE.match(text):
        return "text"
    if text[0] in "\"'[{":
        return "closed" if _closes_on_its_line(text) else "text"
    return "plain"


def _key_kind(number: int, bare: str, match: "re.Match[str]") -> str:
    """The kind of the value on a key line. A plain value may not hold ``: `` or end on a colon:
    YAML reads that as a second key on the line and refuses the frontmatter."""
    kind = _value_kind(match.group("value"))
    if kind == "plain" and _MAPPING_INDICATOR_RE.search(_without_comment(match.group("value"))):
        raise _bad_line(number, bare, f"the value of '{match.group('key')}' holds a colon YAML reads "
                        "as a second key: put the value in quotes")
    return kind


def _line_kind(stripped: str) -> Optional[str]:
    """``item`` for a list item, ``key`` for a mapping key line, None for anything else."""
    if stripped == "-" or stripped.startswith("- "):
        return "item"
    return "key" if _ANY_KEY_RE.match(stripped) else None


def _check_collection_line(entry: _Entry, number: int, bare: str, indent: int, stripped: str) -> None:
    """A line of the list or map below a key with no value on its own line. A key there opens a
    compact list (items at the key's own indentation, as PyYAML writes them) when it has no value
    on its line either."""
    level = entry.child_indent
    if level is not None and indent > level:
        return  # deeper: it belongs to the child above it, which this check does not walk
    if level is not None and indent < level:
        raise _bad_line(number, bare, f"it is indented less than the lines above it under '{entry.key}'")
    kind = _line_kind(stripped)
    if kind is None:
        raise _bad_line(number, bare, f"it is neither a key nor a list item under '{entry.key}'")
    if kind == "key":
        if entry.child_kind == "item":
            raise _bad_line(number, bare, f"a key among the list items under '{entry.key}'")
        entry.compact_open = _key_kind(number, bare, _ANY_KEY_RE.match(stripped)) == "collection"
    elif entry.child_kind == "key" and not entry.compact_open:
        raise _bad_line(number, bare, f"a list item among the keys under '{entry.key}'")
    entry.child_indent = indent
    entry.child_kind = entry.child_kind or kind


def _check_child(entry: Optional[_Entry], number: int, bare: str, indent: int, stripped: str) -> None:
    """An indented line, or a list item at column 0 (YAML's compact list), below ``entry``."""
    if entry is None:
        raise _bad_line(number, bare, "an indented line or list item before the first key")
    if entry.kind == "collection":
        _check_collection_line(entry, number, bare, indent, stripped)
    elif indent == 0:
        raise _bad_line(number, bare, f"a list item under '{entry.key}', which has a value on its line")
    elif entry.kind == "closed":
        raise _bad_line(number, bare, f"an indented line under '{entry.key}', whose value already "
                        "ended on its line")
    elif entry.kind == "plain" and _line_kind(stripped) == "key":
        raise _bad_line(number, bare, f"a key under '{entry.key}', which has a value on its line")


def _check_structure(lines: List[str]) -> None:
    """Raise FrontmatterError at the first line YAML would not read as part of the top-level
    block mapping. One level deep: scalars, block scalars, quoted or flow values that run on,
    and the list or map below a key; what lies deeper is left to the entry above it. Line
    numbers count the opening ``---`` as line 1, as an editor does."""
    entry: Optional[_Entry] = None
    for number, line in enumerate(lines, start=2):
        bare = line.rstrip("\r\n")
        stripped = bare.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(bare) - len(bare.lstrip(" \t"))
        if "\t" in bare[:indent]:
            raise _bad_line(number, bare, "a tab in the indentation, which YAML does not allow")
        if indent == 0 and _line_kind(stripped) != "item":
            top = _ANY_KEY_RE.match(bare)
            if top is None:
                raise _bad_line(number, bare, "a line at column 0 must be a key: value line")
            entry = _Entry(key=top.group("key"), kind=_key_kind(number, bare, top))
            continue
        _check_child(entry, number, bare, indent, stripped)


def _split_frontmatter(text: str) -> Tuple[Optional[List[str]], str]:
    """The frontmatter lines (fences left out, line ends kept) and the body after the closing
    fence; ``(None, text)`` when the text does not open with a ``---`` line. Raises ValueError
    for a frontmatter that opens and never closes."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != "---":
        return None, text
    for idx in range(1, len(lines)):
        if lines[idx].rstrip() in _FENCE_CLOSE:
            return lines[1:idx], "".join(lines[idx + 1:])
    raise ValueError("the frontmatter opens with --- and never closes")


def _parse_scalar(raw: str) -> Any:
    """One scalar as the wrap helper writes it: a JSON string or list, an integer, true/false,
    null, a single-quoted or a plain string. Raises ValueError on a malformed quoted value."""
    value = raw.strip()
    if value[:1] in ('"', "["):
        return json.loads(value)
    if value[:1] == "'":
        if len(value) < 2 or not value.endswith("'"):
            raise ValueError(f"unterminated quoted value {value!r}")
        return value[1:-1].replace("''", "'")
    value = _TRAILING_COMMENT_RE.sub("", value)
    lowered = value.lower()
    if lowered in ("", "null", "~"):
        return None
    if lowered in ("true", "false"):
        return lowered == "true"
    return int(value) if _INT_RE.match(value) else value


def _scalar_or_raw(raw: str) -> Any:
    """A value the parser cannot read stays its raw text, so one odd key in a daily log never
    hides the keys around it. The checks then refuse it by type."""
    try:
        return _parse_scalar(raw)
    except ValueError:
        return raw.strip()


def read_frontmatter(path: Path, *, strict: bool = True) -> Dict[str, Any]:
    """Top-level keys of a markdown file's frontmatter. A key with no inline value gets a dict
    of the ``key: value`` lines indented below it (one level, as the wrap block uses); list
    items and deeper nesting are skipped. Raises OSError or ValueError (unclosed fence).

    ``strict`` (the default) first refuses a frontmatter YAML would not read, with
    FrontmatterError (a ValueError) naming the line. ``strict=False`` reads what it can, so one
    odd line never hides the keys around it: the daily log is read that way."""
    lines, _ = _split_frontmatter(path.read_text(encoding="utf-8"))
    return _parse_frontmatter(lines, strict=strict)


def _parse_frontmatter(lines: Optional[List[str]], *, strict: bool) -> Dict[str, Any]:
    if strict:
        _check_structure(lines or [])
    result: Dict[str, Any] = {}
    current: Optional[Dict[str, Any]] = None
    for line in lines or []:
        bare = line.rstrip("\r\n")
        if not bare.strip() or bare.lstrip().startswith("#"):
            continue
        nested = _NESTED_KEY_RE.match(bare)
        if nested:
            if current is not None:
                current[nested.group(1)] = _scalar_or_raw(nested.group(2))
            continue
        top = _TOP_KEY_RE.match(bare)
        if top is None:
            current = None
        elif top.group(2).strip():
            result[top.group(1)] = _scalar_or_raw(top.group(2))
            current = None
        else:
            current = result[top.group(1)] = {}
    return result


def _opens_top_level_entry(bare: str) -> bool:
    """A column-0 line that starts the next top-level entry. Blank lines, comments and a list
    item at column 0 (YAML's compact list, which belongs to the key above it) do not."""
    return bool(bare) and bare[0] not in " \t#" and not (bare[0] == "-" and bare[1:2] in ("", " "))


def _without_top_key(lines: List[str], key: str) -> List[str]:
    """``lines`` minus every ``key:`` entry: its key line and every line up to the next
    top-level key, blank lines and comments inside it included (OI-2116: stopping at the first
    blank line left the rest of the entry behind without its key)."""
    kept: List[str] = []
    inside = False
    for line in lines:
        bare = line.rstrip("\r\n")
        if _opens_top_level_entry(bare):
            top = _TOP_KEY_RE.match(bare)
            inside = bool(top and top.group(1) == key)
        if not inside:
            kept.append(line)
    return kept


def _render_wrap_block(block: Dict[str, Any]) -> str:
    def quoted(value: Optional[str]) -> str:
        return "null" if value is None else json.dumps(value, ensure_ascii=False)

    return (
        "wrap:\n"
        f"  start_id: {quoted(block['start_id'])}\n"
        f"  started_at: {block['started_at']}\n"
        f"  wrapped_at: {block['wrapped_at']}\n"
        f"  nuggets: {block['nuggets']}\n"
        f"  nugget_headings: {json.dumps(block['nugget_headings'], ensure_ascii=False)}\n"
        f"  daily_log: {quoted(block['daily_log'])}\n"
        f"  bucket: {quoted(block['bucket'])}\n"
    )


def render_wrap_block(text: str, block: Dict[str, Any]) -> str:
    """``text`` with ``block`` under ``wrap:`` in its frontmatter. Every other key keeps its lines
    and its place, an earlier ``wrap:`` entry goes as a whole, and the body stays byte for byte;
    a text without frontmatter gets one. The result is parsed again, strictly, before it is
    returned: FrontmatterError when it would not parse or would not read ``block`` back."""
    lines, body = _split_frontmatter(text)
    kept = _without_top_key(lines or [], "wrap")
    result = "---\n" + "".join(kept) + _render_wrap_block(block) + "---\n" + body
    front, _ = _split_frontmatter(result)
    if _parse_frontmatter(front, strict=True).get("wrap") != block:
        raise FrontmatterError("the rewritten wrap: block does not read back as it was written")
    return result


def write_wrap_block(handoff: Path, block: Dict[str, Any]) -> None:
    """``render_wrap_block`` on the handoff, written atomically: a per-writer mkstemp name next
    to the handoff, then ``os.replace`` (``atomic_io.atomic_write_text``). A result that would
    not parse raises FrontmatterError and leaves the handoff untouched."""
    atomic_write_text(handoff, render_wrap_block(handoff.read_text(encoding="utf-8"), block))


# ── wrap proof: wrap-start / wrap-finish (operator decision 2026-10-16 a) ────

# ``### <date> · <slug> · <title>``, the bucket entry heading of the build-log skill.
_BUCKET_HEADING_RE = re.compile(r"^###\s+\d{4}-\d{2}-\d{2}\s+·\s+(.+?)\s+·\s+\S.*$")


class WrapHelperError(Exception):
    """wrap-start / wrap-finish cannot do their part; the message says what is missing."""


def wrap_start_path(sdir: Path) -> Path:
    return sdir / WRAP_START_FILE


def bucket_headings(bucket: Optional[Path]) -> List[str]:
    """Every nugget heading in the bucket, in file order, duplicates kept. A bucket that does
    not exist yet (the first nugget of a month creates it) has none."""
    if bucket is None:
        return []
    try:
        text = bucket.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [line.rstrip() for line in text.splitlines() if _BUCKET_HEADING_RE.match(line)]


def _heading_slug(heading: str) -> Optional[str]:
    match = _BUCKET_HEADING_RE.match(heading)
    return match.group(1).strip() if match else None


def new_nugget_headings(before: List[str], after: List[str], slug: str) -> List[str]:
    """This slug's headings in ``after`` that ``before`` did not hold, in file order. Counted as
    a multiset: a second nugget under a title that already exists still counts."""
    seen = Counter(before)
    fresh: List[str] = []
    for heading in after:
        if _heading_slug(heading) != slug:
            continue
        if seen[heading] > 0:
            seen[heading] -= 1
            continue
        fresh.append(heading)
    return fresh


def start_wrap(sdir: Path, *, project_root: str, slug: str, bucket: Optional[Path],
               now: Optional[float] = None) -> Dict[str, Any]:
    """Write ``wrap-start.json``: a fresh ``start_id``, the start time, the bucket, and the
    headings in it now. A new record carries no ``consumed_at``, so it proves the next wrap; the
    block that wrap-finish publishes names the ``start_id``, so a newer start retires an older
    block. Written under the record's lock, so it never lands between a finish's read and its
    consumption."""
    record = {
        "start_id": secrets.token_hex(8),
        "started_at": _iso_utc(time.time() if now is None else now),
        "project_root": project_root,
        "project_slug": slug,
        "bucket": str(bucket) if bucket is not None else None,
        "headings": bucket_headings(bucket),
    }
    path = wrap_start_path(sdir)
    with slot_lock(path):
        atomic_write_json(path, record)
    return record


def _relative_to_root(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _unconsumed_start(path: Path) -> Dict[str, Any]:
    """The start record of the running wrap. Raises WrapHelperError when there is none, or when
    a finish already consumed it: one wrap-start proves one wrap."""
    start = _read_json(path)
    if not start or not isinstance(start.get("started_at"), str) or not start.get("project_slug"):
        raise WrapHelperError(f"no usable {path}: run wrap-start first, as the first step of "
                              "the wrap")
    if not isinstance(start.get("start_id"), str) or not start["start_id"]:
        raise WrapHelperError(f"{path} carries no start_id, so no block could bind to it (an "
                              "older fabric wrote it): run wrap-start first")
    if start.get("consumed_at") is not None:
        raise WrapHelperError(f"{path} already proved the wrap that finished at "
                              f"{start['consumed_at']}, and one wrap-start proves one wrap: run "
                              "wrap-start first")
    return start


def _wrap_block_from(start: Dict[str, Any], *, project_root: Path, daily_log: Path,
                     wrapped_at: str) -> Dict[str, Any]:
    bucket = Path(start["bucket"]) if start.get("bucket") else None
    headings = new_nugget_headings([h for h in start.get("headings") or [] if isinstance(h, str)],
                                   bucket_headings(bucket), str(start["project_slug"]))
    return {
        "start_id": start["start_id"],
        "started_at": start["started_at"],
        "wrapped_at": wrapped_at,
        "nuggets": len(headings),
        "nugget_headings": headings,
        "daily_log": _relative_to_root(daily_log, project_root),
        "bucket": str(bucket) if bucket is not None else None,
    }


def _require_wrap_files(handoff: Path, daily_log: Path) -> None:
    if not handoff.is_file():
        raise WrapHelperError(f"handoff not found: {handoff} (the wrap writes it before "
                              "wrap-finish)")
    if not daily_log.is_file():
        raise WrapHelperError(f"daily log not found: {daily_log} (the wrap writes it before "
                              "wrap-finish)")


def _proof_text(handoff: Path, block: Dict[str, Any]) -> str:
    try:
        return render_wrap_block(handoff.read_text(encoding="utf-8"), block)
    except ValueError as exc:
        raise WrapHelperError(f"the handoff {handoff} cannot take the wrap block: {exc}; nothing "
                              "written, the handoff and the start record are unchanged: repair "
                              "the frontmatter and run wrap-finish again") from exc


def _consume_then_publish(path: Path, start: Dict[str, Any], *, handoff: Path,
                          block: Dict[str, Any], text: str) -> None:
    """The two writes of a finish, in the order that makes them one step for compose: the start
    record is consumed by this block first, then the block is published. compose accepts a block
    only when the record it names was consumed by exactly that block, so a crash between the two
    leaves a consumed start and no proof, never a reusable start beside a valid one."""
    try:
        atomic_write_json(path, {**start, "consumed_at": block["wrapped_at"]})
    except OSError as exc:
        raise WrapHelperError(f"cannot consume {path} ({exc}); nothing published, the start "
                              "stays usable: run wrap-finish again") from exc
    try:
        atomic_write_text(handoff, text)
    except OSError as exc:
        raise WrapHelperError(f"{path} is consumed but the handoff {handoff} was not written "
                              f"({exc}), so this wrap has no proof: run wrap-start and finish the "
                              "wrap again") from exc


def finish_wrap(sdir: Path, *, project_root: Path, handoff: Path, daily_log: Path,
                now: Optional[float] = None) -> Dict[str, Any]:
    """Compute this wrap's nuggets from ``wrap-start.json``, check the ``wrap:`` block it makes,
    consume the start record, and publish the block in the handoff.

    Raises WrapHelperError, with nothing written, when the start record is missing or already
    consumed, the handoff or the daily log is missing, or the rewritten handoff would not parse.
    Raises WrapProofError, with nothing written, when the spawner's check refuses the block; the
    start record then stays usable, so the wrap repairs what the check names and finishes again.
    A failed write is a WrapHelperError that says which of the two writes landed.

    One wrap-start proves one wrap: only a finish whose block passes the check writes
    ``consumed_at`` into the start record, so a second finish cannot stamp a fresh ``wrapped_at``
    on old evidence. The whole read-check-write holds the record's lock (``slot_lock``), so a
    second finish, or a wrap-start, cannot land between the read and the consumption."""
    finished = time.time() if now is None else now
    path = wrap_start_path(sdir)
    with slot_lock(path):
        start = _unconsumed_start(path)
        _require_wrap_files(handoff, daily_log)
        block = _wrap_block_from(start, project_root=project_root, daily_log=daily_log,
                                 wrapped_at=_iso_utc(finished))
        text = _proof_text(handoff, block)
        check_wrap_block(block, project_root=project_root,
                         max_age_minutes=DEFAULT_MAX_AGE_MINUTES, now=finished)
        _consume_then_publish(path, start, handoff=handoff, block=block, text=text)
    return block


# ── wrap proof: the check compose runs (operator decision 2026-10-16 b) ──────


class WrapProofError(Exception):
    """One wrap-proof check failed. ``check`` names it, ``detail`` says what was found."""

    def __init__(self, check: str, detail: str):
        super().__init__(f"{check}: {detail}")
        self.check = check
        self.detail = detail

    def refusal(self, handoff: Path) -> str:
        return (f"handoff {handoff} fails wrap proof check '{self.check}': {self.detail}. "
                "The emergency path is t0_rotate_spawn.sh --skip-wrap-reason \"<why>\", recorded "
                "in the ledger; otherwise run /build-log wrap")


def _wrap_block(handoff: Path) -> Dict[str, Any]:
    try:
        block = read_frontmatter(handoff).get("wrap")
    except (OSError, ValueError) as exc:
        raise WrapProofError("wrap_block", f"the frontmatter cannot be read ({exc})") from exc
    if not isinstance(block, dict) or not block:
        raise WrapProofError("wrap_block", "the frontmatter has no wrap: block, so nothing "
                             "proves that /build-log wrap ran")
    return block


def _wrap_window(block: Dict[str, Any], *, now: float, max_age_minutes: int) -> Tuple[float, float]:
    started, wrapped = _parse_utc(block.get("started_at")), _parse_utc(block.get("wrapped_at"))
    if started is None or wrapped is None:
        raise WrapProofError("wrap_times", "started_at and wrapped_at must both be ISO-8601 times "
                             "with a zone, as wrap-start and wrap-finish write them")
    if started > wrapped:
        raise WrapProofError("wrap_order", f"started_at {block['started_at']} lies after "
                             f"wrapped_at {block['wrapped_at']}")
    if wrapped > now + WRAP_TOLERANCE_SECONDS:
        raise WrapProofError("wrapped_at_future", f"wrapped_at {block['wrapped_at']} lies "
                             f"{int(wrapped - now)} s in the future (tolerance "
                             f"{WRAP_TOLERANCE_SECONDS} s)")
    age = now - wrapped
    if age > max_age_minutes * 60:
        raise WrapProofError("wrapped_at_age", f"the wrap finished {int(age // 60)} min ago "
                             f"(limit {max_age_minutes})")
    return started, wrapped


def _check_daily_log(block: Dict[str, Any], *, project_root: Path, started: float,
                     wrapped: float) -> None:
    value = block.get("daily_log")
    if not isinstance(value, str) or not value.strip():
        raise WrapProofError("daily_log", "the block names no daily_log")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_root / path
    try:
        flag = read_frontmatter(path, strict=False).get("handoff")
        mtime = path.stat().st_mtime
    except (OSError, ValueError) as exc:
        raise WrapProofError("daily_log", f"{path} cannot be read ({exc})") from exc
    if flag is not True:
        raise WrapProofError("daily_log_handoff", f"{path} has no 'handoff: true' in its frontmatter")
    if not started <= mtime <= wrapped + WRAP_TOLERANCE_SECONDS:
        raise WrapProofError("daily_log_window", f"{path} was last written at {_iso_utc(mtime)}, "
                             f"outside this wrap's window [{block['started_at']}, "
                             f"{block['wrapped_at']} + {WRAP_TOLERANCE_SECONDS} s]")


def _check_nuggets(block: Dict[str, Any]) -> None:
    count, headings = block.get("nuggets"), block.get("nugget_headings")
    if not isinstance(headings, list) or not all(isinstance(h, str) for h in headings):
        raise WrapProofError("nuggets_count", "nugget_headings is not a list of headings")
    if isinstance(count, bool) or not isinstance(count, int) or count != len(headings):
        raise WrapProofError("nuggets_count", f"nuggets is {count!r} but nugget_headings lists "
                             f"{len(headings)}")
    if not headings:
        return
    bucket = block.get("bucket")
    bucket_path = Path(bucket).expanduser() if isinstance(bucket, str) and bucket else None
    if bucket_path is None or not bucket_path.is_file():
        raise WrapProofError("nugget_in_bucket", f"the block lists {len(headings)} nugget(s) but "
                             f"its bucket {bucket!r} is not a file")
    try:
        present = set(bucket_headings(bucket_path))
    except (OSError, ValueError) as exc:
        raise WrapProofError("nugget_in_bucket", f"{bucket_path} cannot be read ({exc})") from exc
    missing = [h for h in headings if h not in present]
    if missing:
        raise WrapProofError("nugget_in_bucket", f"{len(missing)} heading(s) not in {bucket_path}, "
                             f"first: {missing[0]}")


def _check_start_binding(block: Dict[str, Any], sdir: Path) -> None:
    """The block counts only when the start record it names was consumed by exactly this block:
    the same ``start_id``, and ``consumed_at`` equal to its ``wrapped_at``. wrap-finish consumes
    before it publishes, so no crash leaves a block that passes here beside a reusable start."""
    start_id = block.get("start_id")
    if not isinstance(start_id, str) or not start_id:
        raise WrapProofError("wrap_start_binding", "the block names no start_id, so no wrap-start "
                             "backs it")
    path = wrap_start_path(sdir)
    record = _read_json(path) or {}
    if record.get("start_id") != start_id:
        raise WrapProofError("wrap_start_binding", f"{path} is not the start record this block "
                             f"names ({start_id}): it is gone, or a newer wrap-start replaced it")
    if record.get("consumed_at") != block.get("wrapped_at"):
        raise WrapProofError("wrap_start_binding", f"{path} was not consumed by this block "
                             f"(consumed_at {record.get('consumed_at')!r}, wrapped_at "
                             f"{block.get('wrapped_at')!r}): wrap-finish did not complete")


def check_wrap_block(block: Dict[str, Any], *, project_root: Path, max_age_minutes: int,
                     now: Optional[float] = None) -> None:
    """The checks on the block's content: its window, its daily log, its nuggets. wrap-finish
    runs them on the block it is about to write, compose on the block it reads."""
    started, wrapped = _wrap_window(block, now=time.time() if now is None else now,
                                    max_age_minutes=max_age_minutes)
    _check_daily_log(block, project_root=project_root, started=started, wrapped=wrapped)
    _check_nuggets(block)


def verify_wrap_proof(handoff: Path, *, project_root: Path, sdir: Path, max_age_minutes: int,
                      now: Optional[float] = None) -> Dict[str, Any]:
    """The checks of the plan, in order, and last the binding to the start record in ``sdir``.
    Returns the block; raises WrapProofError naming the first check that fails. The curation
    step of the wrap is not proven."""
    block = _wrap_block(handoff)
    check_wrap_block(block, project_root=project_root, max_age_minutes=max_age_minutes, now=now)
    _check_start_binding(block, sdir)
    return block


# ── emergency path (operator decision 2026-10-16 c) ──────────────────────────


def transcript_problem(path: Path) -> Optional[str]:
    """Why ``path`` is not a readable JSONL transcript, or None when it is one: a regular file
    whose first non-empty line is a JSON object."""
    if not path.is_file():
        return f"{path} is not a file"
    try:
        with path.open("rb") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                try:
                    first = json.loads(raw)
                except ValueError:
                    return f"{path} is not JSONL: its first line is not JSON"
                if not isinstance(first, dict):
                    return f"{path} is not JSONL: its first line is not a JSON object"
                return None
    except OSError as exc:
        return f"{path} cannot be read ({exc})"
    return f"{path} is empty"


def pending_transcript(sdir: Path, pane: str, session_id: Optional[str] = None) -> Optional[str]:
    """The transcript the pending marker of ``pane`` records. With ``session_id``, only a
    marker of that session counts."""
    data = _read_json(pending_path(sdir, pane)) if pane else None
    if not data or (session_id and data.get("session_id") != session_id):
        return None
    value = data.get("transcript_path")
    return value if isinstance(value, str) and value else None


def catch_up_text(*, reason: str, transcript: str) -> str:
    """The head of a skip run's prompt: the catch-up wrap from the predecessor's transcript,
    read only through the bounded digest."""
    quoted = shlex.quote(transcript)
    helper = shlex.quote(str(Path(__file__).resolve()))
    return (
        f"Eerst een inhaal-wrap: deze rotatie sloeg /build-log wrap over (reden: {reason}). "
        f"Draai /build-log wrap --from-transcript {quoted} voordat je iets anders doet. Lees het "
        f"transcript van je voorganger alleen via de digest: python3 {helper} transcript-digest "
        f"--transcript {quoted}"
    )


def ledger_path() -> Path:
    """The ledger ``emit_event`` appends to: the override, else append_receipt's own default."""
    override = os.environ.get(RECEIPTS_FILE_ENV)
    if override:
        return Path(override).expanduser()
    from append_receipt import _resolve_receipts_file

    return _resolve_receipts_file(None).expanduser()


def wrap_skips(ledger: Path, *, since_days: int, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """The ``t0_context_rotation_wrap_skipped`` receipts of the last ``since_days`` days, in
    ledger order. A ledger that does not exist yet holds none; other read errors raise."""
    cutoff = (time.time() if now is None else now) - since_days * 86400
    skips: List[Dict[str, Any]] = []
    try:
        fh = ledger.open(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return skips
    with fh:
        for line in fh:
            if WRAP_SKIPPED_TRIGGER not in line:
                continue
            try:
                receipt = json.loads(line)
            except ValueError:
                continue
            if not isinstance(receipt, dict) or receipt.get("trigger") != WRAP_SKIPPED_TRIGGER:
                continue
            stamp = _parse_utc(receipt.get("timestamp"))
            if stamp is not None and stamp >= cutoff:
                skips.append(receipt)
    return skips


# ── transcript digest: the catch-up wrap's only read (risk T15) ──────────────

_CUT_MARKER = "[... the start of this entry was cut to fit the budget]\n"


def _digest_entry(entry: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """(header, text) of a user or assistant entry. None for everything the digest leaves out:
    tool calls, tool results, thinking, images, attachments, harness-written meta and
    ``<synthetic>`` messages, and sidechains."""
    role = entry.get("type")
    if role not in ("user", "assistant") or entry.get("isSidechain") is True \
            or entry.get("isMeta") is True:
        return None
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("model") == "<synthetic>":
        return None
    content = message.get("content")
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [block["text"] for block in content if isinstance(block, dict)
                 and block.get("type") == "text" and isinstance(block.get("text"), str)]
    else:
        return None
    text = "\n\n".join(part.strip() for part in parts if part.strip())
    if not text:
        return None
    return f"## {role} · {entry.get('timestamp') or '?'}\n", text + "\n\n"


def _post_boundary_entries(path: Path) -> Tuple[List[Tuple[str, str]], bool]:
    """The digest entries after the last ``compact_boundary`` (all of them when there is none),
    and whether a boundary was seen."""
    entries: List[Tuple[str, str]] = []
    boundary = False
    with path.open("rb") as fh:
        for raw in fh:
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary" \
                    and entry.get("isSidechain") is not True:
                entries, boundary = [], True
                continue
            picked = _digest_entry(entry)
            if picked is not None:
                entries.append(picked)
    return entries, boundary


def _fit_newest(entries: List[Tuple[str, str]], max_bytes: int) -> List[str]:
    """The newest entries that fit ``max_bytes`` (UTF-8), oldest first. The entry that no longer
    fits whole keeps its header and the end of its text, so the budget is used up."""
    kept: List[str] = []
    used = 0
    for header, text in reversed(entries):
        block = header + text
        size = len(block.encode("utf-8"))
        if used + size <= max_bytes:
            kept.append(block)
            used += size
            continue
        room = max_bytes - used - len((header + _CUT_MARKER).encode("utf-8"))
        if room > 0:
            tail = text.encode("utf-8")[-room:].decode("utf-8", errors="ignore")
            kept.append(header + _CUT_MARKER + tail)
        break
    kept.reverse()
    return kept


def transcript_digest(path: Path, *, max_bytes: int) -> Tuple[str, Dict[str, Any]]:
    """The user and assistant text after the last ``compact_boundary`` within ``max_bytes``:
    the newest entries win the budget, the text runs oldest first. Raises OSError."""
    entries, boundary = _post_boundary_entries(path)
    blocks = _fit_newest(entries, max_bytes)
    text = "".join(blocks)
    return text, {"entries": len(entries), "kept": len(blocks), "boundary": boundary,
                  "bytes": len(text.encode("utf-8"))}


# ── CLI (used by scripts/t0_rotate_spawn.sh) ─────────────────────────────────


def _proof_state_dir(root: Path) -> Path:
    """The state dir wrap-start and wrap-finish resolve for ``root``, where the start record the
    block names lives. A root whose project cannot be resolved has no start record to bind to."""
    try:
        return state_dir(str(root.expanduser().resolve()))
    except RuntimeError as exc:
        raise WrapProofError("wrap_start_binding", f"the rotation state dir of {root} cannot be "
                             f"resolved ({exc})") from exc


def _wrap_gate(args: argparse.Namespace, handoff_path: Path) -> Tuple[Optional[str], int]:
    """The wrap proof of a normal run, or the transcript check of a skip run. Returns the
    catch-up step for the prompt (skip run only) and an exit code, 0 to go on."""
    if args.skip_wrap_reason is None:
        if args.transcript:
            sys.stderr.write("--transcript only applies with --skip-wrap-reason\n")
            return None, 2
        root = Path(args.project_root) if args.project_root else Path.cwd()
        try:
            verify_wrap_proof(handoff_path, project_root=root, sdir=_proof_state_dir(root),
                              max_age_minutes=args.max_age_minutes)
        except WrapProofError as exc:
            sys.stderr.write(exc.refusal(handoff_path) + "\n")
            return None, EXIT_WRAP_PROOF
        return None, 0
    reason = _one_line(args.skip_wrap_reason)
    if not reason:
        sys.stderr.write("--skip-wrap-reason needs a reason; an empty one is refused\n")
        return None, 2
    if not args.transcript:
        sys.stderr.write("a skip run needs the predecessor's transcript (--transcript) for the "
                         "catch-up wrap; nothing composed\n")
        return None, EXIT_WRAP_PROOF
    problem = transcript_problem(Path(args.transcript))
    if problem:
        sys.stderr.write(f"the catch-up wrap needs a readable JSONL transcript: {problem}; "
                         "nothing composed\n")
        return None, EXIT_WRAP_PROOF
    return catch_up_text(reason=reason, transcript=args.transcript), 0


def _cmd_compose(args: argparse.Namespace) -> int:
    # Absolute: continuation.json is read later from the successor's folder, not from here.
    handoff_path = Path(os.path.abspath(os.path.expanduser(args.handoff)))
    try:
        handoff = parse_handoff(handoff_path)
    except OSError as exc:
        sys.stderr.write(f"handoff unreadable: {handoff_path}: {exc}\n")
        return 3
    if not handoff.first_step:
        sys.stderr.write(
            f"handoff {handoff_path} has no numbered step under '## Next steps' "
            "(or '## Volgende stappen'); refusing to hand over without a first step\n"
        )
        return 4
    catch_up, rc = _wrap_gate(args, handoff_path)
    if rc:
        return rc
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out / "prompt.txt", kickoff_prompt(
        handoff_path=str(handoff_path), first_step=handoff.first_step,
        has_goal=handoff.goal is not None, catch_up=catch_up,
    ))
    atomic_write_text(out / "first_step.txt", handoff.first_step)
    # A re-run into the same run dir (after a catch-up wrap) follows the handoff it reads: a
    # goal that is gone takes its goal.txt with it.
    goal_file = out / "goal.txt"
    if handoff.goal is not None:
        atomic_write_text(goal_file, goal_followup_text(handoff.goal, str(handoff_path)))
    else:
        goal_file.unlink(missing_ok=True)
    atomic_write_json(out / CONTINUATION_FILE, {
        "run_id": out.name, "first_step": handoff.first_step,
        "goal": goal_file.read_text(encoding="utf-8") if handoff.goal is not None else None,
        "handoff": str(handoff_path),
    })
    return 0


def _cmd_wrap_start(args: argparse.Namespace) -> int:
    slug = (args.project_slug or "").strip()
    if not slug or "·" in slug:
        sys.stderr.write(f"--project-slug must be a non-empty slug without '·', got {args.project_slug!r}\n")
        return 2
    root = Path(args.project_root).expanduser().resolve()
    bucket = Path(os.path.abspath(os.path.expanduser(args.bucket))) if args.bucket else None
    sdir = state_dir(str(root))
    try:
        record = start_wrap(sdir, project_root=str(root), slug=slug, bucket=bucket)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"wrap-start: cannot record the start in {sdir}: {exc}\n")
        return 3
    sys.stdout.write(f"wrap-start: {wrap_start_path(sdir)} started_at={record['started_at']} "
                     f"bucket={record['bucket'] or '-'} headings={len(record['headings'])}\n")
    return 0


def _under_root(value: str, root: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def _cmd_wrap_finish(args: argparse.Namespace) -> int:
    root = Path(args.project_root).expanduser().resolve()
    handoff = _under_root(args.handoff, root) if args.handoff else root / "daily-log" / "handoff.md"
    try:
        block = finish_wrap(state_dir(str(root)), project_root=root, handoff=handoff,
                            daily_log=_under_root(args.daily_log, root))
    except WrapProofError as exc:
        # finish_wrap runs the spawner's check before it writes, so the wrap learns about a
        # refusal while it can still repair it (a daily log without `handoff: true`, written
        # before wrap-start).
        sys.stderr.write(f"wrap-finish wrote nothing: the spawner would refuse this proof: "
                         f"check '{exc.check}': {exc.detail}. The start record stays usable: "
                         "repair this and run wrap-finish again\n")
        return EXIT_WRAP_PROOF
    except (WrapHelperError, OSError, ValueError) as exc:
        sys.stderr.write(f"wrap-finish: {exc}\n")
        return 3
    sys.stdout.write(f"wrap-finish: {handoff} wrapped_at={block['wrapped_at']} "
                     f"nuggets={block['nuggets']} daily_log={block['daily_log']}\n")
    return 0


def _cmd_record_skip(args: argparse.Namespace) -> int:
    """Ledger first (ADR-005): the skip receipt lands before any window opens. A ledger that
    refuses it refuses the spawn, so a skip never goes unrecorded (risk T9)."""
    reason = _one_line(args.reason or "")
    if not reason:
        sys.stderr.write("record-skip: an empty reason is refused\n")
        return 2
    sdir = state_dir(args.project_root)
    session_id = args.session_id or pending_session_id(sdir, args.pane) or None
    if not emit_event(
        WRAP_SKIPPED_TRIGGER, file=args.run_dir, reason=reason, transcript=args.transcript,
        handoff=args.handoff, run_dir=args.run_dir, session_id=session_id,
        pane=args.pane or None, old_window=args.old_window,
    ):
        sys.stderr.write(f"record-skip: the ledger did not take {WRAP_SKIPPED_TRIGGER}; an "
                         "unrecorded skip does not spawn\n")
        return EXIT_WRAP_PROOF
    return 0


def _cmd_pending_transcript(args: argparse.Namespace) -> int:
    value = pending_transcript(state_dir(args.project_root), args.pane, args.session_id or None)
    sys.stdout.write(f"{value or ''}\n")
    return 0


def _cmd_skips(args: argparse.Namespace) -> int:
    ledger = ledger_path()
    try:
        skips = wrap_skips(ledger, since_days=args.since)
    except OSError as exc:
        sys.stderr.write(f"skips: ledger {ledger} unreadable: {exc}\n")
        return 3
    for receipt in skips:
        reason = json.dumps(receipt.get("reason") or "", ensure_ascii=False)
        sys.stdout.write(f"{receipt.get('timestamp')}  reason={reason}  "
                         f"transcript={receipt.get('transcript') or '-'}  "
                         f"run_dir={receipt.get('run_dir') or receipt.get('file') or '-'}\n")
    sys.stdout.write(f"{len(skips)} wrap skip(s) in the last {args.since} day(s) (ledger: {ledger})\n")
    return 0


def _cmd_transcript_digest(args: argparse.Namespace) -> int:
    path = Path(args.transcript).expanduser()
    try:
        text, stats = transcript_digest(path, max_bytes=args.max_bytes)
    except OSError as exc:
        sys.stderr.write(f"transcript-digest: transcript unreadable: {path}: {exc}\n")
        return 3
    # Bytes, not text: the budget is counted in UTF-8 whatever the locale of stdout is.
    sys.stdout.flush()
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()
    scope = "after the last compact_boundary" if stats["boundary"] else "from the start (no compact_boundary)"
    sys.stderr.write(f"transcript-digest: {stats['kept']} of {stats['entries']} user/assistant "
                     f"entries {scope}, {stats['bytes']} bytes (budget {args.max_bytes})\n")
    return 0


def _cmd_state_dir(args: argparse.Namespace) -> int:
    sys.stdout.write(f"{state_dir(args.project_root)}\n")
    return 0


def _cmd_latch(args: argparse.Namespace) -> int:
    sdir = state_dir(args.project_root)
    session_id = args.session_id or pending_session_id(sdir, args.pane) or None
    if not session_id:
        sys.stderr.write(
            f"t0_rotation_state: no session id for pane {args.pane!r} (no --session-id, no "
            "pending marker); latching by pane only\n"
        )
    targets = latch_targets(sdir, session_id=session_id, pane=args.pane)
    # Ledger first (ADR-005). The successor is already up when this runs, so a ledger that
    # refuses does not withhold the latch: that would only make the old session nag, and
    # nothing can retry this transition. The failure is reported instead.
    if not emit_event(
        "t0_context_rotation_started",
        file=str(targets[0]) if targets else str(sdir),
        session_id=session_id, pane=args.pane or None,
        old_window=args.old_window, new_window=args.new_window,
        handoff=args.handoff, goal_followup=bool(args.goal_followup),
    ):
        sys.stderr.write(
            "t0_rotation_state: the ledger did not take t0_context_rotation_started; "
            "latching anyway, the successor is already up\n"
        )
    write_latch(sdir, session_id=session_id, pane=args.pane,
                old_window=args.old_window, new_window=args.new_window)
    sys.stdout.write(f"{session_id or ''}\n")
    return 0


def _cmd_run_dir(args: argparse.Namespace) -> int:
    """The absolute run dir of ``run_id``, from the resolved state dir. A run id that names a
    path instead of a run, or a run dir that does not exist, exits 1 and prints nothing."""
    if not args.run_id or _safe_key(args.run_id) != args.run_id or args.run_id in (".", ".."):
        sys.stderr.write(f"run-dir: {args.run_id!r} is not a run id\n")
        return 1
    path = (state_dir(args.project_root) / args.run_id).resolve()
    if not path.is_dir():
        sys.stderr.write(f"run-dir: no run dir {path}\n")
        return 1
    sys.stdout.write(f"{path}\n")
    return 0


def _skip_receipt_time(run_dir: Path) -> Optional[float]:
    """When the wrap-skip receipt of this run was booked: the newest one naming ``run_dir``."""
    stamps = [_parse_utc(r.get("timestamp")) for r in wrap_skips(ledger_path(), since_days=3650)
              if r.get("run_dir") and Path(str(r["run_dir"])).expanduser().resolve() == run_dir]
    known = [stamp for stamp in stamps if stamp is not None]
    return max(known) if known else None


def _catch_up_problem(run_dir: Path) -> Optional[str]:
    """Why the handoff does not prove the catch-up wrap of this skip run, or None when it does:
    the wrap block of the handoff must be newer than the run's skip receipt."""
    continuation = _read_json(run_dir / CONTINUATION_FILE) or {}
    handoff = continuation.get("handoff")
    if not handoff:
        return f"{run_dir / CONTINUATION_FILE} names no handoff"
    skipped_at = _skip_receipt_time(run_dir)
    if skipped_at is None:
        return f"the ledger holds no {WRAP_SKIPPED_TRIGGER} receipt for run dir {run_dir}"
    try:
        wrapped_at = _parse_utc(_wrap_block(Path(handoff)).get("wrapped_at"))
    except WrapProofError as exc:
        return f"the handoff {handoff} carries no readable wrap block ({exc.detail})"
    if wrapped_at is None or wrapped_at <= skipped_at:
        return (f"the wrap in {handoff} is not newer than the skip receipt of this run "
                f"({_iso_utc(skipped_at)}): run the catch-up wrap first")
    return None


def _cmd_kickoff_done(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    if not run_dir.is_dir():
        sys.stderr.write(f"kickoff-done: no run dir {run_dir}\n")
        return 1
    if args.catch_up_done:
        problem = _catch_up_problem(run_dir)
        if problem:
            sys.stderr.write(f"kickoff-done: {problem}; nothing written\n")
            return 3
    marker = run_dir / KICKOFF_DONE_FILE
    # Ledger first (ADR-005): the receipt lands before the marker it describes. A ledger that
    # does not take it leaves no marker and a non-zero exit.
    if not emit_event(KICKOFF_DONE_TRIGGER, file=str(marker), run_id=run_dir.name,
                      catch_up_done=bool(args.catch_up_done)):
        sys.stderr.write(f"kickoff-done: the ledger did not take {KICKOFF_DONE_TRIGGER}; "
                         f"{marker} not written\n")
        return EXIT_LEDGER_REFUSED
    atomic_write_json(marker, {"run_id": run_dir.name, "kickoff_done_at": _utc_now_iso(),
                               "catch_up_done": bool(args.catch_up_done)})
    return 0


def _cmd_event(args: argparse.Namespace) -> int:
    fields: Dict[str, Any] = {}
    for pair in args.field or []:
        key, _, value = pair.partition("=")
        if key:
            fields[key] = value
    if not emit_event(args.trigger, file=args.file, **fields):
        sys.stderr.write(f"event: the ledger did not take {args.trigger}\n")
        return EXIT_LEDGER_REFUSED
    return 0


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {value}")
    return number


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"must be more than 0, got {value}")
    return number


def _add_compose_parser(sub: Any) -> None:
    compose = sub.add_parser("compose", help="check the handoff and its wrap proof, write "
                             "prompt.txt/first_step.txt/goal.txt")
    compose.add_argument("--handoff", required=True)
    compose.add_argument("--out-dir", required=True)
    compose.add_argument("--max-age-minutes", type=_non_negative_int, default=DEFAULT_MAX_AGE_MINUTES,
                         help="oldest wrapped_at that still counts (default %(default)s)")
    compose.add_argument("--project-root", default=None,
                         help="root a relative daily_log resolves against (default: cwd)")
    compose.add_argument("--skip-wrap-reason", default=None,
                         help="emergency path: skip the wrap proof, catch-up wrap first")
    compose.add_argument("--transcript", default=None,
                         help="the predecessor's transcript; required with --skip-wrap-reason")
    compose.set_defaults(func=_cmd_compose)


def _add_wrap_parsers(sub: Any) -> None:
    start = sub.add_parser("wrap-start", help="first step of /build-log wrap: record the start")
    start.add_argument("--project-root", required=True)
    start.add_argument("--project-slug", required=True)
    start.add_argument("--bucket", default=None, help="the month bucket; without it, 0 nuggets")
    start.set_defaults(func=_cmd_wrap_start)

    finish = sub.add_parser("wrap-finish", help="last step of /build-log wrap: write the wrap: block")
    finish.add_argument("--project-root", required=True)
    finish.add_argument("--daily-log", required=True, help="relative paths resolve against the root")
    finish.add_argument("--handoff", default=None, help="default <root>/daily-log/handoff.md")
    finish.set_defaults(func=_cmd_wrap_finish)


def _add_skip_parsers(sub: Any) -> None:
    record = sub.add_parser("record-skip", help="book the t0_context_rotation_wrap_skipped receipt")
    record.add_argument("--project-root", default=None)
    record.add_argument("--pane", default="")
    record.add_argument("--session-id", default="")
    record.add_argument("--reason", required=True)
    record.add_argument("--transcript", required=True)
    record.add_argument("--handoff", required=True)
    record.add_argument("--run-dir", required=True)
    record.add_argument("--old-window", default=None)
    record.set_defaults(func=_cmd_record_skip)

    pending = sub.add_parser("pending-transcript", help="print the pending marker's transcript_path")
    pending.add_argument("--project-root", default=None)
    pending.add_argument("--pane", required=True)
    pending.add_argument("--session-id", default="")
    pending.set_defaults(func=_cmd_pending_transcript)

    skips = sub.add_parser("skips", help="list the wrap-skip receipts")
    skips.add_argument("--since", type=_non_negative_int, default=7, help="days (default %(default)s)")
    skips.set_defaults(func=_cmd_skips)

    digest = sub.add_parser("transcript-digest", help="post-boundary user/assistant text in a budget")
    digest.add_argument("--transcript", required=True)
    digest.add_argument("--max-bytes", type=_positive_int, default=DIGEST_DEFAULT_MAX_BYTES)
    digest.set_defaults(func=_cmd_transcript_digest)


def _add_run_parsers(sub: Any) -> None:
    run = sub.add_parser("run-dir", help="print the absolute run dir of a rotation run")
    run.add_argument("run_id")
    run.add_argument("--project-root", default=None)
    run.set_defaults(func=_cmd_run_dir)

    done = sub.add_parser("kickoff-done", help="mark the kickoff of a run as done (marker + receipt)")
    done.add_argument("--run-dir", required=True)
    done.add_argument("--catch-up-done", action="store_true",
                      help="skip run: first check that the handoff carries a wrap newer than the skip")
    done.set_defaults(func=_cmd_kickoff_done)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="T0 context-rotation shared state")
    sub = parser.add_subparsers(dest="cmd", required=True)

    _add_compose_parser(sub)
    _add_wrap_parsers(sub)
    _add_skip_parsers(sub)
    _add_run_parsers(sub)

    sdir = sub.add_parser("state-dir", help="print the rotation state dir")
    sdir.add_argument("--project-root", default=None)
    sdir.set_defaults(func=_cmd_state_dir)

    latch = sub.add_parser("latch", help="write the rotation latch and its receipt")
    latch.add_argument("--project-root", default=None)
    latch.add_argument("--pane", default="")
    latch.add_argument("--session-id", default="")
    latch.add_argument("--old-window", required=True)
    latch.add_argument("--new-window", required=True)
    latch.add_argument("--handoff", default=None)
    latch.add_argument("--goal-followup", action="store_true")
    latch.set_defaults(func=_cmd_latch)

    event = sub.add_parser("event", help="append one rotation state_mutation receipt")
    event.add_argument("--trigger", required=True)
    event.add_argument("--file", required=True)
    event.add_argument("--field", action="append", help="key=value, repeatable")
    event.set_defaults(func=_cmd_event)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
