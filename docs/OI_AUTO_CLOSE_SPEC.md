# OI Auto-Close Specification

**Feature**: F29 — Dashboard Agent Stream (ancillary)
**Version**: 1.0
**Date**: 2026-04-06

## Problem

Open items accumulate indefinitely. File-size and function-size violations (e.g., "file X exceeds 300 lines") remain open long after the underlying code has been refactored, split, or deleted. At the time of writing, 724 stale OIs exist, most of which reference violations that no longer exist in the codebase.

Manual closure is not scalable. T0 cannot review each OI individually against the current codebase state during every digest cycle.

## Solution

Add deterministic auto-close to `open_items_manager.py` that runs during `digest` and can be triggered explicitly via `--rescan`. For each open OI that matches a known rescannable pattern, verify whether the violation still exists. If it does not, auto-close with an evidence-based reason.

## Rescannable OI Categories

### Category 1: File Size Violations

**Pattern match**: OI title matches `file .+ exceeds \d+L` or `file .+ exceeds \d+ lines` (case-insensitive).

**Extraction**: Parse the file path and threshold from the title.

**Verification**:
```bash
wc -l < "<file_path>"
```

**Auto-close condition**: File does not exist OR actual line count <= threshold.

**Close reason format**:
- File deleted: `auto-resolved: file no longer exists`
- Size reduced: `auto-resolved: actual {N}L, threshold {M}L`

### Category 2: Function Size Violations

**Pattern match**: OI title matches `function .+ exceeds \d+L` or `function .+ in .+ exceeds \d+ lines`.

**Extraction**: Parse function name, file path (if present), and threshold.

**Verification**: For Python files:
```python
# Find function definition and count lines until next def/class at same indent or dedent
grep -n "def {function_name}" "{file_path}"
```

Then count lines from `def` to the next function/class definition or end of file, excluding blank lines and comments at the boundary.

For shell files:
```bash
# Find function definition and count lines until closing brace
grep -n "{function_name}()" "{file_path}"
```

**Auto-close condition**: Function does not exist in the file OR actual line count <= threshold.

**Close reason format**:
- Function removed: `auto-resolved: function {name} no longer exists in {file}`
- Size reduced: `auto-resolved: function {name} actual {N}L, threshold {M}L`
- File removed: `auto-resolved: file no longer exists`

### Category 3: Missing File References

**Pattern match**: OI title or details reference a specific file path that can be extracted.

**Verification**: Check if the referenced file exists.

**Auto-close condition**: Only if the OI severity is `info` AND the referenced file no longer exists AND the OI title suggests a file-specific issue (not a systemic concern).

This category is conservative — only `info` severity items auto-close on file deletion. `warn` and `blocker` items referencing deleted files may indicate the fix moved the problem rather than resolving it.

## Implementation

### CLI Interface

```bash
# Explicit rescan (standalone)
python scripts/open_items_manager.py rescan

# Rescan as part of digest
python scripts/open_items_manager.py digest --rescan

# Dry run — show what would be closed without closing
python scripts/open_items_manager.py rescan --dry-run
```

### `rescan` Subcommand

```python
def rescan_items(args):
    """Rescan open items and auto-close resolved violations."""
    data = load_items()
    dry_run = getattr(args, 'dry_run', False)
    closed = []

    for item in data["items"]:
        if item["status"] != "open":
            continue

        result = check_violation(item)
        if result is None:
            continue  # not a rescannable pattern
        if result["resolved"]:
            if not dry_run:
                item["status"] = "done"
                item["closed_reason"] = result["reason"]
                item["closed_at"] = datetime.now().isoformat()
                item["updated_at"] = datetime.now().isoformat()
                item["closed_by"] = "auto-rescan"
            closed.append({
                "id": item["id"],
                "title": item["title"],
                "reason": result["reason"],
            })

    if not dry_run and closed:
        save_items(data)
        for c in closed:
            audit_log_entry(
                "auto_close",
                item_id=c["id"],
                reason=c["reason"],
                source="rescan",
            )

    # Print summary
    print(f"{'[DRY RUN] ' if dry_run else ''}Rescan complete: {len(closed)} items {'would be ' if dry_run else ''}closed")
    for c in closed:
        print(f"  {c['id']}: {c['reason']}")

    if not dry_run:
        generate_digest()
```

### `check_violation` Function

