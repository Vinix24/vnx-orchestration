#!/bin/bash
# userpromptsubmit_worker_intelligence_inject.sh
# Purpose: Inject task-relevant intelligence into T1-T3 worker prompts
# Compatible with Claude Code 2.1+ hook decision system
#
# Injects per-prompt: relevant patterns (max 3), prevention rules, session insights
# Token budget: <400 tokens (≈1600 chars) per injection
# Degrades gracefully when no dispatch or empty intelligence (A-5)
# Logs all injection events to intelligence_usage.ndjson (G-L7)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE_DIR="${VNX_STATE_DIR:-$PROJECT_ROOT/.vnx-data/state}"
TERMINAL_STATE="$STATE_DIR/terminal_state.json"
USAGE_LOG="$STATE_DIR/intelligence_usage.ndjson"

# ── Safe exit: always allow, never block dispatch (A-5) ────────────
safe_exit() {
  echo '{"decision": "allow"}'
  exit 0
}

# ── Determine which terminal is running ───────────────────────────
TERMINAL_ID="${VNX_TERMINAL:-}"
if [[ -z "$TERMINAL_ID" ]]; then
  case "$PWD" in
    */T1) TERMINAL_ID="T1" ;;
    */T2) TERMINAL_ID="T2" ;;
    */T3) TERMINAL_ID="T3" ;;
    *)    safe_exit ;;
  esac
fi

# ── Require jq ────────────────────────────────────────────────────
if ! command -v jq >/dev/null 2>&1; then
  safe_exit
fi

# ── Get active dispatch from terminal state ────────────────────────
if [[ ! -f "$TERMINAL_STATE" ]]; then
  safe_exit
fi

DISPATCH_ID=$(jq -r ".terminals.${TERMINAL_ID}.claimed_by // empty" "$TERMINAL_STATE" 2>/dev/null || true)
if [[ -z "${DISPATCH_ID:-}" ]] || [[ "$DISPATCH_ID" == "null" ]]; then
  safe_exit
fi

# ── Find dispatch file ─────────────────────────────────────────────
DISPATCH_FILE=""
for dir in active completed pending staging; do
  candidate="$PROJECT_ROOT/.vnx-data/dispatches/$dir/${DISPATCH_ID}.md"
  if [[ -f "$candidate" ]]; then
    DISPATCH_FILE="$candidate"
    break
  fi
done

if [[ -z "${DISPATCH_FILE:-}" ]]; then
  safe_exit
fi

# ── Extract task metadata from dispatch ───────────────────────────
GATE=$(grep -m1 "^Gate:" "$DISPATCH_FILE" 2>/dev/null | awk '{print $2}' || true)
AGENT=$(grep -m1 "^Role:" "$DISPATCH_FILE" 2>/dev/null | awk '{print $2}' || true)
# Extract first non-empty line after "Instruction:" as task summary
TASK=$(awk '/^Instruction:/{found=1; next} found && NF>0{print; exit}' "$DISPATCH_FILE" 2>/dev/null | head -c 200 || true)
TASK="${TASK:-dispatch task}"
# Strip markdown formatting characters
TASK=$(echo "$TASK" | sed 's/[#*`]//g' | tr -s ' ' | sed 's/^[[:space:]]*//')

# ── Query intelligence via gather_intelligence.py ─────────────────
GATHER_SCRIPT="$PROJECT_ROOT/scripts/gather_intelligence.py"
if [[ ! -f "$GATHER_SCRIPT" ]]; then
  safe_exit
fi

INTELLIGENCE_JSON=$(python3 "$GATHER_SCRIPT" gather \
  "$TASK" \
  "${TERMINAL_ID}" \
  "${AGENT:-}" \
  "${GATE:-}" \
  "${DISPATCH_ID}" \
  2>/dev/null || true)

if [[ -z "${INTELLIGENCE_JSON:-}" ]]; then
  safe_exit
fi

# Bail if dispatch was blocked by intelligence validation
IS_BLOCKED=$(echo "$INTELLIGENCE_JSON" | jq -r '.dispatch_blocked // false' 2>/dev/null || echo "false")
if [[ "$IS_BLOCKED" == "true" ]]; then
  safe_exit
fi

