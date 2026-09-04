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

case "$PWD" in
  */terminals/T0|*/T0)
    TERMINAL="T0"
    ;;
  */terminals/T1|*/T1)
    TERMINAL="T1"
    ;;
  */terminals/T2|*/T2)
    TERMINAL="T2"
    ;;
  */terminals/T3|*/T3)
    TERMINAL="T3"
    ;;
  *)
    # Not a VNX terminal directory — exit silently
    echo '{}'
    exit 0
    ;;
esac

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

    # ── Resolve the CENTRAL state dir (ADR-026), same resolver as the runtime ──
    # The old resolver only checked ".vnx-data/state" / ".claude/vnx-data/state"
    # up to 5 levels above $PWD — both repo-local candidates. ADR-026 makes the
    # per-project CENTRAL store (~/.vnx-data/<project_id>/state) canonical, so
    # a checkout with no repo-local .vnx-data (the normal case since the
    # central-store cutover) silently found nothing, and this hook injected a
    # plausible-looking "No open items data" briefing while recent_receipts,
    # tracks, pr_queue and strategic_state were actually just unreachable
    # (fail-open). path_parity_check.sh (OI-852) and build_t0_state_hook.sh
    # (OI-859) hit the identical repo-local-vs-central split-brain and both
    # fixed it the same way: shell out to the canonical Python resolver
    # (vnx_paths.resolve_paths), which stays worktree/CWD-agnostic and in
    # lockstep with the runtime — never a second hand-rolled resolver.
    # Anchored on this hook's OWN file location (not $PWD), so resolution
    # doesn't depend on which terminal directory happened to be current.
    _VNX_STATE_DIR=""
    _VNX_DATA_DIR=""
    _VNX_PY=""
    _HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)" || _HOOK_DIR=""
    if [ -n "$_HOOK_DIR" ] && [ -f "$_HOOK_DIR/../scripts/lib/vnx_paths.py" ]; then
      _VNX_PY="$_HOOK_DIR/../.venv/bin/python"
      [ -x "$_VNX_PY" ] || _VNX_PY="/opt/homebrew/opt/python@3.12/bin/python3.12"
      [ -x "$_VNX_PY" ] || _VNX_PY="python3"
      _VNX_PATHS_OUT="$("$_VNX_PY" -c "
import sys
sys.path.insert(0, '$_HOOK_DIR/../scripts/lib')
from vnx_paths import resolve_paths
p = resolve_paths()
print(p['VNX_STATE_DIR'])
print(p['VNX_DATA_DIR'])
" 2>/dev/null || true)"
      _VNX_STATE_DIR="$(printf '%s\n' "$_VNX_PATHS_OUT" | sed -n 1p)"
      _VNX_DATA_DIR="$(printf '%s\n' "$_VNX_PATHS_OUT" | sed -n 2p)"
    fi

    if [ -n "$_VNX_STATE_DIR" ] && [ -d "$_VNX_STATE_DIR" ]; then
      # Terminal states from the shadow state file (scripts/lib/terminal_state_shadow.py).
      # Golf 3A fix: this used to glob terminal_state_${_t}.json (a per-terminal
      # suffixed filename) but the actual writer (TERMINAL_STATE_FILENAME in
      # terminal_state_shadow.py) has only ever written the SINGULAR
      # terminal_state.json, with a `.terminals.<id>` map inside it. The suffixed
      # glob matched nothing, on any project — this section silently fell
      # through to "No terminal state data" every session, discovered via this
      # dispatch's own measurement discipline, not reported by anything.
      _tsf="$_VNX_STATE_DIR/terminal_state.json"
      if [ -f "$_tsf" ]; then
        for _t in T1 T2 T3; do
          _status=$(jq -r --arg t "$_t" '.terminals[$t].status // "unknown"' "$_tsf" 2>/dev/null || echo "unknown")
          _last_activity=$(jq -r --arg t "$_t" '.terminals[$t].last_activity // "unknown"' "$_tsf" 2>/dev/null || echo "unknown")
          T0_TERMINAL_STATES="${T0_TERMINAL_STATES}${_t}: ${_status} (last_activity: ${_last_activity})\n"
        done
      fi

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

      T0_STATE_SECTION="Terminals:
