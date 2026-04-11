#!/usr/bin/env bash
# dispatch-agent — thin CLI wrapper for routing agent dispatches via SubprocessAdapter
#
# Usage:
#   dispatch-agent --agent blog-writer --instruction "Write about AI governance"
#   dispatch-agent --agent research-analyst --instruction "Summarize LLM safety papers" --model opus
#
# Validates agent dir, writes dispatch.json to pending/, then executes via
# subprocess_dispatch.py.  Agent must have agents/<name>/CLAUDE.md present.

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS_LIB="$SCRIPTS_DIR/lib"
PROJECT_ROOT="$(cd "$SCRIPTS_DIR/.." && pwd)"

AGENT=""
INSTRUCTION=""
MODEL="${VNX_MODEL:-sonnet}"

# --- arg parsing ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent)       AGENT="$2";       shift 2 ;;
    --instruction) INSTRUCTION="$2"; shift 2 ;;
    --model)       MODEL="$2";       shift 2 ;;
    -h|--help)
      echo "Usage: dispatch-agent --agent <name> --instruction <text> [--model <model>]"
      echo ""
      echo "  --agent        Agent name (must exist under agents/)"
      echo "  --instruction  Instruction text for the agent"
      echo "  --model        Claude model to use (default: sonnet)"
      exit 0 ;;
    *)
      echo "[dispatch-agent] Unknown argument: $1" >&2
      exit 1 ;;
  esac
done

# --- validate required args ---
if [[ -z "$AGENT" ]]; then
  echo "[dispatch-agent] --agent is required" >&2; exit 1
fi
if [[ -z "$INSTRUCTION" ]]; then
  echo "[dispatch-agent] --instruction is required" >&2; exit 1
fi

# --- validate agent directory ---
if [[ ! -f "$PROJECT_ROOT/agents/$AGENT/CLAUDE.md" ]]; then
  echo "[dispatch-agent] Agent not found: agents/$AGENT/CLAUDE.md does not exist" >&2
  if [[ -d "$PROJECT_ROOT/agents" ]]; then
    echo "[dispatch-agent] Available agents:" >&2
    ls "$PROJECT_ROOT/agents/" | sed 's/^/  /' >&2
  fi
  exit 1
fi

# --- write dispatch.json to pending/ and capture dispatch_id ---
DISPATCH_ID=$(
  VNX_AGENT="$AGENT" \
  VNX_PROJECT_ROOT="$PROJECT_ROOT" \
  VNX_INSTRUCTION="$INSTRUCTION" \
  python3 - <<'PYEOF'
import os, json, sys
from pathlib import Path
from datetime import datetime, timezone

agent = os.environ["VNX_AGENT"]
project_root = Path(os.environ["VNX_PROJECT_ROOT"])
instruction = os.environ["VNX_INSTRUCTION"]

sys.path.insert(0, str(project_root / "scripts" / "lib"))
from headless_dispatch_writer import generate_dispatch_id

dispatch_id = generate_dispatch_id(f"agent-{agent}", "A")
pending_dir = project_root / ".vnx-data" / "dispatches" / "pending" / dispatch_id
pending_dir.mkdir(parents=True, exist_ok=True)

payload = {
    "dispatch_id": dispatch_id,
    "terminal": "T1",
    "track": "A",
    "role": agent,
    "skill_name": agent,
    "gate": "gate_fix",
    "cognition": "normal",
    "priority": "P1",
    "pr_id": None,
    "parent_dispatch": None,
    "feature": "F40",
    "branch": None,
    "instruction": instruction,
    "context_files": [],
    "created_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}

dispatch_path = pending_dir / "dispatch.json"
tmp_path = dispatch_path.with_suffix(".json.tmp")
tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
tmp_path.replace(dispatch_path)
print(dispatch_id)
PYEOF
)

echo "[dispatch-agent] Created dispatch: $DISPATCH_ID"

# --- execute via subprocess_dispatch.py ---
python3 "$SCRIPTS_LIB/subprocess_dispatch.py" \
  --terminal-id "T1" \
  --dispatch-id "$DISPATCH_ID" \
  --instruction "$INSTRUCTION" \
  --model "$MODEL" \
  --role "$AGENT"

echo "[dispatch-agent] Agent dispatch complete: $DISPATCH_ID"
