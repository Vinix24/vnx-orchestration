"""Deur D5: ``vnx-gate/door`` is a required check on main, and the door's own paths are gate-merge.

ADR-061 and the plan ``deur-afgedwongen`` §4 and §15: the declared protection
names the door permit, the files that decide what a permit is fall under the
merge gate's risk class, and taking the entry out again is a weakening.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import review_stack_support  # noqa: F401  (puts scripts/ and scripts/lib on sys.path)

import forge_protection_drift as fpd
import review_risk_class as rrc
import smart_router
from ci_contexts import RequiredCheck

ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = ROOT / "scripts" / "forge" / "branch_protection.yaml"
DOOR_CHECK = RequiredCheck(context="vnx-gate/door", app_id=4869217)
HEAD = "b" * 40
#: ``plan_scope_rule_version()`` on origin/main before D5 (b8fa9e22f).
VERSION_BEFORE_D5 = "0e24a4e6629bc962"


def _config(text: str | None = None) -> fpd.ProtectionConfig:
    return fpd.parse_protection_config(text if text is not None else YAML_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", [
    "scripts/lib/ci_contexts.py",
    "scripts/lib/forge_check_run.py",
    "scripts/lib/forge_gate_publisher.py",
    "scripts/lib/required_contexts_gate.py",
    "scripts/lib/pr_readiness.py",
])
def test_the_doors_own_paths_are_gate_merge(path):
    assert smart_router.plan_scope_path_category(path) == "gate-merge"


def test_the_door_permit_is_a_required_check_bound_to_the_vnx_gate_app():
    assert DOOR_CHECK in _config().checks


def test_the_door_check_sits_next_to_the_other_vnx_gate_check():
    contexts = [check.context for check in _config().checks]
    assert "vnx-gate/review" in contexts
    review = next(c for c in _config().checks if c.context == "vnx-gate/review")
    assert review.app_id == DOOR_CHECK.app_id


def test_the_rule_version_moved_and_an_old_version_risk_is_recomputed(tmp_path):
    assert smart_router.plan_scope_rule_version() != VERSION_BEFORE_D5
    reading = rrc.DiffReading(("scripts/lib/ci_contexts.py",), merge_base="a" * 40)
    stale = replace(rrc.derive_risk_class(rrc.DiffReading(("docs/x.md",), merge_base="a" * 40), head_sha=HEAD),
                    rule_version=VERSION_BEFORE_D5)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / f"pr-{review_stack_support.PR}-codex_gate.json").write_text(json.dumps({
        "gate": "codex_gate", "pr_number": review_stack_support.PR, "commit_sha": HEAD,
        "requested_at": "2026-11-08T10:00:00Z", rrc.RECORD_KEY: stale.to_record(),
    }), encoding="utf-8")
    risk = rrc.head_risk_class(tmp_path, str(review_stack_support.PR), HEAD, reading)
    assert (risk.risk_class, risk.source) == ("high", rrc.SOURCE_RECOMPUTED)
    assert ("scripts/lib/ci_contexts.py", "gate-merge") in risk.deciding


def test_taking_the_door_check_out_again_is_a_weakening():
    text = YAML_PATH.read_text(encoding="utf-8")
    kept = [line for line in text.splitlines(keepends=True) if 'context: "vnx-gate/door"' not in line]
    assert len(kept) == len(text.splitlines(keepends=True)) - 1
    weaker = fpd.to_normalized_dict(_config("".join(kept)))
    weakening, fields = fpd.is_weakening(fpd.to_normalized_dict(_config()), weaker)
    assert weakening is True
    assert any("vnx-gate/door" in field for field in fields)
