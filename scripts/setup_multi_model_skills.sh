#!/usr/bin/env bash
# setup_multi_model_skills.sh
# Symlinks VNX skills to Claude, Codex and Gemini platform folders
#
# Run from your project root:
#   ./vnx-system/scripts/setup_multi_model_skills.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VNX_SKILLS="$SCRIPT_DIR/../skills"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Skills to symlink (folders with SKILL.md)
SKILLS=$(find "$VNX_SKILLS" -maxdepth 1 -type d ! -name skills ! -name '.' | sort)

echo "VNX Multi-Model Skills Setup"
echo "============================"
echo "Source: $VNX_SKILLS"
echo "Project: $PROJECT_ROOT"
echo ""

for PLATFORM in .claude .codex .gemini; do
    TARGET="$PROJECT_ROOT/$PLATFORM/skills"
    
    # Create platform skills directory
    mkdir -p "$TARGET"
    
    COUNT=0
    for SKILL_DIR in $SKILLS; do
        SKILL_NAME=$(basename "$SKILL_DIR")
        
        # Skip if not a skill folder (no SKILL.md)
        [ -f "$SKILL_DIR/SKILL.md" ] || continue
        
        # Create symlink (relative path for portability)
        RELATIVE_PATH=$(python3 -c "import os.path; print(os.path.relpath('$SKILL_DIR', '$TARGET'))")
        
        if [ -L "$TARGET/$SKILL_NAME" ]; then
            echo "  ↻ $PLATFORM/skills/$SKILL_NAME (already linked)"
        elif [ -d "$TARGET/$SKILL_NAME" ]; then
            echo "  ⚠ $PLATFORM/skills/$SKILL_NAME (exists, skipping)"
        else
            ln -s "$RELATIVE_PATH" "$TARGET/$SKILL_NAME"
            echo "  ✓ $PLATFORM/skills/$SKILL_NAME → vnx-system/skills/$SKILL_NAME"
        fi
        COUNT=$((COUNT + 1))
    done
    
    echo "  $COUNT skills linked to $PLATFORM/skills/"
    echo ""
done

# Copy skills.yaml if platforms need it
for PLATFORM in .codex .gemini; do
    if [ ! -f "$PROJECT_ROOT/$PLATFORM/skills/skills.yaml" ]; then
        cp "$VNX_SKILLS/skills.yaml" "$PROJECT_ROOT/$PLATFORM/skills/skills.yaml"
        echo "Copied skills.yaml to $PLATFORM/skills/"
    fi
done

echo "Done! All platforms can now use VNX skills."
echo ""
echo "Verify with:"
echo "  ls -la $PROJECT_ROOT/.claude/skills/"
echo "  ls -la $PROJECT_ROOT/.codex/skills/"
echo "  ls -la $PROJECT_ROOT/.gemini/skills/"
