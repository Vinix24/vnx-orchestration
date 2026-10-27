#!/usr/bin/env python3
"""F39 Context Assembler — builds the full prompt that headless T0 needs.

Usage:
    python3 scripts/f39/replay_context_assembler.py \\
        --state $VNX_STATE_DIR/t0_state.json \\
        --receipt <json-string-or-file> \\
        [--state-dir $VNX_STATE_DIR] \\
        [--output prompt.txt]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_LIB = _REPO_ROOT / "scripts" / "lib"
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

import tracks as tracks_db  # noqa: E402
from project_scope import current_project_id  # noqa: E402

# Default paths (resolved relative to repo root)
_DEFAULT_SKILL = _REPO_ROOT / ".claude" / "skills" / "t0-orchestrator" / "SKILL.md"
_DEFAULT_CLAUDE_MD = _REPO_ROOT / ".claude" / "terminals" / "T0" / "CLAUDE.md"
_DEFAULT_STATE = _REPO_ROOT / ".vnx-data" / "state" / "t0_state.json"

# JSON decision schema T0 must emit
_DECISION_SCHEMA = """{
  "decision": "DISPATCH" | "COMPLETE" | "WAIT" | "REJECT" | "ESCALATE",
  "reason": "<concise explanation>",
  "receipt_valid": true | false,
  "next_action": "<concrete next step>",
  "dispatch_target": "T1" | "T2" | "T3" | null,
  "dispatch_task": "<what the target should do>" | null,
  "open_items_action": [
    {"id": "<OI-xxx or 'new'>", "action": "close" | "defer" | "create" | "wontfix", "reason": "<...>"}
  ]
}"""

_OUTPUT_INSTRUCTION = f"""\
---
## Your Task

Process the receipt above. Apply these 5 rules in order:

1. **risk ≤ 0.3 + success + work pending** → DISPATCH next task
   *(why: clean work should proceed without delay)*
   **EXCEPTION:** If the receipt claims specific file changes (note mentions file edits/commits, or files_modified is non-empty), you MUST verify at least one claim against git_context before dispatching — even at risk 0. If the git log shows no matching new commit, the claim is unverified → REJECT instead.

2. **risk 0.3–0.8** → DISPATCH follow-up audit to T3
   *(why: elevated risk needs a second look before proceeding)*

3a. **risk > 0.8 AND (status=failure OR blocking_count > 0)** → REJECT
    *(why: hard failures must be re-dispatched, not carried forward)*
3b. **risk > 0.8 AND status=success** → ESCALATE (contradictory signals)
    *(why: clean surface + high risk advisory is a contradiction — human judgment needed)*

4. **architectural change OR new dependency OR policy violation** → ESCALATE
   *(why: these require human judgment, not autonomous decisions)*

5. **default with no pending work AND all required review gates have results** → COMPLETE
   *(why: when nothing remains to dispatch, close the PR)*
   **EXCEPTION:** If any review gate (codex_gate, kimi_gate, ci_status) is absent (null) or in status "requested"/"queued", → WAIT instead of COMPLETE. Gate discipline is mandatory.

**HARD CONSTRAINTS (enforced by code before you are invoked — you will never see these):**
- Ghost/duplicate receipts → WAIT (auto-handled)
- Required review gates not complete → WAIT (hard block)
- All terminals busy → WAIT (auto-checked)
- Unmet PR dependencies (pr_progress.blocked non-empty) → WAIT (auto-blocked)
- Worker failure with retry budget remaining (retry_count < 3) → DISPATCH (auto-retry)
- CI failure in receipt (ci_status=failure + ci_failure_check) → DISPATCH (auto-fix)
- Feature complete (100% + no blockers + all gates passed) → COMPLETE (auto-detected)

**You handle ONLY soft decisions:**
1. Quality judgment: "Is the work good enough to proceed?"
2. Escalation: "Does a human need to see this?"
3. Planning: "What task should be dispatched next?"
4. Scope: "Did the worker do something unexpected?"

Output ONLY a valid JSON object (no markdown, no prose before or after):
{_DECISION_SCHEMA}
"""


def _read_text(path: Path) -> str:
    """Read a file, returning empty string on error."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f"[FILE NOT FOUND: {path}]"


def _truncate(text: str, max_chars: int, label: str) -> str:
    """Truncate text if it exceeds max_chars, adding a note."""
    if len(text) <= max_chars:
        return text
    kept = text[:max_chars]
    return f"{kept}\n\n[... truncated {label} — {len(text) - max_chars} chars omitted ...]"


