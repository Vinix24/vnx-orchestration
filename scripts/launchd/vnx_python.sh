#!/usr/bin/env bash
# vnx_python.sh <engine-root>: print the interpreter a launchd job starts (the root's venv, else Homebrew 3.12, else the first of python3.13/3.12/3.11 on PATH, else python3); exit 78 when it is outside the root's requires-python.
fail() { echo "ERROR: interpreter ${PY:-?} ${V:-?}: $1" >&2; exit 78; }
ROOT="${1:-}"; [ -n "$ROOT" ] || fail "usage: vnx_python.sh <engine-root>"
PY=python3
[ -x /opt/homebrew/opt/python@3.12/bin/python3.12 ] && PY=/opt/homebrew/opt/python@3.12/bin/python3.12
if [ "$PY" = python3 ]; then
    for N in python3.13 python3.12 python3.11; do P="$(command -v "$N")" && { PY="$P"; break; }; done
fi
[ -x "$ROOT/.venv/bin/python" ] && PY="$ROOT/.venv/bin/python"
[[ "$("$PY" --version 2>&1)" =~ ([0-9]+)\.([0-9]+) ]] || fail "version is not readable"
V="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}"; HAVE=$((BASH_REMATCH[1] * 1000 + BASH_REMATCH[2]))
SPEC="$(sed -n 's/^requires-python[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$ROOT/pyproject.toml" 2>/dev/null | head -n 1)"
[ -n "$SPEC" ] || fail "no requires-python in $ROOT/pyproject.toml"
for C in ${SPEC//,/ }; do
    [[ "$C" =~ ^(\>=|\<=|\>|\<)([0-9]+)\.([0-9]+) ]] || fail "requires-python clause $C is not understood"
    B=$((BASH_REMATCH[2] * 1000 + BASH_REMATCH[3]))
    case "${BASH_REMATCH[1]}" in '>=') ((HAVE >= B)) ;; '>') ((HAVE > B)) ;; '<=') ((HAVE <= B)) ;; *) ((HAVE < B)) ;; esac || fail "outside requires-python $SPEC"
done
echo "$PY"
