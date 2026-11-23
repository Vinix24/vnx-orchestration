"""OI-2975: a missing schema module is a named violation, not a crash, not a pass."""
from __future__ import annotations

import builtins
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import report_to_receipt_converter as rtc  # noqa: E402

_BODY = SimpleNamespace(valid=True, missing=[], placeholder=False)
_DID = "20261117-oi2975-test"


def _fm_text(**extra) -> str:
    from test_report_to_receipt_converter import _v1_frontmatter_base

    fm = _v1_frontmatter_base(_DID)
    fm.update(extra)
    return "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n## Summary\n\nx\n"


def _checks(text: str) -> list:
    return rtc._run_fail_closed_checks(text, _DID, _BODY, merged={})


def _fm_violations(violations: list) -> list:
    return [v for v in violations if v.startswith("frontmatter_v1")]


def test_unimportable_schema_module_is_named_violation(monkeypatch):
    # None in sys.modules makes `import unified_report_schema` raise ImportError.
    monkeypatch.setitem(sys.modules, "unified_report_schema", None)
    found = _fm_violations(_checks(_fm_text()))
    assert len(found) == 1
    assert found[0].startswith("frontmatter_v1: schema module unavailable (ModuleNotFoundError: ")


def test_unavailable_message_is_truncated(monkeypatch):
    real_import = builtins.__import__

    def fake(name, *a, **k):
        if name == "unified_report_schema":
            raise ModuleNotFoundError("x" * 500)
        return real_import(name, *a, **k)

    monkeypatch.delitem(sys.modules, "unified_report_schema", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake)
    found = _fm_violations(_checks(_fm_text()))
    assert found == [
        "frontmatter_v1: schema module unavailable (ModuleNotFoundError: " + "x" * 120 + ")"
    ]


def test_invalid_frontmatter_still_reports_violation():
    found = _fm_violations(_checks(_fm_text(exit_code="bad")))
    assert found and "schema module unavailable" not in found[0]


def test_valid_frontmatter_has_no_frontmatter_violation():
    assert _fm_violations(_checks(_fm_text())) == []
