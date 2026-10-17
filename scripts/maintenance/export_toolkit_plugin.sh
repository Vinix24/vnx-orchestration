#!/bin/sh
# Export the context-rotation plugin from this repository to a local clone of
# the public toolkit repo, on a new branch, as one reviewable commit.
#
# Usage:
#   export_toolkit_plugin.sh --commit <sha> --target <toolkit-clone> --tag context-rotation-v<version> [--source <repo>]
#
# Exit status: 0 exported, or nothing to export; 1 refused or failed, with the
# target back exactly as it was found; 2 usage; 3 failed and the target could
# not be put back, with what is left printed.
#
# POSIX sh, not bash: nothing here needs arrays or [[ ]], and /bin/sh is dash on
# Debian and Ubuntu and bash 3.2 on macOS, so staying POSIX is what keeps one
# file running on both. The JSON, README and version work is done by python3
# (standard library only), which the plugin itself requires anyway.
#
# The state of the target is decided, not trusted. The run refuses a clone with
# any local change, an operation in progress, an ignored file or a link where it
# writes. Everything it writes is built from the export branch's own base
# commit, never from the working tree; it commits only its own three paths; and
# every exit that is not a successful export puts the clone back on the ref,
# commit and branches it had. What it does and refuses is described in the
# plugin README, section "Maintainers". It never pushes, never tags and never
# touches the network: the last thing it prints is the commands the operator
# runs.
set -eu

OWNED_SRC="packages/claude-code-toolkit/context-rotation"
OWNED_DST="context-rotation"
MARKET_REL=".claude-plugin/marketplace.json"
README_REL="README.md"
TAG_PREFIX="context-rotation-v"

STATE="check"
TMP=""

die() {
    echo "export_toolkit_plugin: $*" >&2
    exit 1
}

usage() {
    echo "usage: $0 --commit <sha> --target <toolkit-clone> --tag ${TAG_PREFIX}<version> [--source <repo>]" >&2
    exit 2
}

# ---------------------------------------------------------------------------
# python3 helpers (stdin carries the program, so arguments are positional)
# ---------------------------------------------------------------------------

# pj_get <plugin.json> <key>: print a non-empty string field, else exit 1.
pj_get() {
    python3 -I - "$1" "$2" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8")).get(sys.argv[2])
if not isinstance(value, str) or not value:
    sys.exit(1)
print(value)
PY
}

# version_newer <old> <new>: exit 0 when new is a higher version than old.
version_newer() {
    python3 -I - "$1" "$2" <<'PY'
import re, sys

def key(text):
    found = re.match(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(n) for n in found.groups()) if found else (0, 0, 0)

sys.exit(0 if key(sys.argv[2]) > key(sys.argv[1]) else 1)
PY
}

