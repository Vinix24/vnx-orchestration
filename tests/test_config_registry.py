#!/usr/bin/env python3
"""Tests for config_registry — the operator config SSOT (P0 config control-plane foundation).

Dispatch-ID: 20260627-config-registry

Covers the flag inventory (defaults must MIRROR the current read-site fallbacks), the resolution
precedence chain (override > DB > env > default), and the behaviour-preserving guarantee: with no
override / no DB layer / no env, get() returns exactly the current-code default.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import config_registry as cr  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # Drop any inherited VNX_* / overrides so tests resolve against the registry, and reset the DB layer.
    for k in list(cr.CONFIG_REGISTRY):
        monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{cr._bare(k)}", raising=False)
    cr.set_db_resolver(None)
    yield
    cr.set_db_resolver(None)


# ---------------------------------------------------------------------------
# Inventory: defaults mirror the current code (codex finding #2)
# ---------------------------------------------------------------------------

def test_ci_gate_activated_wiring_gate_stays_parked():
    # OI-1385: VNX_CI_GATE_REQUIRED activated (default "0" -> "1", status PARK -> ACTIVATE).
    # Its 5-read-site chain was already proven live on PR #1628 (ci_gate ran, status=pass,
    # contract_hash+report_path present, commit_sha matched the PR head), and the
    # obligation-runner's bounded-pending handling for a temporarily-unavailable gate
    # (OI-1384 #1633, OI-1400 #1641/#1672) is confirmed on main before this flip.
    assert cr.CONFIG_REGISTRY["VNX_CI_GATE_REQUIRED"].default == "1"
    assert cr.CONFIG_REGISTRY["VNX_CI_GATE_REQUIRED"].status == "ACTIVATE"
    # VNX_WIRING_GATE_REQUIRED stays PARKed -- a deliberate decision (closure_verifier cannot
    # attest wiring_gate's evidence yet), pinned by
    # test_wiring_gate_shadow_mode_decision_tied_to_closure_verifier_exclusion below.
    assert cr.CONFIG_REGISTRY["VNX_WIRING_GATE_REQUIRED"].default == "0"
    assert cr.CONFIG_REGISTRY["VNX_WIRING_GATE_REQUIRED"].status == "PARK"


def test_evidence_bound_gate_default_advisory_and_requires_approval():
    entry = cr.CONFIG_REGISTRY["VNX_EVIDENCE_BOUND_GATE"]
    assert entry.default == "advisory"
    assert entry.type == "enum"
    assert entry.requires_approval is True


def test_feature_toggles_default_off_and_provider_deepseek():
    assert cr.CONFIG_REGISTRY["VNX_SCOUT_PREPASS"].default == "0"
    assert cr.CONFIG_REGISTRY["VNX_TAGGER_ENABLED"].default == "0"
    assert cr.CONFIG_REGISTRY["VNX_TAGGER_PROVIDER"].default == "deepseek"
    assert cr.CONFIG_REGISTRY["VNX_USE_CENTRAL_DB"].default == ""


def test_gate_and_autopilot_require_approval():
    assert cr.CONFIG_REGISTRY["VNX_CI_GATE_REQUIRED"].requires_approval is True
    assert cr.CONFIG_REGISTRY["VNX_WIRING_GATE_REQUIRED"].requires_approval is True
    assert cr.CONFIG_REGISTRY["VNX_EVIDENCE_BOUND_GATE"].requires_approval is True
    assert cr.CONFIG_REGISTRY["VNX_ROADMAP_AUTOPILOT"].requires_approval is True
    # fail-safe intelligence toggles do not require approval
    assert cr.CONFIG_REGISTRY["VNX_SCOUT_PREPASS"].requires_approval is False


def test_federation_is_planned_and_not_writable():
    fed = cr.CONFIG_REGISTRY["VNX_USE_FEDERATION"]
    assert fed.planned is True
    assert fed.writable_from_ui is False


def test_central_db_is_env_only_routing():
    # VNX_USE_CENTRAL_DB is a process-start routing decision, surfaced read-only — never UI-writable
    # (live-toggling would split reads across DBs mid-process).
    assert cr.CONFIG_REGISTRY["VNX_USE_CENTRAL_DB"].writable_from_ui is False


# ---------------------------------------------------------------------------
# Resolution precedence
# ---------------------------------------------------------------------------

def test_default_when_nothing_set():
    assert cr.get("VNX_SCOUT_PREPASS") == "0"  # = current-code behaviour
    assert cr.get_bool("VNX_SCOUT_PREPASS") is False


def test_env_overrides_default():
    import os
    os.environ["VNX_SCOUT_PREPASS"] = "1"
    try:
        assert cr.get("VNX_SCOUT_PREPASS") == "1"
        assert cr.get_bool("VNX_SCOUT_PREPASS") is True
    finally:
        del os.environ["VNX_SCOUT_PREPASS"]


def test_override_beats_env(monkeypatch):
    monkeypatch.setenv("VNX_SCOUT_PREPASS", "1")
    monkeypatch.setenv("VNX_OVERRIDE_SCOUT_PREPASS", "0")
    assert cr.get("VNX_SCOUT_PREPASS") == "0"  # the emergency brake wins


def test_db_layer_beats_env_but_loses_to_override(monkeypatch):
    monkeypatch.setenv("VNX_SCOUT_PREPASS", "0")
    cr.set_db_resolver(lambda pid, key: "1" if key == "VNX_SCOUT_PREPASS" else None)
    assert cr.get("VNX_SCOUT_PREPASS") == "1"          # DB beats env
    monkeypatch.setenv("VNX_OVERRIDE_SCOUT_PREPASS", "0")
    assert cr.get("VNX_SCOUT_PREPASS") == "0"           # override beats DB


def test_db_resolver_error_falls_through(monkeypatch):
    def _boom(pid, key):
        raise RuntimeError("db down")
    cr.set_db_resolver(_boom)
    # DB error must not raise — falls through to the default.
    assert cr.get("VNX_SCOUT_PREPASS") == "0"


def test_unknown_key_returns_none():
    assert cr.get("VNX_NOT_A_REAL_FLAG") is None


# ---------------------------------------------------------------------------
# all_effective
# ---------------------------------------------------------------------------

def test_all_effective_marks_defaults_and_planned():
    rows = {r["key"]: r for r in cr.all_effective()}
    assert rows["VNX_SCOUT_PREPASS"]["is_default"] is True
    assert rows["VNX_USE_FEDERATION"]["planned"] is True
    assert rows["VNX_CI_GATE_REQUIRED"]["requires_approval"] is True


def test_all_effective_reflects_env(monkeypatch):
    monkeypatch.setenv("VNX_TAGGER_ENABLED", "1")
    rows = {r["key"]: r for r in cr.all_effective()}
    assert rows["VNX_TAGGER_ENABLED"]["value"] == "1"
    assert rows["VNX_TAGGER_ENABLED"]["is_default"] is False


# ---------------------------------------------------------------------------
# Subsystem cockpit metadata (framework-status-audit-and-cockpit PR-1)
# ---------------------------------------------------------------------------

def test_every_registry_entry_has_subsystem_and_status():
    for key, entry in cr.CONFIG_REGISTRY.items():
        assert entry.subsystem, f"{key} is missing a subsystem"
        assert entry.status in cr.ALLOWED_STATUSES, f"{key} has invalid status {entry.status!r}"


def test_all_effective_includes_subsystem_and_status():
    for row in cr.all_effective():
        assert row["subsystem"], f"{row['key']} all_effective() row is missing subsystem"
        assert row["status"] in cr.ALLOWED_STATUSES, (
            f"{row['key']} all_effective() row has invalid status {row['status']!r}"
        )


def test_config_registry_subsystems_disjoint_from_flag_backed_subsystems():
    flag_backed = {entry.subsystem for entry in cr.CONFIG_REGISTRY.values()}
    flag_less = set(cr.CONFIG_REGISTRY_SUBSYSTEMS)
    assert not (flag_backed & flag_less), "a subsystem name is represented as both flag-backed and flag-less"


def test_config_registry_subsystems_have_status_and_description():
    for name, meta in cr.CONFIG_REGISTRY_SUBSYSTEMS.items():
        assert meta.get("status") in cr.ALLOWED_STATUSES, f"{name} has invalid status"
        assert meta.get("description"), f"{name} is missing a description"


# ---------------------------------------------------------------------------
# PR-2: net-new subsystem flags (display metadata only)
# ---------------------------------------------------------------------------

PR2_NEW_FLAGS = (
    "VNX_GOVERNANCE_ENFORCED",
    "VNX_LEARNING_LOOP_ENABLED",
    "VNX_DREAM_SCHEDULER_ENABLED",
    "VNX_INJECTION_FEEDBACK_ENABLED",
    "VNX_PLAN_GATE_COMPLEX_ONLY",
    "VNX_HASH_CHAIN_REQUIRED",
    "VNX_ATTESTATION_REQUIRED",
    "VNX_MIGRATION_SYSTEM",
)


def test_pr2_new_flags_exist_default_off():
    for key in PR2_NEW_FLAGS:
        entry = cr.CONFIG_REGISTRY[key]
        if key == "VNX_MIGRATION_SYSTEM":
            assert entry.default == "manifest"
            assert entry.type == "enum"
        else:
            assert entry.default == "0", f"{key} must default off"
            assert entry.type == "bool"


def test_pr2_approval_flags_require_approval():
    for key in ("VNX_GOVERNANCE_ENFORCED", "VNX_HASH_CHAIN_REQUIRED", "VNX_ATTESTATION_REQUIRED"):
        assert cr.CONFIG_REGISTRY[key].requires_approval is True


def test_pr2_non_approval_flags_do_not_require_approval():
    for key in (
        "VNX_LEARNING_LOOP_ENABLED", "VNX_DREAM_SCHEDULER_ENABLED",
        "VNX_INJECTION_FEEDBACK_ENABLED", "VNX_PLAN_GATE_COMPLEX_ONLY",
    ):
        assert cr.CONFIG_REGISTRY[key].requires_approval is False


def test_pr2_migration_system_is_read_only():
    entry = cr.CONFIG_REGISTRY["VNX_MIGRATION_SYSTEM"]
    assert entry.writable_from_ui is False
    assert entry.default == "manifest"


def test_pr2_new_flags_have_correct_subsystem_and_status():
    expected = {
        "VNX_GOVERNANCE_ENFORCED": ("governance-enforcement-stack", "PARK"),
        "VNX_LEARNING_LOOP_ENABLED": ("intelligence-self-learning-loop", "ACTIVATE"),
        "VNX_DREAM_SCHEDULER_ENABLED": ("dream-consolidation", "ACTIVATE"),
        "VNX_INJECTION_FEEDBACK_ENABLED": ("injection-effectiveness-eval-loop", "ACTIVATE"),
        # "plan-gate-task-class-scope" — matches the docs/core/SUBSYSTEMS.md
        # seed row (§5 of the PRD), distinct from "plan-gate-panel"
        # (VNX_PLAN_GATE_ENFORCE) below. Fixed in
        # framework-status-audit-and-cockpit PR-3 so the cockpit generator has
        # exactly one canonical flag per ledger row.
        "VNX_PLAN_GATE_COMPLEX_ONLY": ("plan-gate-task-class-scope", "SCOPE"),
        "VNX_HASH_CHAIN_REQUIRED": ("receipt-hash-chain", "PARK"),
        "VNX_ATTESTATION_REQUIRED": ("signed-attestation", "PARK"),
        "VNX_MIGRATION_SYSTEM": ("migration-mechanisms", "PARK"),
    }
    for key, (subsystem, status) in expected.items():
        entry = cr.CONFIG_REGISTRY[key]
        assert entry.subsystem == subsystem, key
        assert entry.status == status, key


def test_pr2_does_not_duplicate_already_registered_flags():
    # VNX_EVIDENCE_BOUND_GATE and VNX_PLAN_GATE_ENFORCE predate PR-2 (PR-1 backfilled their
    # subsystem/status). PR-2 must not re-register them or alter their existing metadata.
    assert cr.CONFIG_REGISTRY["VNX_EVIDENCE_BOUND_GATE"].subsystem == "evidence-bound-gate"
    assert cr.CONFIG_REGISTRY["VNX_EVIDENCE_BOUND_GATE"].status == "PARK"
    assert cr.CONFIG_REGISTRY["VNX_PLAN_GATE_ENFORCE"].subsystem == "plan-gate-panel"
    assert cr.CONFIG_REGISTRY["VNX_PLAN_GATE_ENFORCE"].status == "SCOPE"
    for key in PR2_NEW_FLAGS:
        assert key not in ("VNX_EVIDENCE_BOUND_GATE", "VNX_PLAN_GATE_ENFORCE")


def test_pr2_registering_flags_does_not_change_effective_value_when_unset():
    # Metadata-only guarantee (§2.1): registering a flag must not change what get()/get_bool()
    # resolve to when nothing overrides it — no read-site behaviour change.
    for key in PR2_NEW_FLAGS:
        entry = cr.CONFIG_REGISTRY[key]
        assert cr.get(key) == entry.default


# ---------------------------------------------------------------------------
# OI-1385: canonical_flags() — explicit, order-independent canonical selection
# ---------------------------------------------------------------------------
#
# Before this dispatch, the cockpit picked a subsystem's canonical flag by
# taking whichever CONFIG_REGISTRY entry a dict iteration happened to visit
# LAST. That let VNX_GOVERNANCE_ENFORCED (zero production read-sites) win
# over VNX_CI_GATE_REQUIRED (5 read-sites, hard-blocking) purely because it
# was appended later in the file. These tests pin the replacement rule
# against synthetic registries so the mechanism is tested in isolation from
# the 17+ real subsystems.


def _entry(key, *, subsystem, read_site_wired=True, cockpit_canonical=False, default="0"):
    return cr.ConfigEntry(
        key=key, type="bool", default=default, category="gate", description=key,
        writable_from_ui=True, requires_approval=False,
        subsystem=subsystem, status="PARK",
        read_site_wired=read_site_wired, cockpit_canonical=cockpit_canonical,
    )


def test_canonical_flags_single_wired_candidate_wins_automatically():
    """A subsystem with exactly one read_site_wired entry needs no explicit
    cockpit_canonical marker -- it wins by construction, matching every
    single-flag subsystem in the real registry today."""
    registry = {"A": _entry("A", subsystem="sub")}
    assert cr.canonical_flags(registry) == {"sub": "A"}


def test_canonical_flags_raises_on_two_marked_candidates():
    """Two entries in the same subsystem both marked cockpit_canonical=True
    is an unresolved ambiguity -- must fail loud, not silently pick one."""
    registry = {
        "A": _entry("A", subsystem="sub", cockpit_canonical=True),
        "B": _entry("B", subsystem="sub", cockpit_canonical=True),
    }
    with pytest.raises(ValueError, match="2 read_site_wired candidates"):
        cr.canonical_flags(registry)


def test_canonical_flags_raises_on_zero_marked_candidates():
    """Two wired entries in the same subsystem, NEITHER marked
    cockpit_canonical=True, is equally unresolved -- must fail loud rather
    than default to dict order."""
    registry = {
        "A": _entry("A", subsystem="sub"),
        "B": _entry("B", subsystem="sub"),
    }
    with pytest.raises(ValueError, match="0 marked cockpit_canonical"):
        cr.canonical_flags(registry)


def test_canonical_flags_raises_when_all_candidates_unwired():
    """A subsystem where every entry has read_site_wired=False has no
    eligible candidate at all -- must fail loud, not fall back to showing an
    unwired flag's static value."""
    registry = {
        "A": _entry("A", subsystem="sub", read_site_wired=False),
        "B": _entry("B", subsystem="sub", read_site_wired=False),
    }
    with pytest.raises(ValueError, match="no read_site_wired"):
        cr.canonical_flags(registry)


