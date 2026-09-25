#!/usr/bin/env python3
"""t0_rotation_state — the state the T0 context guard and the rotation spawner share.

Three producers/consumers meet here:
  - ``scripts/hooks/t0_context_guard.py`` writes a PENDING marker when a T0 session crosses
    the force threshold, and reads the LATCH to stop nagging once the rotation started;
  - ``scripts/t0_rotate_spawn.sh`` parses the handoff, composes what it types into the new
    window, and writes the LATCH once the successor is up;
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

LATCH TTL. A latch older than ``LATCH_TTL_SECONDS`` no longer counts. The successor kills the
old window within its first turn; an old session that is still alive half an hour after its
rotation started means the successor died during boot, and the guard should re-arm.

HANDOFF CONTRACT (docs/operations/T0_CONTEXT_ROTATION.md): numbered next steps under a
``## Next steps`` (or ``## Volgende stappen``) heading, and — when a /goal was running — a
``## Actief /goal`` section with a ``Directive:`` line and the remaining tasks as a list.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_LIB_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _LIB_DIR.parent
for _p in (str(_LIB_DIR), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

STATE_DIR_ENV = "VNX_T0_ROTATION_STATE_DIR"
RECEIPTS_FILE_ENV = "VNX_T0_ROTATION_RECEIPTS_FILE"
LATCH_TTL_SECONDS = 30 * 60
# The hard limit Claude Code puts on a /goal directive (see the goal-prompting skill).
GOAL_MAX_CHARS = 4000

_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_key(value: str) -> str:
    return _SAFE_KEY_RE.sub("_", value.strip())


def state_dir(project_root: Optional[str] = None) -> Path:
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    from project_root import resolve_central_data_dir, resolve_project_id

    project_id = resolve_project_id(project_root or os.getcwd())
    return resolve_central_data_dir(project_id) / "state" / "t0_rotation"


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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
                  level: str, transcript_path: str) -> bool:
    """Record that ``session_id`` in ``pane`` is rotation-pending. True when this is a
    transition worth a receipt — the first crossing for that session, or a change of band
    (force -> hard) — and False when the marker already said the same."""
    path = pending_path(sdir, pane or "no-pane")
    existing = _read_json(path)
    is_new = not existing or existing.get("session_id") != session_id
    changed = is_new or existing.get("level") != level
    first_seen = _utc_now_iso() if is_new else existing.get("first_seen")
    _write_json_atomic(path, {
        "session_id": session_id,
        "pane": pane,
        "tokens": tokens,
        "level": level,
        "transcript_path": transcript_path,
        "first_seen": first_seen,
        "updated": _utc_now_iso(),
    })
    return changed


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


def write_latch(sdir: Path, *, session_id: Optional[str], pane: str,
                old_window: str, new_window: str) -> List[Path]:
    payload = {
        "session_id": session_id,
        "pane": pane,
        "old_window": old_window,
        "new_window": new_window,
        "started": _utc_now_iso(),
    }
    written: List[Path] = []
    if session_id:
        path = latch_path_for_session(sdir, session_id)
        _write_json_atomic(path, payload)
        written.append(path)
    if pane:
        path = latch_path_for_pane(sdir, pane)
        _write_json_atomic(path, payload)
        written.append(path)
    return written


# ── receipts ─────────────────────────────────────────────────────────────────


def emit_event(trigger: str, *, file: str, **fields: Any) -> bool:
    """Append a ``state_mutation`` receipt for a rotation transition. Best-effort: a receipt
    failure is reported on stderr and never breaks the guard or the rotation."""
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


def kickoff_prompt(*, old_window: str, handoff_path: str, first_step: str, has_goal: bool) -> str:
    prompt = (
        "Je bent de verse T0 na een context-rotatie. Sluit eerst het vorige venster met dit "
        f"commando: tmux kill-window -t {old_window} . Voer daarna de kickoff-skill uit op de "
        f"handoff ({handoff_path}). Pak daarna stap 1 op: {first_step}"
    )
    if has_goal:
        prompt += (" Na deze beurt stuur ik je automatisch het /goal-vervolg uit de handoff;"
                   " start zelf geen nieuwe /goal.")
    return _one_line(prompt)


# ── CLI (used by scripts/t0_rotate_spawn.sh) ─────────────────────────────────


def _cmd_compose(args: argparse.Namespace) -> int:
    handoff_path = Path(args.handoff)
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
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "prompt.txt").write_text(kickoff_prompt(
        old_window=args.old_window, handoff_path=str(handoff_path),
        first_step=handoff.first_step, has_goal=handoff.goal is not None,
    ), encoding="utf-8")
    (out / "first_step.txt").write_text(handoff.first_step, encoding="utf-8")
    if handoff.goal is not None:
        (out / "goal.txt").write_text(goal_followup_text(handoff.goal, str(handoff_path)),
                                      encoding="utf-8")
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
    written = write_latch(sdir, session_id=session_id, pane=args.pane,
                          old_window=args.old_window, new_window=args.new_window)
    emit_event(
        "t0_context_rotation_started",
        file=str(written[0]) if written else str(sdir),
        session_id=session_id, pane=args.pane or None,
        old_window=args.old_window, new_window=args.new_window,
        handoff=args.handoff, goal_followup=bool(args.goal_followup),
    )
    sys.stdout.write(f"{session_id or ''}\n")
    return 0


def _cmd_event(args: argparse.Namespace) -> int:
    fields: Dict[str, Any] = {}
    for pair in args.field or []:
        key, _, value = pair.partition("=")
        if key:
            fields[key] = value
    emit_event(args.trigger, file=args.file, **fields)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="T0 context-rotation shared state")
    sub = parser.add_subparsers(dest="cmd", required=True)

    compose = sub.add_parser("compose", help="parse the handoff, write prompt.txt/goal.txt")
    compose.add_argument("--handoff", required=True)
    compose.add_argument("--old-window", required=True)
    compose.add_argument("--out-dir", required=True)
    compose.set_defaults(func=_cmd_compose)

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
