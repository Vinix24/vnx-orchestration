#!/bin/bash
# Enhanced Queue UI with two-column layout - content left, actions right

# CRITICAL FIX: Remove 'set -e' to prevent immediate exit on any error
# Background: 'set -e' caused popup to close immediately when minor commands failed
# Solution: Use 'set -uo pipefail' to catch undefined vars but allow error recovery
set -uo pipefail

# TTY handling for tmux popup - FORCE interactive mode
if [ ! -t 0 ]; then
  exec </dev/tty >/dev/tty 2>&1
fi
stty sane 2>/dev/null || true
export TERM="${TERM:-screen-256color}"
export EDITOR="${EDITOR:-vim}"

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"
VNX_DIR="$VNX_HOME"
QUEUE_DIR="$VNX_DISPATCH_DIR/queue"
PENDING_DIR="$VNX_DISPATCH_DIR/pending"
COMPLETED_DIR="$VNX_DISPATCH_DIR/completed"
REJECTED_DIR="$VNX_DISPATCH_DIR/rejected"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'
DIM='\033[2m'

# Get terminal dimensions
COLS=$(tput cols)
LINES=$(tput lines)

# Calculate column widths (70% for content, 30% for actions)
CONTENT_WIDTH=$((COLS * 70 / 100))
ACTION_WIDTH=$((COLS - CONTENT_WIDTH - 3))  # -3 for separator

# Calculate available content area (leave space for header, footer, and prompt)
CONTENT_LINES=$((LINES - 8))  # 3 for header, 1 for footer, 4 for prompt area

