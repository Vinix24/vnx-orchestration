# shellcheck shell=bash
# rp_time.sh - Time/timestamp helpers
# Sourced by scripts/receipt_processor_v4.sh
# Requires: $MODE, $MAX_AGE_HOURS, $LAST_PROCESSED, $WATERMARK_FILE

# Calculate cutoff timestamp based on mode
get_cutoff_time() {
    case "$MODE" in
        monitor)
            # Monitor mode: only process new reports from now on
            date '+%Y%m%d-%H%M%S'
            ;;
        catchup)
            # Catchup mode: process reports from last N hours
            # Cross-platform: try GNU date first, then BSD (macOS)
            date -d "-${MAX_AGE_HOURS} hours" '+%Y%m%d-%H%M%S' 2>/dev/null \
                || date -v-${MAX_AGE_HOURS}H '+%Y%m%d-%H%M%S'
            ;;
        manual)
            # Manual mode: use stored timestamp or default to 1 hour
            if [ -f "$LAST_PROCESSED" ]; then
                cat "$LAST_PROCESSED"
            else
                date -d "-1 hour" '+%Y%m%d-%H%M%S' 2>/dev/null \
                    || date -v-1H '+%Y%m%d-%H%M%S'
            fi
            ;;
    esac
}

# Extract timestamp from report filename
extract_timestamp() {
    local filename=$(basename "$1")
    # Match YYYYMMDD-HHMMSS pattern at start of filename
    echo "$filename" | grep -oE '^[0-9]{8}-[0-9]{6}'
}

# Compute the cutoff epoch seconds for report age filtering based on current MODE.
_spr_get_cutoff_seconds() {
    if [ "$MODE" = "monitor" ]; then
        # Monitor mode: use watermark-based processing.
        # Only process reports newer than the last successfully processed report's mtime.
        # On first run (no watermark), fall back to 24 hours to avoid replaying history.
        if [ -f "$WATERMARK_FILE" ]; then
            local cs
            cs=$(cat "$WATERMARK_FILE" 2>/dev/null)
            if ! [[ "$cs" =~ ^[0-9]+$ ]]; then
                cs=$(($(date +%s) - 86400))
            fi
            echo "$cs"
        else
            echo "$(($(date +%s) - 86400))"
        fi
    elif [ "$MODE" = "manual" ] && [ -f "$WATERMARK_FILE" ]; then
        # Manual mode: honor last-processed watermark (epoch seconds) when available
        local cs
        cs=$(cat "$WATERMARK_FILE" 2>/dev/null)
        if ! [[ "$cs" =~ ^[0-9]+$ ]]; then
            cs=$(($(date +%s) - (MAX_AGE_HOURS * 3600)))
        fi
        echo "$cs"
    else
        # Catchup mode (or manual with no prior watermark): process last N hours
        echo "$(($(date +%s) - (MAX_AGE_HOURS * 3600)))"
    fi
}
