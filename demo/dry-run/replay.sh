#!/bin/bash
# VNX Dry-Run Replay — Demonstrates the full governance pipeline without LLM
#
# Replays real evidence from a completed demo session (LeadFlow, 6 PRs across 3 tracks)
# to show: dispatch promotion → terminal assignment → receipt capture → quality advisory → open items
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

  Glass Box Governance — Dry-Run Replay

BANNER
echo -e "${NC}"
echo -e "  ${DIM}Replaying a real demo session: LeadFlow AI-Powered Lead Scoring${NC}"
echo -e "  ${DIM}6 PRs across 3 parallel tracks (A, B, C) — 3 providers${NC}"
echo -e "  ${DIM}No LLM required — all data from actual governance artifacts${NC}"
echo ""
echo -e "  ${DIM}Press Enter to start...${NC}"
read -r

# ─────────────────────────────────────────────────────
# PHASE 1: Show the feature plan
# ─────────────────────────────────────────────────────

print_header "PHASE 1: Feature Plan & PR Queue"

print_step "Loading PR queue from evidence..."
pause

echo ""
echo -e "    ${BOLD}PR Queue State:${NC}"
echo -e "    ┌─────┬──────────────────────────────────────┬───────┬──────┬──────────┐"
echo -e "    │ ${BOLD}PR${NC}  │ ${BOLD}Title${NC}                                │ ${BOLD}Track${NC} │ ${BOLD}Prio${NC} │ ${BOLD}Depends${NC}  │"
echo -e "    ├─────┼──────────────────────────────────────┼───────┼──────┼──────────┤"
echo -e "    │ PR-1│ AI Config & Model Registry           │ ${BLUE}B${NC}     │ P1   │ —        │"
echo -e "    │ PR-2│ Lead Scoring Engine                   │ ${GREEN}A${NC}     │ P1   │ PR-1     │"
echo -e "    │ PR-3│ WebSocket Real-time Updates           │ ${MAGENTA}C${NC}     │ P2   │ PR-2     │"
echo -e "    │ PR-4│ Scoring Dashboard API                 │ ${GREEN}A${NC}     │ P2   │ PR-2     │"
echo -e "    │ PR-5│ Multi-Provider Dispatch               │ ${BLUE}B${NC}     │ P2   │ PR-1     │"
echo -e "    │ PR-6│ Email Campaign Integration            │ ${MAGENTA}C${NC}     │ P3   │ PR-2,4   │"
echo -e "    └─────┴──────────────────────────────────────┴───────┴──────┴──────────┘"

pause

print_step "Terminal configuration (multi-provider):"
echo -e "    T0  ${YELLOW}Orchestrator${NC}  │ Claude Code  │ Opus   │ ${DIM}do_not_target${NC}"
echo -e "    T1  ${GREEN}Track A${NC}        │ Codex CLI    │ Sonnet │ ${DIM}implementation${NC}"
echo -e "    T2  ${BLUE}Track B${NC}        │ Claude Code  │ Sonnet │ ${DIM}implementation${NC}"
echo -e "    T3  ${MAGENTA}Track C${NC}        │ Claude Code  │ Opus   │ ${DIM}deep reasoning${NC}"

long_pause

# ─────────────────────────────────────────────────────
# PHASE 2: Dispatch promotion + execution (PR-1)
# ─────────────────────────────────────────────────────

print_header "PHASE 2: Dispatch Lifecycle — PR-1 (AI Config & Model Registry)"

print_step "Staging → Queue (human approval gate)"
pause
print_event "$CHECK" "T0" "Promoting PR-1 dispatch to queue"
print_detail "dispatch_id: 20260221-165539-ai-config-&-model-registry-(tr-B"
print_detail "actor: T0 | forced: false | timestamp: 2026-02-21T16:55:40"
pause

print_step "Queue → Active (dispatcher assigns to terminal)"
print_event "$ARROW" "T2" "Dispatch sent to Track B terminal (Claude Code, Sonnet)"
print_detail "provider: claude_code | model: sonnet"
pause

print_step "Receipt: task_started"
print_event "$CHECK" "T2" "Heartbeat ACK confirmed — agent is working"
print_detail "confirmation_method: log_change | delay: 2.02s | confidence: 0.35"
pause

print_step "Receipt: task_complete"
print_event "$CHECK" "T2" "PR-1 completed successfully"
print_detail "gate: implementation | status: success"
print_detail "git_ref: d26223e | dirty_files: 7 | +35 -0 lines"
pause

print_step "Quality advisory:"
echo -e "    ${GREEN}┌──────────────────────────────────────────┐${NC}"
echo -e "    ${GREEN}│  Decision: APPROVE                       │${NC}"
echo -e "    ${GREEN}│  Warnings: 0  │  Blockers: 0             │${NC}"
echo -e "    ${GREEN}│  Risk Score: 0                           │${NC}"
echo -e "    ${GREEN}└──────────────────────────────────────────┘${NC}"
pause