# Function to print in columns with better alignment
print_column() {
    local left="$1"
    local right="$2"
    local max_height="$3"  # Maximum lines to print
    local left_lines=()
    local right_lines=()

    # Split left content into lines and strip ANSI codes for length calculation
    while IFS= read -r line; do
        # Get actual display length without ANSI codes
        local clean_line=$(echo -e "$line" | sed 's/\x1b\[[0-9;]*m//g')
        local line_length=${#clean_line}

        # Wrap long lines for left column
        if [ $line_length -gt $CONTENT_WIDTH ]; then
            # Need to wrap - this is complex with ANSI codes, so simplify
            left_lines+=("$line")
        else
            left_lines+=("$line")
        fi
    done <<< "$left"

    # Split right content into lines
    while IFS= read -r line; do
        right_lines+=("$line")
    done <<< "$right"

    # Find max lines (but cap at max_height if provided)
    local max_lines=${#left_lines[@]}
    if [ ${#right_lines[@]} -gt $max_lines ]; then
        max_lines=${#right_lines[@]}
    fi
    if [ -n "$max_height" ] && [ $max_lines -gt $max_height ]; then
        max_lines=$max_height
    fi

    # Print both columns side by side with proper alignment
    local lines_printed=0
    for ((i=0; i<$max_lines && lines_printed<$max_height; i++)); do
        local left_text="${left_lines[$i]:-}"
        local right_text="${right_lines[$i]:-}"

        # Calculate padding for left column (accounting for ANSI codes)
        local clean_left=$(echo -e "$left_text" | sed 's/\x1b\[[0-9;]*m//g')
        local padding=$((CONTENT_WIDTH - ${#clean_left}))

        # Print with proper spacing
        echo -en "$left_text"
        printf "%*s" $padding ""
        echo -e " ${DIM}│${NC} $right_text"
        ((lines_printed++))
    done

    # Fill remaining space with empty lines if needed
    while [ $lines_printed -lt $max_height ]; do
        printf "%*s" $CONTENT_WIDTH ""
        echo -e " ${DIM}│${NC}"
        ((lines_printed++))
    done
}

# Function to edit file
edit_in_pane() {
    local file="$1"

    # Check if file is writable
    if [ ! -w "$file" ]; then
        echo -e "${RED}⚠ File is read-only (chmod u+w \"$file\")${NC}"
        read -n 1 -p "Press any key..." _
        return
    fi

    # Use the popup_editor.sh wrapper which handles TTY properly
    local editor_script="$VNX_DIR/scripts/popup_editor.sh"
    if [ -x "$editor_script" ]; then
        "$editor_script" "$file"
    elif command -v nano >/dev/null 2>&1; then
        nano "$file"
    else
        # Force vim into simple mode for popup
        vim -u NONE -N "$file"
    fi
}

# Clear screen
clear

# Check for dispatches in queue
# NOTE: Temporarily disable nounset to safely handle empty arrays on bash 3.2 (macOS)
set +u
QUEUE_FILES=()
while IFS= read -r -d '' f; do
    QUEUE_FILES+=("$f")
done < <(find "$QUEUE_DIR" -name "*.md" -type f -print0 2>/dev/null | sort -z)
QUEUE_COUNT=${#QUEUE_FILES[@]}
set -u

# Debug output to log file (for troubleshooting)
echo "[$(date)] Queue check: Found $QUEUE_COUNT files in $QUEUE_DIR" >> "$VNX_LOGS_DIR/queue_ui.log"
if [ $QUEUE_COUNT -gt 0 ]; then
    for qf in "${QUEUE_FILES[@]}"; do
        echo "  - $(basename "$qf")" >> "$VNX_LOGS_DIR/queue_ui.log"
    done
fi

if [ $QUEUE_COUNT -eq 0 ]; then
    pending_count=0
    pending_count=$(find "$PENDING_DIR" -name "*.md" -type f 2>/dev/null | wc -l | tr -d ' ')
    if [ "$pending_count" -gt 0 ]; then
        echo -e "${YELLOW}No dispatches awaiting approval (queue empty).${NC}"
        echo -e "${BLUE}${pending_count} dispatch(es) already pending.${NC}"
    else
        echo -e "${YELLOW}No dispatches in queue${NC}"
    fi
    sleep 1
    exit 0
fi

# Show summary at the top
echo -e "${BOLD}${CYAN}═══ Queue Status ═══${NC}"
echo -e "${GREEN}Total dispatches in queue: ${BOLD}$QUEUE_COUNT${NC}"
echo ""

# Function to format dispatch content for left column
format_dispatch_content() {
    local file="$1"
    local output=""
    local in_instruction=false

    while IFS= read -r line; do
        # Trim leading spaces for pattern matching but preserve original for display
        local trimmed=$(echo "$line" | sed 's/^[[:space:]]*//')

        if [[ "$trimmed" =~ ^\[\[TARGET ]]; then
            output+="${BOLD}${YELLOW}$line${NC}\n"
        elif [[ "$trimmed" =~ ^Role: ]]; then
            output+="${CYAN}$line${NC}\n"
        elif [[ "$trimmed" =~ ^Workflow: ]]; then
            output+="${GREEN}$line${NC}\n"
        elif [[ "$trimmed" =~ ^Context: ]]; then
            output+="${BLUE}$line${NC}\n"
        elif [[ "$trimmed" =~ ^Gate: ]] || [[ "$trimmed" =~ ^Priority: ]]; then
            output+="${CYAN}$line${NC}\n"
        elif [[ "$trimmed" =~ ^Completed ]]; then
            output+="${YELLOW}$line${NC}\n"
        elif [[ "$trimmed" =~ ^Instruction: ]]; then
            output+="${BOLD}$line${NC}\n"
            in_instruction=true
        elif [[ "$trimmed" =~ ^\[\[DONE\]\] ]]; then
            output+="${BOLD}${YELLOW}$line${NC}\n"
            in_instruction=false
        elif [ "$in_instruction" = true ] && [[ "$trimmed" =~ ^- ]]; then
            output+="${NC}$line\n"
        else
            output+="$line\n"
        fi
    done < "$file"

    echo -e "$output"
}

# Function to validate dispatch format
validate_dispatch() {
    local file="$1"
    local content=$(cat "$file")
    local errors=""

    # Check for required fields (allowing for leading whitespace)
    if ! echo "$content" | grep -q '^[[:space:]]*\[\[TARGET:[A-C]\]\]'; then
        errors+="  ${RED}⚠ Missing or invalid TARGET${NC}\n"
    fi
    if ! echo "$content" | grep -q '^[[:space:]]*Gate:'; then
        errors+="  ${RED}⚠ Missing Gate field${NC}\n"
    fi
    if ! echo "$content" | grep -q '^[[:space:]]*\[\[DONE\]\]'; then
        errors+="  ${RED}⚠ Missing [[DONE]] marker${NC}\n"
    fi

    if [ -z "$errors" ]; then
        echo -e "  ${GREEN}✓ Dispatch format valid${NC}"
        return 0
    else
        echo -e "$errors"
        return 1
    fi
}

# Function to create action menu for right column
create_action_menu() {
    local current=$1
    local total=$2
    local filename=$3
    local menu=""

    menu+="${BOLD}${CYAN}═══ Queue Manager ═══${NC}\n"
    menu+="File: ${YELLOW}$filename${NC}\n"
    menu+="Item: ${BOLD}$current/$total${NC}\n"
    menu+="\n"
    menu+="${BOLD}${YELLOW}─── Actions ───${NC}\n"
    menu+="${GREEN}[A]${NC} Accept → pending\n"
    menu+="${RED}[R]${NC} Reject → archive\n"
    menu+="${BLUE}[S]${NC} Skip → next item\n"
    menu+="${CYAN}[E]${NC} Edit dispatch\n"
    menu+="${YELLOW}[V]${NC} View full content\n"
    menu+="${RED}[Q]${NC} Quit manager\n"
    menu+="\n"
    menu+="${BOLD}${CYAN}─── Navigation ───${NC}\n"
    menu+="${CYAN}[N]${NC} Next item\n"
    menu+="${CYAN}[P]${NC} Previous item\n"
    menu+="${CYAN}[Space]${NC} Next item\n"
    menu+="\n"
    menu+="${BOLD}─── Validation ───${NC}\n"

    echo -e "$menu"
}

# Process each dispatch (interactive mode)
CURRENT_INDEX=0

while true; do
    # Rescan queue directory to get current state
    set +u
    QUEUE_FILES=()
    while IFS= read -r -d '' f; do
        QUEUE_FILES+=("$f")
    done < <(find "$QUEUE_DIR" -name "*.md" -type f -print0 2>/dev/null | sort -z)
    QUEUE_COUNT=${#QUEUE_FILES[@]}
    set -u

    # Check if queue is empty
    if [ $QUEUE_COUNT -eq 0 ]; then
        echo -e "\n${GREEN}✅ Queue is now empty!${NC}"
        echo -e "${CYAN}All dispatches processed.${NC}"
        sleep 2
        exit 0
    fi

    # Ensure index is within bounds
    if [ $CURRENT_INDEX -ge $QUEUE_COUNT ]; then
        CURRENT_INDEX=$((QUEUE_COUNT - 1))
    fi
    if [ $CURRENT_INDEX -lt 0 ]; then
        CURRENT_INDEX=0
    fi

    FILE="${QUEUE_FILES[$CURRENT_INDEX]}"

    # Double-check file exists
    if [ ! -f "$FILE" ]; then
        echo -e "${RED}Error: File not found, rescanning...${NC}"
        continue
    fi

    FILENAME=$(basename "$FILE")

    # Debug output to log
    echo "[$(date)] Displaying dispatch $((CURRENT_INDEX+1))/$QUEUE_COUNT: $FILENAME" >> "$VNX_LOGS_DIR/queue_ui.log"

    # Clear screen for each dispatch
    clear

    # Prepare content for both columns
    LEFT_CONTENT=$(format_dispatch_content "$FILE")
    RIGHT_MENU=$(create_action_menu $((CURRENT_INDEX+1)) $QUEUE_COUNT "$FILENAME")

    # Add validation status to right menu
    VALIDATION_STATUS=$(validate_dispatch "$FILE" 2>&1)
    RIGHT_MENU+="$VALIDATION_STATUS\n"

    # Draw header
    echo -e "${BOLD}${CYAN}╔$(printf '═%.0s' $(seq 1 $((CONTENT_WIDTH))))╤$(printf '═%.0s' $(seq 1 $((ACTION_WIDTH))))╗${NC}"
    echo -e "${BOLD}${CYAN}║${NC} ${BOLD}Dispatch Content${NC}$(printf ' %.0s' $(seq 1 $((CONTENT_WIDTH - 17)))) ${DIM}│${NC} ${BOLD}Actions & Info${NC}"
    echo -e "${BOLD}${CYAN}╟$(printf '─%.0s' $(seq 1 $((CONTENT_WIDTH))))┼$(printf '─%.0s' $(seq 1 $((ACTION_WIDTH))))╢${NC}"

    # Print columns with calculated max height
    print_column "$LEFT_CONTENT" "$RIGHT_MENU" "$CONTENT_LINES"

    # Draw footer
    echo -e "${BOLD}${CYAN}╚$(printf '═%.0s' $(seq 1 $((CONTENT_WIDTH))))╧$(printf '═%.0s' $(seq 1 $((ACTION_WIDTH))))╝${NC}"

    echo ""
    echo -n -e "${BOLD}Your choice: ${NC}"

    # Read user input (now properly attached to TTY)
    IFS= read -r -n 1 choice 2>/dev/null || choice=""
    echo ""  # New line after input

    # Convert to lowercase for comparison (portable way)
    choice_lower=$(echo "$choice" | tr '[:upper:]' '[:lower:]')

    case "$choice_lower" in
        a)
            # Accept - move to pending
            mv "$FILE" "$PENDING_DIR/"
            echo -e "\n${GREEN}✅ Accepted! Moved to pending for dispatch${NC}"
            echo -e "${CYAN}Dispatcher will pick it up shortly...${NC}"
            sleep 1.5
            # Stay on same index (next file will shift down)
            ;;
        r)
            # Reject - move to rejected
            echo -n -e "\n${YELLOW}Rejection reason (optional): ${NC}"
            IFS= read -r reason 2>/dev/null || reason=""
            if [ -n "$reason" ]; then
                echo "# Rejected: $reason" >> "$FILE"
                echo "# Date: $(date)" >> "$FILE"
            fi
            mv "$FILE" "$REJECTED_DIR/"
            echo -e "${RED}❌ Rejected and archived${NC}"
            sleep 1
            # Stay on same index (next file will shift down)
            ;;
        s)
            # Skip
            echo -e "\n${BLUE}⏭️  Skipped to next${NC}"
            CURRENT_INDEX=$((CURRENT_INDEX + 1))
            sleep 0.5
            ;;
        e)
            # Edit with better editor support
            echo -e "\n${CYAN}📝 Opening editor...${NC}"

            # Make a backup before editing
            cp "$FILE" "${FILE}.backup"

            # Edit the file
            edit_in_pane "$FILE"

            # Check if valid after edit
            if validate_dispatch "$FILE"; then
                echo -e "${GREEN}✓ Edits saved and validated${NC}"
                rm -f "${FILE}.backup"
            else
                echo -e "${RED}⚠ Edit resulted in invalid format${NC}"
                echo -n "Restore backup? [Y/n]: "
                IFS= read -r -n 1 restore 2>/dev/null || restore="y"
                restore_lower=$(echo "$restore" | tr '[:upper:]' '[:lower:]')
                if [[ "$restore_lower" != "n" ]]; then
                    cp "${FILE}.backup" "$FILE"
                    echo -e "\n${YELLOW}Backup restored${NC}"
                fi
                rm -f "${FILE}.backup"
            fi

            sleep 1.5
            # Stay on current item
            ;;
        v)
            # View with scrollable content
            echo -e "\n${CYAN}📄 Showing full content (use Page Up/Down or arrows to scroll)...${NC}"

            # Use less for scrolling if available
            if command -v less >/dev/null 2>&1; then
                less -R "$FILE"
            else
                # Fallback to simple pagination
                echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                lines_shown=0
                max_lines=$((LINES - 10))

                while IFS= read -r line; do
                    echo "$line"
                    lines_shown=$((lines_shown + 1))
                    if [ $lines_shown -ge $max_lines ]; then
                        echo -e "\n${BLUE}--- Press any key for next page, or 'q' to quit ---${NC}"
                        IFS= read -r -n 1 key 2>/dev/null || key=""
                        if [[ "$key" == "q" || "$key" == "Q" ]]; then
                            break
                        fi
                        clear
                        lines_shown=0
                    fi
                done < "$FILE"

                echo -e "\n${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                echo -e "${BLUE}Press any key to return to queue...${NC}"
                IFS= read -r -n 1 2>/dev/null || true
            fi

            # Stay on current item
            ;;
        n|' ')
            # Next
            CURRENT_INDEX=$((CURRENT_INDEX + 1))
            if [ $CURRENT_INDEX -ge $QUEUE_COUNT ]; then
                echo -e "\n${YELLOW}Already at last item${NC}"
                CURRENT_INDEX=$((QUEUE_COUNT - 1))
                sleep 0.5
            else
                echo -e "\n${BLUE}⏭️  Next item${NC}"
                sleep 0.5
            fi
            ;;
        p)
            # Previous
            if [ $CURRENT_INDEX -gt 0 ]; then
                CURRENT_INDEX=$((CURRENT_INDEX - 1))
                echo -e "\n${BLUE}⏮️  Previous item${NC}"
                sleep 0.5
            else
                echo -e "\n${YELLOW}Already at first item${NC}"
                sleep 0.5
            fi
            ;;
        q)
            # Quit
            echo -e "\n${YELLOW}👋 Exiting queue manager...${NC}"
            exit 0
            ;;
        *)
            # Invalid choice
            echo -e "\n${RED}❓ Invalid choice. Please try again.${NC}"
            sleep 1
            # Stay on current item
            ;;
    esac
done
