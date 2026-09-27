"""The harness-lane verdict contract is ONE object, not three copies (C6 step 3).

``gate_runner``, ``glm_gate`` and ``kimi_gate`` each used to carry their own
byte-identical copy of the verdict contract and the model/timeout/diff-cap
defaults. A copy drifts silently: an edit to one shape leaves the other two
behind and no test fails, because every module reads its own copy. The three
readers now alias the SAME objects in ``gate_lane_contract``. These tests pin
that identity — a byte-identical copy smuggled back in would be a different
object, so ``is`` fails and the copy can no longer go unnoticed.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import codex_parser
import config_registry
import config_runtime
import config_store_db
import gate_lane_contract
import gate_recorder
import gate_runner
import glm_gate
import kimi_gate
import vnx_paths
from providers import provider_registry


def test_verdict_contract_is_one_object_across_all_three_readers():
    assert glm_gate._VERDICT_CONTRACT is gate_lane_contract.VERDICT_CONTRACT
    assert kimi_gate._VERDICT_CONTRACT is gate_lane_contract.VERDICT_CONTRACT
    assert gate_runner._HARNESS_LANE_VERDICT_CONTRACT is gate_lane_contract.VERDICT_CONTRACT
    # The two gates must read the same object as each other, not merely two
    # equal aliases of the source.
    assert glm_gate._VERDICT_CONTRACT is kimi_gate._VERDICT_CONTRACT


def test_valid_verdicts_is_one_object_across_all_readers():
    """OI-1767 fix-forward (third): glm_gate/kimi_gate/codex_parser used to
    each carry their own byte-identical ``{"pass", "fail", "blocked"}``
    literal. A fourth literal copy in ``codex_parser.extract_verdict_block``
    would repeat the exact drift ``gate_lane_contract`` exists to prevent
    (see module docstring) — all readers now alias the SAME object.
    """
    assert glm_gate._VALID_VERDICTS is gate_lane_contract.VALID_VERDICTS
    assert kimi_gate._VALID_VERDICTS is gate_lane_contract.VALID_VERDICTS
    assert codex_parser.VALID_VERDICTS is gate_lane_contract.VALID_VERDICTS


def test_verdict_contract_is_not_the_codex_reviewer_template():
    # gate_runner's codex/gemini reviewer asks for a different, richer findings
    # shape. Aliasing that template here would silently change what the
    # harness-lane gates ask for, so the identity must NOT hold.
    assert gate_runner._HARNESS_LANE_VERDICT_CONTRACT is not gate_runner._REVIEWER_VERDICT_TEMPLATE


def test_both_verdict_templates_ask_for_file_path_and_line():
    # OI finding-ankers: a finding's location used to live only in prose — 0 of
    # 634 findings measured 2026-09-15 carried a structured file_path/line.
    # Red on main before this dispatch: neither template mentioned the field
    # names at all. Both readers (harness-lane gates via gate_lane_contract,
    # codex/gemini via gate_runner) must ask for the same two field names.
    for contract in (gate_lane_contract.VERDICT_CONTRACT, gate_runner._REVIEWER_VERDICT_TEMPLATE):
        assert '"file_path"' in contract
        assert '"line"' in contract


def test_both_verdict_templates_explain_when_empty_is_correct_identically():
    # The two contracts are deliberately NOT the same object (see
    # test_verdict_contract_is_not_the_codex_reviewer_template above), so
    # nothing else pins them together. Without this test, the "leave it empty
    # rather than guess" guidance can drift out of sync between the two
    # copies — precisely the failure mode this repo hit six times this week
    # with a copied config value, just on prose instead of a constant.
    guidance = (
        'file_path and line point at the ONE new line a finding is about, repo-relative. '
        'Leave file_path="" and line=0 for a finding about the PR as a whole, a missing '
        "file, or anything that does not point at a single line — a guessed line number is "
        "worse than an empty one.\n"
    )
    assert guidance in gate_lane_contract.VERDICT_CONTRACT
    assert guidance in gate_runner._REVIEWER_VERDICT_TEMPLATE


def test_model_timeout_and_diff_cap_are_one_source():
    assert gate_runner._HARNESS_LANE_MODEL is gate_lane_contract.MODEL_DEFAULTS
    assert glm_gate.DEFAULT_MODEL == gate_lane_contract.MODEL_DEFAULTS["glm_gate"][1]
    assert kimi_gate.DEFAULT_MODEL == gate_lane_contract.MODEL_DEFAULTS["kimi_gate"][1]
    assert glm_gate.DEFAULT_TIMEOUT == gate_lane_contract.TIMEOUT_SECONDS
    assert kimi_gate.DEFAULT_TIMEOUT == gate_lane_contract.TIMEOUT_SECONDS
    assert gate_runner._HARNESS_LANE_TIMEOUT_SECONDS == gate_lane_contract.TIMEOUT_SECONDS
    # OI-1874: the diff cap is no longer one shared constant -- glm_gate,
    # kimi_gate and gate_runner's harness-lane path all resolve it through the
    # SAME function (gate_lane_contract.max_diff_chars), keyed per gate, so a
    # drift between the three readers is structurally impossible even though
    # the resolved VALUE now differs by gate.
    assert glm_gate.max_diff_chars is gate_lane_contract.max_diff_chars
    assert kimi_gate.max_diff_chars is gate_lane_contract.max_diff_chars
    assert gate_runner._harness_lane_max_diff_chars is gate_lane_contract.max_diff_chars


@pytest.fixture
def isolate_config_runtime(monkeypatch):
    """kimi_gate finding (round 2): ``max_diff_chars`` reads through
    ``config_runtime.get``, whose ``autowire()`` binds ``config_registry``'s
    DB resolver to whatever project this process's environment /
    ``vnx_paths.resolve_paths()`` finds -- on this repo's own dev machine
    that is a real, already-populated ``project_config``. ``config_registry.
    get``'s precedence puts that DB layer ABOVE env vars (see
    ``config_registry.py``'s ``get()``), so deleting the env vars alone does
    not stop a stored operator value from winning. Mirror ``tests/
    test_config_runtime.py``'s own isolation: block the canonical resolver
    so autowire can never find a real store, and reset the DB layer around
    each test.

    Module-level and importable (OI-1874 r3) so any test file that dispatches
    a real ``glm_gate``/``kimi_gate`` run and needs its ``max_diff_chars``
    resolution held to a known value can request this fixture instead of
    re-deriving the same env-var/DB isolation -- see its reuse in
    ``tests/test_oi1851_truncated_gate_not_a_pass.py``.
    """
    for key, _default in gate_lane_contract.DIFF_CHAR_CONFIG.values():
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{config_registry._bare(key)}", raising=False)
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    monkeypatch.setattr(vnx_paths, "resolve_paths", lambda: {})
    config_runtime._wired_for.clear()
    config_registry.set_db_resolver(None)
    config_registry.set_default_project_id(None)
    yield
    config_runtime._wired_for.clear()
    config_registry.set_db_resolver(None)
    config_registry.set_default_project_id(None)


class TestMaxDiffChars:
    """OI-1874: the per-gate diff-char cap resolver.

    kimi_gate runs on the kimi CLI OAuth subscription and kimi-k3 carries a
    1M-token context (wave7_models.yaml), so its default is 400000 -- far
    above glm_gate's and deepseek_gate's 50000, both API-credit fallback
    seats billed per token. A missing/invalid/non-positive config value falls
    back to that SAME gate's own default -- never to unlimited, never to a
    different gate's default -- and an unknown gate name gets the
    conservative 50000.
    """

    @pytest.fixture(autouse=True)
    def _isolate_config_runtime(self, isolate_config_runtime):
        """Apply the shared isolation fixture automatically for every test in
        this class -- the class used to own this isolation directly; it now
        just requests the module-level, reusable fixture above."""

    def test_stored_project_config_value_overrides_but_other_gates_keep_defaults(
        self, tmp_path, monkeypatch
    ):
        """Proves the isolation fixture above is real, not just deleting env
        vars that a stored value never needed. Wire a real temp DB carrying a
        stored ``VNX_KIMI_GATE_MAX_DIFF_CHARS`` value -- the exact shape an
        operator sets via the dashboard -- and confirm it takes effect once
        wired, while glm_gate/deepseek_gate, which carry no stored value,
        still resolve to their OWN hardcoded defaults: never the kimi_gate
        override, and never each other's."""
        sd = tmp_path / "state"
        sd.mkdir()
        conn = sqlite3.connect(sd / "runtime_coordination.db")
        entry = config_registry.CONFIG_REGISTRY["VNX_KIMI_GATE_MAX_DIFF_CHARS"]
        config_store_db.set_config(
            conn, "some-project", "VNX_KIMI_GATE_MAX_DIFF_CHARS", "999",
            actor="op",
            approval_id="test-approval" if entry.requires_approval else None,
        )
        conn.close()
        monkeypatch.setattr(vnx_paths, "resolve_paths", lambda: {"VNX_STATE_DIR": str(sd)})
        monkeypatch.setattr(vnx_paths, "project_id_from_state_dir", lambda _sd: "some-project")

        assert gate_lane_contract.max_diff_chars("kimi_gate") == 999
        assert gate_lane_contract.max_diff_chars("glm_gate") == 50000
        assert gate_lane_contract.max_diff_chars("deepseek_gate") == 50000

    def test_defaults_differ_by_gate(self, monkeypatch):
        for key in (
            "VNX_KIMI_GATE_MAX_DIFF_CHARS", "VNX_GLM_GATE_MAX_DIFF_CHARS",
            "VNX_DEEPSEEK_GATE_MAX_DIFF_CHARS",
        ):
            monkeypatch.delenv(key, raising=False)
        assert gate_lane_contract.max_diff_chars("kimi_gate") == 400000
        assert gate_lane_contract.max_diff_chars("glm_gate") == 50000
        assert gate_lane_contract.max_diff_chars("deepseek_gate") == 50000

    def test_config_override_is_honored_per_gate(self, monkeypatch):
        monkeypatch.setenv("VNX_KIMI_GATE_MAX_DIFF_CHARS", "12345")
        monkeypatch.delenv("VNX_GLM_GATE_MAX_DIFF_CHARS", raising=False)
        assert gate_lane_contract.max_diff_chars("kimi_gate") == 12345
        # An override on one gate must never leak onto another's resolution.
        assert gate_lane_contract.max_diff_chars("glm_gate") == 50000

    def test_invalid_value_falls_back_to_that_gates_own_default(self, monkeypatch):
        monkeypatch.setenv("VNX_KIMI_GATE_MAX_DIFF_CHARS", "not-a-number")
        assert gate_lane_contract.max_diff_chars("kimi_gate") == 400000

    def test_non_positive_value_falls_back_to_that_gates_own_default(self, monkeypatch):
        monkeypatch.setenv("VNX_GLM_GATE_MAX_DIFF_CHARS", "0")
        assert gate_lane_contract.max_diff_chars("glm_gate") == 50000
        monkeypatch.setenv("VNX_GLM_GATE_MAX_DIFF_CHARS", "-100")
        assert gate_lane_contract.max_diff_chars("glm_gate") == 50000

    def test_unknown_gate_gets_the_conservative_default(self):
        assert gate_lane_contract.max_diff_chars("some_future_gate") == 50000

    def test_config_registry_carries_the_same_defaults(self):
        # gate_lane_contract.DIFF_CHAR_CONFIG and config_registry.CONFIG_REGISTRY
        # carry the SAME default per gate in two places (a config key needs a
        # registered entry for the dashboard/UI; the resolver needs its own
        # fallback) -- this pins them from drifting apart.
        import config_registry

        for gate, (key, default) in gate_lane_contract.DIFF_CHAR_CONFIG.items():
            entry = config_registry.CONFIG_REGISTRY.get(key)
            assert entry is not None, f"{key} (for {gate}) is missing from CONFIG_REGISTRY"
            assert int(entry.default) == default, f"{key} default drifted from DIFF_CHAR_CONFIG"


