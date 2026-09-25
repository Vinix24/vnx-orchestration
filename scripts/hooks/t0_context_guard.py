#!/usr/bin/env python3
"""T0 context guard — enforce the 500K rotation rule on a T0 orchestrator session.

Operator decision 2026-09-25: at 500K context a T0 MUST rotate to a fresh session. Work in
flight may be finished; nothing new is started. A running /goal carries over to the successor.

Registered on UserPromptSubmit, PreToolUse and Stop. T0 only: any other session (T1-T3,
a dispatch worktree, a headless worker carrying VNX_DISPATCH_ID) is a silent no-op, decided
by the same identity check the T0 rotation safety net uses
(``session_stop_rotation._is_t0_session``) plus a dispatch-worktree cwd check.

Bands (``scripts/lib/t0_context_budget.py``, thresholds from the config registry):

  < WARN              nothing.
  >= WARN   (400K)    UserPromptSubmit adds context: the size, "wind down, prepare the rotation".
  >= FORCE  (500K)    rotation is pending.
                      PreToolUse denies STARTING new work (``BLOCKED_STARTS`` below).
                      UserPromptSubmit refuses a new ``/goal``.
                      Stop blocks the end of the turn once: finish what runs to a boundary,
                      start nothing, then run the rotate flow.
  >= HARD   (600K)    Stop demands the rotation now, work in flight included; the handoff
                      carries that work over.

Loop protection. The Stop block fires at most once per turn: Claude Code sets
``stop_hook_active`` on the stop that follows a Stop-hook continuation, and the guard lets that
one through. Once ``scripts/t0_rotate_spawn.sh`` started the successor it writes a latch for this
session (``scripts/lib/t0_rotation_state.py``); a latched session is never Stop-blocked again,
so the old session can go quiet while the successor kills its window.

Unmeasurable context (no transcript, no usage block) is always "do nothing": the guard never
blocks on missing data.

Output follows the hook contract: silence (exit 0, empty stdout) for a no-op,
``hookSpecificOutput`` for context and PreToolUse decisions, ``{"decision": "block"}`` for the
Stop and UserPromptSubmit blocks, which is those events' documented shape. Exit 0 always.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

_HOOKS_DIR = Path(__file__).resolve().parent
_LIB_DIR = _HOOKS_DIR.parent / "lib"
for _p in (str(_LIB_DIR), str(_HOOKS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import t0_context_budget as budget
import t0_rotation_state as rotation_state
from session_stop_rotation import _is_t0_session

_DISPATCH_WORKTREE_CWD_RE = re.compile(r"(?:^|/)\.vnx-data/worktrees/")

ROTATE_HOW = (
    "Rotatie = de rotate-skill: /build-log wrap (schrijft daily-log/handoff.md met genummerde "
    "next steps en, als er een /goal loopt, een sectie '## Actief /goal'), daarna "
    "scripts/t0_rotate_spawn.sh, dat een verse T0 start die de handoff via /kickoff oppakt."
)


def is_guarded_session(cwd: str, env: Dict[str, str]) -> bool:
    """T0, and not a dispatch worktree. A worker never gets rotation pressure from this hook."""
    if not _is_t0_session(cwd, env):
        return False
    return _DISPATCH_WORKTREE_CWD_RE.search((cwd or "").replace("\\", "/")) is None


# ── what counts as STARTING new work ─────────────────────────────────────────
#
# Kept small and explicit on purpose. Everything not listed stays allowed at >= FORCE, because
# finishing is allowed: merges via pr_merge.py, gates, waiting on CI, closing OI's, reading,
# `vnx dispatch --dry-run`, and the rotation itself.

_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;|\n]")
_VNX_DISPATCH_RE = re.compile(r"(?:^|[\s(`])(?:\S*/)?vnx\s+dispatch(?:-agent)?(?=\s|$)")
_BRIDGE_SCRIPT_RE = re.compile(r"\bpython[0-9.]*\s+(?:-\S+\s+)*\S*dispatch_bridge\.py\b")
_PYTHON_RE = re.compile(r"\bpython[0-9.]*\b")
_BRIDGE_CALL_RE = re.compile(r"\b(?:stage_spec_bundle|bridge_dispatch|deliver_via_door)\s*\(")
_GH_PR_CREATE_RE = re.compile(r"(?:^|[\s(`])gh\s+pr\s+create\b")
_HELP_RE = re.compile(r"(?:^|\s)(?:--help|-h)(?=\s|$)")


def _segments(command: str) -> List[str]:
    return [seg.strip() for seg in _SEGMENT_SPLIT_RE.split(command or "") if seg.strip()]


def _starts_dispatch(command: str) -> bool:
    for seg in _segments(command):
        if _VNX_DISPATCH_RE.search(seg) and "--dry-run" not in seg and not _HELP_RE.search(seg):
            return True
    return False


def _stages_via_bridge(command: str) -> bool:
    for seg in _segments(command):
        if _BRIDGE_SCRIPT_RE.search(seg) and not _HELP_RE.search(seg):
            return True
    # python -c / heredoc calling the staging API directly (the whole command, since a heredoc
    # spans lines). Needs both a python invocation and a call, so grep/cat of the file passes.
    return bool(_PYTHON_RE.search(command or "") and _BRIDGE_CALL_RE.search(command or ""))


def _creates_pr(command: str) -> bool:
    return any(_GH_PR_CREATE_RE.search(seg) for seg in _segments(command))


def _starts_goal(tool_name: str, tool_input: dict) -> bool:
    if tool_name == "Skill":
        return str(tool_input.get("skill") or "").strip().lstrip("/").lower() == "goal"
    if tool_name == "SlashCommand":
        return _is_goal_prompt(str(tool_input.get("command") or ""))
    return False


def _is_goal_prompt(text: str) -> bool:
    stripped = (text or "").lstrip()
    return stripped == "/goal" or stripped.startswith("/goal ")


@dataclass(frozen=True)
class BlockedStart:
    name: str
    why: str
    matches: Callable[[str, dict], bool]


def _bash(predicate: Callable[[str], bool]) -> Callable[[str, dict], bool]:
    return lambda tool, tin: tool == "Bash" and predicate(str(tin.get("command") or ""))


BLOCKED_STARTS: List[BlockedStart] = [
    BlockedStart(
        "vnx dispatch",
        "vuurt een nieuwe worker af (ook `vnx dispatch stage` en `vnx dispatch-agent`); "
        "`--dry-run` blijft toegestaan",
        _bash(_starts_dispatch)),
    BlockedStart(
        "dispatch_bridge staging",
        "zet een nieuwe dispatch klaar in de pending-map, de eerste stap van nieuw werk",
        _bash(_stages_via_bridge)),
    BlockedStart(
        "gh pr create",
        "opent een nieuwe PR die een gate-ronde en een merge vraagt die deze sessie niet meer "
        "afmaakt",
        _bash(_creates_pr)),
    BlockedStart(
        "/goal",
        "start een nieuwe autonome opdracht; een lopende /goal gaat via de handoff mee",
        _starts_goal),
]


def blocked_start(tool_name: str, tool_input: dict) -> Optional[BlockedStart]:
    for rule in BLOCKED_STARTS:
        if rule.matches(tool_name, tool_input or {}):
            return rule
    return None


# ── messages ─────────────────────────────────────────────────────────────────


def _k(tokens: int) -> str:
    return f"{tokens:,}".replace(",", ".")


def _warn_context(tokens: int, th: budget.Thresholds) -> str:
    return (
        f"[VNX T0 context-guard] Context is {_k(tokens)} tokens (waarschuwing vanaf {_k(th.warn)}, "
        f"rotatie verplicht vanaf {_k(th.force)}). Rond af en bereid de rotatie voor: breng "
        f"lopend werk naar een grens en houd de handoff bij. {ROTATE_HOW}"
    )


def _force_context(tokens: int, th: budget.Thresholds, level: str) -> str:
    if level == budget.LEVEL_HARD:
        return (
            f"[VNX T0 context-guard] Context is {_k(tokens)} tokens, boven de harde grens "
            f"{_k(th.hard)}. Roteer NU, ook met werk in de lucht; de handoff draagt dat werk "
            f"over. {ROTATE_HOW}"
        )
    return (
        f"[VNX T0 context-guard] Context is {_k(tokens)} tokens, boven {_k(th.force)}: rotatie "
        f"staat klaar. Maak lopend werk af, start niets nieuws (geen dispatch, geen staging, "
        f"geen gh pr create, geen nieuwe /goal) en roteer op de eerstvolgende grens. {ROTATE_HOW}"
    )


def _deny_reason(rule: BlockedStart, tokens: int, th: budget.Thresholds) -> str:
    return (
        f"[VNX T0 context-guard] Geweigerd: {rule.name} {rule.why}. Context is {_k(tokens)} "
        f"tokens (grens {_k(th.force)}); na die grens start T0 geen nieuw werk meer. Maak lopend "
        f"werk af en roteer; de verse T0 pakt dit op via de handoff. {ROTATE_HOW}"
    )


def _stop_reason(tokens: int, th: budget.Thresholds, level: str) -> str:
    if level == budget.LEVEL_HARD:
        return (
            f"[VNX T0 context-guard] Context is {_k(tokens)} tokens, boven de harde grens "
            f"{_k(th.hard)}. Voer de rotatie NU uit (de rotate-skill), ook als er nog werk loopt: "
            f"zet wat in de lucht is in de handoff onder de genummerde next steps, zodat de verse "
            f"T0 het afmaakt. {ROTATE_HOW}"
        )
    return (
        f"[VNX T0 context-guard] Context is {_k(tokens)} tokens, boven {_k(th.force)}: deze T0 "
        f"moet roteren. Rond af wat nog loopt tot een grens, start niets nieuws, en voer daarna "
        f"de rotatie uit (de rotate-skill). {ROTATE_HOW}"
    )


# ── hook entry ───────────────────────────────────────────────────────────────


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _mark_pending(cwd: str, session_id: str, pane: str, tokens: int, level: str,
                  transcript_path: str) -> bool:
    """Write the pending marker (receipt on a transition). Returns the latch state."""
    try:
        sdir = rotation_state.state_dir(cwd)
        if rotation_state.write_pending(sdir, pane=pane, session_id=session_id, tokens=tokens,
                                        level=level, transcript_path=transcript_path):
            rotation_state.emit_event(
                "t0_context_rotation_pending",
                file=str(rotation_state.pending_path(sdir, pane or "no-pane")),
                session_id=session_id or None, pane=pane or None, tokens=tokens, level=level,
            )
        return rotation_state.is_latched(sdir, session_id=session_id, pane=pane)
    except Exception as exc:  # vnx-silent-except: state trouble must not break the session; reported
        sys.stderr.write(f"t0_context_guard: rotation state unavailable: {exc}\n")
        return False


def handle(payload: dict, env: Dict[str, str]) -> Optional[dict]:
    """The hook's decision for ``payload``; None means stay silent."""
    cwd = str(payload.get("cwd") or os.getcwd())
    if not is_guarded_session(cwd, env):
        return None
    event = str(payload.get("hook_event_name") or "")
    transcript_path = str(payload.get("transcript_path") or "")
    tokens = budget.context_tokens(transcript_path)
    th = budget.thresholds()
    level = budget.level(tokens, th)
    if level == budget.LEVEL_OK or tokens is None:
        return None

    pending = level in (budget.LEVEL_FORCE, budget.LEVEL_HARD)
    session_id = str(payload.get("session_id") or "")
    pane = env.get("TMUX_PANE", "")
    latched = _mark_pending(cwd, session_id, pane, tokens, level, transcript_path) if pending else False

    if event == "UserPromptSubmit":
        if pending and _is_goal_prompt(str(payload.get("prompt") or "")):
            return {"decision": "block",
                    "reason": _deny_reason(BLOCKED_STARTS[-1], tokens, th)}
        text = _force_context(tokens, th, level) if pending else _warn_context(tokens, th)
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                       "additionalContext": text}}

    if not pending:
        return None

    if event == "PreToolUse":
        tool_input = payload.get("tool_input")
        rule = blocked_start(str(payload.get("tool_name") or ""),
                             tool_input if isinstance(tool_input, dict) else {})
        if rule is None:
            return None
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": _deny_reason(rule, tokens, th)}}

    if event == "Stop":
        if latched or payload.get("stop_hook_active"):
            return None
        return {"decision": "block", "reason": _stop_reason(tokens, th, level)}

    return None


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        decision = handle(payload, dict(os.environ))
    except Exception as exc:  # vnx-silent-except: a guard bug must never wedge T0; reported on stderr
        sys.stderr.write(f"t0_context_guard: {exc}\n")
        decision = None
    if decision is not None:
        _emit(decision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
