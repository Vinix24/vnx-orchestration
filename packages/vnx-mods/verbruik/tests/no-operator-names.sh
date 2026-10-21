#!/usr/bin/env bash
# Fails when a file of this package names one of the operator's own projects.
# The names are spelled in pieces so that this file does not contain them itself.
set -euo pipefail

package="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
names=("vnx""-dev" "mission""-control" "seocrawler""-v2" "sales""-copilot" "pacompany""-engine")
args=()

for name in "${names[@]}"; do
  args+=(-e "$name")
done

hits="$(grep -rIil "${args[@]}" "$package" --exclude-dir=node_modules --exclude=no-operator-names.sh || true)"

if [ -n "$hits" ]; then
  echo "operator project names found in:" >&2
  echo "$hits" >&2
  exit 1
fi

echo "ok: no operator project names in $package"
