# VNX Dispatcher System - Technical Reference
**Owner**: T-MANAGER

**Version**: V7.3 (Template Compilation System) — **LEGACY REFERENCE**
**Status**: Reference (V8.2 is current production)
**Last Updated**: 2026-02-18
**Purpose**: V7.3 template compilation reference. For current production dispatcher, see V8 section below.

---

> **NOTE**: This document describes the V7.3 template compilation dispatcher which is no longer
> the production dispatcher. **V8.2 Minimal** (`dispatcher_v8_minimal.sh`) is the current
> production system. V7 is maintained as a reference and rollback option.
>
> For architecture overview, see `core/00_VNX_ARCHITECTURE.md`.

---

## V8.2 Production Dispatcher (Current)

### Overview

The V8 dispatcher (`dispatcher_v8_minimal.sh`) replaces V7's template compilation with native skill activation. This achieves an 87% token reduction (200 vs 1500 tokens per dispatch).

### Key Differences from V7

| Feature | V7.3 | V8.2 |
|---------|------|------|
| Token per dispatch | ~1500 | ~200 |
| Template compilation | Yes (agent library) | No (native skills) |
| Skill activation | N/A | `/skill-name` via send-keys |
| Instruction delivery | paste-buffer (full prompt) | paste-buffer (instruction only) |
| Multi-provider | No | Yes (Claude/Codex/Gemini) |
| Receipt footer | Basic | Rich (Expected Outputs + Report Metadata) |
| PR-ID tracking | No | Yes (included in prompt) |

### Multi-Provider Skill Invocation

```bash
# Claude Code:  /skill-name
# Codex CLI:    $skill-name
# Gemini CLI:   @skill-name
```

The dispatcher detects the terminal's provider and uses the correct invocation format.

### Receipt Footer (V8.2)

Every dispatch includes a structured footer with:
- **Report Metadata** block (Dispatch ID, PR, Track, Gate, Status) — parsed by receipt processor
- **Expected Outputs** section (Implementation Summary, Files Modified, Testing Evidence, Open Items)
- Report write path: `.vnx-data/unified_reports/`

### Parallel PR Queue Support

V8.2 supports multiple PRs in progress simultaneously across different tracks:
- `pr_queue_state.yaml` stores `in_progress` as a list (not a single string)
- Multiple tracks can run in parallel (e.g., PR-2 on Track A + PR-5 on Track B)
- Backward compatible: reads old single-string format

### Singleton Enforcement (V8)

The dispatcher uses `vnx_proc_acquire_lock()` with:
- Atomic `mkdir` for lock acquisition
- PID + fingerprint validation
- **Timestamp-based stale expiry**: locks include `created_at` and `heartbeat` timestamps; locks older than `VNX_LOCK_MAX_AGE` (default 3600s) are auto-expired

### Large Payload Transport (V8)

`tmux_load_buffer_safe()` prevents silent truncation of large dispatch payloads:
- Payloads > `VNX_DISPATCH_MAX_INLINE` (default 50KB) are written to a temp file at `$VNX_DATA_DIR/dispatch_payloads/`
- `tmux load-buffer <file>` replaces stdin pipe
- Temp files are cleaned after successful delivery; stale files (>60min) cleaned by `vnx recover`

### Retry with Exponential Backoff (V8)

`tmux_retry(max_attempts, command...)` wraps all tmux delivery operations:
- 3 attempts with exponential backoff (1s, 2s, 4s)
- Applies to: `tmux_load_buffer_safe`, `tmux paste-buffer`, `tmux send-keys`

### Recovery Command (V8)

`vnx recover [--aggressive] [--dry-run]` performs 6 recovery actions:
1. Clear stale process locks (age + PID death detection)
2. Kill orphan processes (scoped to project)
3. Move incomplete dispatches from `active/` to `failed/`
4. Reset stale terminal claims to idle
5. Clear unclean-shutdown marker
6. Clean stale payload temp files

---

## V7.3 Legacy Reference

*The following sections document the V7.3 template compilation system for reference.*

