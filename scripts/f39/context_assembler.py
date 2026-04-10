#!/usr/bin/env python3
"""F39 Context Assembler — builds the full prompt that headless T0 needs.

Usage:
    python3 scripts/f39/context_assembler.py \\
        --state .vnx-data/state/t0_state.json \\
        --receipt <json-string-or-file> \\
        [--feature-plan FEATURE_PLAN.md] \\
        [--output prompt.txt]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Default paths (resolved relative to repo root)
_DEFAULT_SKILL = _REPO_ROOT / ".claude" / "skills" / "t0-orchestrator" / "SKILL.md"
_DEFAULT_CLAUDE_MD = _REPO_ROOT / ".claude" / "terminals" / "T0" / "CLAUDE.md"
_DEFAULT_FEATURE_PLAN = _REPO_ROOT / "FEATURE_PLAN.md"
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
   **EXCEPTION:** If any review gate (codex_gate, gemini_review, ci_status) is absent (null) or in status "requested"/"queued", → WAIT instead of COMPLETE. Gate discipline is mandatory.

**HARD CONSTRAINTS (enforced by code before you are invoked):**
- Required review gates must be completed — if any are pending, you will not be called
- Ghost/duplicate receipts are auto-rejected before reaching you
- Terminal availability is auto-checked

Your job is soft decisions only: quality judgment, risk assessment, next task planning.
You will never be asked to handle these hard constraints — they are pre-filtered.

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


def _extract_active_feature_section(feature_plan_text: str) -> str:
    """Extract the first in-progress or active feature block from FEATURE_PLAN.md."""
    lines = feature_plan_text.splitlines()
    in_section = False
    section_lines: list[str] = []
    for line in lines:
        # Feature headings are typically ## F<N> or # F<N>
        if line.startswith("## ") or line.startswith("# "):
            if in_section:
                break  # We captured one complete section
            # Look for markers indicating active work
            lower = line.lower()
            if any(kw in lower for kw in ("in progress", "active", "wip", "current")):
                in_section = True
                section_lines.append(line)
        elif in_section:
            section_lines.append(line)

    if section_lines:
        return "\n".join(section_lines)

    # Fallback: return first 50 lines
    return "\n".join(lines[:50])


def assemble_t0_context(
    state_path: Path,
    receipt: dict[str, Any],
    feature_plan_path: Path = _DEFAULT_FEATURE_PLAN,
    skill_path: Path = _DEFAULT_SKILL,
    claude_md_path: Path = _DEFAULT_CLAUDE_MD,
) -> str:
    """Assemble complete T0 orchestrator context for headless execution.

    Returns a single prompt string containing:
    1. T0 CLAUDE.md instructions
    2. T0 orchestrator skill
    3. Current state snapshot (from t0_state.json)
    4. Feature plan excerpt (active feature only)
    5. The receipt to process
    6. Explicit instruction: output decision as JSON
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

    # 4. Feature plan excerpt
    if feature_plan_path.exists():
        fp_text = feature_plan_path.read_text(encoding="utf-8", errors="replace")
        excerpt = _extract_active_feature_section(fp_text)
        sections.append(
            "# SECTION 4: Feature Plan (active feature excerpt)\n\n"
            + _truncate(excerpt, 3000, "feature plan")
        )
    else:
        sections.append("# SECTION 4: Feature Plan\n\n[FEATURE_PLAN.md not found]")

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
    parser.add_argument("--feature-plan", default=str(_DEFAULT_FEATURE_PLAN))
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

    prompt = assemble_t0_context(
        state_path=Path(args.state),
        receipt=receipt,
        feature_plan_path=Path(args.feature_plan),
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