def _track_goal_section(receipt: dict[str, Any], state_dir: Path | None) -> str | None:
    """SECTION 4: the dispatch track's goal from the tracks DB (ADR-047).

    The tracks DB is the only plan source, so the prompt carries the goal of
    the track the receipt names instead of an excerpt of FEATURE_PLAN.md.
    ``state_dir`` is the store to read; a caller that has none (the replay
    harness assembles its prompt from a scenario fixture, not from a live
    state dir) gets no section at all — an invented plan would be worse than
    no plan. The same holds when the receipt names no track, the track is not
    in the DB, or it carries no goal: the section is left out rather than
    filled with a placeholder that reads like planning truth.
    """
    if state_dir is None:
        return None
    track_id = str(receipt.get("track") or "").strip()
    if not track_id or not (Path(state_dir) / tracks_db.DB_FILENAME).exists():
        return None
    try:
        track = tracks_db.get_track(Path(state_dir), track_id, current_project_id())
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        # An unreadable store is named in the prompt, never silently skipped:
        # T0 sees that its planning context is missing and why.
        return f"# SECTION 4: Track Goal\n\n[TRACKS DB UNREADABLE: {exc}]"
    if not track:
        return None
    goal = str(track.get("goal_state") or "").strip()
    if not goal:
        return None
    return (
        f"# SECTION 4: Track Goal (tracks DB) — {track.get('track_id')}\n\n"
        f"Phase: {track.get('phase')}\n\n"
        + _truncate(goal, 3000, "track goal")
    )


def assemble_t0_context(
    state_path: Path,
    receipt: dict[str, Any],
    state_dir: Path | None = None,
    skill_path: Path = _DEFAULT_SKILL,
    claude_md_path: Path = _DEFAULT_CLAUDE_MD,
) -> str:
    """Assemble complete T0 orchestrator context for headless execution.

    Returns a single prompt string containing:
    1. T0 CLAUDE.md instructions
    2. T0 orchestrator skill
    3. Current state snapshot (from t0_state.json)
    4. The dispatch track's goal from the tracks DB (only when one resolves)
    5. The receipt to process
    6. Explicit instruction: output decision as JSON

    ADR-047: there is no plan file to excerpt any more. Section 4 is the
    track's goal from the tracks DB, and it is absent when the caller has no
    ``state_dir`` or the track cannot be resolved.
    """
    sections: list[str] = []

    # 1. T0 CLAUDE.md
    claude_md = _read_text(claude_md_path)
    sections.append("# SECTION 1: T0 Terminal Instructions (CLAUDE.md)\n\n" + claude_md)

    # 2. T0 Orchestrator Skill
    skill_md = _read_text(skill_path)
    sections.append("# SECTION 2: T0 Orchestrator Skill\n\n" + skill_md)

    # 3. Current state snapshot
    try:
        state_text = state_path.read_text(encoding="utf-8", errors="replace")
        state_obj = json.loads(state_text)
        # Pretty-print for readability, capped at 8000 chars
        state_pretty = json.dumps(state_obj, indent=2)
    except Exception as exc:
        state_pretty = f"[STATE LOAD ERROR: {exc}]"
    sections.append(
        "# SECTION 3: Current System State (t0_state.json)\n\n"
        "```json\n"
        + _truncate(state_pretty, 8000, "state snapshot")
        + "\n```"
    )

    # 4. Track goal (tracks DB). Omitted when it does not resolve; the later
    # section numbers stay fixed so the chain replay's SECTION 6 keeps its name.
    track_section = _track_goal_section(receipt, state_dir)
    if track_section is not None:
        sections.append(track_section)

    # 5. Receipt to process
    receipt_json = json.dumps(receipt, indent=2)
    sections.append(
        "# SECTION 5: Receipt to Process\n\n"
        "```json\n"
        + receipt_json
        + "\n```"
    )

    # 6. Decision instruction
    sections.append(_OUTPUT_INSTRUCTION)

    return "\n\n".join(sections)


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble headless T0 context prompt")
    parser.add_argument("--state", default=str(_DEFAULT_STATE), help="Path to t0_state.json")
    parser.add_argument("--receipt", required=True, help="Receipt JSON string or path to JSON file")
    parser.add_argument(
        "--state-dir",
        default=None,
        help="State dir holding the tracks DB (defaults to the parent directory of --state)",
    )
    parser.add_argument("--skill", default=str(_DEFAULT_SKILL))
    parser.add_argument("--claude-md", default=str(_DEFAULT_CLAUDE_MD))
    parser.add_argument("--output", default=None, help="Write prompt to file instead of stdout")
    args = parser.parse_args()

    # Parse receipt: file path or inline JSON
    receipt_raw = args.receipt
    receipt_path = Path(receipt_raw)
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    else:
        receipt = json.loads(receipt_raw)

    state_path = Path(args.state)
    prompt = assemble_t0_context(
        state_path=state_path,
        receipt=receipt,
        state_dir=Path(args.state_dir) if args.state_dir else state_path.parent,
        skill_path=Path(args.skill),
        claude_md_path=Path(args.claude_md),
    )

    if args.output:
        Path(args.output).write_text(prompt, encoding="utf-8")
        print(f"Prompt written to {args.output} ({len(prompt)} chars)", file=sys.stderr)
    else:
        print(prompt)

    return 0


if __name__ == "__main__":
    sys.exit(main())