$(echo -e "${T0_TERMINAL_STATES:-No terminal state data}")
${T0_OPEN_ITEMS:-No open items data}"
    else
      # Not-found is a DIFFERENT state than found-but-empty (a genuinely
      # measured zero). Say so explicitly instead of falling through to a
      # "No open items data" default that would read as an ordinary quiet
      # project instead of an unresolved store — the exact fail-open this
      # fix closes.
      T0_STATE_SECTION="VNX STATE STORE NOT FOUND — terminal states, open items, receipts and PR queue are UNAVAILABLE this session (UNMEASURED, not zero). Resolved path: ${_VNX_STATE_DIR:-<resolution failed: vnx_paths.py unreachable from here>}. Expected under ADR-026 at ~/.vnx-data/<project_id>/state — verify the receipt processor has run for this project."
    fi

    # ── Beacon health digest (component heartbeats, D3b absence-is-loud) ──
    # Measured 30-08: 9 component beacons, 7 not "ok" (2 fail, 5 stale), and
    # none of the 5 existing readers of health_beacon.all_beacons()/
    # beacon_summary() sit in a human's path without them opening something
    # first — 2 live behind a dashboard, 2 behind a CLI, and the 5th
    # (t0_state.json, via scripts/build_t0_state.py) sat frozen for 23 days.
    # SessionStart is the one moment every session hits unprompted.
    #
    # Reuses the EXISTING scripts/health_check.py CLI (an existing
    # all_beacons() caller) via subprocess instead of importing
    # health_beacon here directly — the count of distinct call sites into
    # all_beacons()/beacon_summary() must not grow for this PR (D3b
    # dispatch). Reads live rather than through t0_state.json: that
    # projection only refreshes via build_t0_state_hook.sh, which is gated
    # on VNX_HOME (D1 poort B, unmerged as of this PR) — a live read here
    # cannot inherit that staleness.
    BEACON_SECTION=""
    _HEALTH_CHECK_PY="$_HOOK_DIR/../scripts/health_check.py"
    if [ -n "$_VNX_DATA_DIR" ] && [ -n "$_VNX_PY" ] && [ -f "$_HEALTH_CHECK_PY" ]; then
      _BEACON_JSON="$("$_VNX_PY" "$_HEALTH_CHECK_PY" --state-dir "$_VNX_DATA_DIR" --json 2>/dev/null || true)"
      if [ -n "$_BEACON_JSON" ] && command -v jq &>/dev/null; then
        _BEACON_PARSE_OK=$(echo "$_BEACON_JSON" | jq -e '.beacons | type == "object"' >/dev/null 2>&1 && echo yes || echo no)
        if [ "$_BEACON_PARSE_OK" = "yes" ]; then
          _BEACON_TOTAL=$(echo "$_BEACON_JSON" | jq '.beacons | length' 2>/dev/null || echo "0")
          _BEACON_BAD=$(echo "$_BEACON_JSON" | jq -r '
            [.beacons | to_entries[] | select(.value.health != "ok")]
            | sort_by(if .value.health == "fail" then 0 elif .value.health == "corrupt" then 1 else 2 end)
            | .[] | "  - [\(.value.health)] \(.key): last_run \(.value.last_run_iso // "unknown"), age \(.value.age_seconds // "unknown")s"
          ' 2>/dev/null || true)
          _BEACON_BAD_COUNT=$(echo "$_BEACON_BAD" | grep -c . || true)
          if [ "$_BEACON_TOTAL" -eq 0 ] 2>/dev/null; then
            BEACON_SECTION="Beacon health: 0 component beacons found under ${_VNX_DATA_DIR}/health — either nothing has run yet, or beacons are writing elsewhere. Cannot yet distinguish \"never ran\" from \"nothing to report\" (needs D3a's expected-vs-present register)."
          elif [ "$_BEACON_BAD_COUNT" -gt 0 ] 2>/dev/null; then
            BEACON_SECTION="Beacon health: ${_BEACON_TOTAL} components, ${_BEACON_BAD_COUNT} NOT ok
$_BEACON_BAD"
          else
            BEACON_SECTION="Beacon health: ${_BEACON_TOTAL} components, all ok"
          fi
        else
          BEACON_SECTION="BEACON HEALTH UNAVAILABLE this session (UNMEASURED, not zero) — health_check.py did not return a beacons object."
        fi
      else
        BEACON_SECTION="BEACON HEALTH UNAVAILABLE this session (UNMEASURED, not zero) — health_check.py produced no output, or jq is missing."
      fi
    else
      BEACON_SECTION="BEACON HEALTH UNAVAILABLE this session (UNMEASURED, not zero) — data dir unresolved or scripts/health_check.py missing."
    fi

    # ── State artifact freshness (golf 3A, absence-is-loud #1, OI-1512 fix-forward) ──
    # T0 reads a handful of point-in-time snapshots at SessionStart (t0_state.json,
    # open_items.json, terminal_state.json, dashboard_status.json,
    # t0_recommendations.json) with nothing surfacing how old they are. Measured
    # 2026-08-29: a session orchestrated a merge against a 22-day-old t0_state.json
    # (dashboard_status.json 63 days, t0_recommendations.json 73 days) and nobody
    # noticed until the merge itself. scripts/lib/session_state_freshness.py reads
    # each file's own declared timestamp (falling back to mtime), same precedent as
    # t0_state_health.py, and classifies missing/stale/fresh/unknown — three real
    # branches, never conflating "never populated" with "went stale". Reused here via
    # subprocess exactly like health_check.py above, not reimplemented inline.
    FRESHNESS_SECTION=""
    _FRESHNESS_PY="$_HOOK_DIR/../scripts/lib/session_state_freshness.py"
    if [ -n "$_VNX_STATE_DIR" ] && [ -n "$_VNX_PY" ] && [ -f "$_FRESHNESS_PY" ]; then
      _FRESHNESS_JSON="$("$_VNX_PY" "$_FRESHNESS_PY" --state-dir "$_VNX_STATE_DIR" --json 2>/dev/null || true)"
      if [ -n "$_FRESHNESS_JSON" ] && command -v jq &>/dev/null; then
        _FRESHNESS_PARSE_OK=$(echo "$_FRESHNESS_JSON" | jq -e '.artifacts | type == "object"' >/dev/null 2>&1 && echo yes || echo no)
        if [ "$_FRESHNESS_PARSE_OK" = "yes" ]; then
          _FRESHNESS_THRESHOLD=$(echo "$_FRESHNESS_JSON" | jq -r '.threshold_hours')
          _FRESHNESS_ANY_STALE=$(echo "$_FRESHNESS_JSON" | jq -r '.any_stale')
          _FRESHNESS_LINES=$(echo "$_FRESHNESS_JSON" | jq -r '
            .artifacts | to_entries | sort_by(.key)[] |
            if .value.status == "missing" then "  - [missing] \(.key): not found"
            elif .value.status == "unknown" then "  - [unknown] \(.key): timestamp unreadable"
            elif .value.status == "stale" then "  - [STALE] \(.key): age \(.value.age_human) (source: \(.value.source))"
            else "  - [fresh] \(.key): age \(.value.age_human) (source: \(.value.source))"
            end
          ')
          if [ "$_FRESHNESS_ANY_STALE" = "true" ]; then
            FRESHNESS_SECTION="STATE FRESHNESS: BLOCKED — one or more session-start artifacts are older than ${_FRESHNESS_THRESHOLD}h (one working session; see scripts/lib/session_state_freshness.py for the measured commit-cadence this threshold is based on). DO NOT dispatch, merge, or close deliverables on the strength of this state — refresh it, or independently re-verify the specific facts it claims, before acting on it.
$_FRESHNESS_LINES"
          else
            FRESHNESS_SECTION="STATE FRESHNESS: ok — no session-start artifact older than ${_FRESHNESS_THRESHOLD}h
$_FRESHNESS_LINES"
          fi
        else
          FRESHNESS_SECTION="STATE FRESHNESS UNAVAILABLE this session (UNMEASURED, not zero) — session_state_freshness.py did not return an artifacts object."
        fi
      else
        FRESHNESS_SECTION="STATE FRESHNESS UNAVAILABLE this session (UNMEASURED, not zero) — session_state_freshness.py produced no output, or jq is missing."
      fi
    else
      FRESHNESS_SECTION="STATE FRESHNESS UNAVAILABLE this session (UNMEASURED, not zero) — state dir unresolved or scripts/lib/session_state_freshness.py missing."
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
Model-invocable skills: @horizon @planner @panel @fabric-reference
Operator-only skills (not model-invocable): @t0-orchestrator @architect
Full registry: skills/skills.yaml (repo) or \$VNX_SKILLS_DIR/skills.yaml (consumer)
Use /t0-orchestrator for orchestration decisions and receipt processing

$FRESHNESS_SECTION

$T0_STATE_SECTION

$BEACON_SECTION

CRITICAL: After every completion receipt, check quality advisory + open items before proceeding.
Skills must NOT use @ prefix in Role field. Skill registry: skills/skills.yaml (repo) or \$VNX_SKILLS_DIR/skills.yaml (consumer).${T0_SKILL_BODY:+

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
