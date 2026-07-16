#!/usr/bin/env bash
# VNX SessionStart Hook — Universal Router
#
# Deployed by 'vnx init' to .claude/hooks/sessionstart.sh
# Routes context based on terminal directory ($PWD).
#
# This hook runs every time Claude Code starts a new session or
# after /clear. It gives each terminal its identity:
#   T0 → Master Orchestrator (brain, no execution)
#   T1 → Worker Track A (implementation)
#   T2 → Worker Track B (testing/integration)
#   T3 → Worker Track C (deep analysis, Opus)

set -euo pipefail

# ── Detect terminal from working directory ───────────────────────────
TERMINAL=""
ROLE=""
TRACK=""

case "$PWD" in
  */terminals/T0|*/T0)
    TERMINAL="T0"
    ROLE="orchestrator"
    ;;
  */terminals/T1|*/T1)
    TERMINAL="T1"
    ROLE="worker"
    TRACK="A"
    ;;
  */terminals/T2|*/T2)
    TERMINAL="T2"
    ROLE="worker"
    TRACK="B"
    ;;
  */terminals/T3|*/T3)
    TERMINAL="T3"
    ROLE="worker"
    TRACK="C"
    ;;
  *)
    # Not a VNX terminal directory — exit silently
    echo '{}'
    exit 0
    ;;
esac

# ── Also check environment variables (set by vnx start) ─────────────
if [ -n "${CLAUDE_ROLE:-}" ]; then
  ROLE="$CLAUDE_ROLE"
fi
if [ -n "${CLAUDE_TRACK:-}" ]; then
  TRACK="$CLAUDE_TRACK"
fi

# ── Resolve project root from directory structure ────────────────────
# Walk up from terminal dir to find project root
PROJECT_NAME=""
PROJECT_ROOT=""
CURRENT_DIR="$PWD"
for _ in 1 2 3 4 5; do
  PARENT="$(dirname "$CURRENT_DIR")"
  if [ -f "$PARENT/.vnx/config.yml" ] || [ -d "$PARENT/.vnx" ]; then
    PROJECT_NAME="$(basename "$PARENT")"
    PROJECT_ROOT="$PARENT"
    break
  fi
  CURRENT_DIR="$PARENT"
done

# ── Build terminal-specific context ──────────────────────────────────
ADDITIONAL_CONTEXT=""

