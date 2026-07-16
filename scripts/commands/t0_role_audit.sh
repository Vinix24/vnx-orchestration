#!/usr/bin/env bash
# VNX fleet audit: does each project's live (or registered) T0 session
# actually load its role file (.claude/terminals/T0/CLAUDE.md)?
#
# Claude Code discovers CLAUDE.md by walking UP from cwd, merging every
# CLAUDE.md found in ancestor directories. .claude/terminals/T0/CLAUDE.md is a
# DESCENDANT of the project root, not an ancestor of it — so a T0 claude
# process only picks it up when its cwd is exactly <project_root>/.claude/
# terminals/T0. Any launch path that starts claude at the project root
# instead (a stale tmux-resurrect/continuum respawn, a manual re-attach, a
# launcher bug) silently drops the orchestrator role file — no error, no
# warning, T0 just runs without its playbook.
#
# This makes that drift observable: it reports, per project/terminal,
# whether the running T0 would load its role file right now.
#
# Also audits a SECOND, independent drift class: role<->skill invocability.
# role-orchestrator.md presupposes that certain skills are loadable (either
# in-context via a CLAUDE.md `@`-import, or model-invocable via the Skill
# tool). Nothing previously cross-checked that presupposition against skill
# frontmatter (`disable-model-invocation: true`) or import targets actually
# existing on disk — that gap is exactly how F1 (t0-orchestrator unloadable
# in the fabric source) went undetected for ~7 weeks. `--static` runs this
# check standalone; the default live-session audit below also runs it
# automatically for every project it discovers.
#
# Usage:
#   bash scripts/commands/t0_role_audit.sh                    # live-session audit (unchanged)
#   bash scripts/commands/t0_role_audit.sh --static [ROOT]     # role<->skill invocability only
#                                                                # ROOT defaults to cwd's git root
#
# Standalone by design — this must also audit OTHER registered VNX projects,
# not just the one it happens to be invoked from, so it does not depend on
# bin/vnx's command loader (PROJECT_ROOT/VNX_HOME/log/err are not assumed).

set -uo pipefail

# Absolute path to this script itself, so the registry sub-pass (a Python
# heredoc, see below) can re-invoke `--static` per registered project without
# guessing where it lives.
_T0_AUDIT_SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