# merge_marketplace <marketplace.json> <plugin.json>: add or update only the
# context-rotation entry. Every other byte of an existing file stays; a missing
# file is created. Prints created, updated or unchanged.
merge_marketplace() {
    python3 -I - "$1" "$2" <<'PY'
import json, os, sys

market_path, plugin_path = sys.argv[1], sys.argv[2]
meta = json.load(open(plugin_path, encoding="utf-8"))
entry = {
    "name": meta["name"],
    "source": "./context-rotation",
    "version": meta["version"],
    "description": meta["description"],
}
decoder = json.JSONDecoder()


def skip(text, i):
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def find_plugins(text):
    i = skip(text, 0)
    if text[i] != "{":
        raise ValueError("marketplace.json is not a JSON object")
    i += 1
    while True:
        i = skip(text, i)
        if text[i] == "}":
            raise ValueError("marketplace.json has no plugins list")
        key_start = i
        key, i = decoder.raw_decode(text, i)
        i = skip(text, i)
        i = skip(text, i + 1)
        start = i
        value, i = decoder.raw_decode(text, i)
        if key == "plugins":
            if not isinstance(value, list):
                raise ValueError("plugins is not a list")
            return start, indent_of(text, key_start)
        i = skip(text, i)
        if text[i] == ",":
            i += 1


def elements(text, start):
    i = start + 1
    spans = []
    while True:
        i = skip(text, i)
        if text[i] == "]":
            return spans, i
        begin = i
        value, i = decoder.raw_decode(text, i)
        spans.append((begin, i, value))
        i = skip(text, i)
        if text[i] == ",":
            i += 1


def render(indent):
    lines = json.dumps(entry, indent=2, ensure_ascii=False).split("\n")
    return ("\n" + indent).join(lines)


def indent_of(text, pos):
    line_start = text.rfind("\n", 0, pos) + 1
    prefix = text[line_start:pos]
    return prefix if prefix.strip() == "" else "  "


def merged(text):
    start, key_indent = find_plugins(text)
    spans, close = elements(text, start)
    for begin, end, value in spans:
        if isinstance(value, dict) and value.get("name") == entry["name"]:
            if value == entry:
                return text
            return text[:begin] + render(indent_of(text, begin)) + text[end:]
    if spans:
        indent = indent_of(text, spans[-1][0])
        return text[:spans[-1][1]] + ",\n" + indent + render(indent) + text[spans[-1][1]:]
    inner = key_indent + "  "
    return text[:start] + "[\n" + inner + render(inner) + "\n" + key_indent + "]" + text[close + 1:]


if os.path.exists(market_path):
    old = open(market_path, encoding="utf-8").read()
    try:
        new = merged(old)
    except (ValueError, IndexError) as exc:
        sys.exit("cannot merge into %s: %s" % (market_path, exc))
    json.loads(new)
    status = "unchanged" if new == old else "updated"
else:
    doc = {"name": "claude-code-toolkit", "owner": {"name": "Vinix24"}, "plugins": [entry]}
    new = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    status = "created"
if status != "unchanged":
    tmp = market_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(new)
    os.replace(tmp, market_path)
print(status)
PY
}

# add_readme_row <README.md> <plugin.json>: one row for context-rotation/ in the
# "## Tools" table, once. Prints created, updated or unchanged.
add_readme_row() {
    python3 -I - "$1" "$2" <<'PY'
import json, os, sys

readme_path, plugin_path = sys.argv[1], sys.argv[2]
description = json.load(open(plugin_path, encoding="utf-8"))["description"].replace("|", "\\|")
HEADER = ["| Tool | What it does |", "|---|---|"]
existed = os.path.exists(readme_path)
text = open(readme_path, encoding="utf-8").read() if existed else "# claude-code-toolkit\n"
lines = text.split("\n")

if any(line.lstrip().startswith("|") and "context-rotation/" in line for line in lines):
    print("unchanged")
    sys.exit(0)


def cells(line):
    return [c for c in line.strip().strip("|").split("|")]


def build_row(template):
    link = template is not None and "](" in cells(template)[0]
    first = "[`context-rotation/`](context-rotation/)" if link else "`context-rotation/`"
    width = len(cells(template)) if template is not None else 2
    values = [first, description][:width] + [""] * max(0, width - 2)
    return "| " + " | ".join(values) + " |"


heading = next((n for n, line in enumerate(lines) if line.strip() == "## Tools"), None)
if heading is None:
    body = text if text.endswith("\n") else text + "\n"
    new = body + "\n## Tools\n\n" + "\n".join(HEADER + [build_row(None)]) + "\n"
else:
    end = next((n for n in range(heading + 1, len(lines)) if lines[n].startswith("## ")), len(lines))
    first = next((n for n in range(heading + 1, end) if lines[n].lstrip().startswith("|")), None)
    if first is None:
        lines[heading + 1:heading + 1] = [""] + HEADER + [build_row(None)]
    else:
        last = first
        while last + 1 < end and lines[last + 1].lstrip().startswith("|"):
            last += 1
        template = lines[first + 2] if first + 2 <= last else None
        lines.insert(last + 1, build_row(template))
    new = "\n".join(lines)
tmp = readme_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    handle.write(new)
os.replace(tmp, readme_path)
print("updated" if existed else "created")
PY
}

