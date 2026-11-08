"""ADR-057 (permissions P1): the fleet rules about worker-permission enforcement tell the truth.

Enforcement is default OFF (``worker_permissions.worker_permission_enforcement_enabled``), and
the PreToolUse hook is registered in ``.claude/settings.json`` but inert while it is OFF.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib" / "providers"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

from constraint_enforcer import _load_yaml

CONSTRAINTS = REPO_ROOT / "scripts" / "lib" / "providers" / "provider_constraints.yaml"
LOCKS_DOC = REPO_ROOT / "docs" / "core" / "LOCKS_AND_RELEASES.md"
ADR_057 = REPO_ROOT / "docs" / "governance" / "decisions" / "ADR-057-provider-agnostic-permission-policy.md"
SETTINGS = REPO_ROOT / ".claude" / "settings.json"
RULE_ID = "worker-permission-enforcement-on"


def _rule() -> dict:
    data = _load_yaml(CONSTRAINTS)
    matches = [c for c in data["constraints"] if c.get("id") == RULE_ID]
    assert len(matches) == 1, f"rule {RULE_ID} must exist exactly once, found {len(matches)}"
    return matches[0]


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_rule_does_not_claim_default_on():
    reason = _flat(_rule()["reason"])
    assert "defaults ON since 2026-08-15" not in reason


def test_rule_states_default_off():
    reason = _flat(_rule()["reason"])
    assert "default OFF" in reason


def test_rule_keeps_its_id_and_history_names_the_old_claim():
    rule = _rule()
    history = _flat(str(rule.get("history", "")))
    assert "defaults ON since 2026-08-15" in history
    assert "2026-11-08" in history


def test_locks_doc_does_not_say_hook_is_not_registered():
    assert "hook is not registered" not in _flat(LOCKS_DOC.read_text(encoding="utf-8"))


def test_locks_doc_names_the_registration():
    text = _flat(LOCKS_DOC.read_text(encoding="utf-8"))
    assert ".claude/settings.json" in text
    assert "registered" in text and "inert" in text


def test_hook_really_is_registered_in_settings():
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    commands = [
        hook.get("command", "")
        for entry in settings["hooks"]["PreToolUse"]
        for hook in entry.get("hooks", [])
    ]
    assert any("pretooluse_worker_scope_enforce.sh" in c for c in commands)


def test_adr_057_exists_with_status_and_policy_digest():
    assert ADR_057.is_file()
    text = ADR_057.read_text(encoding="utf-8")
    assert re.search(r"^\*\*Status:\*\*", text, re.MULTILINE)
    assert "policy_digest" in text