# Resolve an `@`-import target relative to the file that contains it.
# $1 = raw path text after the leading `@` on an import line
# $2 = directory of the importing file
_t0_static_resolve_import() {
  local raw="$1" base_dir="$2"
  case "$raw" in
    '~'*) printf '%s' "${raw/#\~/$HOME}" ;;
    /*)   printf '%s' "$raw" ;;
    *)    printf '%s' "$base_dir/$raw" ;;
  esac
}

# Naive frontmatter check: does the FIRST `---`...`---` block contain the
# literal line `disable-model-invocation: true`? This is deliberately not a
# YAML parser — it must never grow into one.
#
# Anchored on the key's own line-start (no leading whitespace — real
# top-level frontmatter keys never have any) and skips comment lines. Without
# the anchor, a `description:` field or comment that merely MENTIONS the
# string `disable-model-invocation: true` (e.g. documenting the flag) false-
# positived as SKILL-UNLOADABLE (finding 3, codex, 2026-07-16).
_t0_static_frontmatter_disables_invocation() {
  awk '
    /^---[ \t]*$/ { n++; if (n == 2) exit }
    n == 1 && /^[ \t]*#/ { next }
    n == 1 && /^disable-model-invocation:[ \t]*true([ \t]|$)/ { found = 1 }
    END { exit(found ? 0 : 1) }
  ' "$1"
}

# Does this project's SessionStart hook (the Claude-Code-only mechanism —
# see hooks/sessionstart.sh and role-orchestrator.md's "Mandatory Startup")
# inject a given skill's SKILL.md body into T0 context? Checked by literal
# path reference, not execution — deliberately static, matching this
# script's "never grow into a real parser/interpreter" discipline. Checks
# the deployed consumer copy first, then the fabric-source template (the
# shape this very repo's own checkout has, since it never runs `vnx init`
# on itself).
_t0_static_hook_injects_skill() {
  local root="$1" skill_name="$2" hook
  for hook in "$root/.claude/hooks/sessionstart.sh" "$root/hooks/sessionstart.sh"; do
    [ -f "$hook" ] || continue
    grep -q "skills/$skill_name/SKILL.md" "$hook" 2>/dev/null && return 0
  done
  return 1
}

# Static role<->skill invocability check for one project root. Prints one
# finding per line (IMPORT-MISSING / SKILL-UNLOADABLE) to stdout; returns 0
# when clean, 1 when any finding was printed.
#
#   IMPORT-MISSING    — an `@`-import line in the T0 CLAUDE.md or
#                        role-orchestrator.md resolves to a file that does
#                        not exist.
#   SKILL-UNLOADABLE  — a backtick-quoted `@<skill>` reference in
#                        role-orchestrator.md names a skill that is neither
#                        in-context (imported by CLAUDE.md, or injected by the
#                        SessionStart hook) nor model-invocable (SKILL.md
#                        missing, or present with `disable-model-invocation:
#                        true` and not otherwise in-context).
#   PLAYBOOK-MECHANISM-GAP — AGENTS.md/GEMINI.md (the codex/gemini T0
#                        surfaces `vnx role sync` mirrors the role into) carry
#                        the role text, but neither provider has a
#                        SessionStart-hook equivalent to deliver the
#                        playbook body in-context there. A tracked, reported
#                        gap (finding 2, codex, 2026-07-16) — not silently
#                        clean, but not required to block on today.
_t0_static_check() {
  local root="$1"
  local t0_dir="$root/.claude/terminals/T0"
  local claude_md="$t0_dir/CLAUDE.md"
  local role_md="$t0_dir/role-orchestrator.md"
  local findings=0
  local imported_targets=""

  local f dir line raw resolved
  for f in "$claude_md" "$role_md"; do
    [ -f "$f" ] || continue
    dir="$(dirname "$f")"
    while IFS= read -r line; do
      [ -z "$line" ] && continue
      raw="${line#@}"
      resolved="$(_t0_static_resolve_import "$raw" "$dir")"
      if [ ! -f "$resolved" ]; then
        echo "IMPORT-MISSING: $f imports '@$raw' -> $resolved (does not exist)"
        findings=$((findings + 1))
      else
        # Normalize (the raw concatenation may still contain `../`) so an
        # import target and a skill's canonical SKILL.md path compare equal
        # whenever they name the same file on disk.
        imported_targets="$imported_targets|$(realpath "$resolved" 2>/dev/null || printf '%s' "$resolved")"
      fi
    done < <(grep -h '^@' "$f" 2>/dev/null)
  done

  if [ -f "$role_md" ]; then
    local skill_name skill_md
    while IFS= read -r skill_name; do
      [ -z "$skill_name" ] && continue
      skill_md="$root/.claude/skills/$skill_name/SKILL.md"

      if [ ! -f "$skill_md" ]; then
        echo "SKILL-UNLOADABLE: role-orchestrator.md references '@$skill_name' but $skill_md does not exist"
        findings=$((findings + 1))
        continue
      fi

      local skill_md_real
      skill_md_real="$(realpath "$skill_md" 2>/dev/null || printf '%s' "$skill_md")"
      case "$imported_targets" in
        *"|$skill_md_real"*) continue ;;  # in-context via CLAUDE.md import; invocability irrelevant
      esac

      if _t0_static_hook_injects_skill "$root" "$skill_name"; then
        continue  # in-context via SessionStart hook injection; invocability irrelevant
      fi

      if _t0_static_frontmatter_disables_invocation "$skill_md"; then
        echo "SKILL-UNLOADABLE: role-orchestrator.md references '@$skill_name' — not imported by CLAUDE.md, not hook-injected, and disable-model-invocation: true in $skill_md"
        findings=$((findings + 1))
      fi
    done < <(grep -oE '`@[a-zA-Z0-9_-]+`' "$role_md" 2>/dev/null | tr -d '`@' | sort -u)
  fi

  # Tri-file surfaces: `vnx role sync --apply` mirrors the SAME Mandatory
  # Startup role text into AGENTS.md (codex) and GEMINI.md (gemini), marked
  # by <!-- VNX:BEGIN T0-ROLE --> ... <!-- VNX:END T0-ROLE -->. A project can
  # audit clean on CLAUDE.md while its codex/gemini T0 surface carries a
  # "Mandatory Startup" step with no working delivery mechanism at all —
  # Claude Code's SessionStart-hook injection has no codex/gemini equivalent
  # yet. Report it rather than staying silent (finding 2); closing the gap
  # itself is tracked separately, not enforced here.
  local provider_file
  for provider_file in "$t0_dir/AGENTS.md" "$t0_dir/GEMINI.md"; do
    [ -f "$provider_file" ] || continue
    grep -q '<!-- VNX:BEGIN T0-ROLE -->' "$provider_file" 2>/dev/null || continue
    echo "PLAYBOOK-MECHANISM-GAP: $provider_file carries the T0 role but has no SessionStart-hook equivalent to deliver the t0-orchestrator playbook body in-context on this surface (tracked open item, not currently blocking)"
    findings=$((findings + 1))
  done

  [ "$findings" -eq 0 ]
}

# All descendant PIDs of $1 (the FULL process tree, not just direct children).
# BFS over pgrep -P; process trees are acyclic so this always terminates.
_t0_audit_descendants() {
  local queue="$1" next child kids acc=""
  while [ -n "$queue" ]; do
    next=""
    for child in $queue; do
      kids="$(pgrep -P "$child" 2>/dev/null)"
      [ -z "$kids" ] && continue
      acc="$acc $kids"
      next="$next $kids"
    done
    queue="$next"
  done
  printf '%s' "$acc"
}

# Detect whether a tmux pane has an active CLI (claude/codex/gemini/node) in
# its process tree. Checked via `ps`, not tmux's `#{pane_current_command}`:
# a running claude process can report its own version string (e.g. "2.1.201")
# there instead of the literal binary name. Walks the whole descendant tree so
# a CLI launched under an intermediate shell/wrapper is still detected.
_t0_audit_active_cli() {
  local pane_pid="$1" pid comm
  [ -z "$pane_pid" ] && return 1
  for pid in "$pane_pid" $(_t0_audit_descendants "$pane_pid"); do
    comm="$(ps -o comm= -p "$pid" 2>/dev/null | tr -d ' ')"
    comm="${comm##*/}"
    case "$comm" in
      claude|codex|gemini|node) return 0 ;;
    esac
  done
  return 1
}

