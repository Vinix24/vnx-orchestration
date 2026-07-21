"""OI-707 — door pre-flight must normalize a bare kimi alias before the registry gate.

Repro this closes: `_model_in_registry('kimi', None, 'kimi')` -> False. A bare
`vnx dispatch-agent --model kimi` (provider name, not a model id) was rejected by
the door pre-flight (`dispatch_cli.build_runtime_snapshot` ->
`constraint_enforcer.check_constraints(check_registry=True)`) BEFORE ever reaching
ProviderAdapter.run()'s shared kimi resolver (20260721-kimi-lane-hardening,
_kimi_resolve_requested_key/_kimi_resolve_cli_model_arg). `--model kimi-k3`
(explicit id) already worked end-to-end.

The fix normalizes bare kimi aliases ('kimi', 'kimi-default', 'kimi_cli') to the
registry's kimi_cli.default_model flag BEFORE the model-in-registry check —
single source of truth, no hardcoded model id. A real/unmapped id (e.g.
'kimi-bogus') must still be rejected: this is a targeted alias normalization,
not a weakened gate.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(_LIB))

from providers.constraint_enforcer import ConstraintEnforcer, _kimi_bare_alias_registry_default


@pytest.fixture(scope="module")
def enforcer() -> ConstraintEnforcer:
    return ConstraintEnforcer()


# ---------------------------------------------------------------------------
# Against the real (committed, static) registry — same convention as
# test_pr10_provider_string_canon.py::TestKimiKeyArgMapping.
# ---------------------------------------------------------------------------


class TestBareKimiAliasAgainstRealRegistry:

    def test_bare_kimi_passes_registry_check(self, enforcer):
        """The exact OI-707 repro: model='kimi' (bare provider name) must now pass."""
        violations = enforcer.check_constraints(provider="kimi", model="kimi", check_registry=True)
        codes = [v.code for v in violations]
        assert "model-not-in-current-registry" not in codes, codes

    def test_bare_kimi_default_alias_passes_registry_check(self, enforcer):
        violations = enforcer.check_constraints(
            provider="kimi", model="kimi-default", check_registry=True,
        )
        codes = [v.code for v in violations]
        assert "model-not-in-current-registry" not in codes, codes

    def test_explicit_kimi_k3_still_passes(self, enforcer):
        """Explicit model ids must keep passing unchanged (no regression)."""
        violations = enforcer.check_constraints(provider="kimi", model="kimi-k3", check_registry=True)
        codes = [v.code for v in violations]
        assert "model-not-in-current-registry" not in codes, codes

    def test_unmapped_kimi_model_still_rejected(self, enforcer):
        """A real/unmapped id must still fail — the alias fix must not weaken the gate."""
        violations = enforcer.check_constraints(
            provider="kimi", model="kimi-bogus", check_registry=True,
        )
        codes = [v.code for v in violations]
        assert "model-not-in-current-registry" in codes, codes

    def test_non_kimi_provider_not_special_cased(self, enforcer):
        """The alias normalization is kimi-only: the literal string 'kimi' as a model
        for another provider must not get a free pass."""
        violations = enforcer.check_constraints(provider="codex", model="kimi", check_registry=True)
        codes = [v.code for v in violations]
        assert "model-not-in-current-registry" in codes, codes


# ---------------------------------------------------------------------------
# Mocked registry — proves the helper reads kimi_cli.default_model dynamically
# (single source of truth), never hardcodes "kimi-k3".
# ---------------------------------------------------------------------------


class TestBareAliasHelperReadsRegistryDefaultDynamically:

    def test_returns_none_for_non_alias_model(self):
        assert _kimi_bare_alias_registry_default("kimi-bogus") is None

    def test_resolves_to_registrys_default_model_flag(self, monkeypatch):
        fake_cfg = SimpleNamespace(default_model="some-future-default")
        monkeypatch.setattr(
            "providers.constraint_enforcer._load_registry",
            lambda: {"kimi_cli": fake_cfg},
        )
        assert _kimi_bare_alias_registry_default("kimi") == "some-future-default"
        assert _kimi_bare_alias_registry_default("kimi-default") == "some-future-default"
        assert _kimi_bare_alias_registry_default("kimi_cli") == "some-future-default"

    def test_returns_none_when_registry_unavailable(self, monkeypatch):
        def _boom():
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr("providers.constraint_enforcer._load_registry", _boom)
        assert _kimi_bare_alias_registry_default("kimi") is None

    def test_returns_none_when_kimi_cli_section_missing(self, monkeypatch):
        monkeypatch.setattr("providers.constraint_enforcer._load_registry", lambda: {})
        assert _kimi_bare_alias_registry_default("kimi") is None

    def test_returns_none_when_default_model_flag_absent(self, monkeypatch):
        fake_cfg = SimpleNamespace(default_model=None)
        monkeypatch.setattr(
            "providers.constraint_enforcer._load_registry",
            lambda: {"kimi_cli": fake_cfg},
        )
        assert _kimi_bare_alias_registry_default("kimi") is None
