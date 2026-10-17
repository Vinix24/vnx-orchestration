#!/bin/sh
# Export the context-rotation plugin from this repository to a local clone of
# the public toolkit repo, on a new branch, as one reviewable commit.
#
# Usage:
#   export_toolkit_plugin.sh --commit <sha> --target <toolkit-clone> --tag context-rotation-v<version> [--source <repo>]
#
# POSIX sh, not bash: nothing here needs arrays or [[ ]], and /bin/sh is dash on
# Debian and Ubuntu and bash 3.2 on macOS, so staying POSIX is what keeps one
# file running on both. The JSON, README and version work is done by python3
# (standard library only), which the plugin itself requires anyway.
#
# What it does and refuses is described in the plugin README, section
# "Maintainers". It never pushes, never tags and never touches the network: the
# last thing it prints is the commands the operator runs.
set -eu

OWNED_SRC="packages/claude-code-toolkit/context-rotation"
OWNED_DST="context-rotation"
MARKET_REL=".claude-plugin/marketplace.json"
README_REL="README.md"
TAG_PREFIX="context-rotation-v"

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

# leak_scrub <exported folder> <extra file>...: the plugin's own patterns from
# its tests/test_isolation.py, applied to every file it exported plus the two
# toolkit-level files. The test module is imported, not copied; its conftest is
# replaced by a stub that only names the package folder, so pytest is not needed.
leak_scrub() {
    python3 -I - "$@" <<'PY'
import pathlib, sys, types

sys.dont_write_bytecode = True
folder = pathlib.Path(sys.argv[1])
extras = [pathlib.Path(p) for p in sys.argv[2:]]
tests = folder / "tests"
if not (tests / "test_isolation.py").is_file():
    print("%s has no tests/test_isolation.py to take the patterns from" % folder)
    sys.exit(2)
stub = types.ModuleType("conftest")
stub.PKG = folder
sys.modules["conftest"] = stub
sys.path.insert(0, str(tests))
import test_isolation as patterns

skip_parts = {"__pycache__", ".pytest_cache"}
files = [p for p in folder.rglob("*") if p.is_file() and not skip_parts & set(p.parts) and p.suffix != ".pyc"]
files += [p for p in extras if p.is_file()]
hits = []
for path in files:
    for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        for label, pattern in patterns.FORBIDDEN.items():
            if pattern.search(line) and not (
                label in patterns.CREDITS_ALLOWED and patterns.is_credits_line(path, line)
            ):
                hits.append("%s:%d %s" % (path.relative_to(folder.parent), number, label))
for hit in hits:
    print(hit)
sys.exit(1 if hits else 0)
PY
}

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
# source commit: named, and merged
# ---------------------------------------------------------------------------

SHA=$(git -C "$SRC" rev-parse --verify -q "$COMMIT^{commit}") || die "unknown commit: $COMMIT"
git -C "$SRC" rev-parse --verify -q refs/remotes/origin/main >/dev/null ||
    die "the source has no origin/main to check the commit against"
git -C "$SRC" merge-base --is-ancestor "$SHA" refs/remotes/origin/main ||
    die "commit $SHA is not an ancestor of origin/main; export a merged commit"

# ---------------------------------------------------------------------------
# target: a git repo root without local changes in what the export writes
# ---------------------------------------------------------------------------

[ -d "$TARGET" ] || die "target is not a directory: $TARGET"
TARGET=$(CDPATH='' cd -- "$TARGET" && pwd -P)
TOP=$(git -C "$TARGET" rev-parse --show-toplevel 2>/dev/null) || die "target is not a git repo: $TARGET"
[ "$(CDPATH='' cd -- "$TOP" && pwd -P)" = "$TARGET" ] || die "target is not the root of its git repo: $TARGET"
tgit() { git -C "$TARGET" "$@"; }
tgit rev-parse --verify -q HEAD >/dev/null || die "target has no commits yet; make an initial commit first"

DIRTY=$(tgit status --porcelain --untracked-files=all -- "$OWNED_DST" "$MARKET_REL" "$README_REL")
[ -z "$DIRTY" ] || die "target has local changes under the paths the export writes; commit or discard them first:
$DIRTY"

# ---------------------------------------------------------------------------
# stage the export outside the target
# ---------------------------------------------------------------------------

TMP=$(mktemp -d "${TMPDIR:-/tmp}/export-toolkit-plugin.XXXXXX") || die "cannot create a temp dir"
trap 'rm -rf "$TMP"' EXIT INT TERM
mkdir "$TMP/new" "$TMP/base"

git -C "$SRC" archive --format=tar -o "$TMP/new.tar" "$SHA" "$OWNED_SRC" 2>/dev/null ||
    die "$OWNED_SRC does not exist at commit $SHA"
tar -xf "$TMP/new.tar" -C "$TMP/new" --strip-components=3
PLUGIN_JSON="$TMP/new/.claude-plugin/plugin.json"
[ -f "$PLUGIN_JSON" ] || die "the export has no .claude-plugin/plugin.json"

NAME=$(pj_get "$PLUGIN_JSON" name) || die "plugin.json has no name"
PLUGIN_VERSION=$(pj_get "$PLUGIN_JSON" version) || die "plugin.json has no version"
pj_get "$PLUGIN_JSON" description >/dev/null || die "plugin.json has no description"
[ "$NAME" = "$OWNED_DST" ] || die "plugin.json names the plugin '$NAME', expected '$OWNED_DST'"
[ "$PLUGIN_VERSION" = "$VERSION" ] ||
    die "version mismatch: plugin.json says $PLUGIN_VERSION, the tag says $VERSION"