# leak_scrub <exported folder> <base name> [<file> <that file at the base>]...:
# the plugin's own patterns from its tests/test_isolation.py, applied to every
# file it exported plus the toolkit-level files. A hit on a toolkit-level line
# the base already had is marked pre-existing: the export did not bring it, but
# it still blocks a publish. The test module is imported, not copied; its
# conftest is replaced by a stub that only names the package folder, so pytest
# is not needed.
leak_scrub() {
    python3 -I - "$@" <<'PY'
import pathlib, sys, types

sys.dont_write_bytecode = True
folder, base_name = pathlib.Path(sys.argv[1]), sys.argv[2]
rest = sys.argv[3:]
extras = list(zip((pathlib.Path(p) for p in rest[0::2]), (pathlib.Path(p) for p in rest[1::2])))
tests = folder / "tests"
if not (tests / "test_isolation.py").is_file():
    print("%s has no tests/test_isolation.py to take the patterns from" % folder)
    sys.exit(2)
stub = types.ModuleType("conftest")
stub.PKG = folder
sys.modules["conftest"] = stub
sys.path.insert(0, str(tests))
import test_isolation as patterns


def lines(path):
    if path is None or not path.is_file():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


skip_parts = {"__pycache__", ".pytest_cache"}
files = [(p, None) for p in folder.rglob("*") if p.is_file() and not skip_parts & set(p.parts) and p.suffix != ".pyc"]
files += [(p, before) for p, before in extras if p.is_file()]
hits = []
for path, before in files:
    old = lines(before)
    shown = path.relative_to(folder.parent)
    for number, line in enumerate(lines(path), 1):
        for label, pattern in patterns.FORBIDDEN.items():
            if not pattern.search(line):
                continue
            if label in patterns.CREDITS_ALLOWED and patterns.is_credits_line(path, line):
                continue
            hit = "%s:%d %s" % (shown, number, label)
            if line in old:
                hit += ", pre-existing: %s:%d on %s already had it before this export" % (
                    shown, old.index(line) + 1, base_name)
            hits.append(hit)
for hit in hits:
    print(hit)
sys.exit(1 if hits else 0)
PY
}

# remove_gone <target> <base listing> <new tree>: the listing holds the tracked
# files under context-rotation/ at the base, NUL-separated as git ls-tree -z
# prints them, so no name is quoted or split. Removes each one the new export
# no longer has, then every directory that removal left empty.
remove_gone() {
    python3 -I - "$1" "$2" "$3" "$OWNED_DST" <<'PY'
import os, sys

target, listing, new_root, owned = (os.fsencode(arg) for arg in sys.argv[1:5])
top = os.path.join(target, owned)
with open(listing, "rb") as handle:
    tracked = [name for name in handle.read().split(b"\0") if name]
for name in tracked:
    if os.path.isfile(os.path.join(new_root, os.path.relpath(name, owned))):
        continue
    path = os.path.join(target, name)
    os.remove(path)
    parent = os.path.dirname(path)
    while parent != top and not os.listdir(parent):
        os.rmdir(parent)
        parent = os.path.dirname(parent)
PY
}

# ---------------------------------------------------------------------------
# what the target holds where the export writes
# ---------------------------------------------------------------------------

tgit() { git -C "$TARGET" "$@"; }

# head_ref: the full name of the branch the clone is on, or the commit when
# HEAD is detached.
head_ref() {
    tgit symbolic-ref -q HEAD || tgit rev-parse HEAD
}