```python
import re
import subprocess

FILE_SIZE_PATTERN = re.compile(
    r'file\s+(.+?)\s+exceeds\s+(\d+)\s*L?(?:ines)?',
    re.IGNORECASE,
)

FUNC_SIZE_PATTERN = re.compile(
    r'function\s+(\S+?)(?:\s+in\s+(.+?))?\s+exceeds\s+(\d+)\s*L?(?:ines)?',
    re.IGNORECASE,
)


def check_violation(item: dict) -> Optional[dict]:
    """Check if an OI's underlying violation still exists.

    Returns None if the item doesn't match a rescannable pattern.
    Returns {"resolved": bool, "reason": str} otherwise.
    """
    title = item.get("title", "")

    # Category 1: file size
    m = FILE_SIZE_PATTERN.search(title)
    if m:
        return _check_file_size(m.group(1).strip(), int(m.group(2)))

    # Category 2: function size
    m = FUNC_SIZE_PATTERN.search(title)
    if m:
        func_name = m.group(1).strip()
        file_path = (m.group(2) or "").strip()
        threshold = int(m.group(3))
        return _check_function_size(func_name, file_path, threshold)

    return None


def _check_file_size(file_path: str, threshold: int) -> dict:
    """Check if a file still exceeds the line threshold."""
    resolved_path = (VNX_ROOT / file_path) if not os.path.isabs(file_path) else Path(file_path)
    if not resolved_path.exists():
        return {"resolved": True, "reason": "auto-resolved: file no longer exists"}

    try:
        line_count = sum(1 for _ in resolved_path.open())
    except OSError:
        return {"resolved": False, "reason": "unable to read file"}

    if line_count <= threshold:
        return {"resolved": True, "reason": f"auto-resolved: actual {line_count}L, threshold {threshold}L"}
    return {"resolved": False, "reason": f"still exceeds: actual {line_count}L, threshold {threshold}L"}


def _check_function_size(func_name: str, file_path: str, threshold: int) -> dict:
    """Check if a function still exceeds the line threshold."""
    if not file_path:
        return {"resolved": False, "reason": "no file path in OI title, cannot verify"}

    resolved_path = (VNX_ROOT / file_path) if not os.path.isabs(file_path) else Path(file_path)
    if not resolved_path.exists():
        return {"resolved": True, "reason": "auto-resolved: file no longer exists"}

    try:
        lines = resolved_path.read_text().splitlines()
    except OSError:
        return {"resolved": False, "reason": "unable to read file"}

    # Find function definition
    func_start = None
    indent = None
    for i, line in enumerate(lines):
        # Python: def func_name(
        if re.match(rf'^(\s*)def\s+{re.escape(func_name)}\s*\(', line):
            func_start = i
            indent = len(line) - len(line.lstrip())
            break
        # Shell: func_name() {
        if re.match(rf'^(\s*){re.escape(func_name)}\s*\(\)', line):
            func_start = i
            indent = len(line) - len(line.lstrip())
            break

    if func_start is None:
        return {"resolved": True, "reason": f"auto-resolved: function {func_name} no longer exists in {file_path}"}

    # Count function body lines (until next def/class at same indent or less)
    func_end = len(lines)
    for i in range(func_start + 1, len(lines)):
        stripped = lines[i].lstrip()
        if not stripped or stripped.startswith('#'):
            continue
        current_indent = len(lines[i]) - len(stripped)
        if current_indent <= indent and (stripped.startswith('def ') or stripped.startswith('class ')):
            func_end = i
            break

    func_length = func_end - func_start
    if func_length <= threshold:
        return {"resolved": True, "reason": f"auto-resolved: function {func_name} actual {func_length}L, threshold {threshold}L"}
    return {"resolved": False, "reason": f"still exceeds: function {func_name} actual {func_length}L, threshold {threshold}L"}
```

### Integration with `digest`

When `digest --rescan` is passed, run `rescan_items()` before generating the digest:

```python
# In digest subcommand handler:
if args.rescan:
    rescan_items(args)
generate_digest()
```

### Audit Trail

Every auto-close writes to `open_items_audit.jsonl`:

```json
{"timestamp": "2026-04-06T16:30:00", "actor": "auto-rescan", "action": "auto_close", "item_id": "OI-042", "reason": "auto-resolved: actual 180L, threshold 300L", "source": "rescan"}
```

### Safety Constraints

1. **Only `open` items are rescanned.** Items already closed/deferred/wontfix are never touched.
2. **Only pattern-matched items are eligible.** Items without a recognized size-violation pattern are skipped.
3. **`blocker` items are auto-closeable** only for file-size and function-size categories (where the verification is deterministic). Category 3 (missing file) only auto-closes `info` items.
4. **Dry-run mode** is available for operator review before bulk closure.
5. **Audit log** records every auto-close with the verification evidence.
6. **No network calls.** All checks are local filesystem operations.

### Expected Impact

With 724 stale OIs, the first `rescan` run is expected to close the majority. Subsequent runs during `digest --rescan` will keep the list current, preventing re-accumulation.
