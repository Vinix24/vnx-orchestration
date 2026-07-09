#!/bin/bash
# List valid skills for T0 reference
# Usage: ./list_valid_skills.sh [--search TERM]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/vnx_paths.sh
source "$SCRIPT_DIR/lib/vnx_paths.sh"
SKILLS_YAML="$VNX_SKILLS_DIR/skills.yaml"
SKILLS_DIR="$VNX_SKILLS_DIR"

# Python helper: load manifest + directory skills, excluding opted-out dirs.
_python_skills() {
python3 - "$SKILLS_YAML" "$SKILLS_DIR" <<'PY'
import sys
from pathlib import Path
import yaml

skills_yaml, skills_dir = Path(sys.argv[1]), Path(sys.argv[2])
manifest = set()
if skills_yaml.is_file():
    try:
        data = yaml.safe_load(skills_yaml.read_text()) or {}
        for key, meta in data.get("skills", {}).items():
            name = meta.get("name", key).lstrip("@")
            manifest.add(name)
    except Exception:
        pass

opt_out = ".vnx-skip-sync"
for child in sorted(skills_dir.iterdir()):
    if child.is_dir() and not child.name.startswith(".") and (child / "SKILL.md").is_file():
        if not (child / opt_out).is_file():
            manifest.add(child.name)

print("\n".join(sorted(manifest)))
PY
}

if [[ "$1" == "--search" ]]; then
    SEARCH_TERM="$2"
    echo "🔍 Searching for skill matching: $SEARCH_TERM"
    echo ""

    MATCH=$(_python_skills | grep -i "$SEARCH_TERM" | head -1)
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
    _python_skills | sed 's/^/  ✓ /'
    echo ""
    echo "💡 Tip: Use --search <term> to find a skill"
    echo "   Example: ./list_valid_skills.sh --search performance"
fi