## Table of Contents
1. [Overview](#overview)
2. [System Architecture](#system-architecture)
3. [Manager Block Processing](#manager-block-processing)
4. [Validation Rules](#validation-rules)
5. [Injection Rules](#injection-rules)
6. [Parsing Rules](#parsing-rules)
7. [Template System](#template-system)
8. [Pane Discovery System](#pane-discovery-system)
9. [Configuration](#configuration)
10. [Operations](#operations)
11. [Integration](#integration)
12. [Testing](#testing)
13. [Appendix](#appendix)

---

## Overview

### Purpose

The VNX Dispatcher System (V7.3) is the core orchestration engine responsible for routing manager blocks from T0 to worker terminals (T1, T2, T3). It performs template compilation, intelligence injection, cognitive analysis, and multi-format dispatch delivery.

### Key Capabilities

1. **Universal Role Support**: Handles ALL agent roles (analyst, architect, debugging_specialist, developer, integration-specialist, junior_developer, performance-engineer, quality-engineer, refactoring-expert, security-engineer, senior-developer)
2. **Template Compilation**: Dynamic prompt generation with instruction/constraints/context injection
3. **Intelligence Integration**: Embeds quality patterns and prevention rules from historical data
4. **Cognitive Analysis**: Automatic complexity detection and flag recommendations
5. **Multi-Format Support**: JSON and Markdown dispatch formats
6. **Track-Based Routing**: A→T1, B→T2, C→T3 with cognition overrides
7. **Progress State Management**: Atomic updates to gate/phase tracking
8. **Smart Pane Discovery**: Self-healing pane ID resolution

### Version Information

- **V5 Dispatcher**: Deprecated (simple dispatch, no template compilation)
- **V7 Dispatcher**: Production active (template compilation, all roles)
- **V7.3 Features**: Template compilation, instruction injection, constraints injection, context injection
- **V7.4 Features**: Intelligence injection with quality_context embedding

---

## System Architecture

### Component Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    DISPATCHER V7 ARCHITECTURE                   │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────────┐                                           │
│  │   Pending Queue  │  (dispatches/pending/*.md)                │
│  └────────┬─────────┘                                           │
│           │                                                      │
│           ▼                                                      │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │         DISPATCHER V7 COMPILATION ENGINE                │   │
│  │  ┌──────────────────────────────────────────────────┐   │   │
│  │  │  1. Extract Metadata                             │   │   │
│  │  │     • Track (A, B, C)                           │   │   │
│  │  │     • Role (architect, developer, etc.)         │   │   │
│  │  │     • Cognition (normal, deep)                  │   │   │
│  │  │     • Priority (P0, P1, P2)                     │   │   │
│  │  │     • Phase, Gates                              │   │   │
│  │  └──────────────────────────────────────────────────┘   │   │
│  │           │                                              │   │
│  │           ▼                                              │   │
│  │  ┌──────────────────────────────────────────────────┐   │   │
│  │  │  2. Validate Dispatch                            │   │   │
│  │  │     • Agent role validation (V7.4)              │   │   │
│  │  │     • Track validation (no T0 dispatch)         │   │   │
│  │  │     • Block structure validation                │   │   │
│  │  └──────────────────────────────────────────────────┘   │   │
│  │           │                                              │   │
│  │           ▼                                              │   │
│  │  ┌──────────────────────────────────────────────────┐   │   │
│  │  │  3. Gather Intelligence (V7.4)                  │   │   │
│  │  │     • Query quality_intelligence.db             │   │   │
│  │  │     • Extract top 5 relevant patterns           │   │   │
│  │  │     • Compile prevention rules                  │   │   │
│  │  │     • Embed quality_context JSON                │   │   │
│  │  └──────────────────────────────────────────────────┘   │   │
│  │           │                                              │   │
│  │           ▼                                              │   │
│  │  ┌──────────────────────────────────────────────────┐   │   │
│  │  │  4. Template Compilation                         │   │   │
│  │  │     • Resolve agent template path               │   │   │
│  │  │     • Extract instruction content               │   │   │
│  │  │     • Load constraints content                  │   │   │
│  │  │     • Extract context file references           │   │   │
│  │  │     • Inject intelligence patterns              │   │   │
│  │  └──────────────────────────────────────────────────┘   │   │
│  │           │                                              │   │
│  │           ▼                                              │   │
│  │  ┌──────────────────────────────────────────────────┐   │   │
│  │  │  5. Terminal Routing                             │   │   │
│  │  │     • Determine executor (track + cognition)    │   │   │
│  │  │     • Resolve pane ID (smart discovery)         │   │   │
│  │  │     • Deliver via send-keys (skill) + paste     │   │   │
│  │  └──────────────────────────────────────────────────┘   │   │
│  │           │                                              │   │
│  │           ▼                                              │   │
│  │  ┌──────────────────────────────────────────────────┐   │   │
│  │  │  6. Progress State Update                        │   │   │
│  │  │     • Update progress_state.yaml                │   │   │
│  │  │     • Notify heartbeat ACK monitor              │   │   │
│  │  │     • Move to completed/                        │   │   │
│  │  └──────────────────────────────────────────────────┘   │   │
│  └─────────────────────────────────────────────────────────┘   │
│           │                                                     │
│           ▼                                                     │
│  ┌──────────────────┐                                          │
│  │  Worker Terminal │  (T1, T2, T3)                            │
│  └──────────────────┘                                          │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Integration Points

1. **Smart Tap V7**: Captures manager blocks → queue directory
2. **Queue Popup Watcher**: Human approval → pending directory
3. **Dispatcher V7**: Template compilation → terminal delivery
4. **Intelligence System**: Pattern injection → enhanced context
5. **Progress State Manager**: Atomic gate updates → state tracking
6. **ACK Monitor**: Dispatch notification → acknowledgment tracking

---

## Manager Block Processing

### V2 Manager Block Format

**Standard Markdown Format**:
```markdown
[[TARGET:A]]
Role: architect
Phase: 2
Gate: implementation
Priority: P0; Cognition: deep
Context: @.claude/vnx-system/docs/architecture/00_VNX_ARCHITECTURE.md
Instruction:
Design the authentication service architecture with JWT tokens.
Consider scalability and security requirements.
[[DONE]]
```

### Metadata Extraction

The dispatcher extracts the following metadata from manager blocks:

**Track Extraction** (Line 73-77):
```bash
extract_track() {
    local file="$1"
    grep -o '^\[\[TARGET:[A-C]\]\]' "$file" | sed 's/.*TARGET://' | sed 's/]].*//' | head -1
}
```

**Cognition Level Extraction** (Line 79-90):
```bash
extract_cognition() {
    local file="$1"
    local cognition=$(grep -i "Cognition:" "$file" | sed 's/.*Cognition:\s*//i' | tr -d ' ')

    # Default to normal if not specified
    if [ -z "$cognition" ]; then
        echo "normal"
    else
        echo "$cognition" | tr '[:upper:]' '[:lower:]'
    fi
}
```

**Priority Extraction** (Line 92-103):
```bash
extract_priority() {
    local file="$1"
    local priority=$(grep -i "Priority:" "$file" | sed 's/.*Priority:\s*\([^;]*\).*/\1/' | tr -d ' ')

    # Default to P1 if not specified
    if [ -z "$priority" ]; then
        echo "P1"
    else
        echo "$priority"
    fi
}
```

**Agent Role Extraction** (Line 105-115):
```bash
extract_agent_role() {
    local file="$1"
    local role=$(grep -i "Role:" "$file" | sed 's/.*Role:[ ]*//i' | sed 's/[ ]*$//' | xargs)

    # Handle malformed role strings - extract ONLY the first word
    # Fixes issues where roles contain descriptions like "architect Simple orchestration"
    role=$(echo "$role" | awk '{print $1}' | xargs)

    echo "$role"
}
```

**Phase Extraction** (Line 132-136):
```bash
extract_phase() {
    local file="$1"
    grep -i "^Phase:" "$file" | sed 's/.*Phase:\s*//' | tr -d ' '
}
```

**Gate Extraction** (Line 138-148):
```bash
extract_completed_gate() {
    local file="$1"
    grep -i "^Completed gate:" "$file" | sed 's/.*Completed gate:\s*//' | tr -d ' '
}

extract_new_gate() {
    local file="$1"
    grep -i "^Gate:" "$file" | sed 's/.*Gate:\s*//' | tr -d ' '
}
```

---

## Validation Rules

### Agent Role Validation (V7.4)

**Purpose**: Validate agent names against available templates and suggest corrections

**Implementation** (Line 1346-1367):
```bash
# Validate agent using intelligence gatherer
validation_result=$(python3 "$VNX_DIR/scripts/gather_intelligence.py" validate "$agent_role" 2>&1)

# Check if validation failed
if echo "$validation_result" | grep -q '"valid": false'; then
    log "ERROR: Agent validation failed for '$agent_role'"
    log "Validation result: $validation_result"

    # Extract suggested agent
    suggested=$(echo "$validation_result" | grep -o '"suggestion": "[^"]*"' | cut -d'"' -f4)
    log "Suggested agent: $suggested"

    # Move to rejected with error info
    echo -e "\n\n[REJECTED: Invalid agent '$agent_role'. Suggested: '$suggested']\n" >> "$dispatch"
    mv "$dispatch" "$REJECTED_DIR/"
    continue
else
    log "Agent validated: $agent_role"
fi
```

**Validation Logic**:
- Queries `gather_intelligence.py` with agent role name
- Checks for exact match in template library
- Uses fuzzy matching for suggestions (Levenshtein distance)
- Rejects dispatch if agent invalid
- Provides suggested alternative

### Block Structure Validation

**Required Elements**:
1. `[[TARGET:A|B|C]]` marker (start)
2. `[[DONE]]` marker (end)
3. Instruction content (between Instruction: and [[DONE]])
4. Valid track identifier (A, B, or C)

**T0 Protection** (Line 1381-1386):
```bash
# Never send to T0
if [ "$track" = "0" ] || [ "$track" = "T0" ]; then
    log "ERROR: Attempting to dispatch to T0 - BLOCKED"
    mv "$dispatch" "$REJECTED_DIR/"
    continue
fi
```

**Purpose**: Prevents accidental dispatch to T0 (read-only orchestrator terminal)

### Role Normalization (Line 117-121)

**Purpose**: Flexible role matching for template resolution

```bash
normalize_role() {
    local role="$1"
    # Remove spaces, punctuation, convert to lowercase
    echo "$role" | tr -d '[:space:][:punct:]' | tr '[:upper:]' '[:lower:]'
}
```

**Examples**:
- `quality-engineer` → `qualityengineer`
- `Integration-Specialist` → `integrationspecialist`
- `debugging_specialist` → `debuggingspecialist`

---

## Injection Rules

### Instruction Injection

**Purpose**: Extract and inject task instructions from manager block into agent template

**Extraction** (Line 819-826):
```bash
extract_instruction_content() {
    local dispatch_file="$1"

    # Extract content between "Instruction:" and "[[DONE]]"
    # This captures the manager block instruction content
    awk '/^Instruction:/{flag=1; next} /^\[\[DONE\]\]/{flag=0} flag' "$dispatch_file"
}
```

**Injection** (Line 602-629):
```python
# Use python for safe multi-line replacement of <INSTRUCTION>
# Write enhanced instruction content to temp file to avoid bash escaping issues
local instruction_temp_file="/tmp/instruction_$$.tmp"
echo "$enhanced_instruction_content" > "$instruction_temp_file"

template_content=$(python3 -c "
import sys
with open('$temp_file', 'r') as f:
    content = f.read()
with open('$instruction_temp_file', 'r') as f:
    instruction_content = f.read()

# Simple instruction injection
content = content.replace('<INSTRUCTION>', instruction_content.strip())
print(content, end='')
")
```

**Template Placeholder**: `<INSTRUCTION>`

### Constraints Injection

**Purpose**: Load project constraints and inject into agent template

**Loading** (Line 858-872):
```bash
load_constraints_content() {
    local constraints_file="$PROJECT_ROOT/.claude/terminals/library/context/constraints.md"

    if [ -f "$constraints_file" ]; then
        echo "## Constraints"
        echo ""
        cat "$constraints_file"
    else
        log "WARNING: Constraints file not found: $constraints_file"
        echo "## Constraints"
        echo ""
        echo "# Project constraints not available"
    fi
}
```

**Injection** (Line 634-650):
```python
# Use python for safe multi-line replacement of <CONSTRAINTS>
local constraints_temp_file="/tmp/constraints_$$.tmp"
echo "$constraints_content" > "$constraints_temp_file"

template_content=$(python3 -c "
import sys
with open('$temp_file', 'r') as f:
    content = f.read()
with open('$constraints_temp_file', 'r') as f:
    constraints_content = f.read()
content = content.replace('<CONSTRAINTS>', constraints_content)
print(content, end='')
")
```

**Template Placeholder**: `<CONSTRAINTS>`

**Constraints File**: `.claude/terminals/library/context/constraints.md`

### Context Injection

**Purpose**: Extract context file references and inject into agent template

**Extraction** (Line 828-856):
```bash
extract_context_content() {
    local dispatch_file="$1"

    # Extract content from "Context:" line and continuation lines until next section
    local context_raw=$(awk '/^Context:/{flag=1} /^Completed gate:|^Gate:|^Priority:|^Instruction:/{flag=0} flag' "$dispatch_file")

    if [ -n "$context_raw" ]; then
        # Extract all context file references from the raw content
        local context_files=$(echo "$context_raw" | grep -o '\[\[@[^]]*\]\]')

        if [ -n "$context_files" ]; then
            # Format context with H2 header and clean file paths
            echo "## Context"
            echo ""
            echo "Before you start implementation, review these documents:"
            echo ""

            # Parse and format each context file reference, keeping @ but removing [[ ]] formatting
            local counter=1
            echo "$context_files" | while read -r context_file; do
                # Remove [[ ]] but keep @ symbol for Claude file resolution
                local clean_file=$(echo "$context_file" | sed 's/\[\[//g' | sed 's/\]\]//g')
                echo "$counter. $clean_file"
                counter=$((counter + 1))
            done
        fi
    fi
}
```

**Format**:
- Input: `Context: [[@.claude/vnx-system/docs/architecture/00_VNX_ARCHITECTURE.md]]`
- Output: Numbered list with @ preserved for Claude resolution

**Injection** (Line 656-671):
```python
# Use python for safe multi-line replacement of <CONTEXT>
local context_temp_file="/tmp/context_$$.tmp"
echo "$context_content" > "$context_temp_file"

template_content=$(python3 -c "
import sys
with open('$temp_file', 'r') as f:
    content = f.read()
with open('$context_temp_file', 'r') as f:
    context_content = f.read()
content = content.replace('<CONTEXT>', context_content)
print(content, end='')
")
```

**Template Placeholder**: `<CONTEXT>`

### Intelligence Injection (V7.4)

**Purpose**: Embed quality patterns and prevention rules from historical data

**Implementation** (Line 676-802):
```bash
# V7.4 INTELLIGENCE: Inject intelligence data if available
if [ -n "$dispatch_file" ] && [ -f "$dispatch_file" ]; then
    # Extract metadata using grep (safe, no complex parsing)
    local pattern_count=$(grep "^pattern_count:" "$dispatch_file" 2>/dev/null | head -1 | cut -d':' -f2 | tr -d ' ' || echo "0")
    local prevention_rules=$(grep "^prevention_rules:" "$dispatch_file" 2>/dev/null | head -1 | cut -d':' -f2 | tr -d ' ' || echo "0")
    local learnings_block=""

    # Only inject if we have data
    if [ "$pattern_count" -gt 0 ] 2>/dev/null || [ "$prevention_rules" -gt 0 ] 2>/dev/null; then
        log "V7.4 INTELLIGENCE: Injecting $pattern_count patterns, $prevention_rules rules"

        # Build compact "Prior Learnings" from quality_context JSON
        learnings_block=$(python3 - "$dispatch_file" <<'PY'
import json
import sys

# ... (Python code for extracting and formatting patterns)
# Outputs:
# ## 🧠 Prior Learnings (Use Before Coding)
# - Past Fix: {title} — {description} ({file_path})
# - Risk: {warning} → {recommendation}
# - Similar Report: {summary} ({report_path})
PY
)

        # Append intelligence section to template
        template_content="${template_content}${intel_section}"
        if [ -n "$learnings_block" ]; then
            template_content="${template_content}${learnings_block}"
        fi
    fi
fi
```

**Intelligence Section Format**:
```markdown
## 📊 VNX Intelligence Context

Based on analysis of 1,143 code patterns and historical project data:
- **Relevant Patterns Found**: 5 code examples matching this task
- **Prevention Rules**: 3 warnings about potential issues

These patterns have been identified from successful implementations in this codebase.

## 🧠 Prior Learnings (Use Before Coding)
- Past Fix: Async queue pattern — Fixed race condition in crawler pool (src/crawler/core/browser_pool.py)
- Risk: Singleton not enforced → Implement bulletproof PID locking
- Similar Report: Browser pool optimization (reports/T3/20260118-browser-pool.md)

Need more context? Use intelligence tools (tags/patterns/antipatterns) — see .claude/terminals/library/templates/tools/intelligence_tools.md
```

---

## Parsing Rules

### Track Extraction

**Pattern**: `[[TARGET:A|B|C]]`

**Implementation** (Line 73-77):
```bash
extract_track() {
    local file="$1"
    grep -o '^\[\[TARGET:[A-C]\]\]' "$file" | sed 's/.*TARGET://' | sed 's/]].*//' | head -1
}
```

**Valid Values**: A, B, C
**Default**: None (rejection if missing)

### Cognition Level Extraction

**Pattern**: `Cognition: normal|deep|ultrathink|enhanced|seq|standard`

**Implementation** (Line 79-90):
```bash
extract_cognition() {
    local file="$1"
    local cognition=$(grep -i "Cognition:" "$file" | sed 's/.*Cognition:\s*//i' | tr -d ' ')

    # Default to normal if not specified
    if [ -z "$cognition" ]; then
        echo "normal"
    else
        echo "$cognition" | tr '[:upper:]' '[:lower:]'
    fi
}
```

**Valid Values**:
- `normal` (default): Standard processing
- `deep`: Enhanced analysis (routes to T3/Opus)
- `ultrathink`: Maximum depth analysis
- `enhanced`: Medium complexity
- `seq`: Sequential thinking
- `standard`: Alias for normal

### Priority Extraction

**Pattern**: `Priority: P0|P1|P2`

**Implementation** (Line 92-103):
```bash
extract_priority() {
    local file="$1"
    local priority=$(grep -i "Priority:" "$file" | sed 's/.*Priority:\s*\([^;]*\).*/\1/' | tr -d ' ')

    # Default to P1 if not specified
    if [ -z "$priority" ]; then
        echo "P1"
    else
        echo "$priority"
    fi
}
```

**Valid Values**:
- `P0`: Critical/urgent
- `P1`: Normal (default)
- `P2`: Low priority

### Agent Role Extraction

**Pattern**: `Role: {agent_name}`

**Implementation** (Line 105-115):
```bash
extract_agent_role() {
    local file="$1"
    local role=$(grep -i "Role:" "$file" | sed 's/.*Role:[ ]*//i' | sed 's/[ ]*$//' | xargs)

    # Handle malformed role strings - extract ONLY the first word
    # Fixes issues where roles contain descriptions
    role=$(echo "$role" | awk '{print $1}' | xargs)

    echo "$role"
}
```

**Valid Roles** (from agent template directory):
- `analyst`
- `architect`
- `debugging_specialist`
- `developer`
- `integration-specialist`
- `junior_developer`
- `performance-engineer`
- `quality-engineer`
- `refactoring-expert`
- `security-engineer`
- `senior-developer`

---

## Template System

### Template Resolution

**Purpose**: Map agent role to template file path with fuzzy matching

**Implementation** (Line 897-942):
```bash
resolve_template_path() {
    local agent_role="$1"
    local templates_dir="$PROJECT_ROOT/.claude/terminals/library/templates/agents"

    # Strip model suffixes first (opus/sonnet)
    local base_role="$agent_role"
    case "$agent_role" in
        *-opus)   base_role="${agent_role%-opus}" ;;
        *-sonnet) base_role="${agent_role%-sonnet}" ;;
    esac

    # Try exact match first
    local template_path="$templates_dir/${base_role}.md"
    if [ -f "$template_path" ]; then
        echo "$template_path"
        return 0
    fi

    # Use normalized role for fallback matching
    local normalized_role=$(normalize_role "$base_role")

    # Try common variations
    for template in "$templates_dir"/*.md; do
        if [ -f "$template" ]; then
            local template_name=$(basename "$template" .md)
            local normalized_template=$(normalize_role "$template_name")

            if [ "$normalized_role" = "$normalized_template" ]; then
                log "V7.3 MATCH: Found template '$template_name' for role '$base_role' via normalization"
                echo "$template"
                return 0
            fi
        fi
    done

    # Fallback to developer template
    log "V7.3 WARNING: No template found for role '$base_role', using developer fallback"
    if [ -f "$templates_dir/developer.md" ]; then
        echo "$templates_dir/developer.md"
        return 0
    else
        log "V7.3 ERROR: Developer fallback template not found"
        return 1
    fi
}
```

**Resolution Order**:
1. Exact match: `{role}.md`
2. Strip model suffix: `{role-opus}` → `{role}.md`
3. Normalized match: `quality-engineer` → `quality_engineer.md`
4. Fallback: `developer.md`

### Template Structure

**Agent Template Format**:
```markdown
You are a {Role} specialized in {domain} for the <project> V2 project.

Behavioral Mindset
{behavioral_guidelines}

Focus Areas
{focus_areas}

Key Actions
{key_actions}

## Intelligence Tools Integration
{intelligence_tools_reference}

## Instructions
<INSTRUCTION>

When you complete your instructions, always create a markdown report.

<CONSTRAINTS>
</CONSTRAINTS>

<CONTEXT>
</CONTEXT>

Outputs
{expected_outputs}

Boundaries
{will_and_will_not}
```

**Template Placeholders**:
- `<INSTRUCTION>`: Injected from manager block
- `<CONSTRAINTS>`: Injected from constraints.md
- `<CONTEXT>`: Injected from context files

**Template Location**: `.claude/terminals/library/templates/agents/{role}.md`

### Template Compilation

**Purpose**: Replace placeholders with actual content

**Implementation** (Line 574-817):
```bash
compile_template() {
    local template_path="$1"
    local instruction_content="$2"
    local constraints_content="$3"
    local context_content="$4"
    local output_file="$5"
    local dispatch_file="$6"

    # Read template content
    local template_content=$(cat "$template_path")

    # Inject INSTRUCTION (Python for safe multi-line replacement)
    # Inject CONSTRAINTS (Python for safe multi-line replacement)
    # Inject CONTEXT (Python for safe multi-line replacement)
    # Inject INTELLIGENCE (if available)

    # Write compiled template to output file
    echo "$template_content" > "$output_file"
}
```

**Compilation Steps**:
1. Load template file
2. Inject instruction content (replace `<INSTRUCTION>`)
3. Inject constraints content (replace `<CONSTRAINTS>`)
4. Inject context file list (replace `<CONTEXT>`)
5. Inject intelligence patterns (append at end)
6. Write compiled prompt to temporary file
7. Deliver to terminal via hybrid dispatch: skill via tmux send-keys + instruction via paste-buffer

---

## Pane Discovery System

### Purpose

Dynamic, self-healing pane ID resolution for terminal delivery. Replaces static pane configuration with multi-method discovery.

### Discovery Methods

**Method 1: By Title** (pane_manager_v2.sh, Line 17-34):
```bash
discover_pane_by_title() {
    local terminal="$1"
    local pane_id

    # Look for panes with matching title
    pane_id=$(tmux list-panes -a -F "#{pane_id} #{pane_title}" 2>/dev/null | \
        grep -E "(T${terminal#T}|${terminal})" | \
        awk '{print $1}' | \
        head -1)

    if [ -n "$pane_id" ]; then
        log "Found $terminal by title: $pane_id"
        echo "$pane_id"
        return 0
    fi
    return 1
}
```

**Method 2: By Working Directory** (pane_manager_v2.sh, Line 36-54):
```bash
discover_pane_by_path() {
    local terminal="$1"
    local pane_id

    # Look for panes in terminal directories
    pane_id=$(tmux list-panes -a -F "#{pane_id} #{pane_current_path}" 2>/dev/null | \
        grep -E "/terminals/(T${terminal#T}|${terminal}|T-MANAGER)" | \
        grep -E "(T${terminal#T}|${terminal})" | \
        awk '{print $1}' | \
        head -1)

    if [ -n "$pane_id" ]; then
        log "Found $terminal by path: $pane_id"
        echo "$pane_id"
        return 0
    fi
    return 1
}
```

**Method 3: By Window Position** (pane_manager_v2.sh, Line 56-79):
```bash
discover_pane_by_window() {
    local terminal="$1"
    local window_index

    # Map terminal to expected window position
    case "$terminal" in
        T0) window_index=0 ;;
        T1) window_index=1 ;;
        T2) window_index=2 ;;
        T3) window_index=3 ;;
        *) return 1 ;;
    esac

    # Try to find by window:pane notation
    local pane_id=$(tmux list-panes -t "vnx:$window_index.0" -F "#{pane_id}" 2>/dev/null | head -1)

    if [ -n "$pane_id" ]; then
        log "Found $terminal by window position: $pane_id"
        echo "$pane_id"
        return 0
    fi
    return 1
}
```

### Caching Strategy

**Cache Configuration**:
- Location: `/tmp/vnx_pane_cache/{terminal}.pane`
- TTL: 300 seconds (5 minutes)
- Validation: Checks pane still exists before using cache
- Auto-invalidation: On pane ID mismatch

**Implementation** (pane_manager_v2.sh, Line 113-128):
```bash
# Check cache first
local cache_file="$PANE_CACHE/${terminal}.pane"
if [ -f "$cache_file" ]; then
    local cache_age=$(( $(date +%s) - $(stat -f%m "$cache_file" 2>/dev/null || stat -c%Y "$cache_file" 2>/dev/null || echo 0) ))
    if [ "$cache_age" -lt "$CACHE_TTL" ]; then
        local cached_pane=$(cat "$cache_file")
        # Verify pane still exists
        if tmux list-panes -F "#{pane_id}" 2>/dev/null | grep -q "^${cached_pane}$"; then
            echo "$cached_pane"
            return 0
        else
            log "Cached pane $cached_pane no longer exists, rediscovering..."
            rm -f "$cache_file"
        fi
    fi
fi
```

### Terminal Routing Logic

**Implementation** (dispatcher Line 384-405):
```bash
determine_executor() {
    local track="$1"
    local cognition="$2"

    # Source pane configuration
    source "$VNX_DIR/scripts/pane_config.sh"

    # Deep cognition ALWAYS goes to T3 (Opus) regardless of track
    if [ "$cognition" = "deep" ]; then
        echo "$(get_pane_id "T3" "$STATE_DIR/panes.json")"  # T3 for deep work
        return
    fi

    # Normal cognition routes by track (FIXED MAPPING)
    case "$track" in
        A) echo "$(get_pane_id "T1" "$STATE_DIR/panes.json")" ;;  # T1 (Track A)
        B) echo "$(get_pane_id "T2" "$STATE_DIR/panes.json")" ;;  # T2 (Track B)
        C) echo "$(get_pane_id "T3" "$STATE_DIR/panes.json")" ;;  # T3 (Track C/Opus)
        *) echo "$(get_pane_id "T1" "$STATE_DIR/panes.json")" ;;  # Default to T1
    esac
}
```

**Routing Rules**:
1. **Cognition Override**: `deep` → T3 (Opus) always
2. **Track A**: T1 (Sonnet) - Crawler development
3. **Track B**: T2 (Sonnet) - Storage pipeline
4. **Track C**: T3 (Opus) - Deep investigations
5. **Default**: T1 if track unknown

---

## Configuration

### Environment Variables

```bash
# Core Directories
PROJECT_ROOT="$PROJECT_ROOT"  # Typically /path/to/<project>
CLAUDE_DIR="$PROJECT_ROOT/.claude"
VNX_DIR="$CLAUDE_DIR/vnx-system"

# Dispatch Directories
DISPATCH_DIR="$VNX_DIR/dispatches"
QUEUE_DIR="$DISPATCH_DIR/queue"
PENDING_DIR="$DISPATCH_DIR/pending"
ACTIVE_DIR="$DISPATCH_DIR/active"
COMPLETED_DIR="$DISPATCH_DIR/completed"
REJECTED_DIR="$DISPATCH_DIR/rejected"

# State Management
STATE_DIR="$VNX_DIR/state"
PROGRESS_FILE="$STATE_DIR/progress.yaml"

# Terminals
TERMINALS_DIR="$CLAUDE_DIR/terminals"

# Logging
LOG_FILE="$VNX_DIR/logs/dispatcher.log"
RUN_ID=$(date +%s)
```

### Directory Structure

```
.claude/vnx-system/
├── dispatches/
│   ├── queue/          # New dispatches (from Smart Tap)
│   ├── pending/        # Awaiting processing
│   ├── active/         # Currently executing
│   ├── completed/      # Successfully dispatched
│   └── rejected/       # Validation failures
├── state/
│   ├── progress.yaml   # Gate/phase tracking
│   ├── panes.json      # Pane ID mappings
│   └── progress_state.yaml  # Enhanced state tracking
├── logs/
│   └── dispatcher.log  # Dispatcher activity log
└── scripts/
    ├── dispatcher_v7_compilation.sh
    ├── pane_manager_v2.sh
    └── singleton_enforcer.sh
```

### Color Configuration

```bash
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color
```

---

## Operations

### Command-Line Usage

**Start Dispatcher**:
```bash
# Production start (with singleton enforcement)
$VNX_HOME/scripts/dispatcher_v7_compilation.sh
```

**Output**:
```
[2026-01-26 14:30:00] Dispatcher V7 COMPILATION starting...
[2026-01-26 14:30:00] Dispatcher V7 PRODUCTION ready. Monitoring /path/to/pending for ALL role dispatches...
V7 PRODUCTION Features: Template compilation, instruction injection, constraints injection
Agent Coverage: Processes ALL agent roles (analyst, architect, debugging_specialist, developer, integration-specialist, junior_developer, performance-engineer, quality-engineer, refactoring-expert, security-engineer, senior-developer) - V5 deprecated
Track routing: A→T1(%1), B→T2(%2), C→T3(%3)
Deep cognition always routes to T3(%3)
```

### Monitoring

**Check Dispatcher Status**:
```bash
# Check if running
ps aux | grep dispatcher_v7_compilation.sh

# View recent logs
tail -f $VNX_HOME/logs/dispatcher.log

# Check queue depth
ls -la $VNX_HOME/dispatches/pending/*.md | wc -l
```

**Expected Log Patterns**:
```
[2026-01-26 14:30:05] Processing V7 PRODUCTION dispatch: 20260126-143005-A.md (Role: architect)
[2026-01-26 14:30:05] Agent validated: architect
[2026-01-26 14:30:05] INFO: Gathering intelligence for dispatch
[2026-01-26 14:30:05] INFO: Intelligence gathered: 5 patterns, 3 rules
[2026-01-26 14:30:05] V7.3 COMPILATION: Starting template compilation
[2026-01-26 14:30:05] V7.3 COMPILATION: Template compiled successfully
[2026-01-26 14:30:05] V7.3 DISPATCH: Routing to terminal %1 (Track: A, Cognition: normal)
[2026-01-26 14:30:05] V7.3 DISPATCH: Successfully sent compiled prompt to %1
[2026-01-26 14:30:05] V7.3 PROGRESS_STATE: ✅ Successfully updated progress_state.yaml for Track A
[2026-01-26 14:30:05] ✓ Dispatch sent to terminal %1 and moved to completed
```

### Troubleshooting

**Problem**: Dispatcher not processing dispatches

**Diagnosis**:
```bash
# Check if dispatcher is running
ps aux | grep dispatcher_v7_compilation.sh

# Check for singleton lock
ls -la $VNX_HOME/state/pids/dispatcher.lock

# Check pending queue
ls -la $VNX_HOME/dispatches/pending/

# Check dispatcher logs
tail -50 $VNX_HOME/logs/dispatcher.log
```

**Solution**: Restart dispatcher (singleton enforcer will prevent duplicates)

---

**Problem**: Dispatches moved to rejected/

**Diagnosis**:
```bash
# Check rejected dispatches
ls -la $VNX_HOME/dispatches/rejected/

# View rejection reason (appended to dispatch file)
tail -20 /path/to/rejected/dispatch.md
```

**Common Rejection Reasons**:
- Invalid agent role (not in template library)
- Missing track identifier
- Attempt to dispatch to T0
- Template compilation failure

---

**Problem**: Pane ID resolution failure

**Diagnosis**:
```bash
# List all tmux panes
tmux list-panes -a -F "#{pane_id} #{pane_title} #{pane_current_path}"

# Check pane cache
ls -la /tmp/vnx_pane_cache/

# Check panes.json state
cat $VNX_HOME/state/panes.json
```

**Solution**:
1. Clear pane cache: `rm -rf /tmp/vnx_pane_cache/`
2. Restart VNX system to regenerate pane mappings

---

## Integration

### Smart Tap Integration

**Flow**: T0 Manager Block → Smart Tap → Queue → Dispatcher

**Smart Tap Responsibilities**:
- Capture manager blocks from T0 output
- Translate JSON → Markdown (if needed)
- Write to `dispatches/queue/`
- Trigger popup for human review

**Dispatcher Responsibilities**:
- Process approved dispatches from `pending/`
- Validate block structure
- Compile agent template
- Deliver to worker terminal

### Intelligence System Integration

**Flow**: Dispatcher → Intelligence Gatherer → Pattern Database → Quality Context

**Implementation** (Line 1388-1411):
```bash
# Gather intelligence for dispatch
if [ -f "$VNX_DIR/scripts/gather_intelligence.py" ]; then
    log "INFO: Gathering intelligence for dispatch"

    # Extract task description for intelligence gathering
    local task_description=$(awk '/^Instruction:/{flag=1; next} /^\[\[DONE\]\]/{flag=0} flag' "$dispatch")

    # Gather intelligence (terminal = T + track letter)
    local terminal="T${track}"
    local intel_result=$(python3 "$VNX_DIR/scripts/gather_intelligence.py" gather "$task_description" "$terminal" "$agent_role" "$gate" 2>&1)

    # Parse JSON results
    local pattern_count=$(echo "$intel_result" | grep '"pattern_count":' | grep -o '[0-9]*' | head -1 || echo "0")
    local prevention_rules=$(echo "$intel_result" | grep '"prevention_rule_count":' | grep -o '[0-9]*' | head -1 || echo "0")

    log "INFO: Intelligence gathered: $pattern_count patterns, $prevention_rules rules"

    # Write intelligence metadata to dispatch file
    echo "" >> "$dispatch"
    echo "[INTELLIGENCE_DATA]" >> "$dispatch"
    echo "pattern_count: $pattern_count" >> "$dispatch"
    echo "prevention_rules: $prevention_rules" >> "$dispatch"
    echo "quality_context: $intel_result" >> "$dispatch"
fi
```

**Intelligence Data Structure**:
```json
{
  "intelligence_version": "1.4.0",
  "agent_validated": true,
  "patterns_available": true,
  "pattern_count": 5,
  "offered_pattern_hashes": ["a1b2c3...", "d4e5f6...", "g7h8i9...", "j0k1l2...", "m3n4o5..."],
  "suggested_patterns": [
    {
      "title": "Async queue pattern",
      "description": "Fixed race condition in crawler pool",
      "file_path": "src/crawler/core/browser_pool.py",
      "quality_score": 95
    }
  ],
  "prevention_rules": [
    {
      "warning": "Singleton not enforced",
      "recommendation": "Implement bulletproof PID locking"
    }
  ]
}
```

### Receipt Processor Integration

**Flow**: Dispatcher Delivery → Worker ACK → Receipt Processor → T0 Intelligence

**Dispatcher Notification** (Line 1145-1150):
```bash
# Notify heartbeat ACK monitor about dispatch (automatic ACK detection)
local dispatch_id="${filename%.md}"
local terminal_id=$(get_terminal_from_pane "$target_pane")
python3 "$VNX_DIR/scripts/notify_dispatch.py" "$dispatch_id" "$terminal_id" "$dispatch_id" 2>/dev/null || {
    log "V7.3 WARNING: Failed to notify heartbeat ACK monitor (non-fatal)"
}
```

**Purpose**: Enable ACK timeout tracking and delivery confirmation

---

## Testing

### Test Procedures

**1. Template Compilation Test**

```bash
# Create test dispatch
cat > /tmp/test_dispatch.md <<'EOF'
[[TARGET:A]]
Role: architect
Phase: 1
Gate: planning
Priority: P0; Cognition: normal
Context: @.claude/vnx-system/docs/architecture/00_VNX_ARCHITECTURE.md
Instruction:
Test template compilation with sample instruction.
Verify context injection and constraints loading.
[[DONE]]
EOF

# Move to pending queue
cp /tmp/test_dispatch.md $VNX_HOME/dispatches/pending/

# Monitor logs
tail -f $VNX_HOME/logs/dispatcher.log

# Verify compilation
# Expected: "V7.3 COMPILATION: Template compiled successfully"
# Expected: "V7.3 DISPATCH: Successfully sent compiled prompt to %1"
```

**2. Agent Validation Test**

```bash
# Test with invalid agent
cat > /tmp/test_invalid_agent.md <<'EOF'
[[TARGET:A]]
Role: invalid_agent_name
Instruction:
Test agent validation rejection.
[[DONE]]
EOF

cp /tmp/test_invalid_agent.md $VNX_HOME/dispatches/pending/

# Expected: Dispatch moved to rejected/ with suggestion
ls -la $VNX_HOME/dispatches/rejected/
cat /path/to/rejected/test_invalid_agent.md
# Should contain: [REJECTED: Invalid agent 'invalid_agent_name'. Suggested: 'developer']
```

**3. Intelligence Injection Test**

```bash
# Test with intelligence-eligible dispatch
cat > /tmp/test_intelligence.md <<'EOF'
[[TARGET:A]]
Role: architect
Priority: P0; Cognition: normal
Instruction:
Design async queue architecture for crawler coordination.
Consider race conditions and singleton patterns.
[[DONE]]
EOF

cp /tmp/test_intelligence.md $VNX_HOME/dispatches/pending/

# Monitor logs for intelligence gathering
# Expected: "INFO: Intelligence gathered: X patterns, Y rules"
# Expected: "V7.4 INTELLIGENCE: Injecting X patterns, Y rules"
```

### Validation

**Dispatch Validation Checklist**:
- [ ] Track identifier extracted (A, B, or C)
- [ ] Agent role validated against template library
- [ ] No T0 dispatch attempts
- [ ] Instruction content extracted successfully
- [ ] Context files formatted correctly (@ preserved)
- [ ] Constraints loaded from constraints.md
- [ ] Intelligence patterns injected (if available)
- [ ] Template compilation successful
- [ ] Pane ID resolved correctly
- [ ] Delivery to terminal successful
- [ ] Progress state updated atomically
- [ ] Dispatch moved to completed/

---

## Appendix

### Files Reference

**Core Scripts**:
- `dispatcher_v8_minimal.sh` - Main dispatcher (V8 native skills)
- `pane_manager_v2.sh` (~300 lines) - Pane discovery system
- `singleton_enforcer.sh` - Process singleton enforcement
- `gather_intelligence.py` - Intelligence pattern gathering
- `update_progress_state.py` - Atomic state updates

**Configuration Files**:
- `constraints.md` - Project constraints
- `panes.json` - Pane ID mappings
- `progress.yaml` - Gate/phase tracking
- `progress_state.yaml` - Enhanced state tracking

**Template Files**:
- `.claude/terminals/library/templates/agents/*.md` - Agent templates
- `.claude/terminals/library/context/constraints.md` - Project constraints

**State Files**:
- `dispatches/queue/*.md` - New dispatches
- `dispatches/pending/*.md` - Awaiting processing
- `dispatches/active/*.md` - In progress
- `dispatches/completed/*.md` - Successfully dispatched
- `dispatches/rejected/*.md` - Validation failures
- `state/quality_intelligence.db` - Pattern database

### Version History

**V7.3** (Current Production):
- Template compilation system
- Instruction/constraints/context injection
- Universal role support (all agents)
- Smart pane discovery
- Robust template resolution

**V7.4** (Current Production):
- Intelligence integration
- Quality context embedding
- Agent role validation
- Pattern injection (top 5)
- Prevention rules

**V8.2** (Current Production — see top of document):
- Native skill activation (87% token reduction)
- Multi-provider dispatch (Claude/Codex/Gemini)
- Rich receipt footer with Expected Outputs
- PR-ID in dispatch prompt
- Parallel PR queue support
- Project-scoped process isolation (VNX_KILL_SCOPE)

**V5** (Deprecated):
- Simple dispatch delivery
- Limited role support
- No template compilation
- Static pane configuration

### Key Functions Summary

| Function | Purpose | Lines |
|----------|---------|-------|
| `extract_track()` | Extract track from [[TARGET:X]] | 73-77 |
| `extract_cognition()` | Extract cognition level | 79-90 |
| `extract_priority()` | Extract priority (P0/P1/P2) | 92-103 |
| `extract_agent_role()` | Extract agent role name | 105-115 |
| `normalize_role()` | Normalize role for matching | 117-121 |
| `is_v7_role_dispatch()` | Check if V7 handles role | 123-130 |
| `extract_phase()` | Extract phase number | 132-136 |
| `extract_completed_gate()` | Extract completed gate | 138-142 |
| `extract_new_gate()` | Extract new gate | 144-148 |
| `extract_task_id()` | Extract task identifier | 150-167 |
| `get_current_gate()` | Get gate from progress.yaml | 169-208 |
| `update_progress_yaml()` | Atomic state update | 210-382 |
| `determine_executor()` | Route to terminal | 384-405 |
| `analyze_cognitive_requirements()` | Cognitive analysis | 409-572 |
| `compile_template()` | Template compilation | 574-817 |
| `extract_instruction_content()` | Extract instructions | 819-826 |
| `extract_context_content()` | Extract context files | 828-856 |
| `load_constraints_content()` | Load constraints | 858-872 |
| `resolve_template_path()` | Find agent template | 897-942 |
| `paste_and_send_to_terminal()` | Deliver to terminal | 944-974 |
| `send_compiled_dispatch_to_terminal()` | Main dispatch flow | 1012-1158 |
| `process_dispatches()` | Main processing loop | 1322-1425 |

---

**Document Status**: Legacy Reference (V8.2 is current production)
**Maintainer**: T-MANAGER (VNX Orchestration Expert)
**Related Documents**:
- `core/00_VNX_ARCHITECTURE.md` — Current architecture (V10.0)
- `core/technical/INTELLIGENCE_SYSTEM.md` — Intelligence patterns
- `operations/RECEIPT_PIPELINE.md` — Receipt delivery pipeline