case "$TERMINAL" in
  T0)
    # Gather live terminal states (best-effort, 2s timeout)
    T0_TERMINAL_STATES=""
    T0_OPEN_ITEMS=""
    # Resolve VNX paths for state files
    _VNX_STATE_DIR=""
    for _candidate in ".vnx-data/state" ".claude/vnx-data/state"; do
      _search="$PWD"
      for _ in 1 2 3 4 5; do
        if [ -d "$_search/$_candidate" ]; then
          _VNX_STATE_DIR="$_search/$_candidate"
          break 2
        fi
        _search="$(dirname "$_search")"
      done
    done

    if [ -n "$_VNX_STATE_DIR" ]; then
      # Terminal states from shadow state files
      for _t in T1 T2 T3; do
        _sf="$_VNX_STATE_DIR/terminal_state_${_t}.json"
        if [ -f "$_sf" ]; then
          _status=$(jq -r '.status // "unknown"' "$_sf" 2>/dev/null || echo "unknown")
          _task=$(jq -r '.current_task // "idle"' "$_sf" 2>/dev/null || echo "idle")
          T0_TERMINAL_STATES="${T0_TERMINAL_STATES}${_t}: ${_status} (${_task})\n"
        fi
      done

      # Open items digest (compact)
      _oi_file="$_VNX_STATE_DIR/open_items.json"
      if [ -f "$_oi_file" ]; then
        _open_count=$(jq '[.items[] | select(.status == "open")] | length' "$_oi_file" 2>/dev/null || echo "0")
        _blocker_count=$(jq '[.items[] | select(.status == "open" and .severity == "blocker")] | length' "$_oi_file" 2>/dev/null || echo "0")
        _warn_count=$(jq '[.items[] | select(.status == "open" and .severity == "warn")] | length' "$_oi_file" 2>/dev/null || echo "0")
        T0_OPEN_ITEMS="Open items: ${_open_count} (${_blocker_count} blocker, ${_warn_count} warn)"
        if [ "$_open_count" -gt 0 ] 2>/dev/null; then
          _top_items=$(jq -r '[.items[] | select(.status == "open")] | sort_by(if .severity == "blocker" then 0 elif .severity == "warn" then 1 else 2 end)[:5][] | "  - [\(.severity)] \(.id): \(.title)"' "$_oi_file" 2>/dev/null || true)
          if [ -n "$_top_items" ]; then
            T0_OPEN_ITEMS="${T0_OPEN_ITEMS}\n${_top_items}"
          fi
        fi
      fi
    fi

    # ── T0 Orchestrator playbook body (in-context injection) ────────────
    # t0-orchestrator is intentionally not model-invocable
    # (disable-model-invocation: true, A-4 hardening), so its content has to
    # reach T0 some other way. A CLAUDE.md `@`-import of the skill body
    # works for a git-tracked project file but trips Claude Code's
    # external-CLAUDE.md-import trust prompt on a fresh autonomous spawn (F1,
    # 2026-07-16 live smoke test) — nobody is there to answer that prompt, so
    # the session just hangs. Reading the body here instead and returning it
    # as hook additionalContext reaches the same content without any import
    # or Skill-tool call, and without ever triggering that prompt. Fail-soft:
    # an absent SKILL.md just means an unchanged (shorter) context, not a
    # hook error — and each hook run recomputes this fresh from disk, so
    # nothing accumulates across repeated SessionStart fires (/clear, etc).
    T0_SKILL_BODY=""
    if [ -n "$PROJECT_ROOT" ]; then
      _T0_SKILL_MD="$PROJECT_ROOT/.claude/skills/t0-orchestrator/SKILL.md"
      if [ -f "$_T0_SKILL_MD" ]; then
        T0_SKILL_BODY="$(cat "$_T0_SKILL_MD" 2>/dev/null || true)"
      fi
    fi

    ADDITIONAL_CONTEXT="T0 Master Orchestrator Active${PROJECT_NAME:+ — $PROJECT_NAME}
Available skills: @t0-orchestrator @architect @planner
Use /t0-orchestrator for orchestration decisions and receipt processing

Terminals:
$(echo -e "${T0_TERMINAL_STATES:-No terminal state data}")
${T0_OPEN_ITEMS:-No open items data}

CRITICAL: After every completion receipt, check quality advisory + open items before proceeding.
Skills must NOT use @ prefix in Role field. Check .vnx/skills/skills.yaml for valid skills.${T0_SKILL_BODY:+

---
$T0_SKILL_BODY}"
    ;;

  T1)
    ADDITIONAL_CONTEXT="T1 Worker (Track A) Active${PROJECT_NAME:+ — $PROJECT_NAME}
Core Instructions: Read @.claude/terminals/T1/CLAUDE.md
Role: Implementation and development
Ready for dispatch from T0 via popup"
    ;;

  T2)
    ADDITIONAL_CONTEXT="T2 Worker (Track B) Active${PROJECT_NAME:+ — $PROJECT_NAME}
Core Instructions: Read @.claude/terminals/T2/CLAUDE.md
Role: Testing, integration, and quality
Ready for dispatch from T0 via popup"
    ;;

  T3)
    ADDITIONAL_CONTEXT="T3 Deep Analysis (Track C) Active${PROJECT_NAME:+ — $PROJECT_NAME}
Core Instructions: Read @.claude/terminals/T3/CLAUDE.md
Role: Architecture review, security, complex investigations (Opus)
Ready for [[TARGET:C]] dispatch from T0"
    ;;
esac

# ── Output JSON for Claude Code ──────────────────────────────────────
if command -v jq &> /dev/null; then
  echo "$ADDITIONAL_CONTEXT" | jq -Rs '{
    hookSpecificOutput: {
      hookEventName: "SessionStart",
      additionalContext: .
    }
  }'
else
  # Manual JSON construction as fallback (no jq)
  ESCAPED_CONTEXT=$(echo "$ADDITIONAL_CONTEXT" | \
    sed 's/\\/\\\\/g' | \
    sed 's/"/\\"/g' | \
    sed ':a;N;$!ba;s/\n/\\n/g')

  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"SessionStart\",\"additionalContext\":\"$ESCAPED_CONTEXT\"}}"
fi
