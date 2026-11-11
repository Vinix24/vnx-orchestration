"""Census: a new reader of prevention_rules cannot land without the activation predicate (loop D8).

Every ``FROM``/``JOIN prevention_rules`` outside ``tests/`` either has its own
``injectable_clause(`` call in the lines before it (after any earlier read in the file), or
stands in ``ALLOWLIST`` with the reason it returns no injectable content.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
READ = re.compile(r"\b(?:FROM|JOIN)\s+prevention_rules\b", re.IGNORECASE)
GUARD = "injectable_clause("
WINDOW = 15
ROOTS = ("scripts", "vnx_cli", "bin", "dashboard")

ALLOWLIST = {  # (path, stripped line): (occurrences, reason)
    ("scripts/check_intelligence_health.py", '"SELECT COUNT(*) FROM prevention_rules"'): (1, "COUNT(*) only"),
    ("scripts/deactivate_pattern_activation.py",
     'row = conn.execute("SELECT source FROM prevention_rules WHERE project_id = ? AND id = ?",'):
        (1, "legacy check of one id before a refusal, prints no rule content"),
    ("scripts/learning_loop.py", '"SELECT id FROM prevention_rules WHERE description = ? AND tag_combination = ?",'):
        (1, "ingest dedupe before a write, no injection"),
    ("scripts/learning_loop.py", '"SELECT id, description AS title, confidence FROM prevention_rules "'):
        (1, "archival candidates for operator review, no injection"),
    ("scripts/lib/dispatch_enricher.py", "SELECT description, recommendation FROM prevention_rules"):
        (1, "WHERE source='behavior_analysis' excludes every loop rule"),
    ("scripts/lib/intelligence_dashboard_data.py", "FROM prevention_rules"):
        (1, "operator view, shows every row; WHERE source='behavior_analysis'"),
    ("scripts/lib/intelligence_hygiene.py", "DELETE FROM prevention_rules"): (1, "writer, returns no rows"),
    ("scripts/lib/pattern_extractor.py", "con.execute(\"DELETE FROM prevention_rules WHERE source='behavior_analysis'\")"):
        (1, "writer, returns no rows"),
    ("scripts/tag_intelligence.py", '"SELECT id, confidence FROM prevention_rules WHERE tag_combination = ?",'):
        (1, "dedupe inside _store_prevention_rule before a write"),
    ("scripts/tag_intelligence.py", '"SELECT COUNT(*) FROM prevention_rules"'): (1, "COUNT(*) only"),
    ("scripts/tag_intelligence.py", "SELECT COUNT(*) FROM prevention_rules WHERE confidence >= 0.7"):
        (1, "COUNT(*) only"),
}
GUARDED = Counter({  # the six readers that return content, decision 14 of the plan
    "scripts/lib/intelligence_sources/failure_prevention.py": 1, "scripts/cached_intelligence.py": 1,
    "scripts/gather_intelligence.py": 1, "scripts/build_t0_quality_digest.py": 1, "scripts/tag_intelligence.py": 2,
})


def _sources(root: Path):
    for top in ROOTS:
        for path in sorted((root / top).rglob("*")):
            in_bin = path.parent == root / "bin"
            if path.is_file() and "__pycache__" not in path.parts and (path.suffix in (".py", ".sh") or in_bin):
                yield path


def census(root: Path, allowlist=ALLOWLIST):
    """(violations, guarded reads per file). A violation is an unguarded, unlisted read or a
    listed line whose occurrence count no longer matches."""
    violations, seen, guarded = [], Counter(), Counter()
    for path in _sources(root):
        rel = path.relative_to(root).as_posix()
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        previous = -1
        for i, line in enumerate(lines):
            if line.lstrip().startswith("#") or not READ.search(line):
                continue
            if (rel, line.strip()) in allowlist:
                seen[(rel, line.strip())] += 1
            elif any(GUARD in text for text in lines[max(previous + 1, i - WINDOW):i]):
                guarded[rel] += 1
            else:
                violations.append(f"{rel}:{i + 1}: {line.strip()}")
            previous = i
    violations += [f"{key}: listed {count}x, found {seen[key]}x" for key, (count, _) in allowlist.items()
                   if seen[key] != count]
    return violations, guarded


def test_every_reader_in_the_repo_is_guarded_or_listed_with_a_reason():
    violations, guarded = census(REPO_ROOT)
    assert violations == []
    assert guarded == GUARDED
    assert all(reason.strip() for _, reason in ALLOWLIST.values())


def test_a_new_unguarded_reader_fails_the_census(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "new_reader.py").write_text(
        'conn.execute("SELECT description\\n  from prevention_rules")\n', encoding="utf-8")
    assert census(tmp_path, {})[0] == ["scripts/new_reader.py:1: conn.execute(\"SELECT description\\n  from "
                                       "prevention_rules\")"]


def test_each_read_needs_its_own_guard_and_a_comment_is_no_read(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tool").write_text("\n".join([
        "# rows come from prevention_rules",
        "sql, params = injectable_clause(conn, state_dir)",
        "q1 = f'SELECT * FROM prevention_rules WHERE {sql}'",
        "q2 = 'SELECT * FROM prevention_rules p JOIN prevention_rules q ON p.id = q.id'",
    ]), encoding="utf-8")
    assert census(tmp_path, {}) == (["bin/tool:4: q2 = 'SELECT * FROM prevention_rules p JOIN prevention_rules "
                                     "q ON p.id = q.id'"], Counter({"bin/tool": 1}))


def test_a_listed_line_that_moved_or_multiplied_is_reported(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "a.py").write_text("x = 'SELECT COUNT(*) FROM prevention_rules'\n" * 2, encoding="utf-8")
    listed = {("scripts/a.py", "x = 'SELECT COUNT(*) FROM prevention_rules'"): (1, "COUNT(*) only"),
              ("scripts/gone.py", "FROM prevention_rules"): (1, "removed reader")}
    violations, _ = census(tmp_path, listed)
    assert violations == ["('scripts/a.py', \"x = 'SELECT COUNT(*) FROM prevention_rules'\"): listed 1x, found 2x",
                          "('scripts/gone.py', 'FROM prevention_rules'): listed 1x, found 0x"]
