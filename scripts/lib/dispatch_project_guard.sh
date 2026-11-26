#!/usr/bin/env bash
# dispatch_project_guard.sh — OI-1067 cross-project contamination guard.
#
# Provides project guards used by dispatcher_minimal.sh:
#
#   vnx_dispatch_resolve_project_id [explicit]
#       Resolve through project_scope using the state store, marker and env.
#       Without a valid source, record the refusal and return non-zero.
#
#   vnx_dispatch_assert_dir_under <child> <parent>
#       Return 0 if canonicalized <child> is the same directory as <parent>
#       or a descendant of it, 1 otherwise. Used to verify VNX_DISPATCH_DIR
#       sits under VNX_DATA_DIR so a stray override cannot make the dispatcher
#       read or write into another tenant's pending/active queues.
#
#   vnx_dispatch_validate_project_id <dispatch_file> <expected_pid> <rejected_dir>
#       Compare the dispatch's stamped Project-ID (extracted via
#       vnx_dispatch_extract_project_id from dispatch_metadata.sh) against
#       <expected_pid>. Behavior:
#         - empty stamp:        print 'reject' and return 1 (OI-1316 — unstamped
#                               dispatches are quarantined, not accepted as legacy)
#         - matching:           print 'match'  and return 0
#         - mismatching:        print 'reject' and return 1; appends a
#                               [REJECTED: project_id mismatch] marker to
#                               the dispatch, then moves it to <rejected_dir>
#         - malformed expected: print 'fatal'  and return 2
#
#       Pure: no logging side effects beyond the dispatch file move + marker.
#       Callers (dispatcher_minimal.sh) translate the printed status into
#       structured log lines.

# Strict allowlist for project_id values (mirrors project_scope.py §3.2).
_VNX_PROJECT_ID_RE='^[a-z][a-z0-9-]{1,31}$'
_VNX_PROJECT_GUARD_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

vnx_dispatch_resolve_project_id() {
    python3 - "$_VNX_PROJECT_GUARD_LIB" "${1:-}" "${VNX_STATE_DIR:-${VNX_DATA_DIR:-.}/state}" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from project_scope import resolve_stamp_project_id, TenantUnresolved
from project_id_refusals import record_project_id_refusal
anchor = Path(sys.argv[3])
try:
    print(resolve_stamp_project_id(explicit=sys.argv[2], db_path=anchor / "runtime_coordination.db"))
except TenantUnresolved as exc:
    record_project_id_refusal("dispatcher_minimal", anchor, "project_id_unresolved")
    sys.exit(str(exc))
PY
}

# Canonicalize a directory path by resolving symlinks. For non-existent paths,
# walk up to the deepest existing ancestor, canonicalize that, then append the
# remaining suffix. This ensures ".." components in a non-existent child path
# cannot slip past the prefix check — bash case "*" matches "/" so a raw
# "../other" suffix would falsely satisfy the parent-prefix pattern.
_vnx_dpg_canon() {
    local p="$1"
    if [ -d "$p" ]; then
        (cd -P "$p" && pwd -P)
        return
    fi
    local base="$p" rest=""
    while [ -n "$base" ] && [ "$base" != "/" ] && ! [ -d "$base" ]; do
        rest="/$(basename "$base")${rest}"
        base="$(dirname "$base")"
    done
    if [ -d "$base" ]; then
        printf '%s\n' "$(cd -P "$base" && pwd -P)${rest}"
    else
        printf '%s\n' "$p"
    fi
}

vnx_dispatch_assert_dir_under() {
    local child="$1" parent="$2"
    local c p
    c="$(_vnx_dpg_canon "$child")"
    p="$(_vnx_dpg_canon "$parent")"
    [ -n "$c" ] && [ -n "$p" ] || return 1
    case "$c/" in
        "$p"/*|"$p/") return 0 ;;
        *) return 1 ;;
    esac
}

vnx_dispatch_validate_project_id() {
    local dispatch="$1" expected="$2" rejected_dir="$3"

    if ! [[ "$expected" =~ $_VNX_PROJECT_ID_RE ]]; then
        printf 'fatal\n'
        return 2
    fi

    local stamped
    stamped="$(vnx_dispatch_extract_project_id "$dispatch" 2>/dev/null || true)"

    if [ -z "$stamped" ]; then
        if ! grep -q "\[REJECTED: unstamped dispatch\]" "$dispatch" 2>/dev/null; then
            printf '\n\n[REJECTED: unstamped dispatch] no Project-ID header; dispatcher bound to %q (OI-1316).\n' \
                "$expected" >> "$dispatch"
        fi
        if [ -n "$rejected_dir" ] && [ -d "$rejected_dir" ] && [ -f "$dispatch" ]; then
            mv "$dispatch" "$rejected_dir/" 2>/dev/null || true
        fi
        printf 'reject\n'
        return 1
    fi

    if [ "$stamped" = "$expected" ]; then
        printf 'match\n'
        return 0
    fi

    if ! grep -q "\[REJECTED: project_id mismatch" "$dispatch" 2>/dev/null; then
        printf '\n\n[REJECTED: project_id mismatch] dispatch stamped Project-ID=%q but dispatcher bound to %q (OI-1067).\n' \
            "$stamped" "$expected" >> "$dispatch"
    fi
    if [ -n "$rejected_dir" ] && [ -d "$rejected_dir" ] && [ -f "$dispatch" ]; then
        mv "$dispatch" "$rejected_dir/" 2>/dev/null || true
    fi
    printf 'reject\n'
    return 1
}