def test_canonical_flags_excludes_unwired_entry_structurally():
    """read_site_wired=False excludes a candidate via its own property, not
    a name check -- with one wired + one unwired entry, the wired one wins
    with no cockpit_canonical marker needed at all (the unwired entry is
    simply not in the running)."""
    registry = {
        "WIRED": _entry("WIRED", subsystem="sub", read_site_wired=True),
        "UNWIRED": _entry("UNWIRED", subsystem="sub", read_site_wired=False),
    }
    assert cr.canonical_flags(registry) == {"sub": "WIRED"}


def test_canonical_flags_result_is_independent_of_dict_insertion_order():
    """T0 follow-up (23-08): the SAME two candidate entries in reversed
    insertion order must resolve to the SAME canonical flag. Before OI-1385,
    _canonical_flags picked "whichever entry is last in CONFIG_REGISTRY's
    dict order" -- reversing the order silently flipped the winner. This is
    exactly what nearly happened for real: a parallel PR adding
    VNX_DEFAULT_REVIEW_STACK to governance-enforcement-stack landed it BEFORE
    the other three entries, so VNX_GOVERNANCE_ENFORCED stayed canonical only
    because of where the new line was pasted -- had it landed after, the
    cockpit would have shown a comma-separated stack string as the whole
    afdwinglaag's "effective value".
    """
    forward = {
        "A": _entry("A", subsystem="sub", cockpit_canonical=True),
        "B": _entry("B", subsystem="sub"),
    }
    reversed_order = {
        "B": _entry("B", subsystem="sub"),
        "A": _entry("A", subsystem="sub", cockpit_canonical=True),
    }
    assert list(reversed_order.keys()) == ["B", "A"], "fixture must actually differ in order"
    winner_forward = cr.canonical_flags(forward)["sub"]
    winner_reversed = cr.canonical_flags(reversed_order)["sub"]
    assert winner_forward == winner_reversed == "A", (
        "canonical selection must not depend on CONFIG_REGISTRY iteration order"
    )


