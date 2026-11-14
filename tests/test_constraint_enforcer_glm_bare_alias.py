"""OI-1798 - the bare provider string `glm` resolves to the `zai` registry key.

Own file because tests/test_constraint_enforcer.py is in scripts/ci/test_exclusions.txt
(OI-952), so CI Profile A would never run this regression test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(_LIB))


# ---------------------------------------------------------------------------
# OI-1798 — the bare provider string `glm` resolves to the `zai` registry key,
# so the deprecated-glm-models allowlist cannot be sidestepped. Both resolvers.
# ---------------------------------------------------------------------------

def _enforcer_classes():
    import constraint_enforcer as legacy
    import providers.constraint_enforcer as current

    return [
        pytest.param(current.ConstraintEnforcer, current._registry_key_for, id="providers"),
        pytest.param(legacy.ConstraintEnforcer, legacy._registry_key_for, id="legacy"),
    ]


@pytest.mark.parametrize("enforcer_cls,key_for", _enforcer_classes())
class TestBareGlmAlias:

    @pytest.mark.parametrize("provider", ["glm", "GLM", " Glm "])
    def test_bare_glm_resolves_to_zai(self, enforcer_cls, key_for, provider):
        assert key_for(provider, None) == "zai"

    @pytest.mark.parametrize("provider", ["glm", "GLM"])
    def test_bare_glm_old_model_blocked(self, enforcer_cls, key_for, provider):
        with pytest.raises(Exception, match="deprecated-glm-models"):
            enforcer_cls().enforce(provider=provider, model="glm-5.1")

    def test_bare_glm_allowed_model_passes(self, enforcer_cls, key_for):
        enforcer_cls().enforce(provider="glm", model="glm-5.2")

    @pytest.mark.parametrize("provider", ["glm-harness", "litellm:zai", "zai"])
    def test_existing_zai_routes_keep_outcome(self, enforcer_cls, key_for, provider):
        assert key_for(provider, None) == "zai"
        with pytest.raises(Exception, match="deprecated-glm-models"):
            enforcer_cls().enforce(provider=provider, model="glm-5.1")
