#!/bin/bash
# VNX Context Rotation — Dry-Run Replay
#
# Replays a simulated context rotation scenario: T1 exhausts its context window
# during PR-2 (Lead Scoring Engine), seamlessly hands over, and resumes in a
# fresh session — all governed by vnx_context_monitor.sh + vnx_handover_detector.sh
# + vnx_rotate.sh.
#
# Usage: bash replay.sh [--fast]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVIDENCE_DIR="$SCRIPT_DIR/evidence"

# Speed control
DELAY=2
if [[ "${1:-}" == "--fast" ]]; then
    DELAY=0.5
fi

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# Symbols
CHECK="${GREEN}✓${NC}"
ARROW="${CYAN}→${NC}"
BLOCK="${RED}✗${NC}"
WARN="${YELLOW}⚠${NC}"
INFO="${BLUE}ℹ${NC}"

# ─────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────

print_header() {
    echo ""
    echo -e "${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}${CYAN}  $1${NC}"
    echo -e "${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

print_step() {
    echo -e "\n${BOLD}  ▸ $1${NC}"
}

print_detail() {
    echo -e "    ${DIM}$1${NC}"
}

print_event() {
    local symbol="$1" terminal="$2" message="$3"
    local color="${CYAN}"
    case "$terminal" in
        T1) color="${GREEN}" ;;
        T2) color="${BLUE}" ;;
        T3) color="${MAGENTA}" ;;
        T0) color="${YELLOW}" ;;
    esac
    echo -e "    ${symbol} ${color}[${terminal}]${NC} ${message}"
}

pause() {
    sleep "$DELAY"
}

long_pause() {
    sleep "$(echo "$DELAY * 2" | bc)"
}

# ─────────────────────────────────────────────────────
# PHASE 0: Introduction
# ─────────────────────────────────────────────────────

clear
echo -e "${BOLD}${CYAN}"
cat << 'BANNER'

  ██╗   ██╗███╗   ██╗██╗  ██╗
  ██║   ██║████╗  ██║╚██╗██╔╝
  ██║   ██║██╔██╗ ██║ ╚███╔╝
  ╚██╗ ██╔╝██║╚██╗██║ ██╔██╗
   ╚████╔╝ ██║ ╚████║██╔╝ ██╗
    ╚═══╝  ╚═╝  ╚═══╝╚═╝  ╚═╝

  Context Rotation — Dry-Run Replay

BANNER
echo -e "${NC}"
echo -e "  ${DIM}Simulating context exhaustion and seamless session handover on T1${NC}"
echo -e "  ${DIM}Scenario: PR-2 Lead Scoring Engine (LeadFlow project)${NC}"
echo -e "  ${DIM}No LLM required — all data from simulated governance artifacts${NC}"
echo ""
echo -e "  ${DIM}Press Enter to start...${NC}"
read -r

# ─────────────────────────────────────────────────────
# PHASE 1: Setup — Active Dispatch
# ─────────────────────────────────────────────────────

print_header "PHASE 1: Setup — Active Dispatch on T1"

print_step "Loading terminal state from evidence..."
pause

echo ""
echo -e "    ${BOLD}Active Dispatch:${NC}"
echo -e "    ┌──────────────────────────────────────────────────────────────────┐"
echo -e "    │  Dispatch ID  : 20260221-165539-lead-scoring-engine-A           │"
echo -e "    │  PR           : PR-2 — Lead Scoring Engine                      │"
echo -e "    │  Role         : backend-developer                                │"
echo -e "    │  Provider     : Codex CLI                                        │"
echo -e "    │  Model        : Sonnet                                           │"
echo -e "    │  Gate         : implementation                                   │"
echo -e "    │  Started      : 2026-02-21T16:10:00Z                             │"
echo -e "    └──────────────────────────────────────────────────────────────────┘"

pause

print_step "Terminal state (16:10):"
echo -e "    T0  ${YELLOW}Orchestrator${NC}  │ Claude Code  │ Opus   │ ${DIM}supervisor${NC}"
echo -e "    T1  ${GREEN}working${NC}        │ Codex CLI    │ Sonnet │ ${DIM}PR-2 Lead Scoring Engine${NC}"
echo -e "    T2  ${DIM}idle${NC}             │ Claude Code  │ Sonnet │ ${DIM}—${NC}"
echo -e "    T3  ${DIM}idle${NC}             │ Claude Code  │ Opus   │ ${DIM}—${NC}"

