"""tests/test_beta3_e1_review_gate_chain.py — BETA3-E1 (dispatch
20260826-beta3-e1-overname-keten-en-uitputtingstoets): the review-gate
takeover CHAIN (codex_gate -> kimi_gate -> glm_gate -> deepseek_gate,
operator decision 26-08) plus the per-provider exhaustion test the chain is
built on.

Point 1 is the core of this dispatch, not the chain: measured on the live
store (26-08), NONE of the four recorded unavailable results (glm_gate x3,
kimi_gate x1) carried the exhaustion marker -- the takeover trigger had
never fired on production data. This file's per-provider table proves the
classifier CAN discriminate, per provider, a real exhaustion from everything
that merely resembles one.

Mid-build, T0 surfaced a FIFTH, freshly-measured live case
(glm-gate-pr1691-1787754901, 26-08): a genuine OpenRouter 402
(``openrouter_credits``) whose marker landed in the gate's own REPORT, not
in a lane log (none existed for that dispatch) -- so the #1683 lift, which
only ever reads the lane log, never carried it into
residual_risk/reason_detail/summary and the classifier read no_response on
a real exhaustion. ``_scan_seat_failure_text`` (gate_request_handler.py)
fixes this: SAME classifier, extended read order (record fields -> lane log
-> report), never a second marker list.

BETA3-E1c fix-forward (PR #1695, 26-08 afternoon): the codex quota markers
this file's own ``_REAL_EXHAUSTION_TEXT["codex"]`` fixture below was built
on (``insufficient_quota`` / ``exceeded your current quota``) were
AANGENOMEN on the OpenAI-API 429 JSON shape -- no live codex_gate
exhaustion record existed yet when 61ebb567 added them. Hours later codex
genuinely ran out (pr-1696-codex_gate.json, also pr-1691/1692-codex_gate.json
today, identical text): the codex CLI writes its OWN prose, not the OpenAI
API JSON body -- "You've hit your usage limit...", two words off kimi's
already-covered "reached your usage limit" ("hit" vs "reached"). Measured
against 3405a296 (pre-fix, this branch): none of the 11 markers then in
``governance_emit._LANE_EXHAUSTED_MARKERS`` matched this text, and
``_classify_review_seat_failure`` returned ``no_response`` -- reached via
``_scan_seat_failure_text``'s no-match tail, which always collapses to
``no_response`` regardless of what ``_classify_lane_log_text`` privately
labelled the text internally (see ``test_pr1696_real_codex_exhaustion_...``
below for the corrected-vs-dispatch-premise note). A new "hit your usage
limit" marker closes the gap; see ``_PR1696_REAL_CODEX_RECORD`` below for
the verbatim record and ``test_no_structural_signal_discriminates_...`` for
the point-3 investigation into a marker-free signal (finding: none exists
in the current schema).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"
for _p in (str(SCRIPTS_DIR), str(LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config_registry as cr  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    """Isolate config_registry's process-global DB-resolver/project state
    from whatever another test module left wired -- same idiom
    tests/test_config_runtime.py's ``_clean`` fixture uses. Without this, a
    resolver left wired by an earlier test in the same pytest session could
    leak an unrelated project's config into ``config_runtime.get(...)``
    here.
    """
    import config_runtime as crt
    for k in list(cr.CONFIG_REGISTRY):
        monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{cr._bare(k)}", raising=False)
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    crt._wired_for.clear()
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)
    yield
    crt._wired_for.clear()
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)


@pytest.fixture
def manager_env(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    for d in (
        state_dir / "review_gates" / "requests",
        state_dir / "review_gates" / "results",
        reports_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    return {
        "project_root": project_root,
        "data_dir": data_dir,
        "state_dir": state_dir,
        "reports_dir": reports_dir,
        "requests_dir": state_dir / "review_gates" / "requests",
        "results_dir": state_dir / "review_gates" / "results",
    }


def _make_manager():
    import review_gate_manager as rgm
    return rgm.ReviewGateManager()


def _write_result(results_dir: Path, pr_number: int, gate: str, payload: dict) -> Path:
    path = results_dir / f"pr-{pr_number}-{gate}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Point 1 -- per-provider exhaustion discrimination table.
#
# For each of the four chain providers (codex, kimi, glm, deepseek): a REAL
# exhaustion, a parse-miss, an empty response, and a generic provider error
# ("API Error: Content block not found" -- the exact text measured today,
# 26-08, on #1692/#1694). Only the first case may classify lane_exhausted;
# the other three must NOT -- the "woordenschat-waarschuwing": the marker
# list is tested on the NEGATIVE cases too, so a too-broad marker can never
# pass by construction.
# ---------------------------------------------------------------------------

_REAL_EXHAUSTION_TEXT = {
    # AANGENOMEN: OpenAI/codex 429 quota -- documented API error shape
    # (type/code "insufficient_quota"). STALE as of 26-08 afternoon: this
    # was written when "codex ran clean on PR 1693/1689 today" was still
    # true, before codex genuinely ran out hours later (pr-1696-codex_gate.json,
    # BETA3-E1c) with different, CLI-prose wording ("You've hit your usage
    # limit..."), never this JSON shape. Kept here deliberately -- a second,
    # still-plausible form of the same provider's exhaustion is realistic,
    # and this text still correctly classifies lane_exhausted via the
    # "insufficient_quota" marker. The GEMETEN counterpart is
    # ``_PR1696_REAL_CODEX_RECORD`` further down this file.
    "codex": (
        "governed codex dispatch failed: Error code: 429 - {'error': "
        "{'message': 'You exceeded your current quota, please check your "
        "plan and billing details.', 'type': 'insufficient_quota', "
        "'code': 'insufficient_quota'}}"
    ),
    # kimi 403 -- real shape, tests/test_dlv5_review_gate_takeover.py's own
    # _KIMI_LANE_EXHAUSTED_RESULT fixture.
    "kimi": (
        "governed kimi dispatch failed: Error code: 403 - {'error': "
        "{'message': 'Your account has been suspended, please contact us "
        "via api-feedback@moonshot.cn', 'type': 'access_terminated_error'}}"
    ),
    # glm/openrouter 402 -- real shape from _LANE_EXHAUSTED_MARKERS
    # ("requires more credits" / "openrouter_credits"); the actual live
    # PR-1691 case is exercised separately below via the report-fallback,
    # since its OWN residual_risk field never carries this text at all.
    "glm": (
        "governed glm dispatch failed: Error code: 402 - {'error': "
        "{'message': 'This request requires more credits, or fewer "
        "max_tokens.', 'code': 'openrouter_credits'}}"
    ),
    # deepseek 402 -- real shape, governance_emit._LANE_EXHAUSTED_MARKERS'
    # own "insufficient balance" / "insufficient_balance" entries.
    "deepseek": (
        "governed deepseek dispatch failed: Error code: 402 - {'error': "
        "{'message': 'Insufficient Balance', 'type': 'insufficient_balance'}}"
    ),
}

_GENERIC_PROVIDER_ERROR_TEXT = {
    # Measured 26-08 on #1692/#1694 (glm_gate + kimi_gate unavailable=4 in
    # the live store) -- a real, non-exhaustion API error text.
    provider: f"governed {provider} dispatch failed: API Error: Content block not found"
    for provider in ("codex", "kimi", "glm", "deepseek")
}

_PARSE_MISS_TEXT = {
    provider: (
        f"{provider} returned a 812-char report, but it contained no readable "
        f"```json``` verdict block (parse miss -- {provider} did respond)"
    )
    for provider in ("codex", "kimi", "glm", "deepseek")
}

PROVIDER_CASES = []
for _provider in ("codex", "kimi", "glm", "deepseek"):
    _gate = f"{_provider}_gate"
    PROVIDER_CASES.append((_provider, "real_exhaustion", {
        "gate": _gate, "status": "unavailable", "reason": "dispatch_error",
        "residual_risk": _REAL_EXHAUSTION_TEXT[_provider],
    }, "lane_exhausted"))
    PROVIDER_CASES.append((_provider, "parse_miss", {
        "gate": _gate, "status": "unavailable", "reason": "parse_error",
        "residual_risk": _PARSE_MISS_TEXT[_provider],
    }, "unreadable_verdict"))
    PROVIDER_CASES.append((_provider, "empty_response", {
        "gate": _gate, "status": "unavailable", "reason": "no_verdict",
    }, "no_response"))
    PROVIDER_CASES.append((_provider, "generic_provider_error", {
        "gate": _gate, "status": "unavailable", "reason": "dispatch_error",
        "residual_risk": _GENERIC_PROVIDER_ERROR_TEXT[_provider],
    }, "no_response"))


@pytest.mark.parametrize(
    "provider,case,result,expected", PROVIDER_CASES,
    ids=[f"{p}-{c}" for p, c, _r, _e in PROVIDER_CASES],
)
def test_per_provider_exhaustion_discrimination(provider, case, result, expected, manager_env, monkeypatch):
    monkeypatch.chdir(manager_env["project_root"])
    manager = _make_manager()
    actual = manager._classify_review_seat_failure(result)
    assert actual == expected, (
        f"provider={provider} case={case}: expected {expected!r}, got {actual!r} "
        f"(result={result!r})"
    )


# ---------------------------------------------------------------------------
# Point 1 (T0 live finding, glm-gate-pr1691-1787754901, 26-08): a REAL, fresh
# glm_gate exhaustion -- the OpenRouter balance ran out mid-run -- where the
# marker exists but never reaches residual_risk/reason_detail/summary
# because NO lane log exists for this dispatch (the error landed in the
# gate's own report instead). This is the glm counterpart to the kimi-403
# from OI-1452, and the first freshly-measured exhaustion whose marker
# demonstrably exists but does not arrive at the classified fields.
# ---------------------------------------------------------------------------

_PR1691_REAL_402_REPORT_EXCERPT = (
    "governed glm dispatch failed: Error code: 402 - {'error': {'message': "
    "'This request requires more credits, or fewer max_tokens. You "
    "requested up to 32000 tokens, but can only afford 7702', "
    "'code': 402, 'metadata': {'limit_source': 'openrouter_credits'}}}"
)

_PR1691_RESULT = {
    "gate": "glm_gate",
    "pr_number": 1691,
    "status": "unavailable",
    "reason": "dispatch_error",
    "dispatch_id": "glm-gate-pr1691-1787754901",
    # The EXACT measured shape: the record's own fields carry only the
    # frontmatter-derived detail, never the raw 402 body.
    "residual_risk": (
        "glm's own report frontmatter stamps this run as failed "
        "(exit_code=1, token_usage.output=2276) — provider-side outage, not "
        "a review outcome"
    ),
    "reason_detail": None,
    "summary": "glm gate: UNAVAILABLE (provider outage/no verdict — NOT a review fail)",
    "contract_hash": "",
    "provider": "glm",
    "branch": "fix/pr1691",
}


def test_pr1691_real_glm_exhaustion_classifies_no_response_without_report_fallback():
    """BEFORE this dispatch's fix: the record's own fields carry no marker
    and there is no lane log, so the classifier reads no_response on a real
    exhaustion -- exactly what T0 measured live. Proven here WITHOUT wiring
    a report_path at all (the record alone), matching the measured baseline.
    """
    from gate_request_handler import GateRequestHandlerMixin
    assert GateRequestHandlerMixin._classify_review_seat_failure(None, _PR1691_RESULT) == "no_response"


def test_pr1691_real_glm_exhaustion_classifies_lane_exhausted_via_report_fallback(manager_env, monkeypatch):
    """AFTER: with report_path pointing at the gate's own real report text
    (no lane log present for this dispatch_id -- matching the measured
    baseline exactly), the SAME classifier now reads the marker off the
    report and correctly returns lane_exhausted.
    """
    monkeypatch.chdir(manager_env["project_root"])
    report_file = manager_env["reports_dir"] / "glm-gate-pr1691-1787754901.md"
    report_file.write_text(_PR1691_REAL_402_REPORT_EXCERPT, encoding="utf-8")

    record = dict(_PR1691_RESULT, report_path=str(report_file))
    lane_log = manager_env["data_dir"] / "logs" / "conversations" / "glm-gate-pr1691-1787754901.log"
    assert not lane_log.exists(), "measured baseline: no lane log exists for this dispatch"

    manager = _make_manager()
    assert manager._classify_review_seat_failure(record) == "lane_exhausted"


# ---------------------------------------------------------------------------
# BETA3-E1c fix-forward (PR #1695, 26-08 afternoon) -- the codex marker was
# GEMETEN, not AANGENOMEN. Verbatim from
# state/review_gates/results/pr-1696-codex_gate.json (identical text also
# recorded on pr-1691/1692-codex_gate.json the same afternoon).
# ---------------------------------------------------------------------------

_PR1696_REAL_CODEX_RECORD = {
    "gate": "codex_gate",
    "pr_id": "1696",
    "pr_number": 1696,
    "status": "unavailable",
    "reason": "exit_nonzero",
    "reason_detail": (
        "Subprocess exited with code 1: You've hit your usage limit. Upgrade "
        "to Pro (https://chatgpt.com/explore/pro), visit "
        "https://chatgpt.com/codex/settings/usage to purchase more credits or "
        "try again at 8:53 PM."
    ),
    "duration_seconds": 4.162857542047277,
    "partial_output_lines": 4,
    "runner_pid": 67702,
    "killed_at": "2026-08-26T18:01:55Z",
    "summary": (
        "codex_gate UNAVAILABLE (gate did not run — exit_nonzero: "
        "Subprocess exited with code 1: You've hit your usage limit. Upgrade "
        "to Pro (https://chatgpt.com/explore/pro), visit "
        "https://chatgpt.com/codex/settings/usage to purchase more credits or "
        "try again at 8:53 PM.) — NOT a review fail"
    ),
    "contract_hash": "",
    "report_path": "",
    "blocking_findings": [],
    "advisory_findings": [],
    "required_reruns": ["codex_gate"],
    "residual_risk": "Gate exit_nonzero. Re-run required.",
    "recorded_at": "2026-08-26T18:01:55Z",
    "branch": "dispatch/20260826-beta3-f-uitval-overschrijft-verdict-niet",
    "commit_sha": "ed3bdd97caf88094a4854067beed38594dc03d59",
}


def test_pr1696_real_codex_exhaustion_classifies_lane_exhausted():
    """GEMETEN 26-08: pr-1696-codex_gate.json is codex's own CLI prose for a
    genuine usage-limit exhaustion, not the OpenAI-API JSON 429 shape the
    pre-fix markers were AANGENOMEN on.

    Correction against this dispatch's own premise: the dispatch instruction
    for this fix-forward claimed the pre-fix classification was
    'unreadable_verdict'. Measured directly against this exact record on
    3405a296 (pre-fix, before ``hit your usage limit`` was added): the
    result was actually 'no_response', not 'unreadable_verdict'. Reason:
    status='unavailable' + reason='exit_nonzero' skips straight past the
    'parse_error'/'no_verdict' branches in
    ``GateRequestHandlerMixin._classify_review_seat_failure`` into
    ``_scan_seat_failure_text``, whose no-match tail always returns
    ``('no_response', ...)`` -- even though
    ``governance_emit._classify_lane_log_text`` privately labels a no-match
    'unreadable_verdict' internally, that label never survives
    ``_scan_seat_failure_text``'s collapse to lane_exhausted/no_response.
    Both states mean "no takeover", so the dispatch's core finding (the
    chain never fired) was correct even though its state label was not.

    AFTER the "hit your usage limit" marker: lane_exhausted.
    """
    from gate_request_handler import GateRequestHandlerMixin
    result = GateRequestHandlerMixin._classify_review_seat_failure(None, _PR1696_REAL_CODEX_RECORD)
    assert result == "lane_exhausted", (
        f"expected lane_exhausted on the real codex usage-limit record, got {result!r}"
    )


def test_codex_exhausted_real_record_rolls_to_kimi_gate(manager_env, monkeypatch):
    """GEMETEN 26-08: with the real pr-1696 codex usage-limit record on
    disk, request_reviews for codex_gate must walk the takeover chain
    (BETA3-E1's codex_gate -> kimi_gate hop) to kimi_gate, carrying the real
    usage-limit detail into the annotation -- proving the chain fires on
    genuine production data, not just on the AANGENOMEN 429 fixture.
    """
    monkeypatch.chdir(manager_env["project_root"])
    pr_number = 1696
    _write_result(manager_env["results_dir"], pr_number, "codex_gate", _PR1696_REAL_CODEX_RECORD)

    manager = _make_manager()
    with _patch_governance_receipt():
        result = manager.request_reviews(
            pr_number=pr_number,
            branch="dispatch/20260826-beta3-f-uitval-overschrijft-verdict-niet",
            review_stack=["codex_gate"],
            risk_class="medium",
            changed_files=["scripts/foo.py"],
            mode="per_pr",
            dispatch_id="beta3-e1c-codex-real-takeover-test",
        )

    seat = result["requested"][0]
    assert seat["gate"] == "kimi_gate", f"expected the walk to land on kimi_gate, got {seat['gate']!r}"
    assert seat["takeover_from"] == "codex_gate"
    assert "usage limit" in seat["failure_reason"].lower(), (
        f"expected the actual usage-limit detail embedded in failure_reason: {seat['failure_reason']!r}"
    )

    on_disk_path = manager_env["requests_dir"] / f"pr-{pr_number}-kimi_gate.json"
    assert on_disk_path.exists()
    on_disk = json.loads(on_disk_path.read_text())
    assert on_disk["takeover_from"] == "codex_gate"


# ---------------------------------------------------------------------------
# BETA3-E1c point 3 -- is there a structural signal (status code, reason
# enum, provider identity) that discriminates a REAL exhaustion from a
# GENERIC provider error, independent of prose? Investigated against the
# four real failure records named in the dispatch:
#
#   pr-1691-glm_gate.json    real OpenRouter 402 exhaustion    reason="dispatch_error"
#   pr-1692-glm_gate.json    generic "Content block not found" reason="dispatch_error"
#   pr-1694-glm_gate.json    generic "Content block not found" reason="dispatch_error"
#   pr-1696-codex_gate.json  real codex usage-limit exhaustion reason="exit_nonzero"
#
# (pr-1691/1692/1694-glm_gate.json have since been overwritten on the live
# store by later gate reruns on other PRs today -- their measured content is
# what this file's own _PR1691_RESULT/_PR1691_REAL_402_REPORT_EXCERPT and
# _GENERIC_PROVIDER_ERROR_TEXT fixtures already captured from the BETA3-E1
# measurement, so the case comparison below reuses those, not a re-read of
# the now-drifted live store.)
#
# FINDING: no structural signal exists in the current schema. Two separate
# dispatch mechanisms record gate failures, and `reason` is a HOW-was-the-
# failure-detected bucket in BOTH, never a WHY:
#   - governed-API-dispatch (glm_gate.py/kimi_gate.py): reason="dispatch_error"
#     is set for EVERY governed-dispatch exception (glm_gate.py:565-583) and
#     EVERY frontmatter run_failed=True (glm_gate.py:606-608), regardless of
#     whether the underlying cause was a 402 quota body or an unrelated API
#     error. A real exhaustion and a generic error are indistinguishable at
#     this field.
#   - subprocess-kill runner (codex_gate/ci_gate/gemini_review via
#     scripts/lib/gate_recorder.py's shared EXECUTION_FAILURE_REASONS
#     frozenset: exit_nonzero/timeout/subprocess_error/network_error/
#     auth_error/...): reason="exit_nonzero" is the SAME value
#     scripts/lib/gate_executor.py:210 assigns to an entirely unrelated
#     ci_gate `gh pr checks` subprocess failure -- proving it is shared
#     infra plumbing, not a provider-specific or exhaustion-specific signal.
# No record in either mechanism carries a separate numeric HTTP status or
# error-code field; the 402/403/429/"usage limit" detail lives ONLY inside
# reason_detail/residual_risk/summary prose. A "provider" field exists but
# only on some PASS records (glm's own governed dispatch stamps it on
# success -- see pr-1696-glm_gate.json), never reliably on a failure record,
# so it cannot serve as a discriminator either.
#
# NOTHING INVENTED: no structural signal is asserted here beyond what these
# two tests lock down as actually measured. The marker list remains the
# only viable signal until a gate runner is changed to capture the
# provider's HTTP status/error code as its own field.
# ---------------------------------------------------------------------------

def test_reason_field_does_not_discriminate_glm_exhaustion_from_generic_error():
    """governed-API-dispatch mechanism: a real 402 exhaustion (GEMETEN,
    pr-1691) and a generic "Content block not found" error (GEMETEN,
    pr-1692/1694) record the IDENTICAL reason -- proving `reason` alone
    cannot pre-filter ahead of (or replace) the marker-text scan.
    """
    real_exhaustion_case = next(
        r for p, c, r, _e in PROVIDER_CASES if p == "glm" and c == "real_exhaustion"
    )
    generic_error_case = next(
        r for p, c, r, _e in PROVIDER_CASES if p == "glm" and c == "generic_provider_error"
    )
    assert real_exhaustion_case["reason"] == generic_error_case["reason"] == "dispatch_error"
    assert _PR1691_RESULT["reason"] == "dispatch_error", "the actual live pr-1691 record, same reason"


def test_reason_field_is_shared_infra_not_a_codex_specific_exhaustion_signal():
    """subprocess-kill mechanism: codex's real usage-limit exhaustion
    (GEMETEN, pr-1696) uses reason='exit_nonzero' -- the same enum value
    ci_gate's unrelated `gh pr checks` subprocess failure uses
    (scripts/lib/gate_executor.py:210), proving it is a how-was-it-detected
    bucket shared across every gate on this runner, not a why.
    """
    from gate_recorder import EXECUTION_FAILURE_REASONS
    assert _PR1696_REAL_CODEX_RECORD["reason"] == "exit_nonzero"
    assert _PR1696_REAL_CODEX_RECORD["reason"] in EXECUTION_FAILURE_REASONS, (
        "exit_nonzero is one value in a shared execution-failure enum, not a "
        "codex- or exhaustion-specific marker"
    )


# ---------------------------------------------------------------------------
# Point 2 -- ordered chain, three-step hop, full path carried in the
# annotation (not just the last jump).
# ---------------------------------------------------------------------------

def test_three_step_chain_carries_full_path_in_annotation(manager_env, monkeypatch):
    monkeypatch.chdir(manager_env["project_root"])
    manager = _make_manager()
    pr_number = 9501

    _write_result(manager_env["results_dir"], pr_number, "codex_gate", {
        "gate": "codex_gate", "pr_number": pr_number, "status": "unavailable",
        "reason": "dispatch_error",
        "residual_risk": _REAL_EXHAUSTION_TEXT["codex"],
    })
    _write_result(manager_env["results_dir"], pr_number, "kimi_gate", {
        "gate": "kimi_gate", "pr_number": pr_number, "status": "unavailable",
        "reason": "dispatch_error",
        "residual_risk": _REAL_EXHAUSTION_TEXT["kimi"],
    })

    with _patch_governance_receipt():
        result = manager.request_reviews(
            pr_number=pr_number,
            branch="fix/three-step-chain",
            review_stack=["codex_gate"],
            risk_class="medium",
            changed_files=["scripts/lib/gate_request_handler.py"],
            mode="per_pr",
            dispatch_id="beta3-e1-three-step-test",
        )

    seat = result["requested"][0]
    assert seat["gate"] == "glm_gate", f"expected the walk to land on glm_gate, got {seat['gate']!r}"
    assert seat["takeover_from"] == "kimi_gate"
    path = seat["takeover_path"]
    assert [hop["gate"] for hop in path] == ["codex_gate", "kimi_gate"], (
        f"expected the FULL walked path, not just the last hop: {path!r}"
    )
    assert "codex_gate" in seat["failure_reason"] and "kimi_gate" in seat["failure_reason"], (
        f"failure_reason must name every gate on the path, not only the last: {seat['failure_reason']!r}"
    )
    assert "insufficient_quota" in path[0]["detail"] or "429" in path[0]["detail"]
    assert "access_terminated_error" in path[1]["detail"] or "403" in path[1]["detail"]

    # On-disk: glm_gate's OWN request record also carries the full path.
    on_disk = json.loads((manager_env["requests_dir"] / f"pr-{pr_number}-glm_gate.json").read_text())
    assert [hop["gate"] for hop in on_disk["takeover_path"]] == ["codex_gate", "kimi_gate"]


# ---------------------------------------------------------------------------
# Point 3 -- deepseek_gate as the configured end-link: glm_gate exhausted
# (using the REAL pr-1691 record + report, T0's request) rolls to
# deepseek_gate, which is skipped with a named reason because E2 has not
# shipped its runner. The resulting record must be distinguishable from a
# seat that was never requested at all.
# ---------------------------------------------------------------------------

def test_glm_exhausted_real_record_rolls_to_deepseek_named_skip(manager_env, monkeypatch):
    monkeypatch.chdir(manager_env["project_root"])
    pr_number = 1691

    report_file = manager_env["reports_dir"] / "glm-gate-pr1691-1787754901.md"
    report_file.write_text(_PR1691_REAL_402_REPORT_EXCERPT, encoding="utf-8")
    _write_result(
        manager_env["results_dir"], pr_number, "glm_gate",
        dict(_PR1691_RESULT, report_path=str(report_file)),
    )

    manager = _make_manager()
    with _patch_governance_receipt():
        result = manager.request_reviews(
            pr_number=pr_number,
            branch="fix/pr1691",
            review_stack=["glm_gate"],
            risk_class="medium",
            changed_files=["scripts/glm_gate.py"],
            mode="per_pr",
            dispatch_id="beta3-e1-deepseek-skip-test",
        )

    seat = result["requested"][0]
    assert seat["gate"] == "deepseek_gate"
    assert seat["status"] == "not_executable"
    assert seat["reason"] == "gate_runner_missing"
    assert "E2" in seat["reason_detail"] or "deepseek_gate.py" in seat["reason_detail"]
    assert seat["takeover_from"] == "glm_gate"
    # The REAL 402 detail is PRESERVED in the annotation, not merely a
    # pointer back to glm_gate's own (mutable) result record.
    assert "openrouter_credits" in seat["failure_reason"] or "more credits" in seat["failure_reason"], (
        f"expected the actual 402 detail embedded in failure_reason: {seat['failure_reason']!r}"
    )

    # Distinguishable from "never requested": a real, non-empty on-disk
    # record exists for deepseek_gate, with the takeover chain intact.
    on_disk_path = manager_env["requests_dir"] / f"pr-{pr_number}-deepseek_gate.json"
    assert on_disk_path.exists()
    on_disk = json.loads(on_disk_path.read_text())
    assert on_disk["takeover_path"][0]["gate"] == "glm_gate"

    # Preservation check (T0's second finding): even if glm_gate's OWN
    # result record were later overwritten, the deepseek_gate takeover
    # record already carries its own independent copy of the reason.
    _write_result(manager_env["results_dir"], pr_number, "glm_gate", {
        "gate": "glm_gate", "pr_number": pr_number, "status": "pass",
        "contract_hash": "dd5ac45f7e84535e",
    })
    on_disk_again = json.loads(on_disk_path.read_text())
    assert "openrouter_credits" in on_disk_again["failure_reason"] or "more credits" in on_disk_again["failure_reason"], (
        "the deepseek_gate takeover record must keep carrying the original 402 "
        "detail even after glm_gate's own result record is overwritten by a "
        "later run -- an annotation that only referenced the source record "
        "would have silently lost this"
    )


# ---------------------------------------------------------------------------
# Point 2 -- the chain genuinely running empty (no next hop configured at
# all, distinct from "next hop has no runner yet"): a named terminal
# end-state, never a silent re-dispatch of the originally-requested gate.
# ---------------------------------------------------------------------------

def test_chain_runs_empty_is_named_terminal_state(manager_env, monkeypatch):
    monkeypatch.chdir(manager_env["project_root"])
    monkeypatch.setenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "kimi_gate,glm_gate")
    manager = _make_manager()
    pr_number = 9502

    _write_result(manager_env["results_dir"], pr_number, "kimi_gate", {
        "gate": "kimi_gate", "pr_number": pr_number, "status": "unavailable",
        "reason": "dispatch_error", "residual_risk": _REAL_EXHAUSTION_TEXT["kimi"],
    })
    _write_result(manager_env["results_dir"], pr_number, "glm_gate", {
        "gate": "glm_gate", "pr_number": pr_number, "status": "unavailable",
        "reason": "dispatch_error", "residual_risk": _REAL_EXHAUSTION_TEXT["glm"],
    })

    with _patch_governance_receipt():
        result = manager.request_reviews(
            pr_number=pr_number,
            branch="fix/chain-empty",
            review_stack=["kimi_gate"],
            risk_class="medium",
            changed_files=["scripts/foo.py"],
            mode="per_pr",
            dispatch_id="beta3-e1-chain-empty-test",
        )

    seat = result["requested"][0]
    assert seat["status"] == "chain_exhausted"
    assert seat["reason"] == "takeover_chain_exhausted"
    assert [hop["gate"] for hop in seat["takeover_path"]] == ["kimi_gate", "glm_gate"]
    assert seat["failure_reason"].strip() != ""

    marker_file = manager_env["results_dir"] / f"pr-{pr_number}-kimi_gate-chain-exhausted.json"
    assert marker_file.exists()
    # Never overwrote kimi_gate's or glm_gate's OWN result records.
    kimi_own = json.loads((manager_env["results_dir"] / f"pr-{pr_number}-kimi_gate.json").read_text())
    assert kimi_own["status"] == "unavailable"
    glm_own = json.loads((manager_env["results_dir"] / f"pr-{pr_number}-glm_gate.json").read_text())
    assert glm_own["status"] == "unavailable"


# ---------------------------------------------------------------------------
# Point 2 -- unreadable_verdict / no_response never progress the chain
# (control, beyond dlv5's kimi-only coverage: glm and codex too).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("gate,successor", [("codex_gate", "kimi_gate"), ("glm_gate", "deepseek_gate")])
def test_unreadable_verdict_never_takes_over(gate, successor, manager_env, monkeypatch):
    monkeypatch.chdir(manager_env["project_root"])
    manager = _make_manager()
    pr_number = 9503

    _write_result(manager_env["results_dir"], pr_number, gate, {
        "gate": gate, "pr_number": pr_number, "status": "unavailable",
        "reason": "parse_error", "residual_risk": "no readable verdict block (parse miss)",
    })

    with _patch_governance_receipt():
        result = manager.request_reviews(
            pr_number=pr_number, branch="fix/unreadable", review_stack=[gate],
            risk_class="medium", changed_files=["scripts/foo.py"], mode="per_pr",
            dispatch_id="beta3-e1-unreadable-test",
        )
    seat = result["requested"][0]
    assert seat["gate"] == gate, f"unreadable_verdict must abstain, never take over to {successor}"
    assert "takeover" not in seat


@pytest.mark.parametrize("gate", ["codex_gate", "glm_gate"])
def test_no_response_never_takes_over(gate, manager_env, monkeypatch):
    monkeypatch.chdir(manager_env["project_root"])
    manager = _make_manager()
    pr_number = 9504

    _write_result(manager_env["results_dir"], pr_number, gate, {
        "gate": gate, "pr_number": pr_number, "status": "unavailable",
        "reason": "no_verdict",
    })

    with _patch_governance_receipt():
        result = manager.request_reviews(
            pr_number=pr_number, branch="fix/no-response", review_stack=[gate],
            risk_class="medium", changed_files=["scripts/foo.py"], mode="per_pr",
            dispatch_id="beta3-e1-no-response-test",
        )
    seat = result["requested"][0]
    assert seat["gate"] == gate
    assert "takeover" not in seat


# ---------------------------------------------------------------------------
# Point 2/4 -- config validation: unknown gate name and a cycle both fail
# loud at READ time.
# ---------------------------------------------------------------------------

def test_unknown_gate_name_in_config_fails_loud(monkeypatch):
    from gate_request_handler import ReviewGateTakeoverConfigError, _parse_review_gate_takeover_chain
    with pytest.raises(ReviewGateTakeoverConfigError, match="bogus_gate"):
        _parse_review_gate_takeover_chain("kimi_gate,bogus_gate,glm_gate")


def test_cyclic_config_fails_loud(monkeypatch):
    from gate_request_handler import ReviewGateTakeoverConfigError, _parse_review_gate_takeover_chain
    with pytest.raises(ReviewGateTakeoverConfigError, match="kimi_gate"):
        _parse_review_gate_takeover_chain("kimi_gate,glm_gate,kimi_gate")


def test_unknown_gate_name_via_full_resolution_fails_loud(manager_env, monkeypatch):
    monkeypatch.chdir(manager_env["project_root"])
    monkeypatch.setenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "kimi_gate,not_a_real_gate")
    from gate_request_handler import ReviewGateTakeoverConfigError, _build_review_gate_takeover_chain
    with pytest.raises(ReviewGateTakeoverConfigError, match="not_a_real_gate"):
        _build_review_gate_takeover_chain()


# ---------------------------------------------------------------------------
# Point 4 -- absent config falls back to the built-in standard chain; an
# EXPLICIT empty config means no chain at all. Different outcomes.
# ---------------------------------------------------------------------------

def test_absent_config_falls_back_to_standard_chain(manager_env, monkeypatch):
    monkeypatch.chdir(manager_env["project_root"])
    from gate_request_handler import _build_review_gate_takeover_chain
    chain = _build_review_gate_takeover_chain()
    assert chain == {
        "codex_gate": "kimi_gate",
        "kimi_gate": "glm_gate",
        "glm_gate": "deepseek_gate",
    }


def test_explicit_empty_config_means_no_chain_at_all(manager_env, monkeypatch):
    monkeypatch.chdir(manager_env["project_root"])
    monkeypatch.setenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "")
    from gate_request_handler import _build_review_gate_takeover_chain
    assert _build_review_gate_takeover_chain() == {}


def test_explicit_empty_config_leaves_exhausted_seat_undecided_not_chain_exhausted(manager_env, monkeypatch):
    """An empty config is the operator's choice for NO takeover at all --
    an exhausted seat simply stays that gate, re-dispatched normally, same
    as before any takeover mechanism existed. It must NOT produce a
    chain_exhausted record (that state is reserved for a chain that WAS
    entered and then ran out)."""
    monkeypatch.chdir(manager_env["project_root"])
    monkeypatch.setenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "")
    manager = _make_manager()
    pr_number = 9505

    _write_result(manager_env["results_dir"], pr_number, "kimi_gate", {
        "gate": "kimi_gate", "pr_number": pr_number, "status": "unavailable",
        "reason": "dispatch_error", "residual_risk": _REAL_EXHAUSTION_TEXT["kimi"],
    })

    with _patch_governance_receipt():
        result = manager.request_reviews(
            pr_number=pr_number, branch="fix/empty-chain", review_stack=["kimi_gate"],
            risk_class="medium", changed_files=["scripts/foo.py"], mode="per_pr",
            dispatch_id="beta3-e1-empty-chain-test",
        )
    seat = result["requested"][0]
    assert seat["gate"] == "kimi_gate"
    assert seat["status"] != "chain_exhausted"
    assert "takeover" not in seat
    assert not (manager_env["results_dir"] / f"pr-{pr_number}-kimi_gate-chain-exhausted.json").exists()


# ---------------------------------------------------------------------------
# Registry/module literal parity canary -- catches drift between the two
# places the standard chain string is spelled out.
# ---------------------------------------------------------------------------

def test_registry_default_matches_module_constant():
    from gate_request_handler import _DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN
    assert cr.CONFIG_REGISTRY["VNX_REVIEW_GATE_TAKEOVER_CHAIN"].default == _DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN


def test_codex_gate_added_with_kimi_gate_as_first_fallback():
    from gate_request_handler import _parse_review_gate_takeover_chain, _DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN
    chain = _parse_review_gate_takeover_chain(_DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN)
    assert chain["codex_gate"] == "kimi_gate", (
        "codex_gate must be the new entry point; the 22-08 kimi_gate -> glm_gate "
        "relationship must survive unchanged as the SECOND hop"
    )
    assert chain["kimi_gate"] == "glm_gate"
    assert chain["glm_gate"] == "deepseek_gate"


# ---------------------------------------------------------------------------
# BETA3-E1b fix-forward (codex-poort, PR #1695, contract_hash a554cbbe33f5d102
# on 4d010b23) -- three blocking findings on
# ``gate_request_handler._read_lane_log_or_report_text``:
#
#   1. PATH-TRAVERSAL in the report fallback: ``result["report_path"]`` is
#      on-disk STATE (a prior gate's result record), not trusted input, and
#      the fallback read it with a bare ``Path(report_path)`` -- no escape
#      check at all, unlike the lane-log source two lines above it
#      (``governance_emit._resolve_lane_log_path``, which the codex-poort
#      itself calls "defense in depth"). ``_resolve_report_path`` closes
#      this the same way: resolve, then ``relative_to`` the reports dir, or
#      refuse.
#   2. A silent ``except OSError: pass`` collapsed "the report was
#      unreadable" into the same silent no-op as "there was nothing to
#      read" -- the exact toestand-1/2/3 conflation this whole cluster
#      exists to stop. The read failure is now logged with its path.
# ---------------------------------------------------------------------------

def test_report_path_absolute_escape_is_refused(manager_env, monkeypatch, tmp_path, caplog):
    """An absolute report_path pointing OUTSIDE unified_reports/ must be
    refused -- even though the file exists and its real content (the exact
    PR-1691 402 excerpt) would otherwise classify as lane_exhausted. Proves
    the guard, not just its absence of a crash: the classifier must land on
    no_response, since a path-traversal read must never smuggle a real
    exhaustion marker in through an out-of-bounds file.
    """
    monkeypatch.chdir(manager_env["project_root"])
    outside_file = tmp_path / "outside" / "not-a-report.md"
    outside_file.parent.mkdir(parents=True, exist_ok=True)
    outside_file.write_text(_PR1691_REAL_402_REPORT_EXCERPT, encoding="utf-8")

    record = dict(_PR1691_RESULT, report_path=str(outside_file))
    manager = _make_manager()

    with caplog.at_level("WARNING"):
        caplog.clear()
        text = manager._read_lane_log_or_report_text(record)

    assert text == "", "a report_path outside unified_reports/ must never be read"
    assert "escaped" in caplog.text, "the refusal must be logged, citing the escape"
    assert manager._classify_review_seat_failure(record) == "no_response", (
        "a refused report_path must never let the real 402 marker reach the classifier"
    )


def test_report_path_relative_traversal_is_refused(manager_env, monkeypatch, tmp_path, caplog):
    """Same guard, the classic ``../`` traversal shape: a relative
    report_path resolves against cwd (``project_root``, one level below
    ``tmp_path``), so climbing out with ``../`` reaches a real file this
    test controls -- proving the guard is not merely "absolute paths only".
    """
    monkeypatch.chdir(manager_env["project_root"])
    secret_file = tmp_path / "secret-outside.md"
    secret_file.write_text(_PR1691_REAL_402_REPORT_EXCERPT, encoding="utf-8")

    record = dict(_PR1691_RESULT, report_path="../secret-outside.md")
    manager = _make_manager()

    with caplog.at_level("WARNING"):
        caplog.clear()
        text = manager._read_lane_log_or_report_text(record)

    assert text == "", "a relative ../ report_path escaping unified_reports/ must never be read"
    assert "escaped" in caplog.text


def test_report_fallback_absent_empty_unreadable_are_distinguishable(manager_env, monkeypatch, caplog):
    """The three read states the report fallback can hit -- bestand
    afwezig, bestand leeg, bestand onleesbaar -- must each be independently
    reachable, and only the unreadable case may log a warning. Collapsing
    an OSError into silence (the pre-fix ``except OSError: pass``) makes
    "the report was corrupt/permission-denied" indistinguishable from "no
    report exists yet" -- exactly the toestand conflation this cluster
    exists to stop.
    """
    monkeypatch.chdir(manager_env["project_root"])
    manager = _make_manager()
    dispatch_id = "beta3-e1b-readstate-test"
    report_file = manager_env["reports_dir"] / f"{dispatch_id}.md"
    record = {"dispatch_id": dispatch_id, "report_path": str(report_file)}

    # 1. Absent -- no file at all.
    with caplog.at_level("WARNING"):
        caplog.clear()
        text = manager._read_lane_log_or_report_text(record)
    assert text == ""
    assert caplog.text == "", "an absent report must not log a warning"

    # 2. Empty -- file exists, whitespace-only content.
    report_file.write_text("   \n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        caplog.clear()
        text = manager._read_lane_log_or_report_text(record)
    assert text == ""
    assert caplog.text == "", "an empty report must not log a warning"

    # 3. Unreadable -- file exists with real content, but the read itself
    # raises OSError (permission denied / disk error / etc, simulated).
    report_file.write_text(_PR1691_REAL_402_REPORT_EXCERPT, encoding="utf-8")
    real_read_text = Path.read_text

    def _raise_on_report_file(self, *args, **kwargs):
        if self == report_file:
            raise OSError("permission denied (simulated)")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _raise_on_report_file)
    with caplog.at_level("WARNING"):
        caplog.clear()
        text = manager._read_lane_log_or_report_text(record)
    assert text == "", "a read failure must still return '' -- never raise out of classification"
    assert str(report_file) in caplog.text and dispatch_id in caplog.text, (
        "an unreadable report MUST log its path and dispatch_id, distinguishing "
        "it from the silent absent/empty cases above"
    )


# ---------------------------------------------------------------------------
# Helper: patch emit_governance_receipt for the duration of a `with` block,
# same target the other review-gate test modules patch.
# ---------------------------------------------------------------------------

def _patch_governance_receipt():
    from unittest.mock import patch
    return patch("governance_receipts.emit_governance_receipt")
