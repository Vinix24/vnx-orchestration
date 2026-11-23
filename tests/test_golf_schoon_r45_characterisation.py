#!/usr/bin/env python3
"""Characterisation of ``parse_protection_config`` (golf schoon R45).

Pins the observable behaviour before the extract-method refactor: the full
returned :class:`ProtectionConfig`, the exact exception type and message of
every validation branch, and the order in which the validations fire when a
document violates several rules at once. Fed recorded protection YAML only;
no ``gh``, no network, no branch-protection read.
"""

from __future__ import annotations

import copy
import dataclasses
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

VNX_ROOT = Path(__file__).resolve().parent.parent
_LIB = VNX_ROOT / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "_golf_r45_forge_protection_drift", _LIB / "forge_protection_drift.py",
    )
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves the module through sys.modules at class creation.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fpd = _load_module()
GATE_APP = 4869217


def _doc(**overrides: Any) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "branch": "main",
        "required_status_checks": {
            "strict": True,
            "checks": [
                {"context": "Profile A", "app_id": 15368},
                {"context": "Any app", "app_id": -1},
                {"context": "Unbound", "app_id": None},
                {"context": "vnx-gate/review", "app_id": GATE_APP},
            ],
        },
        "pending_checks": ["vnx-gate/later"],
        "required_pull_request_reviews": {
            "required_approving_review_count": 2,
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": False,
            "require_last_push_approval": True,
            "bypass_pull_request_allowances": {"users": ["zed", "amy"], "apps": ["bot"]},
        },
        "enforce_admins": True,
        "required_signatures": False,
        "required_linear_history": True,
        "allow_force_pushes": False,
        "allow_deletions": False,
        "allow_fork_syncing": True,
        "block_creations": False,
        "lock_branch": False,
        "required_conversation_resolution": True,
        "restrictions": None,
        "repo": {"allow_auto_merge": True},
        "rulesets": ["r1", "r2"],
        "app": {"slug": "vnx-gate", "app_id": GATE_APP},
        "enforcement": "warn",
        "ci_workflow": "  CI  ",
    }
    doc.update(overrides)
    return doc


def _parse(doc: Dict[str, Any]):
    return fpd.parse_protection_config(yaml.safe_dump(doc))


def _error(doc: Dict[str, Any]) -> str:
    with pytest.raises(fpd.ProtectionConfigError) as info:
        _parse(doc)
    assert type(info.value) is fpd.ProtectionConfigError
    return str(info.value)


def test_full_config_round_trip_is_pinned():
    config = _parse(_doc())
    assert dataclasses.asdict(config) == {
        "branch": "main",
        "strict": True,
        "checks": (
            fpd.RequiredCheck("Profile A", 15368),
            fpd.RequiredCheck("Any app", None),
            fpd.RequiredCheck("Unbound", None),
            fpd.RequiredCheck("vnx-gate/review", GATE_APP),
        ),
        "pending_checks": ("vnx-gate/later",),
        "required_pull_request_reviews_present": True,
        "required_approving_review_count": 2,
        "dismiss_stale_reviews": True,
        "require_code_owner_reviews": False,
        "require_last_push_approval": True,
        "bypass_pull_request_allowances": ("apps:bot", "users:amy", "users:zed"),
        "enforce_admins": True,
        "required_signatures": False,
        "required_linear_history": True,
        "allow_force_pushes": False,
        "allow_deletions": False,
        "allow_fork_syncing": True,
        "block_creations": False,
        "lock_branch": False,
        "required_conversation_resolution": True,
        "allow_auto_merge": True,
        "rulesets": ("r1", "r2"),
        "enforcement": "warn",
        "ci_workflow": "CI",
    }
    assert all(type(c) is fpd.RequiredCheck for c in config.checks)


def test_null_pull_request_reviews_yields_defaults():
    doc = _doc(required_pull_request_reviews=None)
    for key in ("app", "enforcement", "ci_workflow"):
        doc.pop(key)
    doc["required_status_checks"]["checks"] = [{"context": "Profile A", "app_id": 1}]
    config = _parse(doc)
    assert config.required_pull_request_reviews_present is False
    assert config.required_approving_review_count == 0
    assert (config.dismiss_stale_reviews, config.require_code_owner_reviews,
            config.require_last_push_approval) == (False, False, False)
    assert config.bypass_pull_request_allowances == ()
    assert config.enforcement == "enforce"
    assert config.ci_workflow is None