long_pause

# ─────────────────────────────────────────────────────
# PHASE 2: Context Filling Up
# ─────────────────────────────────────────────────────

print_header "PHASE 2: Context Filling Up"

print_step "Tool calls proceeding normally (16:10 — 16:14)..."
pause

print_event "$CHECK" "T1" "Bash: pytest tests/test_scoring.py → 12 tests passing"
print_event "$CHECK" "T1" "Write: src/services/lead_scoring_engine.py (247 lines)"
print_event "$CHECK" "T1" "Edit: src/models/scoring_config.py (+35 lines)"
print_detail "Context: 48% used (52% remaining) — normal"
pause

print_step "Approaching warning threshold (16:16)..."
pause

print_event "$CHECK" "T1" "Bash: python -m pytest tests/ → 35 tests passing"
print_event "$CHECK" "T1" "Write: src/api/scoring_endpoint.py (189 lines)"
print_detail "Context: 52% used (48% remaining)"
echo ""
print_event "$WARN" "T1" "Context pressure WARNING logged (>50% threshold)"
print_detail "Hook: vnx_context_monitor.sh → warning phase, tool NOT blocked"
print_detail "Receipt: context_pressure {phase: warning, used: 52%} appended to receipts"
pause

print_step "Continuing work (16:17 — 16:18)..."
pause

print_event "$CHECK" "T1" "Bash: python -c 'from src.services.lead_scoring_engine import LeadScoringEngine'"
print_event "$CHECK" "T1" "Read: src/models/scoring_config.py"
print_event "$CHECK" "T1" "Edit: src/services/lead_scoring_engine.py (+18 lines, factor weights)"
print_detail "Context: 66% used (34% remaining)"
echo ""
print_event "$WARN" "T1" "Context pressure WARNING logged (>50% threshold)"
print_detail "Receipt: context_pressure {phase: rotation, used: 66%} appended to receipts"

long_pause

# ─────────────────────────────────────────────────────
# PHASE 3: Rotation Triggered
# ─────────────────────────────────────────────────────

print_header "PHASE 3: Rotation Triggered"

print_step "PreToolUse: Rotation threshold crossed (65%)"
pause

print_event "$BLOCK" "T1" "Tool BLOCKED: Bash (pytest tests/ -v)"
echo ""
echo -e "    ${DIM}Hook response:${NC}"
echo -e "    ${RED}┌──────────────────────────────────────────────────────────────────────┐${NC}"
echo -e "    ${RED}│  {                                                                   │${NC}"
echo -e "    ${RED}│    \"decision\": \"block\",                                             │${NC}"
echo -e "    ${RED}│    \"reason\": \"VNX CONTEXT ROTATION REQUIRED (66% used, 34%         │${NC}"
echo -e "    ${RED}│    remaining). Write a handover file NOW to                         │${NC}"
echo -e "    ${RED}│    .vnx-data/rotation_handovers/ named                              │${NC}"
echo -e "    ${RED}│    20260221-161830-T1-ROTATION-HANDOVER.md ...\"                     │${NC}"
echo -e "    ${RED}│  }                                                                   │${NC}"
echo -e "    ${RED}└──────────────────────────────────────────────────────────────────────┘${NC}"
print_detail "Write/Read/Glob/Grep allowed through — only action tools blocked"
pause

print_step "Claude writes handover document..."
pause

echo -e "    ${DIM}Handover content:${NC}"
echo -e "    ${CYAN}┌─────────────────────────────────────────────────────────────────┐${NC}"
echo -e "    ${CYAN}│${NC}  ${BOLD}# T1 Context Rotation Handover${NC}                               ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}  Timestamp: 2026-02-21T16:18:30Z                               ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}  Terminal: T1                                                   ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}  Dispatch-ID: 20260221-165539-lead-scoring-engine-A             ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}  Context Used: 66%                                              ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}                                                                 ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}  ${BOLD}## Status: in-progress${NC}                                       ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}                                                                 ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}  ${BOLD}## Completed Work${NC}                                            ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}  - Scoring engine core (5 scoring factors)                     ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}  - API endpoint /api/scoring/evaluate                           ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}  - Unit tests (35 passing)                                      ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}                                                                 ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}  ${BOLD}## Remaining Tasks${NC}                                           ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}  - Batch scoring endpoint                                       ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}  - Integration tests with lead service                          ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}  - Documentation                                                ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}                                                                 ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}  ${BOLD}## Files Modified${NC}                                            ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}  - src/services/lead_scoring_engine.py (new)                   ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}  - src/api/scoring_endpoint.py (new)                            ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}  - src/models/scoring_config.py (modified)                     ${CYAN}│${NC}"
echo -e "    ${CYAN}│${NC}  - tests/test_scoring.py (new)                                 ${CYAN}│${NC}"
echo -e "    ${CYAN}└─────────────────────────────────────────────────────────────────┘${NC}"
pause

