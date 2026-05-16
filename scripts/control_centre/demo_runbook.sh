#!/usr/bin/env bash
# Wave 5 Control Centre demo runbook.
# Produces a verifiable trace of status → dispatch → track → final-status.
# Run from repo root: bash scripts/control_centre/demo_runbook.sh
#
# No live T0 processes are required. Track will timeout (expected for demo).
# All output is saved under $DEMO_DIR.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEMO_DIR="/tmp/vnx-cc-demo-$(date +%Y%m%d-%H%M%S)"
REGISTRY="$DEMO_DIR/projects.yaml"
CLI="$REPO_ROOT/scripts/control_centre_cli.py"

echo "[setup] Demo directory: $DEMO_DIR"
mkdir -p "$DEMO_DIR/proj-alpha" "$DEMO_DIR/proj-beta"

cat > "$REGISTRY" << EOF
projects:
  - id: proj-alpha
    root: $DEMO_DIR/proj-alpha
  - id: proj-beta
    root: $DEMO_DIR/proj-beta
EOF

echo
echo "[1/5] Initial status — two projects, no T0 spawned yet"
python3 "$CLI" --registry "$REGISTRY" status \
  2>&1 | tee "$DEMO_DIR/01-status.txt"

echo
echo "[2/5] Dispatch task to proj-alpha"
DISPATCH_ID=$(python3 "$CLI" --registry "$REGISTRY" dispatch \
  --project proj-alpha \
  --task "Verify API key rotation logic in src/auth.py" \
  2>/dev/null | head -1)

echo "Dispatch ID: $DISPATCH_ID"
echo "$DISPATCH_ID" > "$DEMO_DIR/02-dispatch-id.txt"

# Show the created dispatch files as proof
PENDING_DIR="$DEMO_DIR/proj-alpha/.vnx-data/dispatches/pending/$DISPATCH_ID"
echo "Dispatch files created:"
ls -la "$PENDING_DIR/" 2>&1 | tee "$DEMO_DIR/02-dispatch-files.txt"
echo "dispatch.json:"
cat "$PENDING_DIR/dispatch.json" 2>&1 | tee "$DEMO_DIR/02-dispatch.json"

echo
echo "[3/5] Track lifecycle (10s timeout — no live T0, will timeout as expected)"
python3 "$CLI" --registry "$REGISTRY" track \
  --project proj-alpha \
  --timeout 10 \
  "$DISPATCH_ID" \
  2>&1 | tee "$DEMO_DIR/03-track.txt" || true

echo
echo "[4/5] Dispatch second task to proj-beta"
DISPATCH_ID_BETA=$(python3 "$CLI" --registry "$REGISTRY" dispatch \
  --project proj-beta \
  --task "Run schema drift check on runtime_coordination.db" \
  2>/dev/null | head -1)

echo "Dispatch ID (beta): $DISPATCH_ID_BETA"
echo "$DISPATCH_ID_BETA" > "$DEMO_DIR/04-dispatch-beta-id.txt"

echo
echo "[5/5] Final status — both projects show pending dispatches in queue"
python3 "$CLI" --registry "$REGISTRY" status \
  2>&1 | tee "$DEMO_DIR/05-final-status.txt"

echo
echo "Pending queue for proj-alpha:"
ls "$DEMO_DIR/proj-alpha/.vnx-data/dispatches/pending/" 2>&1

echo
echo "Pending queue for proj-beta:"
ls "$DEMO_DIR/proj-beta/.vnx-data/dispatches/pending/" 2>&1

echo
echo "=== Demo complete ==="
echo "Trace saved to: $DEMO_DIR"
echo "Files:"
ls "$DEMO_DIR/"
