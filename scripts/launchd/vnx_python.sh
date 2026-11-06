#!/usr/bin/env bash
# vnx_python.sh — print the interpreter a launchd job must start, for one engine root.
#
# Usage: vnx_python.sh <engine-root>
#
# Precedence is the one bin/vnx uses in the foreground: <engine-root>/.venv/bin/python,
# else the pinned Homebrew 3.12, else `python3`. The chosen interpreter is then held
# against `requires-python` in <engine-root>/pyproject.toml. Outside the range, an
# unreadable pyproject or a version that cannot be read -> exit 78 (EX_CONFIG) with one
# stderr line naming the interpreter and its version, and nothing on stdout.
# Bash only: no Python is needed to pick Python.

set -u

ROOT="${1:-}"
if [ -z "$ROOT" ]; then
    echo "usage: $(basename "$0") <engine-root>" >&2
    exit 78
fi

if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
elif [ -x "/opt/homebrew/opt/python@3.12/bin/python3.12" ]; then
    PY="/opt/homebrew/opt/python@3.12/bin/python3.12"
else
    PY="python3"
fi

fail() {
    echo "ERROR: interpreter $PY ${VERSION:-(version unknown)}: $1" >&2
    exit 78
}

VERSION=""
VERSION_OUT="$("$PY" --version 2>&1)" || fail "cannot report its version"
if [[ "$VERSION_OUT" =~ ([0-9]+)\.([0-9]+) ]]; then
    MAJOR="${BASH_REMATCH[1]}"
    MINOR="${BASH_REMATCH[2]}"
    VERSION="$MAJOR.$MINOR"
else
    fail "version output is not readable: $VERSION_OUT"
fi

SPEC="$(sed -n 's/^requires-python[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$ROOT/pyproject.toml" 2>/dev/null | head -n 1)"
[ -n "$SPEC" ] || fail "no requires-python in $ROOT/pyproject.toml"

IFS=',' read -r -a CLAUSES <<< "$SPEC"
for clause in "${CLAUSES[@]}"; do
    clause="${clause// /}"
    if [[ "$clause" =~ ^(\>=|\<=|\>|\<)([0-9]+)\.([0-9]+)(\.[0-9]+)*$ ]]; then
        op="${BASH_REMATCH[1]}"
        bound=$(( ${BASH_REMATCH[2]} * 1000 + ${BASH_REMATCH[3]} ))
    else
        fail "requires-python clause '$clause' is not understood"
    fi
    have=$(( MAJOR * 1000 + MINOR ))
    case "$op" in
        '>=') [ "$have" -ge "$bound" ] || fail "outside requires-python $SPEC" ;;
        '>')  [ "$have" -gt "$bound" ] || fail "outside requires-python $SPEC" ;;
        '<=') [ "$have" -le "$bound" ] || fail "outside requires-python $SPEC" ;;
        '<')  [ "$have" -lt "$bound" ] || fail "outside requires-python $SPEC" ;;
    esac
done

printf '%s\n' "$PY"