print_step "PostToolUse: Handover detected by vnx_handover_detector.sh"
pause

print_event "$CHECK" "T1" "vnx_handover_detector.sh triggered on Write tool"
print_detail "File: rotation_handovers/20260221-161830-T1-ROTATION-HANDOVER.md"
print_detail "Condition: tool_name=Write AND file_path contains 'ROTATION-HANDOVER'"
print_detail "Lock acquired: rotation_T1"
pause

print_step "Rotation receipt emitted"
pause

echo -e "    ${DIM}Receipt appended to t0_receipts.ndjson:${NC}"
echo -e "    ${BOLD}  {${NC}"
echo -e "      ${GREEN}\"event_type\"${NC}: ${YELLOW}\"context_rotation\"${NC},"
echo -e "      ${GREEN}\"terminal\"${NC}: ${YELLOW}\"T1\"${NC},"
echo -e "      ${GREEN}\"dispatch_id\"${NC}: ${YELLOW}\"20260221-165539-lead-scoring-engine-A\"${NC},"
echo -e "      ${GREEN}\"context_used_pct\"${NC}: ${YELLOW}66${NC},"
echo -e "      ${GREEN}\"handover_path\"${NC}: ${YELLOW}\".vnx-data/rotation_handovers/...HANDOVER.md\"${NC}"
echo -e "    ${BOLD}  }${NC}"
print_detail "Source: evidence/rotation_receipt.json"
pause

print_step "Agent stopped"
pause

print_event "$BLOCK" "T1" "PostToolUse returns {\"continue\":false}"
print_detail "stopReason: Context rotation in progress. Handover written."
print_detail "Waiting for /clear from vnx_rotate.sh — do not continue."
print_detail "Claude stops — no more tool calls will execute"

long_pause

# ─────────────────────────────────────────────────────
# PHASE 4: Session Recovery (vnx_rotate.sh)
# ─────────────────────────────────────────────────────

print_header "PHASE 4: Session Recovery (vnx_rotate.sh)"

print_step "vnx_rotate.sh starts (launched by detector, nohup background)"
pause

print_event "$INFO" "T1" "Sleeping 3s — allowing Claude to fully stop"
print_detail "Prevents /clear from racing with in-flight tool output"
pause

print_event "$ARROW" "T1" "C-u sent (clear input line without killing CLI process)"
print_event "$ARROW" "T1" "/clear sent to T1 pane via tmux send-keys"
print_detail "Waiting for /clear completion signal..."
pause

print_event "$CHECK" "T1" "Clear complete — fresh context window active"
print_detail "Signal file: state/rotation_clear_done_T1 (detected after 8s)"
pause

print_step "Skill recovery from original dispatch file"
pause

print_detail "Searching: dispatches/active/20260221-165539-lead-scoring-engine-A*.md"
print_detail "Found: dispatches/completed/20260221-165539-lead-scoring-engine-A.md"
print_detail "Role extracted via vnx_dispatch_extract_agent_role(): backend-developer"
print_detail "Skill validated: validate_skill.py backend-developer → OK"
pause

print_event "$CHECK" "T1" "Skill recovered: /backend-developer"
pause

print_step "Continuation prompt sent to T1 pane"
pause

