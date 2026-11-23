#!/usr/bin/env python3
"""Matcher and generator for the operator-name guard (track agnostisch-standaard).

The matcher counts, per (path, name, fingerprint), the lines of tracked files in
SCOPE that carry one of the operator's own names. The generator reads ONLY the
per-finding table (``tests/fixtures/operator_names/findings.tsv``) and writes the
allowlist the guard compares against: no path rule, no category per directory.
A new finding is allowed by a new table row, which shows in the diff, never by
regenerating. The sentinel tenant name is matched quoted and bare (AG-G3), which
includes the ``DEFAULT`` clause of a column definition. The scope covers ``dashboard/``,
``configs/``, the two installers and ``CODEOWNERS``. The two extra names are
case-sensitive and the second one skips ``claude-<name>``, which is already a name of its own.
``scripts/lib/migration_linter.py`` calls this matcher and this table for that clause.
"""
from __future__ import annotations

import collections
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

SCOPE = ("scripts", "vnx_cli", "schemas", "bin", "hooks", "dashboard", "configs",
         ".vnx/vnx_workers.default.yaml", "install-central.sh", "install-remote.sh", "CODEOWNERS")
# The names are built from halves so this module does not match itself.
_SALES = "sales" + "minds"
_NAMES = ("claude-" + _SALES, "kimi-" + "k3", "Vinix" + "24", "vincent" + "vandeth")
_DEV = "vnx-" + "dev"
NAMES = {n: re.compile(re.escape(n)) for n in _NAMES}
NAMES[_DEV] = re.compile(re.escape(_DEV))
NAMES[_SALES] = re.compile("(?<!claude-)" + re.escape(_SALES))
CATEGORIES = ("frozen_migration", "legacy_tenant_reader", "model_catalog",
              "te_repareren", "upstream_url", "issue_link")
COLUMNS = ("fingerprint", "path", "name", "count", "category", "reason", "group")
REPO = Path(__file__).resolve().parents[2]
TABLE = REPO / "tests/fixtures/operator_names/findings.tsv"
ALLOWLIST = REPO / "tests/fixtures/operator_names/allowlist.json"


def normalise(line: str) -> str:
    return " ".join(line.split())


def fingerprint(line: str) -> str:
    return hashlib.sha256(normalise(line).encode()).hexdigest()[:16]


def scan(root: Path) -> dict:
    """{(path, name, fingerprint): (count, normalised line)} for tracked files in SCOPE."""
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", *SCOPE],
                         capture_output=True, check=True).stdout
    found: dict = {}
    counts: collections.Counter = collections.Counter()
    for raw in out.split(b"\0"):
        path = raw.decode("utf-8", "surrogateescape")
        if not path or path.startswith("tests/") or path.endswith(".md"):
            continue
        data = (Path(root) / path).read_bytes()
        if b"\0" in data[:8000]:  # binary, as git grep treats it
            continue
        for line in data.decode("utf-8", "replace").split("\n"):
            for name, rx in NAMES.items():
                if rx.search(line):
                    key = (path, name, fingerprint(line))
                    counts[key] += 1
                    found[key] = normalise(line)
    return {k: (counts[k], found[k]) for k in counts}


def load_table(path: Path = TABLE) -> list:
    """Rows of the table as dicts; a malformed table raises ValueError, never an empty result."""
    lines = path.read_text(encoding="utf-8").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or tuple(lines[0].split("\t")) != COLUMNS:
        raise ValueError(f"{path}: header is not {COLUMNS}")
    rows = []
    for no, line in enumerate(lines[1:], 2):
        cells = line.split("\t")
        if len(cells) != len(COLUMNS):
            raise ValueError(f"{path}:{no}: {len(cells)} columns, expected {len(COLUMNS)}")
        row = dict(zip(COLUMNS, cells))
        if not row["count"].isdigit() or int(row["count"]) < 1:
            raise ValueError(f"{path}:{no}: count {row['count']!r} is not a positive integer")
        row["count"] = int(row["count"])
        rows.append(row)
    return rows


def validate_table(rows: list) -> list:
    """Problems with the table itself; an empty list means every rule holds."""
    problems, seen = [], {}
    for r in rows:
        key = (r["path"], r["name"], r["fingerprint"])
        tag = f"{r['path']} {r['name']} {r['fingerprint']}"
        if r["name"] not in NAMES:
            problems.append(f"{tag}: unknown name")
        if r["category"] not in CATEGORIES:
            problems.append(f"{tag}: unknown category {r['category']!r}")
        if not r["reason"].strip():
            problems.append(f"{tag}: empty reason")
        if r["category"] == "te_repareren" and not re.fullmatch(r"AG-\d+", r["group"]):
            problems.append(f"{tag}: te_repareren needs a group AG-n, got {r['group']!r}")
        if key in seen:
            problems.append(f"{tag}: listed twice ({seen[key]} and {r['category']})")
        seen[key] = r["category"]
    return problems


def generate(rows: list) -> dict:
    """The allowlist, from the table rows alone."""
    entries = sorted([r["path"], r["name"], r["fingerprint"], r["count"]] for r in rows)
    return {"columns": ["path", "name", "fingerprint", "count"], "entries": entries}


def render(allowlist: dict) -> str:
    head = json.dumps(allowlist["columns"])
    items = [json.dumps(e) for e in allowlist["entries"]]
    body = ",\n".join("  " + ", ".join(items[i:i + 3]) for i in range(0, len(items), 3))  # 3 per line keeps the diff short
    return f'{{"columns": {head}, "entries": [\n{body}\n]}}\n'


def compare(found: dict, allowlist: dict) -> list:
    """Failures between the scan and the allowlist; each names path, name, fingerprint."""
    allowed = {(p, n, f): c for p, n, f, c in allowlist["entries"]}
    problems = []
    for (p, n, f), (count, line) in sorted(found.items()):
        want = allowed.get((p, n, f))
        if want is None:
            problems.append(f"NEW {p} {n} {f}: {line}")
        elif count != want:
            problems.append(f"COUNT {p} {n} {f}: found {count}, table says {want}: {line}")
    for (p, n, f), want in sorted(allowed.items()):
        if (p, n, f) not in found:
            problems.append(f"GONE {p} {n} {f}: table row has no finding; remove the row")
    return problems


def main(argv: list) -> int:
    """``generate`` writes the allowlist from the table; ``check`` compares the tree."""
    cmd = argv[0] if argv else ""
    if cmd == "generate":
        ALLOWLIST.write_text(render(generate(load_table())), encoding="utf-8")
        return 0
    if cmd == "check":
        problems = validate_table(load_table()) + compare(scan(REPO), generate(load_table()))
        print("\n".join(problems))
        return 1 if problems else 0
    print("usage: operator_names.py generate|check", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