def test_canonical_repo_yaml_parses():
    text = (VNX_ROOT / "scripts" / "forge" / "branch_protection.yaml").read_text(encoding="utf-8")
    config = fpd.parse_protection_config(text)
    assert config.branch == "main"
    assert any(c.context == "vnx-gate/door" for c in config.checks)


def test_invalid_yaml_chains_yaml_error():
    with pytest.raises(fpd.ProtectionConfigError) as info:
        fpd.parse_protection_config("a: [unclosed")
    assert str(info.value).startswith("branch_protection.yaml is geen geldige YAML: ")
    assert isinstance(info.value.__cause__, yaml.YAMLError)


def _checks(*entries):
    doc = _doc()
    doc["required_status_checks"]["checks"] = list(entries)
    return doc


@pytest.mark.parametrize("doc_factory, message", [
    (lambda: _doc(branch=""), "branch moet een niet-lege string zijn"),
    (lambda: _doc(required_status_checks=[]),
     "required_status_checks moet een object zijn, kreeg list"),
    (lambda: _doc(required_status_checks={"strict": "y", "checks": [{"context": "a", "app_id": 1}]}),
     "required_status_checks.strict moet een boolean zijn, kreeg str"),
    (lambda: _checks(), "required_status_checks.checks moet een niet-lege lijst zijn"),
    (lambda: _checks({"context": "a"}),
     "required_status_checks.checks[0] mist verplichte velden: ['app_id']"),
    (lambda: _checks({"context": "", "app_id": 1}),
     "required_status_checks.checks[0].context moet een niet-lege string zijn"),
    (lambda: _checks({"context": "a", "app_id": 1}, {"context": "a", "app_id": 2}),
     "required_status_checks.checks bevat 'a' dubbel"),
    (lambda: _checks({"context": "a", "app_id": "x"}),
     "required_status_checks.checks[0].app_id moet een integer of null zijn"),
    (lambda: _checks({"context": "vnx-gate/x", "app_id": None}),
     "required_status_checks.checks[0] ('vnx-gate/x') is een vnx-gate/*-check zonder gebonden "
     "app_id (null of de 'elke app'-sentinel -1): dat zou elke app deze status laten zetten"),
    (lambda: _checks({"context": "vnx-gate/x", "app_id": -1}),
     "required_status_checks.checks[0] ('vnx-gate/x') is een vnx-gate/*-check zonder gebonden "
     "app_id (null of de 'elke app'-sentinel -1): dat zou elke app deze status laten zetten"),
    (lambda: {**_checks({"context": "vnx-gate/x", "app_id": 7}), "app": {"slug": "s", "app_id": None}},
     "required_status_checks.checks[0] ('vnx-gate/x') is een vnx-gate/*-check, maar dit bestand "
     "declareert geen tekenende App (app.app_id ontbreekt of is null): er is geen identiteit om "
     "hem aan te binden, dus zou de check onvervulbaar zijn"),
    (lambda: _checks({"context": "vnx-gate/x", "app_id": 7}),
     "required_status_checks.checks[0] ('vnx-gate/x') is gebonden aan app_id 7, maar de App die "
     f"tekent is app.app_id {GATE_APP}: alleen die App kan deze check vervullen, dus zou hij "
     "permanent rood blijven. Houd het getal in de entry gelijk aan app.app_id"),
    (lambda: _doc(pending_checks=[1]), "pending_checks moet een lijst van strings zijn"),
    (lambda: _doc(required_pull_request_reviews={"required_approving_review_count": 0}),
     "required_pull_request_reviews mist verplichte velden: "
     "['dismiss_stale_reviews', 'require_code_owner_reviews', 'require_last_push_approval']"),
    (lambda: _doc(required_pull_request_reviews={
        "required_approving_review_count": True, "dismiss_stale_reviews": False,
        "require_code_owner_reviews": False, "require_last_push_approval": False}),
     "required_pull_request_reviews.required_approving_review_count moet een niet-negatieve integer zijn"),
    (lambda: _doc(required_pull_request_reviews={
        "required_approving_review_count": -1, "dismiss_stale_reviews": False,
        "require_code_owner_reviews": False, "require_last_push_approval": False}),
     "required_pull_request_reviews.required_approving_review_count moet een niet-negatieve integer zijn"),
    (lambda: _doc(required_pull_request_reviews={
        "required_approving_review_count": 0, "dismiss_stale_reviews": 1,
        "require_code_owner_reviews": 1, "require_last_push_approval": 1}),
     "required_pull_request_reviews.dismiss_stale_reviews moet een boolean zijn, kreeg int"),
    (lambda: _doc(required_pull_request_reviews={
        "required_approving_review_count": 0, "dismiss_stale_reviews": False,
        "require_code_owner_reviews": 1, "require_last_push_approval": 1}),
     "required_pull_request_reviews.require_code_owner_reviews moet een boolean zijn, kreeg int"),
    (lambda: _doc(required_pull_request_reviews={
        "required_approving_review_count": 0, "dismiss_stale_reviews": False,
        "require_code_owner_reviews": False, "require_last_push_approval": 1}),
     "required_pull_request_reviews.require_last_push_approval moet een boolean zijn, kreeg int"),
    (lambda: _doc(required_pull_request_reviews={
        "required_approving_review_count": 0, "dismiss_stale_reviews": False,
        "require_code_owner_reviews": False, "require_last_push_approval": False,
        "bypass_pull_request_allowances": {"bots": []}}),
     "required_pull_request_reviews.bypass_pull_request_allowances heeft onbekende velden: ['bots']"),
    (lambda: _doc(enforce_admins="yes", lock_branch="no"),
     "branch_protection.yaml.enforce_admins moet een boolean zijn, kreeg str"),
    (lambda: _doc(restrictions={}),
     "restrictions moet null zijn (push-restricties worden hier niet gemodelleerd)"),
    (lambda: _doc(repo={}), "repo mist verplichte velden: ['allow_auto_merge']"),
    (lambda: _doc(repo={"allow_auto_merge": "no"}),
     "repo.allow_auto_merge moet een boolean zijn, kreeg str"),
    (lambda: _doc(rulesets="r1"), "rulesets moet een lijst van strings zijn"),
])
def test_validation_messages_are_pinned(doc_factory, message):
    assert _error(doc_factory()) == message