echo -e "    ${DIM}Prompt loaded via tmux load-buffer + paste-buffer:${NC}"
echo -e "    ${BOLD}  ┌──────────────────────────────────────────────────────────────────────┐${NC}"
echo -e "    ${BOLD}  │${NC}  /backend-developer Continue dispatch                              ${BOLD}  │${NC}"
echo -e "    ${BOLD}  │${NC}  20260221-165539-lead-scoring-engine-A.                            ${BOLD}  │${NC}"
echo -e "    ${BOLD}  │${NC}                                                                    ${BOLD}  │${NC}"
echo -e "    ${BOLD}  │${NC}  Read handover: .vnx-data/rotation_handovers/                     ${BOLD}  │${NC}"
echo -e "    ${BOLD}  │${NC}    20260221-161830-T1-ROTATION-HANDOVER.md                        ${BOLD}  │${NC}"
echo -e "    ${BOLD}  │${NC}  Read dispatch: .vnx-data/dispatches/completed/                   ${BOLD}  │${NC}"
echo -e "    ${BOLD}  │${NC}    20260221-165539-lead-scoring-engine-A.md                        ${BOLD}  │${NC}"
echo -e "    ${BOLD}  │${NC}                                                                    ${BOLD}  │${NC}"
echo -e "    ${BOLD}  │${NC}  Resume from where the previous session left off.                 ${BOLD}  │${NC}"
echo -e "    ${BOLD}  └──────────────────────────────────────────────────────────────────────┘${NC}"
pause

print_event "$CHECK" "T1" "Terminal state updated: T1=working (dispatch preserved)"
print_detail "terminal_state_shadow.py --terminal-id T1 --status working"
print_detail "claimed-by: 20260221-165539-lead-scoring-engine-A"
pause

print_step "Continuation receipt emitted"
pause

echo -e "    ${DIM}Receipt appended to t0_receipts.ndjson:${NC}"
echo -e "    ${BOLD}  {${NC}"
echo -e "      ${GREEN}\"event_type\"${NC}: ${YELLOW}\"context_rotation_continuation\"${NC},"
echo -e "      ${GREEN}\"terminal\"${NC}: ${YELLOW}\"T1\"${NC},"
echo -e "      ${GREEN}\"dispatch_id\"${NC}: ${YELLOW}\"20260221-165539-lead-scoring-engine-A\"${NC},"
echo -e "      ${GREEN}\"skill\"${NC}: ${YELLOW}\"backend-developer\"${NC},"
echo -e "      ${GREEN}\"context_used_pct_at_rotation\"${NC}: ${YELLOW}66${NC}"
echo -e "    ${BOLD}  }${NC}"
print_detail "Source: evidence/continuation_receipt.json"
print_detail "Rotation complete — T1 is live with fresh context"

long_pause

# ─────────────────────────────────────────────────────
# PHASE 5: Resumed Session
# ─────────────────────────────────────────────────────

print_header "PHASE 5: Resumed Session — T1 Continues PR-2"

print_step "T1 re-orients from handover and dispatch (16:19)..."
pause

print_event "$CHECK" "T1" "Read: .vnx-data/rotation_handovers/20260221-161830-T1-ROTATION-HANDOVER.md"
print_event "$CHECK" "T1" "Read: .vnx-data/dispatches/completed/20260221-165539-lead-scoring-engine-A.md"
print_detail "Context: 8% used — fresh window"
pause

print_step "T1 resumes remaining work (16:19 — 16:25)..."
pause

print_event "$CHECK" "T1" "Resuming: batch scoring endpoint"
print_event "$CHECK" "T1" "Write: src/api/batch_scoring.py (156 lines)"
print_detail "Implements POST /api/scoring/evaluate-batch (up to 100 leads)"
pause

print_event "$CHECK" "T1" "Write: tests/integration/test_scoring_integration.py (87 lines)"
print_detail "Integration tests with LeadRepository fixture (14 test cases)"
pause

print_event "$CHECK" "T1" "Bash: pytest tests/ → 49 tests passing"
print_detail "Context: 30% used — plenty of room"
pause

print_event "$CHECK" "T1" "Edit: README.md (scoring API section, +22 lines)"
print_detail "Documents /api/scoring/evaluate and /api/scoring/evaluate-batch endpoints"

long_pause

# ─────────────────────────────────────────────────────
# PHASE 6: Task Completion
# ─────────────────────────────────────────────────────

print_header "PHASE 6: Task Completion — PR-2 Done"

print_step "T1 signals completion (16:25)..."
pause

print_event "$CHECK" "T1" "PR-2 completed (across 2 sessions)"
print_detail "git_ref: a4f912b | dirty_files: 6 | +156 -0 lines (second session)"
print_detail "Total: 4 new files, 1 modified, 49 tests passing"
pause

