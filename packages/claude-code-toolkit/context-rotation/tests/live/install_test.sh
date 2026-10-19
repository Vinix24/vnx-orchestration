#!/bin/sh
# Install test of the plugin from an exported marketplace, run by hand.
#
#   sh install_test.sh --commit <sha> --tag context-rotation-v<version> [--source <repo>]
#
# <sha> is a commit that is merged into main of the source repository (the export refuses
# any other) and <repo> is that repository (default: the repository this file is in). The
# export script is <repo>/scripts/maintenance/export_toolkit_plugin.sh.
#
# It builds a throwaway toolkit clone (git init and one commit), exports the plugin into
# it, then in a second throwaway git repo adds that clone as a marketplace, installs
# context-rotation@claude-code-toolkit with --scope project and checks that the plugin is
# listed for that project and for no other scope. Only `claude plugin` subcommands run: no
# session starts. CLAUDE_CONFIG_DIR is never set, so your own login is used as it is. On
# exit, also on failure, it removes what this run added and nothing else: the plugin only
# after its install exited 0, the marketplace only after its add exited 0 and a fresh
# listing shows it pointing at this run's own throwaway clone.
#
# A marketplace named claude-code-toolkit that already exists makes the test refuse before
# it changes anything, because removing the test's copy would remove yours. A listing that
# fails or cannot be read stops the test just the same.
#
# Exit status: 0 passed, 1 failed, 2 usage.
set -u

MARKETPLACE="claude-code-toolkit"
PLUGIN="context-rotation"
PLUGIN_ID="$PLUGIN@$MARKETPLACE"
TAG_PREFIX="context-rotation-v"

commit=""
tag=""
source_repo=""
work=""
project=""
added=0
installed=0

usage() {
    echo "usage: $0 --commit <sha> --tag ${TAG_PREFIX}<version> [--source <repo>]" >&2
    exit 2
}

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

cleanup() {
    status=$?
    trap '' EXIT HUP INT TERM
    if [ "$installed" -eq 1 ] && [ -n "$project" ]; then
        (cd "$project" && claude plugin uninstall "$PLUGIN_ID" --scope project >/dev/null 2>&1) \
            || echo "warning: could not uninstall $PLUGIN_ID, run: claude plugin uninstall $PLUGIN_ID --scope project (in $project)" >&2
    fi
    if [ "$added" -eq 1 ]; then
        claude plugin marketplace remove "$MARKETPLACE" >/dev/null 2>&1 \
            || echo "warning: could not remove the marketplace, run: claude plugin marketplace remove $MARKETPLACE" >&2
    fi
    if [ -n "$work" ] && [ -d "$work" ]; then
        rm -rf "$work"
    fi
    exit "$status"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --commit) [ $# -ge 2 ] || usage; commit=$2; shift 2 ;;
        --tag) [ $# -ge 2 ] || usage; tag=$2; shift 2 ;;
        --source) [ $# -ge 2 ] || usage; source_repo=$2; shift 2 ;;
        *) usage ;;
    esac
done
[ -n "$commit" ] && [ -n "$tag" ] || usage
case "$tag" in
    "$TAG_PREFIX"?*) ;;
    *) usage ;;
esac
version=${tag#"$TAG_PREFIX"}

if [ -z "$source_repo" ]; then
    here=$(cd "$(dirname "$0")" && pwd) || fail "cannot resolve the location of this script"
    source_repo=$(git -C "$here" rev-parse --show-toplevel) || fail "cannot find the source repository; pass --source"
fi
export_script="$source_repo/scripts/maintenance/export_toolkit_plugin.sh"
[ -f "$export_script" ] || fail "no export script at $export_script (is the export merged into $source_repo?)"
command -v claude >/dev/null 2>&1 || fail "claude is not on PATH"
command -v git >/dev/null 2>&1 || fail "git is not on PATH"
command -v python3 >/dev/null 2>&1 || fail "python3 is not on PATH"

listing=$(claude plugin marketplace list 2>/dev/null) \
    || fail "claude plugin marketplace list failed, so it is not known whether $MARKETPLACE is already configured; nothing was changed"
if printf '%s\n' "$listing" | grep -q "$MARKETPLACE"; then
    fail "a marketplace named $MARKETPLACE is already configured; remove it first (claude plugin marketplace remove $MARKETPLACE)"
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/context-rotation-install.XXXXXX") || fail "cannot make a temporary directory"
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
work=$(cd "$work" && pwd -P)
target="$work/toolkit"
mail_host="example.invalid"
project="$work/project"

echo "1. throwaway toolkit clone"
mkdir "$target" "$project" || fail "cannot make the target directories"
git -C "$target" init -q || fail "git init of the target failed"
git -C "$target" -c user.name=install-test -c "user.email=install-test@$mail_host" \
    commit -q --allow-empty -m "initial commit" || fail "initial commit of the target failed"

echo "2. export $commit as $tag"
sh "$export_script" --commit "$commit" --target "$target" --tag "$tag" --source "$source_repo" \
    || fail "the export failed"
branch="export/$PLUGIN-$version"
git -C "$target" checkout -q "$branch" || fail "cannot check out $branch in the target"
[ -f "$target/.claude-plugin/marketplace.json" ] || fail "the export left no .claude-plugin/marketplace.json"
[ -f "$target/$PLUGIN/.claude-plugin/plugin.json" ] || fail "the export left no $PLUGIN/.claude-plugin/plugin.json"

echo "3. second throwaway repo, marketplace add"
git -C "$project" init -q || fail "git init of the project failed"
git -C "$project" -c user.name=install-test -c "user.email=install-test@$mail_host" \
    commit -q --allow-empty -m "initial commit" || fail "initial commit of the project failed"
cd "$project" || fail "cannot enter the project"
claude plugin marketplace add "$target" || fail "claude plugin marketplace add failed; nothing of this run is left to remove"
listing=$(claude plugin marketplace list 2>/dev/null) \
    || fail "claude plugin marketplace list failed after the add; the marketplace may be registered, check: claude plugin marketplace list"
if printf '%s\n' "$listing" | grep -A3 "$MARKETPLACE" | grep -qF "$target"; then
    added=1
else
    fail "$MARKETPLACE is listed but does not point at $target; it is not this run's, so it is left alone"
fi

echo "4. install with --scope project"
claude plugin install "$PLUGIN_ID" --scope project || fail "claude plugin install failed"
installed=1

echo "5. the plugin is listed for this project only"
claude plugin list --json > "$work/list.json" || fail "claude plugin list --json failed"
python3 -I - "$work/list.json" "$PLUGIN_ID" "$project" <<'PY' || fail "the plugin is not listed for this project only (list output: $work/list.json)"
import json, os, sys

path, plugin_id, project = sys.argv[1], sys.argv[2], os.path.realpath(sys.argv[3])
data = json.load(open(path, encoding="utf-8"))
entries = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), [])
mine = [e for e in entries if isinstance(e, dict) and plugin_id in (e.get("id"), e.get("name"), e.get("plugin"))]
scopes = sorted({str(e.get("scope")) for e in mine})
print("listed entries: %d, scopes: %s" % (len(mine), ", ".join(scopes) or "none"))
if scopes != ["project"]:
    sys.exit(1)
paths = [e.get("projectPath") for e in mine if e.get("projectPath")]
if paths and any(os.path.realpath(p) != project for p in paths):
    sys.exit(1)
PY

echo "PASS: $PLUGIN_ID installed for the project at $project only"
