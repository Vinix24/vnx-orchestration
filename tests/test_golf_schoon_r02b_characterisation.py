"""Characterisation tests for golf-schoon R02b (extract-method refactor).

Pins the observable behaviour of the two benchmark ``verify`` functions so the refactor stays
behaviour-neutral. Real temp dirs and real subprocesses; nothing is patched.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_TASKS = _REPO / "scripts/benchmark/field-tests/tasks/t2_medium"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v04 = _load("r02b_verify04", _TASKS / "04_scorer_task/verify.py")
v05 = _load("r02b_verify05", _TASKS / "05_extractor_subclass/verify.py")


def _put(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


# ---------------------------------------------------------------- task 04
_INIT = (
    "import sqlite3\n"
    "c = sqlite3.connect('documents.db')\n"
    "c.execute('CREATE TABLE documents (id INTEGER)')\n"
    "c.commit()\n"
)
_MIG = "CREATE TABLE IF NOT EXISTS document_scores (id INTEGER);\n"
_TESTS04 = "".join(f"def test_{i}():\n    assert True\n" for i in range(15))


def _cli(out: str) -> str:
    return f"print({out!r})\n"


def _seed04(tmp_path: Path, cli_out: str = '{"scored": 45, "skipped": 5}',
            tests: str = _TESTS04, init: str = _INIT) -> Path:
    seed = tmp_path / v04.SEED_REL
    _put(seed, "init_seed_db.py", init)
    _put(seed, "migrations/001_add_document_scores.sql", _MIG)
    _put(seed, "scorer.py", "")
    _put(seed, "cli.py", _cli(cli_out))
    _put(seed, "tests/test_scorer.py", tests)
    return seed


def test_v04_init_script_missing(tmp_path):
    r = v04.verify(tmp_path, {})
    init = tmp_path / v04.SEED_REL / "init_seed_db.py"
    assert r == {
        "pass": False,
        "evidence": f"seed init script missing at {init}",
        "details": {"pass_count": 0, "expected": 4, "files_written": []},
    }


def test_v04_required_files_missing(tmp_path):
    seed = tmp_path / v04.SEED_REL
    _put(seed, "init_seed_db.py", _INIT)
    _put(seed, "scorer.py", "")
    r = v04.verify(tmp_path, {})
    assert r["pass"] is False
    assert r["evidence"] == (
        "worker did not write required files: "
        "['migrations/001_add_document_scores.sql', 'cli.py', 'tests/test_scorer.py']"
    )
    assert r["details"] == {"pass_count": 0, "expected": 4, "files_written": ["scorer.py"]}


def test_v04_init_failure(tmp_path):
    _seed04(tmp_path, init="import sys\nsys.stderr.write('boom')\nsys.exit(3)\n")
    r = v04.verify(tmp_path, {})
    assert r["pass"] is False
    assert r["evidence"] == "init_seed_db failed: boom"
    assert r["details"]["pass_count"] == 0
    assert r["details"]["files_written"] == [
        "migrations/001_add_document_scores.sql", "scorer.py", "cli.py",
        "tests/test_scorer.py",
    ]
    assert "checks" not in r["details"]


def test_v04_all_checks_pass(tmp_path):
    _seed04(tmp_path)
    r = v04.verify(tmp_path, {})
    assert r["pass"] is True
    assert r["evidence"] == (
        "PASS migration applies cleanly; PASS migration idempotent; PASS 15 tests pass; "
        "PASS CLI exits 0 and prints valid {scored, skipped} json totaling 50"
    )
    d = r["details"]
    assert d["pass_count"] == 4 and d["expected"] == 4
    assert [c["name"] for c in d["checks"]][:2] == [
        "migration applies cleanly", "migration idempotent"]
    assert d["checks"][0]["note"].startswith("rc=0 stderr=")
    assert d["checks"][1]["note"] == "rc=0"
    assert d["checks"][2]["note"] == "passed=15 failed=0"
    assert d["checks"][3]["note"].startswith("rc=0 stdout-tail=")


def test_v04_bad_cli_json_and_failing_tests(tmp_path):
    bad = _TESTS04 + "def test_bad():\n    assert False\n"
    _seed04(tmp_path, cli_out='{"scored": 1, "skipped": 1}', tests=bad)
    r = v04.verify(tmp_path, {})
    assert r["pass"] is False
    assert r["evidence"] == (
        "PASS migration applies cleanly; PASS migration idempotent; FAIL 15 tests pass; "
        "FAIL CLI exits 0 and prints valid {scored, skipped} json totaling 50"
    )
    assert r["details"]["pass_count"] == 2
    assert r["details"]["checks"][2]["note"] == "passed=15 failed=1"


def test_v04_broken_migration_skips_idempotent_check(tmp_path):
    seed = _seed04(tmp_path, cli_out="not json")
    _put(seed, "migrations/001_add_document_scores.sql", "THIS IS NOT SQL;\n")
    r = v04.verify(tmp_path, {})
    names = [c["name"] for c in r["details"]["checks"]]
    assert names == ["migration applies cleanly", "15 tests pass",
                     "CLI exits 0 and prints valid {scored, skipped} json totaling 50"]
    assert [c["ok"] for c in r["details"]["checks"]] == [False, True, False]
    assert r["pass"] is False
    assert r["details"]["pass_count"] == 1


# ---------------------------------------------------------------- task 05
_EXT = (
    "import re\n"
    "class EmailLinkExtractor:\n"
    "    def extract(self, html, url):\n"
    "        em = [m.lower() for m in re.findall(r'mailto:([^\"?]+)', html)]\n"
    "        return {'name': 'email_link', 'errors': [], 'data': {\n"
    "            'total_links': len(em), 'unique_emails': len(set(em)),\n"
    "            'emails': sorted(set(em))}}\n"
)
_TESTS05 = "".join(f"def test_{i}():\n    assert True\n" for i in range(5))


def _seed05(tmp_path: Path, ext: str = _EXT, tests: str = _TESTS05) -> Path:
    seed = tmp_path / v05.SEED_REL
    _put(seed, "email_link_extractor.py", ext)
    _put(seed, "tests/test_email_link_extractor.py", tests)
    return seed


def test_v05_missing_files(tmp_path):
    seed = tmp_path / v05.SEED_REL
    _put(seed, "email_link_extractor.py", _EXT)
    r = v05.verify(tmp_path, {})
    assert r == {
        "pass": False,
        "evidence": "worker did not write: ['tests/test_email_link_extractor.py']",
        "details": {"pass_count": 0, "expected": 3,
                    "files_written": ["email_link_extractor.py"]},
    }


def test_v05_all_checks_pass(tmp_path):
    _seed05(tmp_path)
    r = v05.verify(tmp_path, {})
    assert r["pass"] is True
    assert r["evidence"] == (
        "PASS empty-html envelope shape correct; PASS 5 tests pass; "
        "PASS dedup + lowercase + subject-strip work"
    )
    d = r["details"]
    assert d["pass_count"] == 3 and d["expected"] == 3
    assert d["files_written"] == ["email_link_extractor.py",
                                  "tests/test_email_link_extractor.py"]
    assert d["checks"][1]["note"] == "passed=5 failed=0"
    assert d["checks"][0]["note"].startswith("stdout=")


def test_v05_failures(tmp_path):
    bad_ext = "class EmailLinkExtractor:\n    def extract(self, h, u):\n        return {}\n"
    _seed05(tmp_path, ext=bad_ext, tests=_TESTS05 + "def test_x():\n    assert 0\n")
    r = v05.verify(tmp_path, {})
    assert r["pass"] is False
    assert r["evidence"] == (
        "FAIL empty-html envelope shape correct; FAIL 5 tests pass; "
        "FAIL dedup + lowercase + subject-strip work"
    )
    assert r["details"]["pass_count"] == 0
    assert r["details"]["checks"][1]["note"] == "passed=5 failed=1"