BRANCH="export/context-rotation-$VERSION"
git check-ref-format --branch "$BRANCH" >/dev/null 2>&1 || die "not a valid branch name: $BRANCH"
if tgit show-ref --verify -q "refs/heads/$BRANCH"; then
    BRANCH_EXISTS=1
    BASE_REV="refs/heads/$BRANCH"
else
    BRANCH_EXISTS=0
    BASE_REV=HEAD
fi

# What the target already has for the plugin: the version, and whether the
# content differs. A content change needs a higher version.
if tgit cat-file -e "$BASE_REV:$OWNED_DST" 2>/dev/null; then
    tgit archive --format=tar -o "$TMP/base.tar" "$BASE_REV" "$OWNED_DST"
    tar -xf "$TMP/base.tar" -C "$TMP/base" --strip-components=1
fi
if ! diff -r -q "$TMP/base" "$TMP/new" >/dev/null 2>&1; then
    OLD_VERSION=""
    if [ -f "$TMP/base/.claude-plugin/plugin.json" ]; then
        OLD_VERSION=$(pj_get "$TMP/base/.claude-plugin/plugin.json" version) || OLD_VERSION=""
    fi
    if [ -n "$OLD_VERSION" ] && ! version_newer "$OLD_VERSION" "$VERSION"; then
        die "the content changed but the version did not go up (target has $OLD_VERSION, this export is $VERSION); bump the version in plugin.json"
    fi
fi

# The two toolkit-level files are merged on copies first, so a malformed
# marketplace file refuses before anything in the target is touched.
if tgit cat-file -e "HEAD:$MARKET_REL" 2>/dev/null; then cp "$TARGET/$MARKET_REL" "$TMP/marketplace.json"; fi
if tgit cat-file -e "HEAD:$README_REL" 2>/dev/null; then cp "$TARGET/$README_REL" "$TMP/README.md"; fi
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

PREV_REF=$(tgit symbolic-ref -q --short HEAD || tgit rev-parse HEAD)
CREATED=0
if [ "$BRANCH_EXISTS" = 1 ]; then
    [ "$PREV_REF" = "$BRANCH" ] || tgit checkout -q "$BRANCH" || die "cannot check out $BRANCH in the target"
else
    tgit checkout -q -b "$BRANCH" || die "cannot create $BRANCH in the target"
    CREATED=1
fi
TIP_BEFORE=$(tgit rev-parse HEAD)

# Leave the target as it was found: undo the export branch's commit (if any),
# the written files, and a branch this run created.
undo_export() {
    for path in "$OWNED_DST" "$MARKET_REL" "$README_REL"; do
        tgit reset -q HEAD -- "$path" 2>/dev/null || true
        tgit checkout -q -- "$path" 2>/dev/null || true
        tgit clean -fdq -- "$path" 2>/dev/null || true
    done
    tgit reset -q --keep "$TIP_BEFORE" 2>/dev/null || true
    if [ "$CREATED" = 1 ]; then
        tgit checkout -q "$PREV_REF" 2>/dev/null || true
        tgit branch -q -D "$BRANCH" 2>/dev/null || true
    fi
}

# Deletions sync inside context-rotation/ only.
(cd "$TMP/new" && find . -type f | sed 's|^\./||' | LC_ALL=C sort) >"$TMP/new.lst"
tgit ls-files -- "$OWNED_DST" | sed "s|^$OWNED_DST/||" | LC_ALL=C sort >"$TMP/old.lst"
LC_ALL=C comm -23 "$TMP/old.lst" "$TMP/new.lst" | while IFS= read -r gone; do
    rm -f "$TARGET/$OWNED_DST/$gone"
done
if [ -d "$TARGET/$OWNED_DST" ]; then
    find "$TARGET/$OWNED_DST" -depth -type d -empty -exec rmdir {} \;
fi
mkdir -p "$TARGET/$OWNED_DST"
cp -R "$TMP/new/." "$TARGET/$OWNED_DST/"
mkdir -p "$TARGET/.claude-plugin"
cmp -s "$TMP/marketplace.json" "$TARGET/$MARKET_REL" 2>/dev/null || cp "$TMP/marketplace.json" "$TARGET/$MARKET_REL"
cmp -s "$TMP/README.md" "$TARGET/$README_REL" 2>/dev/null || cp "$TMP/README.md" "$TARGET/$README_REL"
rmdir "$TARGET/.claude-plugin" 2>/dev/null || true

tgit add -A -- "$OWNED_DST" "$MARKET_REL" "$README_REL"
if tgit diff --cached --quiet; then
    undo_export
    echo "Nothing to export: $OWNED_DST $VERSION from $SHA is already in $TARGET."
    exit 0
fi

{
    echo "feat(context-rotation): export $VERSION"
    echo
    echo "Source-Commit: $SHA"
    echo "Intended-Tag: $TAG"
} >"$TMP/message"
if [ -n "$(tgit config user.name 2>/dev/null || true)" ] && [ -n "$(tgit config user.email 2>/dev/null || true)" ]; then
    tgit commit -q -F "$TMP/message" || { undo_export; die "git commit failed in the target"; }
else
    tgit -c user.name="context-rotation export" -c user.email="export@localhost" commit -q -F "$TMP/message" ||
        { undo_export; die "git commit failed in the target"; }
fi

# ---------------------------------------------------------------------------
# leak scrub on what was written
# ---------------------------------------------------------------------------

if ! HITS=$(leak_scrub "$TARGET/$OWNED_DST" "$TARGET/$MARKET_REL" "$TARGET/$README_REL"); then
    undo_export
    die "leak scrub failed, the export commit was removed:
$HITS"
fi

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
