#!/bin/bash
# dispatch_create.sh — Payload building functions for dispatcher V8.
# Sourced by dispatcher_v8_minimal.sh.
# Requires: $VNX_DIR, $VNX_DATA_DIR, $ACTIVE_DIR set by orchestrator.
# Requires: dispatch_logging.sh sourced first.

# ===== INSTRUCTION EXTRACTION =====

extract_instruction_content() {
    local dispatch_file="$1"
    # Extract content between "Instruction:" and "[[DONE]]"
    local content
    content=$(awk '/^Instruction:/{flag=1; next} /^\[\[DONE\]\]/{flag=0} flag' "$dispatch_file")
    if [ -n "$content" ]; then
        echo "$content"
        return 0
    fi
    # Fallback: everything after YAML frontmatter, excluding [[TARGET:...]] markers.
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
    # Extract Context: line(s) with @ references.
    # The trailing `|| true` is required: under the parent shell's
    # `set -euo pipefail`, when a dispatch has no inline `[[@...]]` context
    # refs, `grep` exits 1 with no matches; pipefail then propagates that
    # nonzero status out of the command substitution and aborts the function
    # before the YAML-frontmatter fallback runs. See codex round-2 finding 3.
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
    ' "$dispatch_file" | tr ' ' '\n' | grep '^\[\[@' || true)
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

# ===== RECEIPT GENERATION =====

# _build_receipt_metadata — outputs two lines: dispatch_id_for_footer, footer_pr_id.
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
2. If this is a feature or fix: update \`CHANGELOG.md\` with a one-line entry
3. Then write your report below

## Expected Outputs (ALL sections REQUIRED)

When completing your task, create a markdown report with ALL of these sections:

- **Implementation Summary**: What was done, key decisions made
- **Files Modified**: List of changed/created files with brief descriptions
- **Testing Evidence**: Test results with pass counts (e.g. "32 passed in 0.04s")
- **Commit**: Include the git commit hash (run \`git log --oneline -1\`)
- **Open Items**: Issues discovered outside dispatch scope — REQUIRED even if empty (write "None")

**Report Format**: Structured markdown with clear sections and evidence-based findings.

Write your report to: \`${VNX_DATA_DIR}/unified_reports/\`
Filename: \`$(date +%Y%m%d-%H%M%S)-${track}-<short-title>.md\`

---
*VNX V8 - Native Skills + Instruction-Only Dispatch*
RECEIPT_EOF
}

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

# ===== SKILL ACTIVATION MAPPING =====

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

# prepare_dispatch_payload — build complete prompt and resolve execution target.
# Sets _DP_* globals. Returns 1 if any step blocks.
_DP_TARGET_PANE=""
_DP_TERMINAL_ID=""
_DP_PROVIDER=""
_DP_COMPLETE_PROMPT=""
_DP_SKILL_COMMAND=""
_DP_PR_ID=""
_DP_GATE=""
_DP_SKILL_NAME=""
_DP_INSTRUCTION_CONTENT=""

# _pdp_resolve_target — determine target pane with MCP routing and pre-lease probe.
# Sets _PDP_TARGET_PANE on success. Returns 1 if routing or probe blocks.
# For tmux-routed terminals, only validates dispatch settings and probes input
# mode — actual terminal I/O (context clear, model switch, mode activation) is
# deferred to _pdp_apply_terminal_mode_setup, which must be called AFTER the
# dispatch lease is acquired to avoid wiping a worker terminal on lease failure.
_PDP_TARGET_PANE=""
_PDP_NEEDS_MODE_SETUP=0  # 1 when tmux I/O setup is pending (post-lease)
_pdp_resolve_target() {
    local dispatch_file="$1" track="$2" agent_role="$3"
    _PDP_TARGET_PANE=""
    _PDP_NEEDS_MODE_SETUP=0

    local requires_mcp
    requires_mcp=$(vnx_dispatch_extract_requires_mcp "$dispatch_file")
    local target_pane
    if ! target_pane=$(determine_executor "$track" "normal" "$requires_mcp"); then
        log "V8 ERROR: Failed to determine target terminal"
        return 1
    fi

    log "V8 DISPATCH: Routing to terminal $target_pane (Track: $track, Role: $agent_role)"

    # Detect subprocess-routed terminals; skip tmux MODE_CONTROL pre-flight for them.
    # Uses same adapter resolution logic as deliver_dispatch_to_terminal().
    local _rt_terminal_id
    _rt_terminal_id=$(get_terminal_from_pane "$target_pane" "$STATE_DIR/panes.json" 2>/dev/null || true)
    if [ -z "$_rt_terminal_id" ] || [ "$_rt_terminal_id" = "UNKNOWN" ]; then
        _rt_terminal_id="$(track_to_terminal "$track")"
    fi
    local _rt_adapter_var="VNX_ADAPTER_${_rt_terminal_id}"
    local _rt_adapter_type="${!_rt_adapter_var:-tmux}"
    if [[ "$_rt_terminal_id" == "T1" && "$_rt_adapter_type" == "tmux" && -z "${!_rt_adapter_var:-}" ]]; then
        _rt_adapter_type="subprocess"
    fi

    if [[ "$_rt_adapter_type" == "subprocess" ]]; then
        log "V8 DISPATCH: subprocess adapter — skipping tmux MODE_CONTROL pre-flight terminal=$_rt_terminal_id"
        # mode_pre_check sets _CTM_REQUIRES_MODEL and other globals consumed by deliver_dispatch_to_terminal.
        mode_pre_check "$target_pane" "$dispatch_file" || return 1
        _PDP_TARGET_PANE="$target_pane"
        return 0
    fi

    # RES-B1: Best-effort pre-lease pane mode check; skip if in copy/search mode.
    local _pre_probe
    if _pre_probe=$(_input_mode_probe "$target_pane" 2>/dev/null); then
        local _pre_in_mode
        IFS=: read -r _pre_in_mode _ _ <<< "$_pre_probe"
        if [[ "$_pre_in_mode" == "1" ]]; then
            log "V8 INPUT_MODE: pre-lease probe blocked — pane in non-interactive mode, skipping mode config pane=$target_pane dispatch=$(basename "$dispatch_file")"
            return 1
        fi
    fi

    # RES-B2: Validate dispatch settings and populate _CTM_* globals (read-only).
    # Actual terminal I/O (context clear, model switch, mode activation) is deferred
    # to _pdp_apply_terminal_mode_setup, called post-lease by dispatch_with_skill_activation.
    if ! mode_pre_check "$target_pane" "$dispatch_file"; then
        log_structured_failure "mode_pre_check_failed" "Terminal mode pre-check failed" \
            "pane=$target_pane dispatch=$(basename "$dispatch_file")" \
            "pre_mode_configuration" "$(basename "$dispatch_file")" "" ""
        return 1
    fi

    _PDP_NEEDS_MODE_SETUP=1
    _PDP_TARGET_PANE="$target_pane"
    return 0
}

# _pdp_apply_terminal_mode_setup — apply deferred terminal I/O after lease is acquired.
# Sends context-clear, model-switch, and mode-activation commands to the tmux pane.
# Must only be called for tmux-routed terminals when _PDP_NEEDS_MODE_SETUP=1.
# Requires _CTM_* globals set by the earlier mode_pre_check call in _pdp_resolve_target.
_pdp_apply_terminal_mode_setup() {
    local target_pane="$1" dispatch_file="$2"

    if ! reset_terminal_context "$target_pane" "$_CTM_FORCE_NORMAL" "$_CTM_CLEAR_CONTEXT" "$_CTM_PROVIDER"; then
        log_structured_failure "mode_configuration_failed" "Terminal context reset failed post-lease" \
            "pane=$target_pane dispatch=$(basename "$dispatch_file")"
        return 1
    fi
    if ! switch_terminal_model "$target_pane" "$_CTM_REQUIRES_MODEL" "$_CTM_REQUIRES_MODEL_STRENGTH" \
            "$_CTM_PROVIDER" "$_CTM_TERMINAL_ID" "$dispatch_file"; then
        log_structured_failure "mode_configuration_failed" "Terminal model switch failed post-lease" \
            "pane=$target_pane dispatch=$(basename "$dispatch_file")"
        return 1
    fi
    if ! activate_terminal_mode "$target_pane" "$_CTM_MODE" "$_CTM_PROVIDER"; then
        log_structured_failure "mode_configuration_failed" "Terminal mode activation failed post-lease" \
            "pane=$target_pane dispatch=$(basename "$dispatch_file")"
        return 1
    fi

    sleep 2  # delay after mode configuration

    # Pre-clear input line (C-u only; C-c kills CLI process).
    tmux_send_best_effort "$target_pane" C-u 2>/dev/null || true
    sleep 0.5

    log "V8 MODE_CONTROL: Post-lease terminal setup complete pane=$target_pane"
    return 0
}

# _pdp_resolve_skill — validate and resolve skill name from agent role.
# Outputs skill name on stdout. Returns 1 if skill is invalid.
_pdp_resolve_skill() {
    local agent_role="$1" dispatch_file="$2"

    local skill_name
    skill_name=$(map_role_to_skill "$agent_role")
    if [ -z "$skill_name" ]; then
        log "V8 WARNING: Empty skill name for role '$agent_role' (waiting for edit)"
        if ! grep -q "\[SKILL_INVALID\]" "$dispatch_file"; then
            echo -e "\n\n[SKILL_INVALID] Skill for role '$agent_role' not found. Update Role and remove this marker to retry.\n" >> "$dispatch_file"
        fi
        return 1
    fi

    if ! python3 "$VNX_DIR/scripts/validate_skill.py" "$skill_name" >/dev/null 2>&1; then
        log "V8 WARNING: Skill '@${skill_name}' not found in skills.yaml (waiting for edit)"
        if ! grep -q "\[SKILL_INVALID\]" "$dispatch_file"; then
            echo -e "\n\n[SKILL_INVALID] Skill '@${skill_name}' not found in skills.yaml. Update Role and remove this marker to retry.\n" >> "$dispatch_file"
        fi
        return 1
    fi

    log "V8 SKILL: Activating skill @$skill_name for role $agent_role"
    echo "$skill_name"
}

# _pdp_extract_dispatch_metadata — extract phase, gate, task_id, cmd_id from dispatch.
# Sets _PDP_GATE. Outputs receipt footer on stdout (may be empty per RES-D3).
_PDP_GATE=""
_pdp_extract_dispatch_metadata() {
    local dispatch_file="$1" track="$2" dispatch_id="$3"
    _PDP_GATE=""

    local phase gate task_id cmd_id
    phase=$(extract_phase "$dispatch_file")
    gate=$(extract_new_gate "$dispatch_file")
    task_id=$(extract_task_id "$dispatch_file" "$track")
    cmd_id=$(uuidgen 2>/dev/null || echo "$(date +%s)-$$" | sha256sum | cut -c1-16)

    if [ -z "$gate" ]; then
        log "V8: No gate specified, defaulting to 'planning'"
        gate="planning"
    fi
    _PDP_GATE="$gate"

    # RES-D3: receipt footer failure is intentionally non-fatal.
    local receipt_footer
    if ! receipt_footer=$(generate_receipt_footer "$dispatch_file" "$track" "$phase" "$gate" "$task_id" "$cmd_id" "$dispatch_id"); then
        log "V8 WARNING: Failed to generate receipt footer, continuing without (intentionally non-fatal per RES-D3)"
        receipt_footer=""
    fi
    printf '%s' "$receipt_footer"
}

# _pdp_build_intelligence_section — render intelligence data into markdown section.
# Outputs markdown string on stdout (empty if no data).
_pdp_build_intelligence_section() {
    local intelligence_data="$1"
    [ -n "$intelligence_data" ] || return 0

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

    if [ -n "$pattern_summaries" ] || [ -n "$prevention_summaries" ]; then
        printf '\n\n---\n## Intelligence Context\n\n%s%s\n\n---\n' \
            "$pattern_summaries" "$prevention_summaries"
    fi
}

# _pdp_build_context_section — render context files into markdown section.
# Outputs markdown string on stdout (empty if no context files).
_pdp_build_context_section() {
    local context_files="$1"
    [ -n "$context_files" ] || return 0
    printf '\n\n---\n## Context Files\n\nRead the following files for context before starting:\n\n%s\n\n---\n' \
        "$context_files"
}

# _pdp_resolve_terminal — resolve terminal_id from pane, falling back to track mapping.
# Outputs terminal_id on stdout. Returns 1 if unresolvable.
_pdp_resolve_terminal() {
    local target_pane="$1" track="$2" dispatch_id="$3"

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
    echo "$terminal_id"
}

# _pdp_build_skill_command — resolve provider-aware skill invocation prefix and hints.
# Sets _PDP_SKILL_COMMAND and _PDP_EXTRA_SKILLS_HINT.
_PDP_SKILL_COMMAND="" _PDP_EXTRA_SKILLS_HINT=""
_pdp_build_skill_command() {
    local skill_name="$1" provider="$2"
    _PDP_SKILL_COMMAND="" _PDP_EXTRA_SKILLS_HINT=""

    case "$provider" in
        codex_cli|codex)
            _PDP_SKILL_COMMAND="\$${skill_name} "
            _PDP_EXTRA_SKILLS_HINT="Use additional skills as needed (\$test-engineer, \$reviewer, \$debugger) to deliver production-quality results."
            ;;
        gemini_cli|gemini)
            _PDP_SKILL_COMMAND="@${skill_name} "
            _PDP_EXTRA_SKILLS_HINT="Use additional skills as needed (@test-engineer, @reviewer, @debugger) to deliver production-quality results."
            ;;
        *)
            _PDP_SKILL_COMMAND="/${skill_name} "
            _PDP_EXTRA_SKILLS_HINT="Use additional skills as needed (/test-engineer, /reviewer, /debugger) to deliver production-quality results."
            ;;
    esac
    log "V8 SKILL_FORMAT: provider=$provider command='${_PDP_SKILL_COMMAND}'"
}

# _pdp_assemble_prompt — combine all sections into the complete dispatch prompt.
# Outputs the assembled prompt on stdout.
_pdp_assemble_prompt() {
    local pr_id="$1" dispatch_id="$2" track="$3" gate="$4"
    local extra_skills_hint="$5" context_section="$6" intelligence_section="$7"
    local instruction_content="$8" receipt_footer="$9"

    local dispatch_header="## Dispatch Assignment
| Field | Value |
|-------|-------|
| **PR** | ${pr_id:-unknown} |
| **Dispatch-ID** | ${dispatch_id} |
| **Track** | ${track} |
| **Gate** | ${gate} |
"

    printf '%s\nApply your specialized expertise to this task.\n\n**Critical Success Factors:**\n- Maintain high code quality standards and best practices\n- Ensure comprehensive test coverage where applicable\n- Follow established project patterns and conventions\n- Validate all changes against requirements\n- Document significant design decisions\n\n%s\n%s%s\n%s\n\n%s' \
        "$dispatch_header" "$extra_skills_hint" "$context_section" "$intelligence_section" \
        "$instruction_content" "$receipt_footer"
}

prepare_dispatch_payload() {
    local dispatch_file="$1" track="$2" agent_role="$3"
    local intelligence_data="${4:-}" dispatch_id="${5:-}"
    _DP_TARGET_PANE="" _DP_TERMINAL_ID="" _DP_PROVIDER="" _DP_COMPLETE_PROMPT=""
    _DP_SKILL_COMMAND="" _DP_PR_ID="" _DP_GATE="" _DP_SKILL_NAME="" _DP_INSTRUCTION_CONTENT=""
    [ -n "$dispatch_id" ] || dispatch_id="$(basename "$dispatch_file" .md)"

    _pdp_resolve_target "$dispatch_file" "$track" "$agent_role" || return 1
    local target_pane="$_PDP_TARGET_PANE"
    local skill_name; skill_name=$(_pdp_resolve_skill "$agent_role" "$dispatch_file") || return 1

    local instruction_content
    if ! instruction_content=$(extract_instruction_content "$dispatch_file") || [ -z "$instruction_content" ]; then
        log "V8 ERROR: Failed to extract instruction content"; return 1
    fi

    local context_files; context_files=$(extract_context_files "$dispatch_file")
    [ -z "$context_files" ] || log "V8 CONTEXT: Extracted context files from dispatch"

    local receipt_footer; receipt_footer=$(_pdp_extract_dispatch_metadata "$dispatch_file" "$track" "$dispatch_id")
    local gate="$_PDP_GATE"
    local intelligence_section; intelligence_section=$(_pdp_build_intelligence_section "$intelligence_data")
    local context_section; context_section=$(_pdp_build_context_section "$context_files")
    local terminal_id; terminal_id=$(_pdp_resolve_terminal "$target_pane" "$track" "$dispatch_id") || return 1
    local provider; provider=$(get_terminal_provider "$terminal_id")
    local pr_id; pr_id=$(extract_pr_id "$dispatch_file")

    _pdp_build_skill_command "$skill_name" "$provider"
    local complete_prompt; complete_prompt=$(_pdp_assemble_prompt "$pr_id" "$dispatch_id" "$track" "$gate" \
        "$_PDP_EXTRA_SKILLS_HINT" "$context_section" "$intelligence_section" \
        "$instruction_content" "$receipt_footer")

    _DP_TARGET_PANE="$target_pane" _DP_TERMINAL_ID="$terminal_id" _DP_PROVIDER="$provider"
    _DP_COMPLETE_PROMPT="$complete_prompt" _DP_SKILL_COMMAND="$_PDP_SKILL_COMMAND"
    _DP_PR_ID="$pr_id" _DP_GATE="$gate" _DP_SKILL_NAME="$skill_name"
    _DP_INSTRUCTION_CONTENT="$instruction_content"
    return 0
}
