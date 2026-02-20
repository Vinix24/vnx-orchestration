#!/bin/bash
# List valid skills for T0 reference
# Usage: ./list_valid_skills.sh [--search TERM]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"
SKILLS_YAML="$VNX_SKILLS_DIR/skills.yaml"

if [[ "$1" == "--search" ]]; then
    SEARCH_TERM="$2"
    echo "🔍 Searching for skill matching: $SEARCH_TERM"
    echo ""

    # Derive valid skills from skills: block (single source of truth)
    MATCH=$(python3 -c "
import yaml, sys
with open('$SKILLS_YAML') as f:
    d = yaml.safe_load(f)
skills = list(d.get('skills', {}).keys())
term = sys.argv[1].lower()
matches = [s for s in skills if term in s.lower()]
print('\n'.join(matches))
" "$SEARCH_TERM")
    if [[ -n "$MATCH" ]]; then
        echo "✅ Valid skill: $MATCH"
        exit 0
    fi

    # Check common mistakes
    CORRECTION=$(grep -A 30 "^common_mistakes:" "$SKILLS_YAML" | grep -i "^  $SEARCH_TERM:" | sed 's/.*: //')
    if [[ -n "$CORRECTION" ]]; then
        echo "⚠️  '$SEARCH_TERM' is not valid"
        echo "✅ Use instead: $CORRECTION"
        exit 0
    fi

    echo "❌ No match found for '$SEARCH_TERM'"
    echo ""
    echo "💡 Tip: Run without --search to see all valid skills"
    exit 1
else
    echo "📋 Valid Skills (use EXACTLY these names)"
    echo "========================================"
    # Derive from skills: block (single source of truth)
    python3 -c "
import yaml
with open('$SKILLS_YAML') as f:
    d = yaml.safe_load(f)
for s in sorted(d.get('skills', {}).keys()):
    print(f'  ✓ {s}')
"
    echo ""
    echo "💡 Tip: Use --search <term> to find a skill"
    echo "   Example: ./list_valid_skills.sh --search performance"
fi