# ---------------------------------------------------------------------------
# OI-1385: canonical_flags() against the REAL registry
# ---------------------------------------------------------------------------


def test_real_registry_canonical_flags_does_not_raise():
    """The real CONFIG_REGISTRY must satisfy canonical_flags()'s invariant
    (every subsystem has exactly one eligible winner) -- this is what keeps
    dashboard/api_subsystems.py's build_rows() from 500ing in production."""
    result = cr.canonical_flags()
    assert result, "canonical_flags() returned nothing for the real registry"


def test_real_registry_governance_stack_canonical_is_ci_gate_not_governance_enforced():
    """The fix's actual behaviour change: VNX_CI_GATE_REQUIRED (5 read-sites,
    hard-blocking) is now canonical for governance-enforcement-stack, not
    VNX_GOVERNANCE_ENFORCED (0 read-sites)."""
    result = cr.canonical_flags()
    assert result["governance-enforcement-stack"] == "VNX_CI_GATE_REQUIRED"


def test_real_registry_other_multi_flag_subsystems_unchanged():
    """Bewijs item 3: 'de bestaande rijen voor andere subsystemen veranderen
    niet'. These are the OTHER multi-flag subsystems in the real registry
    (intelligence-self-learning-loop: 6 flags, injection-effectiveness-eval-
    loop: 2, smart-router-staging: 5) -- their canonical winner must match
    what docs/core/SUBSYSTEMS.md already has committed (the pre-OI-1385
    last-wins outcome), unaffected by this fix."""
    result = cr.canonical_flags()
    assert result["intelligence-self-learning-loop"] == "VNX_LEARNING_LOOP_ENABLED"
    assert result["injection-effectiveness-eval-loop"] == "VNX_INJECTION_WHY_ENABLED"
    assert result["smart-router-staging"] == "VNX_SMART_ROUTER_CANARY_PCT"