def _run_gate_capturing_prompt(module, gate: str, tmp_path, monkeypatch, *, diff: str, pr: str):
    """Drive ``module.main()`` end to end (like ``glm_gate``/``kimi_gate``'s own
    CLI entrypoint) and hand back both the recorded terminal result AND the
    literal prompt text the governed dispatcher received — the only way to
    check the record's ``diff_truncated``/``diff_limit`` against what the
    model actually saw, rather than trusting the two agree.
    """
    data_dir = tmp_path / f"data-{gate}"
    captured: dict = {}

    def _make(*_a, **_k):
        def _dispatch(provider, model_arg, instruction, dispatch_id):
            captured["prompt"] = instruction
            reports = data_dir / "unified_reports"
            reports.mkdir(parents=True, exist_ok=True)
            report = (
                "Reviewed the diff.\nNo issues found.\n\n"
                "```json\n"
                '{"verdict": "pass", "findings": [], "residual_risk": null}\n'
                "```\n"
            )
            (reports / f"{dispatch_id}.md").write_text(report, encoding="utf-8")
            return report
        return _dispatch

    monkeypatch.setattr(module, "_get_diff", lambda pr_arg, diff_file: diff)
    monkeypatch.setattr(module, "_make_default_dispatcher", _make)
    monkeypatch.setattr(module, "get_pr_head_branch", lambda pr_number: "feature/oi1874-r3")
    monkeypatch.setattr(module, "get_pr_head_sha", lambda pr_number: "cafedeadbeef")
    module.main(["--pr", pr, "--data-dir", str(data_dir)])
    results_dir = data_dir / "state" / "review_gates" / "results"
    record = json.loads((results_dir / f"pr-{pr}-{gate}.json").read_text(encoding="utf-8"))
    return record, captured["prompt"]