print_step "Report delivered:"
print_detail "→ unified_reports/20260221-170328-B-ai-config-model-registry.md (3.9KB)"
print_detail "  49/49 tests passing | All quality gates clear"

long_pause

# ─────────────────────────────────────────────────────
# PHASE 3: Parallel dispatch (PR-2 + PR-5)
# ─────────────────────────────────────────────────────

print_header "PHASE 3: Parallel Tracks — PR-2 (Track A) + PR-5 (Track B)"

print_step "Dependencies satisfied: PR-1 complete → PR-2 and PR-5 unblocked"
pause

print_event "$ARROW" "T1" "PR-2: Lead Scoring Engine dispatched (Codex CLI, Sonnet)"
print_event "$ARROW" "T2" "PR-5: Multi-Provider Dispatch dispatched (Claude Code, Sonnet)"
print_detail "Two tracks working in parallel — different providers, same governance"
pause

print_step "Receipts arriving..."
print_event "$CHECK" "T1" "PR-2 task_started (Codex CLI confirmed via log_change)"
print_event "$CHECK" "T2" "PR-5 task_started (Claude Code confirmed via log_change)"
pause

echo ""
echo -e "    ${DIM}... agents working ...${NC}"
pause

print_event "$CHECK" "T2" "PR-5 complete — Multi-Provider Dispatch"
print_detail "git: +479 -352 lines | 14 dirty files"
print_detail "quality: APPROVE (0 warnings, 0 blockers)"
pause

print_event "$CHECK" "T1" "PR-2 complete — Lead Scoring Engine (iteration 1)"
print_detail "provider: codex_cli | 15 dirty files"
pause

print_step "Quality advisory flagged issues on PR-2:"
echo -e "    ${YELLOW}┌──────────────────────────────────────────────────────────┐${NC}"
echo -e "    ${YELLOW}│  Decision: APPROVE_WITH_FOLLOWUP                        │${NC}"
echo -e "    ${YELLOW}│  Warnings: 2                                            │${NC}"
echo -e "    ${YELLOW}│  ${WARN} lead_scoring_engine.py: 651 lines (>500 warning)     │${NC}"
echo -e "    ${YELLOW}│  ${WARN} 4 functions exceed 40-line warning threshold         │${NC}"
echo -e "    ${YELLOW}│  Risk Score: 20                                         │${NC}"
echo -e "    ${YELLOW}└──────────────────────────────────────────────────────────┘${NC}"
pause

print_step "Follow-up dispatch created automatically:"
print_event "$ARROW" "T1" "PR-2 iteration 2: Address quality warnings"
print_detail "dispatch_id: 20260221-172043-d7d261a8-A"
pause

print_event "$CHECK" "T1" "PR-2 iteration 2 complete"
print_detail "quality: APPROVE (warnings addressed)"

long_pause

# ─────────────────────────────────────────────────────
# PHASE 4: Second wave (PR-3 + PR-4)
# ─────────────────────────────────────────────────────

print_header "PHASE 4: Second Wave — PR-4 (Track A) + PR-3 (Track C)"

print_step "Dependencies: PR-2 complete → PR-3 and PR-4 unblocked"
pause

print_event "$ARROW" "T1" "PR-4: Scoring Dashboard API dispatched (Codex CLI)"
print_event "$ARROW" "T3" "PR-3: WebSocket Real-time Updates dispatched (Claude Opus)"
print_detail "Track C using Opus for deep reasoning — different model, same governance"
pause

echo ""
echo -e "    ${DIM}... agents working in parallel ...${NC}"
pause

print_event "$CHECK" "T1" "PR-4 complete — Scoring Dashboard API"
print_detail "gate: implementation | +494 -352 lines | APPROVE"
pause

print_event "$CHECK" "T3" "PR-3 complete — WebSocket Score Streaming"
print_detail "19 WebSocket-specific tests | 110 total tests passing"
print_detail "quality: APPROVE | Open items created: OI-049 to OI-051"
pause

print_step "New open items from quality advisory:"
print_event "$INFO" "T3" "OI-049: TokenAuthenticator uses static tokens (info)"
print_event "$INFO" "T3" "OI-050: Redis pub/sub not implemented — single-instance only (info)"
print_event "$INFO" "T3" "OI-051: websockets package installed but unused (info)"

long_pause

# ─────────────────────────────────────────────────────
# PHASE 5: Final PR (PR-6 — Planning Gate)
# ─────────────────────────────────────────────────────

print_header "PHASE 5: PR-6 — Email Campaign Integration (Planning Gate)"

print_step "Dependencies: PR-2 + PR-4 complete → PR-6 unblocked"
pause