def test_real_registry_governance_enforced_can_never_be_canonical():
    entry = cr.CONFIG_REGISTRY["VNX_GOVERNANCE_ENFORCED"]
    assert entry.read_site_wired is False
    result = cr.canonical_flags()
    assert result["governance-enforcement-stack"] != "VNX_GOVERNANCE_ENFORCED"


# ---------------------------------------------------------------------------
# OI-1385: read_site_wired must track MEASURED production usage, not a claim
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXCLUDED_PARTS = frozenset({"tests", "__tests__", ".git", ".vnx-data", "docs", "node_modules"})
_SWEPT_SUFFIXES = frozenset({".py", ".sh", ".ts", ".tsx", ".js"})


def _production_read_sites(flag_name: str) -> list:
    """Repo-wide sweep for `flag_name`, excluding test dirs, docs, and
    config_registry.py's own definition -- mirrors the manual sweep the
    OI-1385 dispatch measured by hand (23-08), so it can be re-run as a live
    drift guard instead of trusting a one-time claim."""
    hits = []
    for path in _REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in _SWEPT_SUFFIXES:
            continue
        rel = path.relative_to(_REPO_ROOT)
        # Exclude on the path RELATIVE to the repo root, not the absolute path — this worktree
        # itself lives under a ".vnx-data/worktrees/..." directory in the outer repo, so an
        # absolute-path check would match ".vnx-data" on every single file and sweep nothing.
        if any(part in _EXCLUDED_PARTS for part in rel.parts):
            continue
        if path.name == "config_registry.py":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if flag_name in text:
            hits.append(str(rel))
    return sorted(hits)


