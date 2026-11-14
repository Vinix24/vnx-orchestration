"""Characterisation tests for golf-schoon R02 (extract-method refactor).

Pins the observable behaviour of the two benchmark ``verify`` functions and of
``build_strategy_projection.build_projection`` so the refactor stays
behaviour-neutral. Real temp dirs and real subprocesses; nothing is patched.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts"))
sys.path.insert(0, str(_REPO / "scripts" / "lib"))

import build_strategy_projection as bsp  # noqa: E402

_TASKS = _REPO / "scripts/benchmark/field-tests/tasks/t2_medium"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


v05 = _load("r02_verify05", _TASKS / "05_extractor_subclass/verify.py")


def _put(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


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


# -------------------------------------------------------- build_projection
def _roadmap(tmp_path: Path, features) -> Path:
    import yaml
    p = tmp_path / "roadmap.yaml"
    p.write_text(yaml.safe_dump({"roadmap_id": "rid", "title": "T", "features": features}))
    return p


_NOW = "2026-06-01T12:00:00+00:00"


def test_projection_full_shape(tmp_path, capsys):
    feats = [
        {"feature_id": "late", "status": "planned", "milestone": "1.0",
         "depends_on": ["done1", "ghost"], "risk_class": "high",
         "review_stack": ["codex", ""], "branch_name": "b", "plan_path": "p.md",
         "notes": "  hi  "},
        {"feature_id": "", "title": "skipped"},
        {"feature_id": "done1", "title": "Done One", "status": "done",
         "pr_queue": [{"pr_id": "x"}, {"pr_id": "#42"}, {"pr_id": "#99"}]},
        {"feature_id": "free", "status": "bogus-status", "risk_class": "weird"},
        {"feature_id": "wip", "status": "in_progress", "pr_queue": [{"pr_id": "#abc"}]},
    ]
    path = _roadmap(tmp_path, feats)
    rm = bsp.build_projection(path, now_iso=_NOW)
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert lines[0] == "[build_strategy_projection] skipping feature with empty feature_id"
    assert any("wave late: depends_on 'ghost' not found" in ln for ln in lines)
    assert not any("depends_on 'done1'" in ln for ln in lines)
    ids = [w.wave_id for w in rm.waves]
    assert ids[0] == "done1"
    by = {w.wave_id: w for w in rm.waves}
    assert ids.index("free") < ids.index("late")
    assert by["done1"].status == "completed" and by["done1"].pr_number == 42
    assert by["done1"].title == "Done One" and by["free"].title == "free"
    assert by["free"].status == "planned" and by["free"].risk_class == "low"
    assert by["late"].risk_class == "high"
    assert by["late"].depends_on == ["done1", "ghost"]
    assert by["late"].review_stack == ["codex"]
    assert (by["late"].branch_name, by["late"].plan_path, by["late"].notes) == ("b", "p.md", "hi")
    assert by["free"].branch_name is None and by["free"].notes is None
    assert by["wip"].pr_number is None
    assert rm.roadmap_id == "rid" and rm.title == "T" and rm.generated_at == _NOW
    assert [p.phase_id for p in rm.phases] == sorted({w.phase_id for w in rm.waves})
    for p in rm.phases:
        assert p.waves == [w.wave_id for w in rm.waves if w.phase_id == p.phase_id]
        assert p.title == bsp._PHASE_TITLES.get(p.phase_id, f"Phase {p.phase_id}")
    assert rm.completed_history == [
        {"wave_id": "done1", "title": "Done One", "completed_at": "2026-06-01", "pr": "#42"}]
    assert rm.operator_decisions == [] and rm.schema_version == 1
    assert rm.notes["source"] == str(path)
    assert rm.notes["generator"] == "scripts/build_strategy_projection.py"


def test_projection_defaults_and_empty(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text("features: []\n")
    rm = bsp.build_projection(p, now_iso=_NOW)
    assert rm.roadmap_id == "vnx-roadmap-autopilot"
    assert rm.title == "VNX Multi-Feature Roadmap"
    assert rm.waves == [] and rm.phases == [] and rm.completed_history == []


def test_projection_status_priority_order(tmp_path):
    feats = [
        {"feature_id": "p2", "status": "planned", "depends_on": ["p1"]},
        {"feature_id": "p1", "status": "planned"},
        {"feature_id": "ip", "status": "in_progress"},
        {"feature_id": "c", "status": "done"},
    ]
    rm = bsp.build_projection(_roadmap(tmp_path, feats), now_iso=_NOW)
    ids = [w.wave_id for w in rm.waves]
    assert ids == ["c", "p1", "p2", "ip"]


def test_projection_missing_file_raises(tmp_path):
    with pytest.raises(Exception):
        bsp.build_projection(tmp_path / "nope.yaml", now_iso=_NOW)