print_step "Quality advisory ran on final output:"
echo -e "    ${YELLOW}┌──────────────────────────────────────────────────────────────────┐${NC}"
echo -e "    ${YELLOW}│  Decision: APPROVE_WITH_FOLLOWUP                                │${NC}"
echo -e "    ${YELLOW}│  Warnings: 2                                                    │${NC}"
echo -e "    ${YELLOW}│  ${WARN} lead_scoring_engine.py: 651 lines (>500 warning)           │${NC}"
echo -e "    ${YELLOW}│  ${WARN} 4 functions exceed 40-line warning threshold               │${NC}"
echo -e "    ${YELLOW}│  Risk Score: 20                                                 │${NC}"
echo -e "    ${YELLOW}└──────────────────────────────────────────────────────────────────┘${NC}"
pause

print_step "Completion receipt appended:"
print_detail "context_rotation_metadata: {rotations: 1, rotation_at_pct: 66, session_count: 2}"
print_detail "dispatch_id_preserved: true — governance chain unbroken"
print_detail "Source: evidence/completion_receipt.json"
pause

print_step "Follow-up dispatch created automatically:"
print_event "$ARROW" "T1" "PR-2 iteration 2: Refactor lead_scoring_engine.py (<500 lines)"
print_detail "dispatch_id: 20260221-172043-d7d261a8-A"

long_pause

# ─────────────────────────────────────────────────────
# PHASE 7: Summary
# ─────────────────────────────────────────────────────

print_header "PHASE 7: Summary — Context Rotation Stats"

echo ""
echo -e "    ${BOLD}Context Rotation Stats:${NC}"
echo -e "    $CHECK Rotations: 1 (T1 at 66% context used)"
echo -e "    $CHECK Recovery time: ~12 seconds (/clear + skill activation + prompt)"
echo -e "    $CHECK Handover quality: Complete (status, work done, remaining tasks, files)"
echo -e "    $CHECK Session continuity: Seamless (same dispatch, same skill, same gate)"
echo ""
echo -e "    ${BOLD}Receipt Chain:${NC}"
echo -e "    1. ${CYAN}task_started${NC}                   (T1 begins PR-2 at 16:10)"
echo -e "    2. ${YELLOW}context_pressure${NC} (warning)     (52% used at 16:16)"
echo -e "    3. ${YELLOW}context_pressure${NC} (rotation)    (66% used at 16:18)"
echo -e "    4. ${RED}context_rotation${NC}               (handover written, agent stopped)"
echo -e "    5. ${GREEN}context_rotation_continuation${NC} (new session started at 16:19)"
echo -e "    6. ${GREEN}task_complete${NC}                  (PR-2 done across 2 sessions at 16:25)"
echo ""
echo -e "    ${BOLD}Governance Integrity:${NC}"
echo -e "    $CHECK Dispatch ID preserved across sessions"
echo -e "    $CHECK Terminal state accurate throughout (working → working)"
echo -e "    $CHECK All 6 events in receipt ledger"
echo -e "    $CHECK Quality advisory ran on final output"
echo -e "    $CHECK Zero human intervention required"
echo ""
echo -e "    ${BOLD}Hook Chain:${NC}"
echo -e "    ${DIM}PreToolUse  → vnx_context_monitor.sh   (blocks at 65%, instructs handover)${NC}"
echo -e "    ${DIM}PostToolUse → vnx_handover_detector.sh (detects Write, fires rotate, stops agent)${NC}"
echo -e "    ${DIM}Background  → vnx_rotate.sh            (/clear + skill recovery + continuation)${NC}"
echo ""
echo -e "    ${BOLD}Evidence Files:${NC}"
echo -e "    ${DIM}evidence/context_pressure_events.ndjson   — 3 pressure events (48%, 52%, 66%)${NC}"
echo -e "    ${DIM}evidence/rotation_receipt.json            — context_rotation receipt${NC}"
echo -e "    ${DIM}evidence/continuation_receipt.json        — context_rotation_continuation receipt${NC}"
echo -e "    ${DIM}evidence/handover.md                      — handover document content${NC}"
echo -e "    ${DIM}evidence/completion_receipt.json          — task_complete with rotation metadata${NC}"
echo ""

echo -e "${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}${CYAN}  Context Rotation: Infinite context. Zero downtime. Full traceability.${NC}"
echo -e "${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  ${DIM}All data replayed from: demo/dry-run-context-rotation/evidence/${NC}"
echo -e "  ${DIM}Source: LeadFlow demo session, 2026-02-21${NC}"
echo -e "  ${DIM}No LLM was used during this replay.${NC}"
echo ""