# checkout_ref <ref>: check out what head_ref printed, as a branch or detached.
checkout_ref() {
    case "$1" in
        refs/heads/*) tgit checkout -q "${1#refs/heads/}" ;;
        *) tgit checkout -q --detach "$1" ;;
    esac
}

# branch_tips: every local branch with the commit it points at.
branch_tips() {
    tgit for-each-ref --format='%(refname) %(objectname)' refs/heads
}

# operations: a merge, rebase, cherry-pick, revert or bisect in progress. A
# stopped rebase or a bisect leaves git status empty, so status alone is not
# enough.
operations() {
    for marker in MERGE_HEAD:merge rebase-merge:rebase rebase-apply:rebase \
        CHERRY_PICK_HEAD:cherry-pick REVERT_HEAD:revert sequencer:cherry-pick-or-revert BISECT_LOG:bisect; do
        path=$(tgit rev-parse --git-path "${marker%%:*}")
        case "$path" in /*) ;; *) path="$TARGET/$path" ;; esac
        if [ -e "$path" ]; then echo "  a ${marker#*:} (${marker%%:*})"; fi
    done
}

# working_links: every path the export writes, and every directory on the way
# to one, that is a symbolic link in the working tree. A write there lands
# wherever the link points, inside the clone or outside it.
working_links() {
    for rel in "$OWNED_DST" .claude-plugin "$MARKET_REL" "$README_REL"; do
        if [ -L "$TARGET/$rel" ]; then echo "  $rel"; fi
    done
    if [ -d "$TARGET/$OWNED_DST" ] && [ ! -L "$TARGET/$OWNED_DST" ]; then
        (cd "$TARGET" && find "$OWNED_DST" -type l | sed 's/^/  /')
    fi
}

# tree_links <rev>: the same question for a commit: a link or submodule
# anywhere under the plugin folder or at a toolkit-level path, or a plugin
# "folder" that is not a folder.
tree_links() {
    {
        tgit ls-tree -r "$1" -- "$OWNED_DST"
        tgit ls-tree "$1" -- .claude-plugin "$MARKET_REL" "$README_REL"
    } | awk -F '\t' -v dst="$OWNED_DST" '
        { split($1, meta, " ") }
        meta[1] == "120000" || meta[1] == "160000" || $2 == dst { print "  " $2 }'
}

# ignored_here: ignored files where the export writes. The run would overwrite
# or commit them, and a rollback could not tell them from its own files.
ignored_here() {
    tgit status --porcelain --ignored --untracked-files=all -- "$OWNED_DST" "$MARKET_REL" "$README_REL" |
        sed 's/^/  /'
}

# ---------------------------------------------------------------------------
# putting the target back
# ---------------------------------------------------------------------------

# restore_owned: the three owned paths back to HEAD in the index and the
# working tree, and every file the run created there removed. Only those paths
# are touched: the clone was clean when the run started, so nothing in them can
# be anyone else's work.
restore_owned() {
    tgit reset -q -- "$OWNED_DST" "$MARKET_REL" "$README_REL" || return 1
    for rel in "$OWNED_DST" "$MARKET_REL" "$README_REL"; do
        if tgit cat-file -e "HEAD:$rel" 2>/dev/null; then
            tgit checkout -q -- "$rel" || return 1
        fi
    done
    tgit clean -fdqx -- "$OWNED_DST" "$MARKET_REL" "$README_REL"
}

# remove_new_dirs: the two directories the run may have created, removed again
# when they did not exist before it and are empty now.
remove_new_dirs() {
    if [ "$OWNED_DIR_BEFORE" = 0 ] && [ -d "$TARGET/$OWNED_DST" ]; then
        rmdir "$TARGET/$OWNED_DST" 2>/dev/null || return 1
    fi
    if [ "$PLUGIN_DIR_BEFORE" = 0 ] && [ -d "$TARGET/.claude-plugin" ]; then
        rmdir "$TARGET/.claude-plugin" 2>/dev/null || return 1
    fi
}

# what_is_left: every way the target differs from what was recorded before
# the first write; prints nothing when it is back as it was found.
what_is_left() {
    changes=$(tgit status --porcelain --untracked-files=all)
    if [ -n "$changes" ]; then printf '  local changes:\n%s\n' "$changes"; fi
    ignored=$(ignored_here)
    if [ -n "$ignored" ]; then printf '  files under the paths the export writes:\n%s\n' "$ignored"; fi
    now=$(head_ref)
    if [ "$now" != "$PREV_REF" ] || [ "$(tgit rev-parse HEAD)" != "$PREV_SHA" ]; then
        echo "  the clone is on ${now#refs/heads/} at $(tgit rev-parse --short HEAD), not on ${PREV_REF#refs/heads/}"
    fi
    tips=$(branch_tips)
    if [ "$tips" != "$TIPS_BEFORE" ]; then
        printf '  the branches differ. Before the run:\n%s\n  now:\n%s\n' "$TIPS_BEFORE" "$tips"
    fi
    if [ "$OWNED_DIR_BEFORE" = 0 ] && [ -e "$TARGET/$OWNED_DST" ]; then
        echo "  the directory $OWNED_DST/, which the run created"
    fi
    if [ "$PLUGIN_DIR_BEFORE" = 0 ] && [ -e "$TARGET/.claude-plugin" ]; then
        echo "  the directory .claude-plugin/, which the run created"
    fi
}

# undo_export: put the clone back exactly as the run found it, and say only
# what happened. Each step that took effect prints one line. Fails, listing
# what is left, when the target does not end up as it was found.
undo_export() {
    if [ "$(head_ref)" = "refs/heads/$BRANCH" ] && [ "$(tgit rev-parse HEAD)" != "$TIP_BEFORE" ]; then
        if tgit reset -q --soft "$TIP_BEFORE"; then echo "  removed the export commit from $BRANCH" >&2; fi
    fi
    if [ -n "$(tgit status --porcelain --ignored --untracked-files=all -- "$OWNED_DST" "$MARKET_REL" "$README_REL")" ]; then
        if restore_owned; then echo "  undid the writes to $OWNED_DST/, $MARKET_REL and $README_REL" >&2; fi
    fi
    if [ "$(head_ref)" != "$PREV_REF" ]; then
        if checkout_ref "$PREV_REF"; then echo "  checked out ${PREV_REF#refs/heads/} again" >&2; fi
    fi
    if [ "$CREATED" = 1 ] && tgit show-ref --verify -q "refs/heads/$BRANCH"; then
        if tgit branch -q -D "$BRANCH"; then echo "  deleted $BRANCH, which this run created" >&2; fi
    fi
    remove_new_dirs || true
    left=$(what_is_left)
    [ -n "$left" ] || return 0
    printf 'export_toolkit_plugin: the cleanup did not complete; still in %s:\n%s\n' "$TARGET" "$left" >&2
    return 1
}

# fail <message>: stop after the first write: report, undo, and exit 1 when
# the target is back as it was found, 3 when it is not.
fail() {
    STATE="undo"
    echo "export_toolkit_plugin: $*" >&2
    if undo_export; then
        echo "The target is back as it was found." >&2
        exit 1
    fi
    exit 3
}

# A stop that no check foresaw (set -e, a signal) during the write phase gets
# the same undo as fail.
on_exit() {
    status=$?
    if [ "$STATE" = write ]; then
        STATE="undo"
        echo "export_toolkit_plugin: stopped with exit status $status while writing into the target" >&2
        if undo_export; then
            echo "The target is back as it was found." >&2
            status=1
        else
            status=3
        fi
    fi
    if [ -n "$TMP" ]; then rm -rf "$TMP"; fi
    exit "$status"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ---------------------------------------------------------------------------
# arguments
# ---------------------------------------------------------------------------

COMMIT=""
TARGET=""
TAG=""
SRC=""
while [ $# -gt 0 ]; do
    case "$1" in
        --commit) [ $# -ge 2 ] || usage; COMMIT=$2; shift 2 ;;
        --target) [ $# -ge 2 ] || usage; TARGET=$2; shift 2 ;;
        --tag) [ $# -ge 2 ] || usage; TAG=$2; shift 2 ;;
        --source) [ $# -ge 2 ] || usage; SRC=$2; shift 2 ;;
        -h | --help) usage ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done
[ -n "$COMMIT" ] && [ -n "$TARGET" ] && [ -n "$TAG" ] || usage

command -v git >/dev/null 2>&1 || die "git is not installed"
command -v python3 >/dev/null 2>&1 || die "python3 is not installed"

case "$TAG" in
    "$TAG_PREFIX"*) VERSION=${TAG#"$TAG_PREFIX"} ;;
    *) die "tag must look like ${TAG_PREFIX}<version>, got: $TAG" ;;
esac
printf '%s\n' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.+-]+)?$' ||
    die "tag version is not a semantic version: $VERSION"

if [ -z "$SRC" ]; then
    SRC=$(CDPATH='' cd -- "$(dirname -- "$0")" && git rev-parse --show-toplevel) ||
        die "cannot find the source repository from the script location; pass --source"
fi
git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1 || die "source is not a git repo: $SRC"

# ---------------------------------------------------------------------------
# source commit: named, merged, and regular files only
# ---------------------------------------------------------------------------

SHA=$(git -C "$SRC" rev-parse --verify -q "$COMMIT^{commit}") || die "unknown commit: $COMMIT"
git -C "$SRC" rev-parse --verify -q refs/remotes/origin/main >/dev/null ||
    die "the source has no origin/main to check the commit against"
git -C "$SRC" merge-base --is-ancestor "$SHA" refs/remotes/origin/main ||
    die "commit $SHA is not an ancestor of origin/main; export a merged commit"
SRC_LINKS=$(git -C "$SRC" ls-tree -r "$SHA" -- "$OWNED_SRC" | awk -F '\t' '
    { split($1, meta, " ") }
    meta[1] == "120000" || meta[1] == "160000" { print "  " $2 }')
[ -z "$SRC_LINKS" ] || die "the plugin ships regular files only, but at $SHA it has a link or submodule:
$SRC_LINKS"

# ---------------------------------------------------------------------------
# target: a clean git repo root, nothing in progress, no link or ignored file
# where the export writes
# ---------------------------------------------------------------------------

[ -d "$TARGET" ] || die "target is not a directory: $TARGET"
TARGET=$(CDPATH='' cd -- "$TARGET" && pwd -P)
TOP=$(git -C "$TARGET" rev-parse --show-toplevel 2>/dev/null) || die "target is not a git repo: $TARGET"
[ "$(CDPATH='' cd -- "$TOP" && pwd -P)" = "$TARGET" ] || die "target is not the root of its git repo: $TARGET"
tgit rev-parse --verify -q HEAD >/dev/null || die "target has no commits yet; make an initial commit first"

DIRTY=$(tgit status --porcelain --untracked-files=all)
[ -z "$DIRTY" ] || die "the target has local changes; the export only writes into a clean clone. Commit, stash or discard them first:
$DIRTY"
BUSY=$(operations)
[ -z "$BUSY" ] || die "the target has an operation in progress; finish or abort it first:
$BUSY"
LINKS=$(working_links)
[ -z "$LINKS" ] || die "the target has a link where the export writes, and the export never writes through a link:
$LINKS"
IGNORED=$(ignored_here)
[ -z "$IGNORED" ] || die "the target has ignored files where the export writes; remove them first:
$IGNORED"

# What every exit that is not a successful export must leave as it is.
PREV_REF=$(head_ref)
PREV_SHA=$(tgit rev-parse HEAD)
TIPS_BEFORE=$(branch_tips)
if [ -e "$TARGET/$OWNED_DST" ]; then OWNED_DIR_BEFORE=1; else OWNED_DIR_BEFORE=0; fi
if [ -e "$TARGET/.claude-plugin" ]; then PLUGIN_DIR_BEFORE=1; else PLUGIN_DIR_BEFORE=0; fi

# ---------------------------------------------------------------------------
# stage the export outside the target
# ---------------------------------------------------------------------------

TMP=$(mktemp -d "${TMPDIR:-/tmp}/export-toolkit-plugin.XXXXXX") || die "cannot create a temp dir"
mkdir "$TMP/new" "$TMP/base"

git -C "$SRC" archive --format=tar -o "$TMP/new.tar" "$SHA" "$OWNED_SRC" 2>/dev/null ||
    die "$OWNED_SRC does not exist at commit $SHA"
tar -xf "$TMP/new.tar" -C "$TMP/new" --strip-components=3 || die "cannot unpack the export"
PLUGIN_JSON="$TMP/new/.claude-plugin/plugin.json"
[ -f "$PLUGIN_JSON" ] || die "the export has no .claude-plugin/plugin.json"

NAME=$(pj_get "$PLUGIN_JSON" name) || die "plugin.json has no name"
PLUGIN_VERSION=$(pj_get "$PLUGIN_JSON" version) || die "plugin.json has no version"
pj_get "$PLUGIN_JSON" description >/dev/null || die "plugin.json has no description"
[ "$NAME" = "$OWNED_DST" ] || die "plugin.json names the plugin '$NAME', expected '$OWNED_DST'"
[ "$PLUGIN_VERSION" = "$VERSION" ] ||
    die "version mismatch: plugin.json says $PLUGIN_VERSION, the tag says $VERSION"

# BASE_REV is the commit the export branch is built on: the existing export
# branch, else the commit the clone is on. Everything the export writes is
# derived from it, never from the working tree.
BRANCH="export/context-rotation-$VERSION"
git check-ref-format --branch "$BRANCH" >/dev/null 2>&1 || die "not a valid branch name: $BRANCH"
if tgit show-ref --verify -q "refs/heads/$BRANCH"; then
    BRANCH_EXISTS=1
    BASE_REV="refs/heads/$BRANCH"
    BASE_NAME=$BRANCH
else
    BRANCH_EXISTS=0
    BASE_REV=HEAD
    BASE_NAME=${PREV_REF#refs/heads/}
fi
BASE_LINKS=$(tree_links "$BASE_REV")
[ -z "$BASE_LINKS" ] || die "$BASE_NAME tracks a link or submodule where the export writes, and the export never writes through a link:
$BASE_LINKS"

# What the base already has for the plugin: the version, and whether the
# content differs. A content change needs a higher version.
if tgit cat-file -e "$BASE_REV:$OWNED_DST" 2>/dev/null; then
    tgit archive --format=tar -o "$TMP/base.tar" "$BASE_REV" "$OWNED_DST"
    tar -xf "$TMP/base.tar" -C "$TMP/base" --strip-components=1 || die "cannot unpack $OWNED_DST from $BASE_NAME"
fi
if ! diff -r -q "$TMP/base" "$TMP/new" >/dev/null 2>&1; then
    OLD_VERSION=""
    if [ -f "$TMP/base/.claude-plugin/plugin.json" ]; then
        OLD_VERSION=$(pj_get "$TMP/base/.claude-plugin/plugin.json" version) || OLD_VERSION=""
    fi
    if [ -n "$OLD_VERSION" ] && ! version_newer "$OLD_VERSION" "$VERSION"; then
        die "the content changed but the version did not go up ($BASE_NAME has $OLD_VERSION, this export is $VERSION); bump the version in plugin.json"
    fi
fi

# The two toolkit-level files are read from the base with git cat-file and
# merged on copies, so a malformed marketplace file refuses before anything in
# the target is touched. The unmerged copies stay for the leak scrub, which
# names a hit the base already had as pre-existing.
if tgit cat-file -e "$BASE_REV:$MARKET_REL" 2>/dev/null; then
    tgit cat-file blob "$BASE_REV:$MARKET_REL" >"$TMP/base-marketplace.json"
    cp "$TMP/base-marketplace.json" "$TMP/marketplace.json"
fi
if tgit cat-file -e "$BASE_REV:$README_REL" 2>/dev/null; then
    tgit cat-file blob "$BASE_REV:$README_REL" >"$TMP/base-README.md"
    cp "$TMP/base-README.md" "$TMP/README.md"
fi
merge_marketplace "$TMP/marketplace.json" "$PLUGIN_JSON" >"$TMP/market.status" ||
    die "could not merge the marketplace entry"
add_readme_row "$TMP/README.md" "$PLUGIN_JSON" >"$TMP/readme.status" || die "could not add the README row"
ENTRY_VERSION=$(python3 -I -c '
import json, sys
plugins = json.load(open(sys.argv[1], encoding="utf-8"))["plugins"]
print(next(p["version"] for p in plugins if p.get("name") == "context-rotation"))
' "$TMP/marketplace.json") || die "the marketplace has no context-rotation entry after the merge"
[ "$ENTRY_VERSION" = "$VERSION" ] ||
    die "version mismatch: marketplace entry says $ENTRY_VERSION, the tag says $VERSION"

# ---------------------------------------------------------------------------
# write into the target on the export branch
# ---------------------------------------------------------------------------

if [ "$BRANCH_EXISTS" = 1 ]; then
    CREATED=0
    TIP_BEFORE=$(tgit rev-parse "refs/heads/$BRANCH")
else
    CREATED=1
    TIP_BEFORE=$PREV_SHA
fi
STATE="write"
if [ "$CREATED" = 1 ]; then
    tgit checkout -q -b "$BRANCH" || fail "cannot create $BRANCH in the target"
elif [ "$PREV_REF" != "refs/heads/$BRANCH" ]; then
    tgit checkout -q "$BRANCH" || fail "cannot check out $BRANCH in the target"
fi

# Again right before the copy, now that the export branch is checked out.
LINKS=$(working_links)
[ -z "$LINKS" ] || fail "the target has a link where the export writes, and the export never writes through a link:
$LINKS"

tgit ls-tree -r -z --name-only "$BASE_REV" -- "$OWNED_DST" >"$TMP/base.lst" ||
    fail "cannot list $OWNED_DST on $BASE_NAME"
remove_gone "$TARGET" "$TMP/base.lst" "$TMP/new" || fail "could not remove the files the source no longer has"
mkdir -p "$TARGET/$OWNED_DST" "$TARGET/.claude-plugin"
cp -R "$TMP/new/." "$TARGET/$OWNED_DST/" || fail "could not copy $OWNED_DST into the target"
cp "$TMP/marketplace.json" "$TARGET/$MARKET_REL" || fail "could not write $MARKET_REL"
cp "$TMP/README.md" "$TARGET/$README_REL" || fail "could not write $README_REL"

# -f: the export's own files are committed even when the toolkit's ignore rules
# match one of them. No other ignored file can be under these paths; the
# preflight refused that.
tgit add -A -f -- "$OWNED_DST" "$MARKET_REL" "$README_REL" || fail "git add failed in the target"
if tgit diff --cached --quiet -- "$OWNED_DST" "$MARKET_REL" "$README_REL"; then
    STATE="undo"
    if undo_export; then
        echo "Nothing to export: $OWNED_DST $VERSION from $SHA is already in $TARGET on $BASE_NAME."
        exit 0
    fi
    exit 3
fi

{
    echo "feat(context-rotation): export $VERSION"
    echo
    echo "Source-Commit: $SHA"
    echo "Intended-Tag: $TAG"
} >"$TMP/message"
if [ -n "$(tgit config user.name 2>/dev/null || true)" ] && [ -n "$(tgit config user.email 2>/dev/null || true)" ]; then
    tgit commit -q -F "$TMP/message" -- "$OWNED_DST" "$MARKET_REL" "$README_REL" ||
        fail "git commit failed in the target"
else
    tgit -c user.name="context-rotation export" -c user.email="export@localhost" \
        commit -q -F "$TMP/message" -- "$OWNED_DST" "$MARKET_REL" "$README_REL" ||
        fail "git commit failed in the target"
fi

# ---------------------------------------------------------------------------
# leak scrub on what was written
# ---------------------------------------------------------------------------

if ! HITS=$(leak_scrub "$TARGET/$OWNED_DST" "$BASE_NAME" \
    "$TARGET/$MARKET_REL" "$TMP/base-marketplace.json" "$TARGET/$README_REL" "$TMP/base-README.md"); then
    fail "leak scrub failed on what the export wrote:
$HITS"
fi
STATE="done"

echo "Exported $OWNED_DST $VERSION from $SHA."
echo "  marketplace entry: $(cat "$TMP/market.status"), README row: $(cat "$TMP/readme.status")"
echo "  branch $BRANCH, commit $(tgit rev-parse --short HEAD)"
echo
echo "Nothing was pushed or tagged. Review, push, merge, then tag:"
echo "  git -C \"$TARGET\" show --stat HEAD"
echo "  git -C \"$TARGET\" push -u origin $BRANCH"
echo "  # after the branch is merged into main in the toolkit:"
echo "  git -C \"$TARGET\" checkout main && git -C \"$TARGET\" pull"
echo "  git -C \"$TARGET\" tag $TAG && git -C \"$TARGET\" push origin $TAG"
