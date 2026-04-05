#!/bin/bash
# dispatch_create.sh — Payload building functions for dispatcher V8.
# Sourced by dispatcher_v8_minimal.sh.
# Requires: $VNX_DIR, $VNX_DATA_DIR, $ACTIVE_DIR set by orchestrator.
# Requires: dispatch_logging.sh sourced first.

# ===== INSTRUCTION EXTRACTION (V8 Core) =====

# Function to extract instruction content from dispatch
extract_instruction_content() {
    local dispatch_file="$1"

    # Extract content between "Instruction:" and "[[DONE]]"
    local content
    content=$(awk '/^Instruction:/{flag=1; next} /^\[\[DONE\]\]/{flag=0} flag' "$dispatch_file")
    if [ -n "$content" ]; then
        echo "$content"
        return 0
    fi

    # Fallback: use everything after YAML frontmatter, excluding [[TARGET:...]] markers.
    content=$(awk '
        BEGIN { in_frontmatter = 0; saw_frontmatter = 0 }
        /^---$/ {
            if (saw_frontmatter == 0) { saw_frontmatter = 1; in_frontmatter = 1; next }
            if (in_frontmatter == 1) { in_frontmatter = 0; next }
        }
        in_frontmatter == 1 { next }
        { print }
    ' "$dispatch_file" | sed '/^\[\[TARGET:/d')

    if [ -n "$content" ]; then
        echo "$content"
        return 0
    fi

    return 1
}

extract_context_files() {
    local dispatch_file="$1"

    # Extract Context: line(s) - simpler approach: grab Context line + next non-blank line before Instruction
    local context
    context=$(awk '
        /^Context:/ {
            sub(/^Context: */, "")
            context = $0
            in_context = 1
            next
        }
        in_context == 1 && /^$/ {
            next
        }
        in_context == 1 && /^Instruction:/ {
            in_context = 0
        }
        in_context == 1 && /^\[\[@/ {
            context = context " " $0
            next
        }
        in_context == 1 {
            in_context = 0
        }
        END {
            if (context) print context
        }
    ' "$dispatch_file" | tr ' ' '\n' | grep '^\[\[@' )

    if [ -n "$context" ]; then
        echo "$context"
        return 0
    fi

    # Fallback: YAML frontmatter context_files list.
    awk '
        BEGIN { in_frontmatter = 0; saw_frontmatter = 0; in_list = 0 }
        /^---$/ {
            if (saw_frontmatter == 0) { saw_frontmatter = 1; in_frontmatter = 1; next }
            if (in_frontmatter == 1) { in_frontmatter = 0; in_list = 0; next }
        }
        in_frontmatter == 1 && /^context_files:/ { in_list = 1; next }
        in_frontmatter == 1 && in_list == 1 {
            if ($0 ~ /^ *-/) { sub(/^ *- */, ""); print; next }
            if ($0 ~ /^[a-zA-Z_]+:/) { in_list = 0 }
        }
    ' "$dispatch_file"
}

# ===== RECEIPT GENERATION (from V7) =====

# _build_receipt_metadata — resolve dispatch and PR identifiers for receipt footer.
# Outputs: two lines — dispatch_id_for_footer and footer_pr_id.
_build_receipt_metadata() {
    local dispatch_file="$1"
    local dispatch_id="$2"

    local footer_pr_id
    footer_pr_id=$(extract_pr_id "$dispatch_file" 2>/dev/null)

    local dispatch_id_for_footer="$dispatch_id"
    if [ -z "$dispatch_id_for_footer" ]; then
        dispatch_id_for_footer=$(vnx_dispatch_extract_dispatch_id "$dispatch_file" 2>/dev/null)
    fi

    printf '%s\n%s\n' "${dispatch_id_for_footer:-unknown}" "${footer_pr_id:-unknown}"
}

# _emit_receipt_template — output the receipt footer heredoc template.
# Args: dispatch_id_for_footer pr_id track gate
_emit_receipt_template() {
    local rid="$1" pr="$2" track="$3" gate="$4"

    cat <<RECEIPT_EOF

---
# Task Completion Guidelines

## Report Metadata (REQUIRED — include this section in your report)

Your report MUST include this metadata block exactly as shown below. The receipt processor parses these fields to track progress and deliver receipts to T0.

\`\`\`
**Dispatch ID**: ${rid}
**PR**: ${pr}
**Track**: ${track}
**Gate**: ${gate}
**Status**: success
\`\`\`

## Before Completing

1. Stage and commit ALL code changes from this task:
   - Conventional commit: \`feat|fix|test|refactor(<scope>): <description>\`
   - Include in commit body: \`Dispatch-ID: ${rid}\`
   - Do NOT commit VNX infrastructure or state directories
2. Then write your report below

## Expected Outputs

When completing your task, create a markdown report with:

- **Implementation Summary**: What was done, key decisions made
- **Files Modified**: List of changed/created files with brief descriptions
- **Testing Evidence**: Test results, validation performed
- **Open Items**: Issues discovered outside dispatch scope (blocker/warn/info)

**Report Format**: Structured markdown with clear sections and evidence-based findings.

Write your report to: \`${VNX_DATA_DIR}/unified_reports/\`
Filename: \`$(date +%Y%m%d-%H%M%S)-${track}-<short-title>.md\`

---
*VNX V8 - Native Skills + Instruction-Only Dispatch*
RECEIPT_EOF
}

# Function to generate receipt footer
generate_receipt_footer() {
    local dispatch_file="$1"
    local track="$2"
    local phase="$3"
    local gate="$4"
    local task_id="$5"
    local cmd_id="$6"
    local dispatch_id="$7"

    local _meta_output _dispatch_id_for_footer _footer_pr_id
    _meta_output=$(_build_receipt_metadata "$dispatch_file" "$dispatch_id")
    _dispatch_id_for_footer=$(echo "$_meta_output" | head -1)
    _footer_pr_id=$(echo "$_meta_output" | tail -1)

    _emit_receipt_template "$_dispatch_id_for_footer" "$_footer_pr_id" "$track" "$gate"
}

# ===== SKILL ACTIVATION MAPPING (V8 Core) =====

# Function to map dispatch role to skill name
map_role_to_skill() {
    local role="$1"

    # Map dispatch roles to native skill names
    case "$role" in
        "debugging-specialist"|"debugging_specialist")
            echo "debugger"
            ;;
        "developer")
            echo "backend-developer"
            ;;
        "senior-developer")
            echo "reviewer"
            ;;
        "performance-engineer"|"perf-engineer")
            echo "performance-profiler"
            ;;
        "integration-specialist")
            echo "api-developer"
            ;;
        "refactoring-expert")
            echo "python-optimizer"
            ;;
        "planner"|"architect"|"backend-developer"|"api-developer"|"frontend-developer"|"test-engineer"|"security-engineer"|"quality-engineer"|"reviewer"|"debugger"|"data-analyst"|"supabase-expert"|"performance-profiler"|"excel-reporter"|"python-optimizer"|"monitoring-specialist"|"vnx-manager"|"t0-orchestrator")
            # Already native skill names - pass through
            echo "$role"
            ;;
        *)
            # Unknown role - pass through (log to stderr to avoid corrupting subshell capture)
            log "V8 WARNING: Unknown role '$role' - using as-is (may fail skill activation)" >&2
            echo "$role"
            ;;
    esac
}

# ===== NEW: prepare_dispatch_payload =====
# Extracted from dispatch_with_skill_activation lines 1355-1601 (except lease/registration).
# Performs: target_pane determination, pre-lease input mode probe, instruction_content extraction,
#           context_files extraction, metadata extraction, receipt_footer generation,
#           intelligence/context section building, terminal_id/provider resolution,
#           pr_id extraction, skill_command building, complete_prompt assembly.
# Params: dispatch_file track agent_role intelligence_data dispatch_id
# Sets globals: _DP_TARGET_PANE _DP_TERMINAL_ID _DP_PROVIDER _DP_COMPLETE_PROMPT _DP_SKILL_COMMAND
# Also sets: _DP_PR_ID _DP_GATE _DP_SKILL_NAME _DP_INSTRUCTION_CONTENT
# Returns 1 if any step blocks.
_DP_TARGET_PANE=""
_DP_TERMINAL_ID=""
_DP_PROVIDER=""
_DP_COMPLETE_PROMPT=""
_DP_SKILL_COMMAND=""
_DP_PR_ID=""
_DP_GATE=""
_DP_SKILL_NAME=""
_DP_INSTRUCTION_CONTENT=""

prepare_dispatch_payload() {
    local dispatch_file="$1"
    local track="$2"
    local agent_role="$3"
    local intelligence_data="${4:-}"
    local dispatch_id="${5:-}"

    _DP_TARGET_PANE=""
    _DP_TERMINAL_ID=""
    _DP_PROVIDER=""
    _DP_COMPLETE_PROMPT=""
    _DP_SKILL_COMMAND=""
    _DP_PR_ID=""
    _DP_GATE=""
    _DP_SKILL_NAME=""
    _DP_INSTRUCTION_CONTENT=""

    if [ -z "$dispatch_id" ]; then
        dispatch_id="$(basename "$dispatch_file" .md)"
    fi

    # Determine target terminal pane (MCP-aware routing)
    local requires_mcp
    requires_mcp=$(vnx_dispatch_extract_requires_mcp "$dispatch_file")
    local target_pane
    if ! target_pane=$(determine_executor "$track" "normal" "$requires_mcp"); then
        log "V8 ERROR: Failed to determine target terminal"
        return 1
    fi

    log "V8 DISPATCH: Routing to terminal $target_pane (Track: $track, Role: $agent_role)"

    # RES-B1 (OI-024): Best-effort pre-lease pane mode check before mode configuration.
    # If pane is in copy/search mode, skip mode config (which uses send-keys) to avoid
    # corrupting operator scrollback. No lease is held at this point — safe to return 1.
    local _pre_probe
    if _pre_probe=$(_input_mode_probe "$target_pane" 2>/dev/null); then
        local _pre_in_mode
        IFS=: read -r _pre_in_mode _ _ <<< "$_pre_probe"
        if [[ "$_pre_in_mode" == "1" ]]; then
            log "V8 INPUT_MODE: pre-lease probe blocked — pane in non-interactive mode, skipping mode config pane=$target_pane dispatch=$(basename "$dispatch_file")"
            return 1
        fi
    fi

    # Configure terminal mode (clear, model switch, mode activation)
    # RES-B2: Use pre_mode_configuration canonical failure code on failure.
    if ! configure_terminal_mode "$target_pane" "$dispatch_file"; then
        log_structured_failure "mode_configuration_failed" "Terminal mode configuration failed" \
            "pane=$target_pane dispatch=$(basename "$dispatch_file")" \
            "pre_mode_configuration" "$(basename "$dispatch_file")" "" ""
        return 1
    fi

    # CRITICAL: Add delay after mode configuration to ensure commands complete
    sleep 2

    # Pre-clear: ensure terminal input line is empty before skill activation
    # NOTE: Do NOT use C-c here — it kills the CLI process, leaving a bare
    # zsh shell where dispatch content gets executed as shell commands.
    # C-u alone safely clears the readline input buffer.
    tmux_send_best_effort "$target_pane" C-u 2>/dev/null || true
    sleep 0.5

    # Map role to skill name
    local skill_name
    skill_name=$(map_role_to_skill "$agent_role")
    if [ -z "$skill_name" ]; then
        log "V8 WARNING: Empty skill name for role '$agent_role' (waiting for edit)"
        if ! grep -q "\[SKILL_INVALID\]" "$dispatch_file"; then
            echo -e "\n\n[SKILL_INVALID] Skill for role '$agent_role' not found. Update Role and remove this marker to retry.\n" >> "$dispatch_file"
        fi
        return 1
    fi

    # Validate skill against skills.yaml before dispatching
    if ! python3 "$VNX_DIR/scripts/validate_skill.py" "$skill_name" >/dev/null 2>&1; then
        log "V8 WARNING: Skill '@${skill_name}' not found in skills.yaml (waiting for edit)"
        if ! grep -q "\[SKILL_INVALID\]" "$dispatch_file"; then
            echo -e "\n\n[SKILL_INVALID] Skill '@${skill_name}' not found in skills.yaml. Update Role and remove this marker to retry.\n" >> "$dispatch_file"
        fi
        return 1
    fi

    log "V8 SKILL: Activating skill @$skill_name for role $agent_role"

    # Extract instruction content
    local instruction_content
    if ! instruction_content=$(extract_instruction_content "$dispatch_file"); then
        log "V8 ERROR: Failed to extract instruction content"
        return 1
    fi

    if [ -z "$instruction_content" ]; then
        log "V8 ERROR: No instruction content found in dispatch"
        return 1
    fi

    # Extract context files (Workflow + Context lines with @ references)
    local context_files
    context_files=$(extract_context_files "$dispatch_file")
    if [ -n "$context_files" ]; then
        log "V8 CONTEXT: Extracted context files from dispatch"
    fi

    # Extract metadata for receipt
    local phase
    phase=$(extract_phase "$dispatch_file")
    local gate
    gate=$(extract_new_gate "$dispatch_file")
    local task_id
    task_id=$(extract_task_id "$dispatch_file" "$track")
    local cmd_id
    cmd_id=$(uuidgen 2>/dev/null || echo "$(date +%s)-$$" | sha256sum | cut -c1-16)

    # Fallback to planning if no gate specified
    if [ -z "$gate" ]; then
        log "V8: No gate specified, defaulting to 'planning'"
        gate="planning"
    fi

    # Generate receipt footer
    # RES-D3: Receipt footer generation failure is intentionally non-fatal.
    local receipt_footer
    if ! receipt_footer=$(generate_receipt_footer "$dispatch_file" "$track" "$phase" "$gate" "$task_id" "$cmd_id" "$dispatch_id"); then
        log "V8 WARNING: Failed to generate receipt footer, continuing without (intentionally non-fatal per RES-D3)"
        receipt_footer=""
    fi

    # Format intelligence data if provided
    local intelligence_section=""
    if [ -n "$intelligence_data" ]; then
        # Extract pattern summaries (title + description, max 5 patterns)
        local pattern_summaries
        pattern_summaries=$(echo "$intelligence_data" | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
    patterns = data.get("suggested_patterns", [])[:5]  # Top 5 patterns
    if patterns:
        print("### 🧠 Relevant Patterns\n")
        for p in patterns:
            title = p.get("title", "Unknown")
            desc = p.get("description", "")[:100]
            rel = p.get("relevance_score", 0)
            fp = p.get("file_path", "")
            lr = p.get("line_range", "")
            loc = f" @ `{fp}:{lr}`" if fp and lr else ""
            print(f"- **{title}** (relevance: {rel:.2f}): {desc}{loc}")
except (json.JSONDecodeError, TypeError) as exc:
    print(f"[NON_CRITICAL] pattern_summary_parse_failed: {exc}", file=sys.stderr)
' 2>/dev/null)

        # Extract prevention rules
        local prevention_summaries
        prevention_summaries=$(echo "$intelligence_data" | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
    rules = data.get("prevention_rules", [])[:3]  # Top 3 rules
    if rules:
        print("\n### ⚠️ Prevention Rules\n")
        for r in rules:
            print(f"- {r}")
except (json.JSONDecodeError, TypeError) as exc:
    print(f"[NON_CRITICAL] prevention_summary_parse_failed: {exc}", file=sys.stderr)
' 2>/dev/null)

        # Combine intelligence sections if they exist
        if [ -n "$pattern_summaries" ] || [ -n "$prevention_summaries" ]; then
            intelligence_section="

---
## Intelligence Context

$pattern_summaries$prevention_summaries

---
"
        fi
    fi

    # Build context section if files were specified
    local context_section=""
    if [ -n "$context_files" ]; then
        context_section="

---
## Context Files

Read the following files for context before starting:

$context_files

---
"
    fi

    # Resolve terminal_id and provider early (needed for skill command format)
    local terminal_id
    if ! terminal_id="$(get_terminal_from_pane "$target_pane" 2>/dev/null)"; then
        terminal_id=""
        log_structured_failure "terminal_resolution_failed" "Failed to resolve terminal id from pane" "pane=$target_pane"
    fi
    if [ -z "$terminal_id" ] || [ "$terminal_id" = "UNKNOWN" ]; then
        terminal_id="$(track_to_terminal "$track")"
    fi

    if [ -z "$terminal_id" ]; then
        log "V8 LOCK: unable to resolve terminal for track=$track dispatch=$dispatch_id"
        return 1
    fi

    local provider
    provider=$(get_terminal_provider "$terminal_id")

    # Extract PR-ID early so it can be included in the prompt
    local pr_id
    pr_id=$(extract_pr_id "$dispatch_file")

    # BUILD COMPLETE PROMPT: skill activation + context + intelligence + instruction + receipt
    # V8.1: Hybrid dispatch - skill via send-keys, instruction via paste-buffer
    # Provider-aware skill invocation:
    #   Claude Code: /skill-name  (slash command)
    #   Codex CLI:   $skill-name  (dollar-sign mention)
    #   Gemini CLI:  @skill-name  (at-sign prefix, also auto-activates on description match)
    local skill_command
    local extra_skills_hint
    case "$provider" in
        codex_cli|codex)
            skill_command="\$${skill_name} "
            extra_skills_hint="Use additional skills as needed (\$test-engineer, \$reviewer, \$debugger) to deliver production-quality results."
            ;;
        gemini_cli|gemini)
            skill_command="@${skill_name} "
            extra_skills_hint="Use additional skills as needed (@test-engineer, @reviewer, @debugger) to deliver production-quality results."
            ;;
        *)
            skill_command="/${skill_name} "
            extra_skills_hint="Use additional skills as needed (/test-engineer, /reviewer, /debugger) to deliver production-quality results."
            ;;
    esac

    log "V8 SKILL_FORMAT: provider=$provider command='${skill_command}'"

    # Build dispatch header so workers know what they're working on
    local dispatch_header="## Dispatch Assignment
| Field | Value |
|-------|-------|
| **PR** | ${pr_id:-unknown} |
| **Dispatch-ID** | ${dispatch_id} |
| **Track** | ${track} |
| **Gate** | ${gate} |
"

    local complete_prompt="${dispatch_header}
Apply your specialized expertise to this task.

**Critical Success Factors:**
- Maintain high code quality standards and best practices
- Ensure comprehensive test coverage where applicable
- Follow established project patterns and conventions
- Validate all changes against requirements
- Document significant design decisions

$extra_skills_hint
$context_section$intelligence_section
$instruction_content

$receipt_footer"

    # Set output globals
    _DP_TARGET_PANE="$target_pane"
    _DP_TERMINAL_ID="$terminal_id"
    _DP_PROVIDER="$provider"
    _DP_COMPLETE_PROMPT="$complete_prompt"
    _DP_SKILL_COMMAND="$skill_command"
    _DP_PR_ID="$pr_id"
    _DP_GATE="$gate"
    _DP_SKILL_NAME="$skill_name"
    _DP_INSTRUCTION_CONTENT="$instruction_content"

    return 0
}