def test_resolver_read_once_per_run_prompt_and_recorded_cap_never_disagree(
    tmp_path, monkeypatch, isolate_config_runtime,
):
    """OI-1874 r3 regression: ``glm_gate.main()``/``kimi_gate.main()`` used to
    call ``gate_lane_contract.max_diff_chars`` TWICE per run — once (via
    ``gate_depth.diff_coverage``) to compute the ``execution_depth`` recorded
    on the terminal result, and again inside ``_build_prompt`` to build the
    actual prompt handed to the model. ``max_diff_chars`` reads through
    ``config_runtime.get``, which can legitimately return a different value
    on the second call if a project-config write (or an operator override)
    races the run — the two calls are not guaranteed to agree just because
    they happen a few lines apart in the same process.

    Simulate exactly that race: monkeypatch each gate's OWN ``max_diff_chars``
    alias (the object identity ``test_model_timeout_and_diff_cap_are_one_source``
    pins above) to hand back a LOW cap on its first call and a much higher one
    on any further call, over a diff sized to sit strictly between the two.
    Red on the pre-fix code (two calls: coverage sees the low cap and books
    ``diff_truncated=True``, but the prompt is then built with the high cap
    and is NOT actually truncated — the assertion below catches exactly that
    disagreement). Green once each gate resolves the cap exactly once per run
    and reuses the same value for both the coverage record and the prompt.
    """
    monkeypatch.delenv("VNX_GLM_GATE_MODEL", raising=False)
    monkeypatch.delenv("VNX_KIMI_GATE_MODEL", raising=False)

    from gate_prompt import TRUNCATION_NOTICE

    low_cap = 2000
    high_cap = 999_000
    # Sized so a resolution at low_cap truncates it and a resolution at
    # high_cap does not — the only way the two caps produce different prompts.
    head = "diff --git a/scripts/head.py b/scripts/head.py\n" + "+x = 1\n" * 50
    tail_line = "+y = 2\n"
    tail = "diff --git a/scripts/tail.py b/scripts/tail.py\n" + tail_line * (
        (low_cap // len(tail_line)) + 100
    )
    diff = head + tail
    assert len(head) < low_cap < len(diff.strip()) < high_cap

    for module, gate in ((glm_gate, "glm_gate"), (kimi_gate, "kimi_gate")):
        calls = iter([low_cap, high_cap, high_cap, high_cap])
        monkeypatch.setattr(module, "max_diff_chars", lambda _gate, _calls=calls: next(_calls))

        record, prompt = _run_gate_capturing_prompt(
            module, gate, tmp_path, monkeypatch, diff=diff, pr="1874",
        )
        depth = record["execution_depth"]
        prompt_was_truncated = TRUNCATION_NOTICE in prompt

        assert prompt_was_truncated == depth["diff_truncated"], (
            f"{gate}: the prompt's actual truncation ({prompt_was_truncated}) "
            f"disagrees with the recorded diff_truncated ({depth['diff_truncated']}) "
            "-- max_diff_chars was read more than once for this run and the "
            "resolver's return value changed between the two calls"
        )
        if prompt_was_truncated:
            assert depth["diff_limit"] == low_cap


def test_model_env_var_names_come_from_the_shared_map():
    assert glm_gate._MODEL_ENV == "VNX_GLM_GATE_MODEL"
    assert kimi_gate._MODEL_ENV == "VNX_KIMI_GATE_MODEL"


def _harness_lane_gates():
    """The set of harness-lane gates, DERIVED from the single registry.

    ``gate_recorder.GATE_PROVIDERS`` is the one place that says which gates are
    harness lanes; ``gate_lane_contract.MODEL_DEFAULTS`` is the second table
    that must carry the model for each of them. Deriving the expectation here
    (instead of hardcoding a list of three names) means a fourth harness gate
    added to ``GATE_PROVIDERS`` is automatically demanded of ``MODEL_DEFAULTS``
    by the same test that caught ``deepseek_gate`` — the problem can no longer
    shift into the test.
    """
    return {
        gate
        for gate, (kind, _name) in gate_recorder.GATE_PROVIDERS.items()
        if kind == gate_recorder.GATE_PROVIDER_HARNESS_LANE
    }


def test_every_harness_lane_gate_has_a_model_default():
    """Every harness-lane gate must have a MODEL_DEFAULTS rule with a model.

    The two tables carry one fact in two places: ``GATE_PROVIDERS`` says which
    gates route through the governed dispatcher, ``MODEL_DEFAULTS`` says which
    model they dispatch on. When a harness gate is missing from the second
    table, ``gate_runner._run_harness_lane_path`` falls back to
    ``("", "")`` and hands ``model=""`` to the dispatcher. This is exactly the
    OI-1714 gap that let ``deepseek_gate`` reach the lane with no model.
    """
    harness_gates = _harness_lane_gates()
    assert harness_gates, (
        "GATE_PROVIDERS has no harness-lane gates — the derivation is empty, "
        "so this test would pass vacuously"
    )
    missing = sorted(
        gate for gate in harness_gates if gate not in gate_lane_contract.MODEL_DEFAULTS
    )
    assert not missing, (
        "every harness-lane gate in GATE_PROVIDERS must have a MODEL_DEFAULTS "
        f"entry, or gate_runner hands model='' to the dispatcher: {missing}"
    )
    empty_models = sorted(
        gate
        for gate in harness_gates
        if not (gate_lane_contract.MODEL_DEFAULTS.get(gate, ("", ""))[1] or "").strip()
    )
    assert not empty_models, (
        "a MODEL_DEFAULTS entry whose default model is empty is the same defect "
        f"as a missing entry — model='' reaches the dispatcher: {empty_models}"
    )


def test_model_defaults_cover_only_harness_lane_gates():
    """A MODEL_DEFAULTS rule for a gate that is not a harness lane is dead.

    ``gate_runner`` only reads ``MODEL_DEFAULTS`` from
    ``_run_harness_lane_path``, which is reached exclusively for harness-lane
    gates. An entry keyed on a path-binary gate (codex_gate, gemini_review) or
    a renamed gate could never fire, and would be the inverse drift of the
    missing-entry case: a rule nobody consumes while a real gate silently runs
    model-less.
    """
    harness_gates = _harness_lane_gates()
    orphan = sorted(set(gate_lane_contract.MODEL_DEFAULTS) - harness_gates)
    assert not orphan, (
        "a MODEL_DEFAULTS entry for a gate that is not a harness lane in "
        "GATE_PROVIDERS is dead config — gate_runner never reads it: {orphan}"
    )


def _registry_model_keys():
    """Every model key wave7_models.yaml carries, DERIVED via the lane's loader.

    ``provider_registry.load()`` is the ADR-036 loader the provider lane uses
    to resolve a model name against wave7_models.yaml. Collecting the model
    keys of every provider section means the expectation here is read from the
    registry, never hardcoded: a model key added or renamed in the yaml changes
    this set, so the linkage can never drift into a second copy of the truth.
    """
    keys = set()
    for cfg in provider_registry.load().values():
        keys.update((cfg.models or {}).keys())
    return keys


def test_model_defaults_exist_in_wave7_registry():
    """Every MODEL_DEFAULTS model must be a key in wave7_models.yaml.

    MODEL_DEFAULTS is the table gate_runner hands to the harness-lane
    dispatcher as ``model=``; wave7_models.yaml is the registry that lane
    resolves the name against. The two carry one fact in two places: rename a
    model key in the registry and the gate silently dispatches a name the lane
    no longer knows (OI-1727). Both sides are derived — the names to check from
    MODEL_DEFAULTS itself, the expectation from the registry — so neither list
    is hardcoded in this test.
    """
    registry_keys = _registry_model_keys()
    assert registry_keys, (
        "provider_registry.load() returned no model keys — the derivation is "
        "empty, so this test would pass vacuously"
    )
    models = [model for _env_var, model in gate_lane_contract.MODEL_DEFAULTS.values()]
    assert models, (
        "MODEL_DEFAULTS is empty — there are no gate defaults to check, so "
        "this test would pass vacuously"
    )
    missing = sorted(model for model in models if model not in registry_keys)
    assert not missing, (
        "every MODEL_DEFAULTS model must be a key in wave7_models.yaml, or the "
        f"harness lane dispatches a name the registry does not know: {missing}"
    )
