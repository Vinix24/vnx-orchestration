#!/bin/sh
# Export one plugin of packages/claude-code-toolkit (context-rotation, or
# context-meter with --plugin context-meter) from this repository to a local
# clone of the public toolkit repo, on a branch, as one reviewable commit.
#
# Usage:
#   export_toolkit_plugin.sh --commit <sha> --target <toolkit-clone> --tag <plugin>-v<version> [--plugin <plugin>] [--source <repo>]
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
# The export never checks out, writes into, resets or cleans the clone's
# working tree or index. It builds its commit in a worktree of its own, in a
# temp dir outside the clone, on the export branch's base commit, with the
# exact bytes and modes of the source commit and none of the clone's hooks.
# The commit is written without a ref. The one ref it writes in the clone is
# the export branch itself, never a branch a symbolic ref would send it to:
# last, after the leak scan of the committed blobs passed, as a compare-and-swap
# on the value it read at the start. The branch is the run's own only once
# that swap succeeded. What it does and refuses is described in the plugin
# README, section "Maintainers". It never pushes, never tags and never touches
# the network: the last thing it prints is the commands the operator runs.
set -eu

# git reads these before -C; inherited from a hook or an alias, they would
# point every command below at another repository or at the operator's index.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY

PLUGINS="context-rotation context-meter"
PLUGIN="context-rotation"
OWNED_SRC="packages/claude-code-toolkit/$PLUGIN"
OWNED_DST="$PLUGIN"
MARKET_REL=".claude-plugin/marketplace.json"
README_REL="README.md"
TAG_PREFIX="$PLUGIN-v"

STATE="check"
TMP=""
WT=""
NEW=""
KEEP_TMP=0

die() {
    echo "export_toolkit_plugin: $*" >&2
    exit 1
}

