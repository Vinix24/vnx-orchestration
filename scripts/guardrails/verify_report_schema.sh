#!/bin/bash
# verify_report_schema.sh — guardrail wrapper around unified_report_schema validator.
#
# Usage: verify_report_schema.sh <unified_report.md> [<more.md> ...]
#
# Exits 0 when every file's YAML frontmatter validates against
# schemas/unified_report_v1.json. Exits 1 with a 'missing field: X' or
# 'invalid type: X' message on the first failure.
#
# PR-D5-E ships this guardrail; PR-D5-F wires governance_emit to enforce it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [ "$#" -lt 1 ]; then
  echo "usage: $(basename "$0") <unified_report.md> [<more.md> ...]" >&2
  exit 2
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"

for report in "$@"; do
  if [ ! -f "$report" ]; then
    echo "verify_report_schema: report not found: $report" >&2
    exit 1
  fi

  REPO_ROOT_ENV="$REPO_ROOT" REPORT_PATH="$report" \
    "$PYTHON_BIN" - <<'PY'
import os
import sys

repo_root = os.environ["REPO_ROOT_ENV"]
report_path = os.environ["REPORT_PATH"]

sys.path.insert(0, os.path.join(repo_root, "scripts", "lib"))

try:
    from unified_report_schema import SchemaViolation, validate_file  # type: ignore
except ImportError as exc:
    sys.stderr.write(
        f"verify_report_schema: cannot import validator ({exc}).\n"
        "Install dependencies: pip install jsonschema pyyaml\n"
    )
    sys.exit(1)

try:
    validate_file(report_path)
except SchemaViolation as exc:
    sys.stderr.write(
        f"verify_report_schema: {report_path}: {exc}\n"
    )
    sys.exit(1)
except Exception as exc:  # surfaces unexpected runtime errors
    sys.stderr.write(
        f"verify_report_schema: {report_path}: unexpected error: {exc}\n"
    )
    sys.exit(1)

print(f"verify_report_schema: {report_path}: ok")
PY

done

exit 0
