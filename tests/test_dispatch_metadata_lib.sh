#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../scripts/lib/dispatch_metadata.sh"

assert_eq() {
  local expected="$1"
  local actual="$2"
  local msg="$3"
  if [ "$expected" != "$actual" ]; then
    echo "FAIL: $msg (expected='$expected' actual='$actual')"
    exit 1
  fi
}

tmp_file="$(mktemp)"
cat > "$tmp_file" <<'EOF'
[[TARGET:B]]
Cognition: Deep
Priority: P2; Cognition: deep
Role: backend-developer extra text
Phase: 2.1
Gate: implementation
PR-ID: PR-99
Dispatch-ID: 20260218-151417-sse-correctness-and-contract-h-A
Mode: planning # comment
ClearContext: FALSE
ForceNormalMode: true
Requires-Model: opus
EOF

assert_eq "B" "$(vnx_dispatch_extract_track "$tmp_file")" "track extraction"
assert_eq "deep" "$(vnx_dispatch_extract_cognition "$tmp_file")" "cognition extraction"
assert_eq "P2" "$(vnx_dispatch_extract_priority "$tmp_file")" "priority extraction"
assert_eq "backend-developer" "$(vnx_dispatch_extract_agent_role "$tmp_file")" "role extraction"
assert_eq "backenddeveloper" "$(vnx_dispatch_normalize_role "Backend Developer")" "role normalization"
assert_eq "2.1" "$(vnx_dispatch_extract_phase "$tmp_file")" "phase extraction"
assert_eq "implementation" "$(vnx_dispatch_extract_new_gate "$tmp_file")" "gate extraction"
assert_eq "PR-99" "$(vnx_dispatch_extract_pr_id "$tmp_file")" "pr-id extraction"
assert_eq "20260218-151417-sse-correctness-and-contract-h-A" "$(vnx_dispatch_extract_dispatch_id "$tmp_file")" "dispatch-id extraction"
assert_eq "planning" "$(vnx_dispatch_extract_mode "$tmp_file")" "mode extraction"
assert_eq "false" "$(vnx_dispatch_extract_clear_context "$tmp_file")" "clear-context extraction"
assert_eq "true" "$(vnx_dispatch_extract_force_normal_mode "$tmp_file")" "force-normal extraction"
assert_eq "opus" "$(vnx_dispatch_extract_requires_model "$tmp_file")" "requires-model extraction"

tmp_dir="$(mktemp -d)"
mv "$tmp_file" "$tmp_dir/B2-4_task.md"
tmp_file="$tmp_dir/B2-4_task.md"
assert_eq "B2-4_task" "$(vnx_dispatch_extract_task_id "$tmp_file" "B")" "task id extraction from filename"

missing_file="$(mktemp)"
assert_eq "P1" "$(vnx_dispatch_extract_priority "$missing_file")" "default priority"
assert_eq "normal" "$(vnx_dispatch_extract_cognition "$missing_file")" "default cognition"
assert_eq "none" "$(vnx_dispatch_extract_mode "$missing_file")" "default mode"
assert_eq "false" "$(vnx_dispatch_extract_clear_context "$missing_file")" "default clear-context"
assert_eq "$(basename "$missing_file")" "$(vnx_dispatch_extract_dispatch_id "$missing_file")" "dispatch-id fallback to filename"

rm -f "$tmp_file" "$missing_file"
rm -rf "$tmp_dir"
echo "PASS: dispatch metadata helpers"