_t0_audit_row() {
  printf '%-22s %-6s %-9s %-9s %-55s\n' "$1" "$2" "$3" "$4" "$5"
}

main() {
  if [ "${1:-}" = "--static" ]; then
    local static_root="${2:-}"
    if [ -z "$static_root" ]; then
      static_root="$(git rev-parse --show-toplevel 2>/dev/null)" || static_root="$(pwd)"
    fi
    local static_output
    static_output="$(_t0_static_check "$static_root")"
    if [ -z "$static_output" ]; then
      echo "(clean — no role<->skill invocability drift found in $static_root)"
      return 0
    fi
    printf '%s\n' "$static_output"
    return 1
  fi

  if ! command -v tmux >/dev/null 2>&1; then
    echo "tmux not found — cannot audit live sessions." >&2
    exit 1
  fi

  _t0_audit_row "PROJECT" "TERM" "LOADED?" "CLI" "CWD"
  _t0_audit_row "-------" "----" "-------" "---" "---"

  local found_any=0
  local seen_roots=""

  # pane_current_path is read LAST so an embedded space in the cwd is absorbed
  # into pane_path by read's remainder rule, instead of shifting into pane_pid.
  while IFS=' ' read -r pane_id pane_pid pane_path; do
    [ -z "$pane_id" ] && continue

    # Skip per-dispatch worker worktrees (tmux_interactive_dispatch.py checks
    # out one worktree per dispatch under .vnx-data/worktrees/). Those panes
    # run a worker (T1/T2/T3-style implementation task), never a T0
    # orchestrator, even though the checkout also happens to contain a
    # .claude/terminals/T0/CLAUDE.md file inherited from the repo tree.
    case "$pane_path" in
      */.vnx-data/worktrees/*) continue ;;
    esac

    local project_root="" loaded=""
    if [ "${pane_path%/.claude/terminals/T0}" != "$pane_path" ]; then
      # cwd is exactly <root>/.claude/terminals/T0 — role file loads.
      project_root="${pane_path%/.claude/terminals/T0}"
      loaded="yes"
    elif [ -f "$pane_path/.claude/terminals/T0/CLAUDE.md" ]; then
      # cwd is a project root that HAS a T0 role file, but this pane is not
      # inside it — the generic project CLAUDE.md loads instead.
      project_root="$pane_path"
      loaded="no"
    else
      continue  # Not a VNX T0-governed pane — skip (e.g. a plain client session).
    fi

    found_any=1
    case " $seen_roots " in
      *" $project_root "*) ;;
      *) seen_roots="$seen_roots $project_root" ;;
    esac

    local cli="(idle)"
    _t0_audit_active_cli "$pane_pid" && cli="running"

    _t0_audit_row "$(basename "$project_root")" "T0" "$loaded" "$cli" "$pane_path"

    local static_output_live
    static_output_live="$(_t0_static_check "$project_root")"
    if [ -n "$static_output_live" ]; then
      echo "  [static] role<->skill invocability drift:"
      printf '%s\n' "$static_output_live" | sed 's/^/    /'
    fi
  done < <(tmux list-panes -a -F '#{pane_id} #{pane_pid} #{pane_current_path}' 2>/dev/null)

  if [ "$found_any" -eq 0 ]; then
    echo "(no live T0 sessions found)"
  fi

  # Cross-reference the project registry (~/.vnx/projects.json) so projects
  # with a configured T0 role file but no live tmux session are still listed,
  # instead of silently disappearing from the report.
  local registry="$HOME/.vnx/projects.json"
  if [ -f "$registry" ] && command -v python3 >/dev/null 2>&1; then
    python3 - "$registry" "$seen_roots" "$_T0_AUDIT_SELF" <<'PYEOF'
import json, os, subprocess, sys

registry_file, seen_raw, audit_self = sys.argv[1], sys.argv[2], sys.argv[3]
seen = set(seen_raw.split())

try:
    with open(registry_file) as f:
        data = json.load(f)
except Exception as exc:
    # Fail loud: a registry that exists but cannot be read/parsed is a real
    # error — swallowing it as exit 0 would report a clean audit while the
    # not-running-projects cross-reference was silently skipped.
    print(
        f"warning: could not read/parse T0 project registry {registry_file}: {exc}",
        file=sys.stderr,
    )
    sys.exit(1)

for p in data.get("projects", []):
    root = p.get("path", "")
    if not root or root in seen:
        continue
    role_file = os.path.join(root, ".claude", "terminals", "T0", "CLAUDE.md")
    if os.path.isfile(role_file):
        print("{:<22} {:<6} {:<9} {:<9} {:<55}".format(
            os.path.basename(root), "T0", "n/a", "(not running)", root))
        static_proc = subprocess.run(
            ["bash", audit_self, "--static", root],
            capture_output=True, text=True,
        )
        if static_proc.returncode != 0 and static_proc.stdout.strip():
            print("  [static] role<->skill invocability drift:")
            for line in static_proc.stdout.strip().splitlines():
                print(f"    {line}")
PYEOF
  fi
}

main "$@"