# ── Check if intelligence has changed since last injection ─────────
INTEL_HASH=$(echo "$INTELLIGENCE_JSON" | sha256sum | cut -d' ' -f1)
LAST_HASH_FILE="$STATE_DIR/.last_worker_intel_hash_${TERMINAL_ID}"
if [[ -f "$LAST_HASH_FILE" ]]; then
  LAST_INTEL_HASH=$(cat "$LAST_HASH_FILE" 2>/dev/null || true)
  if [[ "$INTEL_HASH" == "${LAST_INTEL_HASH:-}" ]]; then
    safe_exit
  fi
fi

# ── Build injection context (token budget: <400 tokens ≈ 1600 chars) ─
OUTPUT_LINES=()

# Relevant patterns (max 3)
PATTERN_COUNT=$(echo "$INTELLIGENCE_JSON" | jq -r '.pattern_count // 0' 2>/dev/null || echo "0")
if [[ "$PATTERN_COUNT" -gt 0 ]]; then
  OUTPUT_LINES+=("Patterns:")
  while IFS= read -r line; do
    [[ -n "$line" ]] && OUTPUT_LINES+=("$line")
  done < <(echo "$INTELLIGENCE_JSON" | jq -r \
    '.suggested_patterns[:3][] | "• \(.title // "pattern" | .[0:60]): \(.description // "" | .[0:80])"' \
    2>/dev/null || true)
fi

# Prevention rules (max 2)
RULE_COUNT=$(echo "$INTELLIGENCE_JSON" | jq -r '.prevention_rule_count // 0' 2>/dev/null || echo "0")
if [[ "$RULE_COUNT" -gt 0 ]]; then
  OUTPUT_LINES+=("Prevention:")
  while IFS= read -r line; do
    [[ -n "$line" ]] && OUTPUT_LINES+=("$line")
  done < <(echo "$INTELLIGENCE_JSON" | jq -r \
    '.prevention_rules[:2][] | "⚠ \(.rule // "" | .[0:60]): \(.recommendation // "" | .[0:80])"' \
    2>/dev/null || true)
fi

# Session insights (max 2)
INSIGHT_COUNT=$(echo "$INTELLIGENCE_JSON" | jq -r '.session_insights | length' 2>/dev/null || echo "0")
if [[ "$INSIGHT_COUNT" -gt 0 ]]; then
  OUTPUT_LINES+=("Context:")
  while IFS= read -r line; do
    [[ -n "$line" ]] && OUTPUT_LINES+=("• $line")
  done < <(echo "$INTELLIGENCE_JSON" | jq -r '.session_insights[:2][]' 2>/dev/null || true)
fi

if [[ "${#OUTPUT_LINES[@]}" -eq 0 ]]; then
  safe_exit
fi

# Assemble output
HEADER="=== VNX [${TERMINAL_ID}] Dispatch: ${DISPATCH_ID} | Gate: ${GATE:-?} ==="
BODY=$(printf '%s\n' "${OUTPUT_LINES[@]}")
FULL_OUTPUT="${HEADER}\n${BODY}"

# Enforce 1600 char ceiling (≈400 tokens)
if [[ "${#FULL_OUTPUT}" -gt 1600 ]]; then
  FULL_OUTPUT="${FULL_OUTPUT:0:1600}"
fi

# ── Log injection audit (G-L7) ─────────────────────────────────────
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "")
PATTERN_IDS=$(echo "$INTELLIGENCE_JSON" | jq -c '.offered_pattern_hashes[:3]' 2>/dev/null || echo "[]")
TOKEN_EST=$(( ${#FULL_OUTPUT} / 4 ))
AUDIT_EVENT="{\"timestamp\":\"${TIMESTAMP}\",\"event_type\":\"injection\",\"terminal\":\"${TERMINAL_ID}\",\"dispatch_id\":\"${DISPATCH_ID}\",\"gate\":\"${GATE:-}\",\"pattern_ids\":${PATTERN_IDS},\"token_estimate\":${TOKEN_EST}}"
echo "$AUDIT_EVENT" >> "$USAGE_LOG" 2>/dev/null || true

# Cache hash for change detection
echo "$INTEL_HASH" > "$LAST_HASH_FILE" 2>/dev/null || true

# ── Output JSON decision for Claude Code 2.1+ ─────────────────────
ESCAPED=$(echo -e "$FULL_OUTPUT" | sed 's/\\/\\\\/g; s/"/\\"/g' | awk '{printf "%s\\n", $0}' | sed 's/\\n$//')
echo "{\"decision\": \"allow\", \"additionalContext\": \"${ESCAPED}\"}"