@pytest.mark.parametrize("overrides, message", [
    # enforcement/ci_workflow are read before the branch check
    ({"branch": "", "enforcement": "loud"}, "enforcement moet een van"),
    ({"branch": "", "ci_workflow": " "}, "ci_workflow moet een niet-lege string zijn"),
    # the branch check fires before the status-check block
    ({"branch": "", "required_status_checks": []}, "branch moet een niet-lege string zijn"),
    # status checks fire before pending_checks
    ({"pending_checks": [1], "required_status_checks": {"strict": True, "checks": []}},
     "required_status_checks.checks moet"),
    # pending_checks before the review block
    ({"pending_checks": [1], "required_pull_request_reviews": {}}, "pending_checks moet"),
    # the review block before the top-level booleans
    ({"enforce_admins": "x", "required_pull_request_reviews": {}},
     "required_pull_request_reviews mist"),
    # top-level booleans before restrictions
    ({"enforce_admins": "x", "restrictions": {}}, "branch_protection.yaml.enforce_admins"),
    # restrictions before repo
    ({"restrictions": {}, "repo": {}}, "restrictions moet null"),
    # repo before rulesets
    ({"repo": {}, "rulesets": "x"}, "repo mist"),
])
def test_validation_order_is_pinned(overrides, message):
    assert _error(_doc(**overrides)).startswith(message)


def test_check_order_first_failing_entry_wins():
    doc = _checks(
        {"context": "ok", "app_id": 1},
        {"context": "ok", "app_id": "x"},
        {"context": "", "app_id": 1},
    )
    assert _error(doc) == "required_status_checks.checks bevat 'ok' dubbel"


def test_input_document_is_not_mutated():
    doc = _doc()
    before = copy.deepcopy(doc)
    _parse(doc)
    assert doc == before