def test_read_site_sweep_instrument_finds_known_hits_for_ci_gate_required():
    """Sanity-check the sweep itself (OI-1385 dispatch requirement): a sweep
    that returns zero for a flag with KNOWN production read-sites means the
    instrument is broken, not that the flag is unwired. A nul-telling is
    first a meetfout."""
    hits = _production_read_sites("VNX_CI_GATE_REQUIRED")
    assert hits, "sweep instrument broken: VNX_CI_GATE_REQUIRED has known production read-sites"
    # the 5 read-sites the dispatch cited by name
    for expected in (
        "scripts/review_gate_manager.py",
        "scripts/lib/gate_request_handler.py",
        "scripts/lib/gate_report_generator.py",
        "scripts/lib/gate_recorder.py",
        "scripts/lib/gate_result_parser.py",
    ):
        assert expected in hits, f"sweep missed known read-site {expected}"


def test_governance_enforced_read_site_wired_matches_measured_usage():
    """Drift guard, symmetric in both directions (OI-1385 item 3): if
    VNX_GOVERNANCE_ENFORCED ever gains a real production read-site,
    read_site_wired must flip to True (else the cockpit stays blind to a
    flag that now matters); if it stays at zero, read_site_wired must stay
    False (else it could silently win canonical_flags() again)."""
    hits = _production_read_sites("VNX_GOVERNANCE_ENFORCED")
    entry = cr.CONFIG_REGISTRY["VNX_GOVERNANCE_ENFORCED"]
    if hits:
        assert entry.read_site_wired is True, (
            f"VNX_GOVERNANCE_ENFORCED now has production read-site(s) {hits} -- flip "
            "read_site_wired to True so it can become canonical again"
        )
    else:
        assert entry.read_site_wired is False, (
            "VNX_GOVERNANCE_ENFORCED has zero production read-sites but read_site_wired=True "
            "-- it would wrongly become eligible as the subsystem's canonical cockpit flag"
        )