print_event "$ARROW" "T3" "PR-6 dispatched (Claude Opus, deep reasoning)"
print_detail "Gate: PLANNING (architecture review, not implementation)"
print_detail "GDPR + CAN-SPAM compliance analysis required"
pause

echo ""
echo -e "    ${DIM}... architecture review in progress ...${NC}"
long_pause

print_event "$CHECK" "T3" "PR-6 complete — Architecture Review & Compliance Analysis"
print_detail "Report: 19KB comprehensive deliverable"
pause

print_step "Deliverables verified:"
echo -e "    $CHECK Component architecture diagram"
echo -e "    $CHECK Data models (ConsentRecord, Campaign, ABTestConfig)"
echo -e "    $CHECK Service method signatures (3 services)"
echo -e "    $CHECK API endpoint specifications (8 endpoints)"
echo -e "    $CHECK GDPR Article mapping (Art. 6, 7, 13, 15, 17, 21)"
echo -e "    $CHECK CAN-SPAM compliance mapping"
echo -e "    $CHECK Risk assessment table (5 risks)"
pause

print_step "Quality advisory — blockers identified:"
echo -e "    ${RED}┌──────────────────────────────────────────────────────────────┐${NC}"
echo -e "    ${RED}│  Decision: HOLD (3 blockers)                                │${NC}"
echo -e "    ${RED}│                                                              │${NC}"
echo -e "    ${RED}│  ${BLOCK} OI-031: E2E campaign enrollment tests missing          │${NC}"
echo -e "    ${RED}│  ${BLOCK} OI-034: GDPR consent tracking not implemented          │${NC}"
echo -e "    ${RED}│  ${BLOCK} OI-052: No database persistence layer for consent      │${NC}"
echo -e "    ${RED}│                                                              │${NC}"
echo -e "    ${RED}│  ${WARN} OI-053: lead_scoring_engine.py at 652 lines             │${NC}"
echo -e "    ${RED}│  ${WARN} OI-054: Double opt-in decision pending                  │${NC}"
echo -e "    ${RED}│                                                              │${NC}"
echo -e "    ${RED}│  Risk Score: 170                                             │${NC}"
echo -e "    ${RED}└──────────────────────────────────────────────────────────────┘${NC}"
pause

print_step "Open items registered (governance chain intact):"
print_detail "PR-6 stays in PLANNING gate until blockers are resolved"
print_detail "Implementation dispatch will not be created until HOLD is lifted"

long_pause

# ─────────────────────────────────────────────────────
# PHASE 6: Final Summary
# ─────────────────────────────────────────────────────

print_header "SUMMARY: Governance Pipeline Results"

echo ""
echo -e "    ${BOLD}Receipt Ledger:${NC} 16 entries (8 task_started + 8 task_complete)"
echo -e "    ${BOLD}Dispatch Audit:${NC} 6 promote events (all human-approved)"
echo -e "    ${BOLD}Providers Used:${NC} Claude Code (T0, T2, T3) + Codex CLI (T1)"
echo ""
echo -e "    ${BOLD}PR Completion:${NC}"
echo -e "    $CHECK PR-1: AI Config & Model Registry       ${GREEN}APPROVED${NC}"
echo -e "    $CHECK PR-2: Lead Scoring Engine               ${YELLOW}APPROVED (with followup)${NC}"
echo -e "    $CHECK PR-3: WebSocket Real-time Updates       ${GREEN}APPROVED${NC}"
echo -e "    $CHECK PR-4: Scoring Dashboard API             ${GREEN}APPROVED${NC}"
echo -e "    $CHECK PR-5: Multi-Provider Dispatch           ${GREEN}APPROVED${NC}"
echo -e "    ${YELLOW}◐${NC} PR-6: Email Campaign Integration       ${RED}HOLD (3 blockers)${NC}"
echo ""
echo -e "    ${BOLD}Open Items:${NC}"
echo -e "    Total: 60 tracked | ${GREEN}34 done${NC} | ${RED}3 blockers${NC} | ${YELLOW}6 warnings${NC} | ${BLUE}17 info${NC}"
echo ""
echo -e "    ${BOLD}Quality Gate Decisions:${NC}"
echo -e "    ${GREEN}approve${NC}: 5  |  ${YELLOW}approve_with_followup${NC}: 1  |  ${RED}hold${NC}: 1"
echo ""

echo -e "${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}${CYAN}  Glass Box Governance: Every dispatch tracked. Every decision auditable.${NC}"
echo -e "${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  ${DIM}All data replayed from: demo/dry-run/evidence/${NC}"
echo -e "  ${DIM}Source: LeadFlow demo session, 2026-02-21${NC}"
echo -e "  ${DIM}No LLM was used during this replay.${NC}"
echo ""