usage() {
    echo "usage: $0 --commit <sha> --target <toolkit-clone> --tag <plugin>-v<version> [--plugin <$(echo "$PLUGINS" | tr ' ' '|')>] [--source <repo>]" >&2
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

# merge_marketplace <marketplace.json> <plugin.json> <shown as>: add or update
# only the entry of the plugin being exported. Every other byte of an existing file stays;
# a missing file is created. Prints created, updated or unchanged.
merge_marketplace() {
    python3 -I - "$1" "$2" "$3" <<'PY'
import json, os, sys

market_path, plugin_path, shown = sys.argv[1], sys.argv[2], sys.argv[3]
meta = json.load(open(plugin_path, encoding="utf-8"))
entry = {
    "name": meta["name"],
    "source": "./" + meta["name"],
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
        json.loads(old)
        new = merged(old)
        json.loads(new)
    except (ValueError, IndexError) as exc:
        sys.exit("cannot merge into %s: %s" % (shown, exc))
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

# add_readme_row <README.md> <plugin.json>: one row for the plugin's folder in the
# "## Tools" table, once. Prints created, updated or unchanged.
add_readme_row() {
    python3 -I - "$1" "$2" <<'PY'
import json, os, sys

readme_path, plugin_path = sys.argv[1], sys.argv[2]
meta = json.load(open(plugin_path, encoding="utf-8"))
folder = meta["name"] + "/"
description = meta["description"].replace("|", "\\|")
HEADER = ["| Tool | What it does |", "|---|---|"]
existed = os.path.exists(readme_path)
text = open(readme_path, encoding="utf-8").read() if existed else "# claude-code-toolkit\n"
lines = text.split("\n")


def cells(line):
    """The cells of a table row; a backslash escapes the character after it, so
    an escaped pipe stays inside its cell."""
    found, cell, i, body = [], "", 0, line.strip()
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            cell += body[i:i + 2]
            i += 2
            continue
        if body[i] == "|":
            found.append(cell)
            cell = ""
        else:
            cell += body[i]
        i += 1
    found.append(cell)
    if found and found[0] == "":
        found = found[1:]
    if found and found[-1] == "":
        found = found[:-1]
    return found


def build_row(template):
    link = template is not None and "](" in cells(template)[0]
    first = "[`%s`](%s)" % (folder, folder) if link else "`%s`" % folder
    width = len(cells(template)) if template is not None else 2
    values = [first, description][:width] + [""] * max(0, width - 2)
    return "| " + " | ".join(values) + " |"


heading = next((n for n, line in enumerate(lines) if line.strip() == "## Tools"), None)
if heading is None:
    body = text if text.endswith("\n") else text + "\n"
    new = body + "\n## Tools\n\n" + "\n".join(HEADER + [build_row(None)]) + "\n"
else:
    end = next((n for n in range(heading + 1, len(lines)) if lines[n].startswith("## ")), len(lines))
    section = range(heading + 1, end)
    if any(lines[n].lstrip().startswith("|") and folder in lines[n] for n in section):
        print("unchanged")
        sys.exit(0)
    first = next((n for n in section if lines[n].lstrip().startswith("|")), None)
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
# file of the exported folder plus the two toolkit-level files. A hit on a line
# of a toolkit-level file that the base already had is marked pre-existing: the
# export did not bring it, but it still blocks a publish. The test module is
# imported, not copied; its conftest is replaced by a stub that only names the
# package folder, so pytest is not needed.
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

# materialize <repo> <rev> <tree path> <dest> [<manifest>]: every file under the
# tree path at rev, written below dest with its mode (755 for 100755, else
# 644). The bytes come from git cat-file: no checkout, no filter, no export
# attribute changes them. The manifest lists each file as "<mode> <name below
# dest>", NUL-terminated. A link or a submodule refuses.
materialize() {
    python3 -I - "$@" <<'PY'
import os, subprocess, sys

repo, rev, top, dest = sys.argv[1:5]
manifest = sys.argv[5] if len(sys.argv) > 5 else None


def run(*args, data=None):
    done = subprocess.run(["git", "-C", repo, *args], input=data, capture_output=True)
    if done.returncode:
        sys.exit("git %s failed: %s" % (args[0], done.stderr.decode("utf-8", "replace").strip()))
    return done.stdout


prefix = os.fsencode(top) + b"/"
entries = []
for record in filter(None, run("ls-tree", "-r", "-z", "--full-tree", rev, "--", top).split(b"\0")):
    meta, name = record.split(b"\t", 1)
    mode, _, oid = meta.split(b" ")
    if mode not in (b"100644", b"100755") or not name.startswith(prefix):
        sys.exit("%s at %s is not a regular file below %s" % (os.fsdecode(name), rev, top))
    entries.append((mode, oid, name[len(prefix):]))
blobs = run("cat-file", "--batch", data=b"".join(oid + b"\n" for _, oid, _ in entries))
pos, records = 0, []
for mode, oid, rel in entries:
    header_end = blobs.index(b"\n", pos)
    size = int(blobs[pos:header_end].split(b" ")[2])
    path = os.path.join(os.fsencode(dest), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(blobs[header_end + 1:header_end + 1 + size])
    os.chmod(path, 0o755 if mode == b"100755" else 0o644)
    pos = header_end + 1 + size + 1
    records.append(mode + b" " + rel + b"\0")
if manifest:
    with open(manifest, "wb") as handle:
        handle.write(b"".join(records))
PY
}

# stage <worktree> <manifest> <folder> [<file> <mode>]...: put exactly these
# bytes and modes in the worktree's index: every manifest entry under <folder>
# with the mode the manifest names, and each toolkit-level file with the mode
# given. hash-object --no-filters, so no filter or attribute of the toolkit
# changes a byte between the export and the commit, and no core.fileMode
# setting changes a mode.
stage() {
    python3 -I - "$@" <<'PY'
import os, subprocess, sys

wt, manifest, folder = sys.argv[1:4]
rest = sys.argv[4:]
entries = []
for record in filter(None, open(manifest, "rb").read().split(b"\0")):
    mode, rel = record.split(b" ", 1)
    entries.append((mode, os.fsencode(folder) + b"/" + rel))
entries += [(os.fsencode(rest[i + 1]), os.fsencode(rest[i])) for i in range(0, len(rest), 2)]


def run(args, data=None):
    done = subprocess.run(["git", "-C", wt, "-c", "core.hooksPath=/dev/null", *args], input=data,
                          capture_output=True)
    if done.returncode:
        sys.exit("git %s failed: %s" % (args[0], done.stderr.decode("utf-8", "replace").strip()))
    return done.stdout


ids = run(["hash-object", "-w", "--no-filters", "--", *[name for _, name in entries]]).split()
run(["update-index", "-z", "--index-info"],
    b"".join(b"%s blob %s\t%s\0" % (mode, oid, name) for (mode, name), oid in zip(entries, ids)))
PY
}

# branch_users <branch>: every worktree of the target's repository that has the
# branch checked out, or is rebasing or bisecting it. git counts all three as
# the branch being in use, and moving it under any of them pulls the ground
# from under that worktree. A symbolic ref that ends at the branch counts as
# the branch: git lists a checkout under the branch a symbolic ref ends at, but
# records a rebase under the name it was started on. Prints nothing when the
# branch is free.
branch_users() {
    python3 -I - "$TARGET" "$1" <<'PY'
import os, subprocess, sys

target, branch = sys.argv[1], sys.argv[2]
ref = "refs/heads/" + branch


def git(*args):
    return subprocess.run(["git", "-C", target, *args], capture_output=True, text=True, check=True).stdout


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return None


def resolved(name):
    """The ref a recorded branch name ends at, past any symbolic ref."""
    if not name:
        return name
    full = name if name.startswith("refs/") else "refs/heads/" + name
    done = subprocess.run(["git", "-C", target, "symbolic-ref", "-q", full], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else full


users, where = [], None
paths = []
for line in git("worktree", "list", "--porcelain").splitlines():
    if line.startswith("worktree "):
        where = line[len("worktree "):]
        paths.append(where)
    elif line == "branch " + ref:
        users.append("  checked out in %s" % where)
common = os.path.join(target, git("rev-parse", "--git-common-dir").strip())
admin = [(common, paths[0] if paths else target)]
linked = os.path.join(common, "worktrees")
for name in sorted(os.listdir(linked)) if os.path.isdir(linked) else []:
    gitdir = os.path.join(linked, name)
    dotgit = read(os.path.join(gitdir, "gitdir")) or gitdir
    admin.append((gitdir, os.path.dirname(os.path.join(gitdir, dotgit))))
for gitdir, shown in admin:
    if ref in (resolved(read(os.path.join(gitdir, "rebase-merge", "head-name"))),
               resolved(read(os.path.join(gitdir, "rebase-apply", "head-name")))):
        users.append("  being rebased in %s" % os.path.normpath(shown))
    if resolved(read(os.path.join(gitdir, "BISECT_START"))) == ref:
        users.append("  being bisected in %s" % os.path.normpath(shown))
print("\n".join(users))
PY
}

# ---------------------------------------------------------------------------
# the target: read through tgit, built in the export's own worktree through
# wgit; neither runs a hook of the target
# ---------------------------------------------------------------------------

tgit() { git -C "$TARGET" -c core.hooksPath=/dev/null "$@"; }
wgit() { git -C "$WT" -c core.hooksPath=/dev/null "$@"; }

# symref_target <repo> <ref>: the ref a symbolic ref ends at; nothing for a
# plain ref or a missing one. Any other answer of git fails, so a ref that
# cannot be read is never taken for a plain one.
symref_target() {
    git -C "$1" symbolic-ref -q "$2" && return 0
    [ $? = 1 ]
}

# branch_tip: the commit the export branch itself holds. Nothing when it is
# missing, or a symbolic ref, which rev-parse would follow.
branch_tip() {
    [ -z "$(symref_target "$TARGET" "refs/heads/$BRANCH" || true)" ] || return 0
    tgit rev-parse -q --verify "refs/heads/$BRANCH" || true
}

# branch_now: what the export branch is now, in words, read the same way.
branch_now() {
    now_to=$(symref_target "$TARGET" "refs/heads/$BRANCH" || true)
    if [ -n "$now_to" ]; then
        echo "is a symbolic ref to $now_to"
    elif now_tip=$(tgit rev-parse -q --verify "refs/heads/$BRANCH"); then
        echo "points at $now_tip"
    else
        echo "does not exist"
    fi
}

# operations: a merge, rebase, cherry-pick, revert or bisect in progress in the
# clone's checkout. A new export branch is built on the commit that checkout is
# on, and during a rebase, a bisect or a pick sequence that is a commit in
# passing. A stopped rebase or a bisect leaves git status empty, so status
# alone is not enough.
operations() {
    for marker in MERGE_HEAD:merge rebase-merge:rebase rebase-apply:rebase \
        CHERRY_PICK_HEAD:cherry-pick REVERT_HEAD:revert sequencer:cherry-pick-or-revert BISECT_LOG:bisect; do
        path=$(tgit rev-parse --git-path "${marker%%:*}")
        case "$path" in /*) ;; *) path="$TARGET/$path" ;; esac
        if [ -e "$path" ]; then echo "  a ${marker#*:} (${marker%%:*})"; fi
    done
}

# tree_links <rev>: a link or submodule anywhere under the plugin folder or at a
# toolkit-level path of a commit, or a plugin "folder" that is not a folder.
tree_links() {
    {
        tgit ls-tree -r --full-tree "$1" -- "$OWNED_DST"
        tgit ls-tree --full-tree "$1" -- .claude-plugin "$MARKET_REL" "$README_REL"
    } | awk -F '\t' -v dst="$OWNED_DST" '
        { split($1, meta, " ") }
        meta[1] == "120000" || meta[1] == "160000" || $2 == dst { print "  " $2 }'
}

# base_mode <path>: the mode the base gives a toolkit-level file, so the export
# changes its bytes and never its mode; 100644 for a file the base lacks.
base_mode() {
    tgit ls-tree --full-tree "$BASE_SHA" -- "$1" | awk '$1 == "100755" { m = $1 } END { print m ? m : "100644" }'
}

# stray_paths: every path the export's tree changes outside its own three.
stray_paths() {
    tgit diff-tree -r --name-only "$BASE_SHA" "$NEW_TREE" | awk -v d="$OWNED_DST/" -v m="$MARKET_REL" -v r="$README_REL" '
        index($0, d) == 1 || index($0, "\"" d) == 1 || $0 == m || $0 == r { next }
        { print "  " $0 }'
}

# scan <rev>: the leak scan over what rev holds at the three paths, read from
# the commit's own blobs and never from a working tree. Prints the hits.
scan() {
    rm -rf "$TMP/scan" && mkdir -p "$TMP/scan/.claude-plugin" || return 2
    materialize "$TARGET" "$1" "$OWNED_DST" "$TMP/scan/$OWNED_DST" || return 2
    for rel in "$MARKET_REL" "$README_REL"; do
        if tgit cat-file -e "$1:$rel" 2>/dev/null; then
            tgit cat-file blob "$1:$rel" >"$TMP/scan/$rel" || return 2
        fi
    done
    leak_scrub "$TMP/scan/$OWNED_DST" "$BASE_NAME" \
        "$TMP/scan/$MARKET_REL" "$TMP/base-marketplace.json" "$TMP/scan/$README_REL" "$TMP/base-README.md"
}

# ---------------------------------------------------------------------------
# putting the target back
# ---------------------------------------------------------------------------

worktree_registered() {
    [ -n "$WT" ] && tgit worktree list --porcelain | grep -Fqx "worktree $WT"
}

# drop_worktree: remove the export's own worktree, files and registration.
drop_worktree() {
    if worktree_registered; then
        tgit worktree remove --force "$WT" || return 1
    fi
}

# what_is_left: what this run wrote in the target and could not take back;
# prints nothing when the target is as it was found.
what_is_left() {
    if [ -n "$NEW" ] && [ "$(branch_tip)" = "$NEW" ]; then
        echo "  $BRANCH points at $NEW, the export commit this run wrote; it was at ${TIP_BEFORE:-nothing (the branch did not exist)}"
    fi
    if worktree_registered; then
        echo "  the export's own worktree, still registered; remove it with:"
        echo "    git -C \"$TARGET\" worktree remove --force \"$WT\""
    fi
}

# undo_export: take back what this run wrote, and say only what happened: the
# branch swapped back when this run's own swap wrote it and the branch itself
# still holds that commit, then the worktree removed. Like the write, the swap
# back is on the branch itself, never on one a symbolic ref sends it to. Fails,
# listing what is left, when the target does not end up as it was found.
undo_export() {
    if [ -n "$NEW" ] && [ "$(branch_tip)" = "$NEW" ]; then
        if [ "$CREATED" = 1 ]; then
            if tgit update-ref --no-deref -d "refs/heads/$BRANCH" "$NEW"; then
                echo "  deleted $BRANCH, which this run created" >&2
            fi
        elif tgit update-ref --no-deref "refs/heads/$BRANCH" "$TIP_BEFORE" "$NEW"; then
            echo "  moved $BRANCH back to $TIP_BEFORE" >&2
        fi
    fi
    if worktree_registered && drop_worktree; then
        echo "  removed the export's own worktree" >&2
    fi
    # A worktree that stays registered keeps its directory, so the command
    # what_is_left prints still works.
    if worktree_registered; then KEEP_TMP=1; fi
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
    if [ -n "$TMP" ] && [ "$KEEP_TMP" = 0 ]; then rm -rf "$TMP"; fi
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
CHOSEN=""
while [ $# -gt 0 ]; do
    case "$1" in
        --commit) [ $# -ge 2 ] || usage; COMMIT=$2; shift 2 ;;
        --target) [ $# -ge 2 ] || usage; TARGET=$2; shift 2 ;;
        --tag) [ $# -ge 2 ] || usage; TAG=$2; shift 2 ;;
        --source) [ $# -ge 2 ] || usage; SRC=$2; shift 2 ;;
        --plugin) [ $# -ge 2 ] || usage; CHOSEN=$2; shift 2 ;;
        -h | --help) usage ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done
[ -n "$COMMIT" ] && [ -n "$TARGET" ] && [ -n "$TAG" ] || usage
if [ -n "$CHOSEN" ]; then
    case " $PLUGINS " in
        *" $CHOSEN "*) ;;
        *) die "unknown plugin: $CHOSEN (one of: $PLUGINS)" ;;
    esac
    PLUGIN=$CHOSEN
    OWNED_SRC="packages/claude-code-toolkit/$PLUGIN"
    OWNED_DST="$PLUGIN"
    TAG_PREFIX="$PLUGIN-v"
fi

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
# The commit is checked against origin/main itself; a symbolic ref there would
# check it against the branch it points at.
ORIGIN_TO=$(symref_target "$SRC" refs/remotes/origin/main) ||
    die "cannot read refs/remotes/origin/main in the source"
[ -z "$ORIGIN_TO" ] ||
    die "refs/remotes/origin/main is a symbolic ref to $ORIGIN_TO in the source, and the commit is checked against origin/main itself"
git -C "$SRC" rev-parse --verify -q refs/remotes/origin/main >/dev/null ||
    die "the source has no origin/main to check the commit against"
git -C "$SRC" merge-base --is-ancestor "$SHA" refs/remotes/origin/main ||
    die "commit $SHA is not an ancestor of origin/main; export a merged commit"
git -C "$SRC" cat-file -e "$SHA:$OWNED_SRC" 2>/dev/null || die "$OWNED_SRC does not exist at commit $SHA"
SRC_LINKS=$(git -C "$SRC" ls-tree -r --full-tree "$SHA" -- "$OWNED_SRC" | awk -F '\t' '
    { split($1, meta, " ") }
    meta[1] == "120000" || meta[1] == "160000" { print "  " $2 }')
[ -z "$SRC_LINKS" ] || die "the plugin ships regular files only, but at $SHA it has a link or submodule:
$SRC_LINKS"

# ---------------------------------------------------------------------------
# target: a git repo root with a commit, nothing in progress in its checkout
# ---------------------------------------------------------------------------

[ -d "$TARGET" ] || die "target is not a directory: $TARGET"
TARGET=$(CDPATH='' cd -- "$TARGET" && pwd -P)
TOP=$(git -C "$TARGET" rev-parse --show-toplevel 2>/dev/null) || die "target is not a git repo: $TARGET"
[ "$(CDPATH='' cd -- "$TOP" && pwd -P)" = "$TARGET" ] || die "target is not the root of its git repo: $TARGET"
tgit rev-parse --verify -q HEAD >/dev/null || die "target has no commits yet; make an initial commit first"
BUSY=$(operations)
[ -z "$BUSY" ] || die "the target has an operation in progress; finish or abort it first:
$BUSY"

TMP=$(mktemp -d "${TMPDIR:-/tmp}/export-toolkit-plugin.XXXXXX") || die "cannot create a temp dir"
TMP=$(CDPATH='' cd -- "$TMP" && pwd -P)
case "$TMP/" in
    "$TARGET"/*) die "the temp dir $TMP is inside the target, and the export builds outside it; set TMPDIR elsewhere" ;;
esac
WT="$TMP/wt"

# ---------------------------------------------------------------------------
# what the export will be: read from the source commit and the base, merged
# on copies in the temp dir
# ---------------------------------------------------------------------------

PLUGIN_JSON="$TMP/plugin.json"
git -C "$SRC" cat-file blob "$SHA:$OWNED_SRC/.claude-plugin/plugin.json" >"$PLUGIN_JSON" 2>/dev/null ||
    die "the export has no .claude-plugin/plugin.json"
NAME=$(pj_get "$PLUGIN_JSON" name) || die "plugin.json has no name"
PLUGIN_VERSION=$(pj_get "$PLUGIN_JSON" version) || die "plugin.json has no version"
pj_get "$PLUGIN_JSON" description >/dev/null || die "plugin.json has no description"
[ "$NAME" = "$OWNED_DST" ] || die "plugin.json names the plugin '$NAME', expected '$OWNED_DST'"
[ "$PLUGIN_VERSION" = "$VERSION" ] ||
    die "version mismatch: plugin.json says $PLUGIN_VERSION, the tag says $VERSION"

# The base is the commit the export is built on: the existing export branch,
# else the commit the clone's checkout is on. Both are read once, here; the
# branch is later written only if it still holds TIP_BEFORE.
BRANCH="export/$PLUGIN-$VERSION"
git check-ref-format --branch "$BRANCH" >/dev/null 2>&1 || die "not a valid branch name: $BRANCH"
# The branch is read and written as itself. A symbolic ref there would send
# both to the branch it points at: the export built on that branch, and moved
# onto it under the checkout of whoever has it out.
POINTS_AT=$(symref_target "$TARGET" "refs/heads/$BRANCH") || die "cannot read refs/heads/$BRANCH in the target"
[ -z "$POINTS_AT" ] ||
    die "refs/heads/$BRANCH is a symbolic ref to $POINTS_AT, and the export writes only the branch itself; remove the symbolic ref first: git -C \"$TARGET\" symbolic-ref --delete refs/heads/$BRANCH (leaves $POINTS_AT as it is)"
if TIP_BEFORE=$(tgit rev-parse -q --verify "refs/heads/$BRANCH^{commit}"); then
    CREATED=0
    BASE_SHA=$TIP_BEFORE
    BASE_NAME=$BRANCH
else
    CREATED=1
    TIP_BEFORE=""
    BASE_SHA=$(tgit rev-parse --verify HEAD)
    BASE_NAME=$(tgit symbolic-ref -q --short HEAD || tgit rev-parse --short HEAD)
fi
USERS=$(branch_users "$BRANCH") || die "cannot list the worktrees of the target"
[ -z "$USERS" ] || die "$BRANCH is in use, and the export only moves a branch no worktree is on; check out another branch there, or finish the operation, first:
$USERS"
BASE_LINKS=$(tree_links "$BASE_SHA")
[ -z "$BASE_LINKS" ] || die "$BASE_NAME tracks a link or submodule where the export writes, and the export never writes through a link:
$BASE_LINKS"

OLD_VERSION=""
if tgit cat-file blob "$BASE_SHA:$OWNED_DST/.claude-plugin/plugin.json" >"$TMP/base-plugin.json" 2>/dev/null; then
    OLD_VERSION=$(pj_get "$TMP/base-plugin.json" version) || OLD_VERSION=""
fi

# The two toolkit-level files are read from the base and merged on copies, so a
# malformed marketplace file refuses before the export's worktree exists. The
# unmerged copies stay for the leak scan, which names a hit the base already
# had as pre-existing.
for rel in "$MARKET_REL" "$README_REL"; do
    copy="$TMP/$(basename "$rel")"
    if [ "$(tgit cat-file -t "$BASE_SHA:$rel" 2>/dev/null || true)" = blob ]; then
        tgit cat-file blob "$BASE_SHA:$rel" >"$copy" || die "cannot read $rel on $BASE_NAME"
        cp "$copy" "$TMP/base-$(basename "$rel")"
    fi
done
merge_marketplace "$TMP/marketplace.json" "$PLUGIN_JSON" "$MARKET_REL on $BASE_NAME" >"$TMP/market.status" ||
    die "could not merge the marketplace entry"
add_readme_row "$TMP/README.md" "$PLUGIN_JSON" >"$TMP/readme.status" || die "could not add the README row"
ENTRY_VERSION=$(python3 -I -c '
import json, sys
plugins = json.load(open(sys.argv[1], encoding="utf-8"))["plugins"]
print(next(p["version"] for p in plugins if p.get("name") == sys.argv[2]))
' "$TMP/marketplace.json" "$PLUGIN") || die "the marketplace has no $PLUGIN entry after the merge"
[ "$ENTRY_VERSION" = "$VERSION" ] ||
    die "version mismatch: marketplace entry says $ENTRY_VERSION, the tag says $VERSION"

# ---------------------------------------------------------------------------
# build the commit in the export's own worktree
# ---------------------------------------------------------------------------

# --no-checkout: the worktree holds the base's index and only the three paths
# the export writes. Checking out the rest of the toolkit would run its filters
# for nothing and put the base's tracked links on disk.
#
# From here on no command in this shell redirects stderr: a signal's trap runs
# inside the redirection of the command it interrupted (bash 3.2), and the
# undo report would land in that file instead of on the terminal.
STATE="write"
tgit worktree add -q --detach --no-checkout "$WT" "$BASE_SHA" ||
    fail "could not create the export's own worktree (git's message above)"
wgit read-tree "$BASE_SHA" || fail "could not read $BASE_NAME into the export's worktree"
materialize "$SRC" "$SHA" "$OWNED_SRC" "$WT/$OWNED_DST" "$TMP/new.manifest" ||
    fail "could not write $OWNED_DST from $SHA"
mkdir -p "$WT/.claude-plugin"
cp "$TMP/marketplace.json" "$WT/$MARKET_REL" || fail "could not write $MARKET_REL"
cp "$TMP/README.md" "$WT/$README_REL" || fail "could not write $README_REL"
wgit rm -r -q -f --cached --ignore-unmatch -- "$OWNED_DST" || fail "could not clear $OWNED_DST in the export's index"
stage "$WT" "$TMP/new.manifest" "$OWNED_DST" \
    "$MARKET_REL" "$(base_mode "$MARKET_REL")" "$README_REL" "$(base_mode "$README_REL")" ||
    fail "could not stage the export"
NEW_TREE=$(wgit write-tree) || fail "could not write the export's tree"
STRAY=$(stray_paths)
[ -z "$STRAY" ] || fail "the export's tree changes paths it does not own:
$STRAY"

if [ "$NEW_TREE" = "$(tgit rev-parse "$BASE_SHA^{tree}")" ]; then
    if ! HITS=$(scan "$BASE_SHA"); then
        fail "nothing to export, but the leak scan failed on what $BASE_NAME already carries:
$HITS"
    fi
    drop_worktree || fail "could not remove the export's own worktree"
    STATE="done"
    echo "Nothing to export: $OWNED_DST $VERSION from $SHA is already in $TARGET on $BASE_NAME."
    exit 0
fi

# A content change needs a higher version. Content is every path, blob id and
# mode under the folder, so a mode-only change counts.
if [ -n "$OLD_VERSION" ] &&
    [ "$(tgit ls-tree -r --full-tree "$BASE_SHA" -- "$OWNED_DST")" != "$(tgit ls-tree -r --full-tree "$NEW_TREE" -- "$OWNED_DST")" ] &&
    ! version_newer "$OLD_VERSION" "$VERSION"; then
    fail "the content changed but the version did not go up ($BASE_NAME has $OLD_VERSION, this export is $VERSION); bump the version in plugin.json"
fi

{
    echo "feat($PLUGIN): export $VERSION"
    echo
    echo "Source-Commit: $SHA"
    echo "Intended-Tag: $TAG"
} >"$TMP/message"
# commit-tree writes the commit and no ref, not even the worktree's own HEAD:
# a symbolic ref made there during the run has nothing to move.
if [ -n "$(wgit config user.name 2>/dev/null || true)" ] && [ -n "$(wgit config user.email 2>/dev/null || true)" ]; then
    CANDIDATE=$(wgit commit-tree -p "$BASE_SHA" -F "$TMP/message" "$NEW_TREE") ||
        fail "git commit-tree failed in the export's worktree"
else
    CANDIDATE=$(wgit -c user.name="$PLUGIN export" -c user.email="export@localhost" \
        commit-tree -p "$BASE_SHA" -F "$TMP/message" "$NEW_TREE") ||
        fail "git commit-tree failed in the export's worktree"
fi
[ "$(wgit rev-parse "$CANDIDATE^{tree}")" = "$NEW_TREE" ] || fail "the export commit is not the tree that was staged"

if ! HITS=$(scan "$CANDIDATE"); then
    fail "leak scan failed on the export commit:
$HITS"
fi
drop_worktree || fail "could not remove the export's own worktree"

# The one write in the clone: the branch itself, never one a symbolic ref sends
# it to, as a compare-and-swap on the value read at the start (empty: it must
# still not exist). The branch is this run's only once that swap succeeded: a
# lost swap means another writer's commit is there, and the undo leaves it
# alone. A signal during the swap is held until its outcome is known, so the
# undo takes back exactly what this run wrote.
USERS=$(branch_users "$BRANCH") || fail "cannot list the worktrees of the target"
[ -z "$USERS" ] || fail "$BRANCH came into use during the export:
$USERS"
HELD=""
trap 'HELD=130' INT
trap 'HELD=143' TERM
if tgit update-ref --no-deref -m "export_toolkit_plugin: $OWNED_DST $VERSION from $SHA" \
    "refs/heads/$BRANCH" "$CANDIDATE" "$TIP_BEFORE"; then
    NEW=$CANDIDATE
fi
trap 'exit 130' INT
trap 'exit 143' TERM
[ -z "$HELD" ] || exit "$HELD"
[ -n "$NEW" ] ||
    fail "could not write $BRANCH, which another writer changed during the export (git's message above); it now $(branch_now). This run wrote nothing to it and leaves it as it is"
STATE="done"

echo "Exported $OWNED_DST $VERSION from $SHA."
echo "  marketplace entry: $(cat "$TMP/market.status"), README row: $(cat "$TMP/readme.status")"
echo "  branch $BRANCH at $(tgit rev-parse --short "$NEW"), on $BASE_NAME; the clone's checkout was not touched"
echo
echo "Nothing was pushed or tagged. Review, push, merge, then tag:"
echo "  git -C \"$TARGET\" show --stat $BRANCH"
echo "  git -C \"$TARGET\" push -u origin $BRANCH"
echo "  # after the branch is merged into main in the toolkit:"
echo "  git -C \"$TARGET\" checkout main && git -C \"$TARGET\" pull"
echo "  git -C \"$TARGET\" tag $TAG && git -C \"$TARGET\" push origin $TAG"