# ---------------------------------------------------------------------------
# OI-1385 item 4: VNX_WIRING_GATE_REQUIRED shadow mode is a decision, tied to
# closure_verifier's own exclusion list — not an independently-drifting flag.
# ---------------------------------------------------------------------------


def test_wiring_gate_shadow_mode_decision_tied_to_closure_verifier_exclusion():
    """VNX_WIRING_GATE_REQUIRED stays PARK/'0' ONLY because wiring_gate sits
    on closure_verifier._GATES_NOT_IMPLEMENTED_BY_CLOSURE -- the closure
    verifier cannot honestly attest its evidence yet, so promoting the flag
    to required would let a gate block merges with findings nothing verifies.
    If wiring_gate is ever promoted off that list, this decision needs a
    fresh review -- fail loud instead of leaving the flag stale forever."""
    scripts_dir = str(_REPO_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import closure_verifier as cv

    assert "wiring_gate" in cv._GATES_NOT_IMPLEMENTED_BY_CLOSURE, (
        "wiring_gate's closure-verifier exclusion changed -- re-review "
        "VNX_WIRING_GATE_REQUIRED's shadow-mode decision in config_registry.py and promote "
        "it out of PARK if the closure verifier can now attest wiring_gate evidence"
    )
    entry = cr.CONFIG_REGISTRY["VNX_WIRING_GATE_REQUIRED"]
    assert entry.default == "0"
    assert entry.status == "PARK"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
