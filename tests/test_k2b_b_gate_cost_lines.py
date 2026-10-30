"""tests/test_k2b_b_gate_cost_lines.py — codex and kimi gates book their own cost line (K2b-b).

Track ``kosten-per-model-in-het-grootboek``, deliverable K2b (ADR-043). K2b-a put
the two lanes' BUILDER runs in the ledger. This is the gate half: a review seat
books one line when its gate ends, under role ``review``, and never a builder
line as well — one run is one line.

- ``codex`` — the gate runs ``codex exec --json`` from ``gate_runner``, so the
  run ends there, with ``execution_depth`` as its only usage report (input,
  output, and the cached part of the input when ``turn.completed`` carried it).
  The codex input count INCLUDES the cached part (OI-2407), so the line books
  the difference; a stream with no cached count leaves both split fields
  ``None`` with ``input_cache_split_unmeasured`` instead of pricing the total as
  uncached.
- ``kimi`` — the gate runs through the governed lane and the seat's report is
  the only usage report (kimi-cli 1.46.0's stream carries none). A report
  stamped ``token_usage_measured: false`` books ``unknown:kimi_cli_no_usage`` /
  ``unavailable:kimi_cli_no_usage``; one stamped ``true`` books its counts.

Both bill flat (codex on the operator's ChatGPT login, kimi on CLI OAuth), so
every line is ``billing: subscription`` with ``cost_usd`` 0 and the registry's
list price in ``api_equivalent_usd``. Every attempt writes exactly one line, a
refire included (attempt 2 of the same PR), and a refused spawn writes one zero
line carrying ``unavailable:spawn_refused``. The booking is fail-open in both
directions: a ledger that raises never changes a gate verdict, and every failed
booking is a WARNING naming the ref, the provider and the reason.

The ledger here is the real writer on a temporary store, the gate is the real
gate (``GateRunner._run_subprocess_path`` / ``kimi_gate.main``), and only the
CLI subprocess boundary is replaced — the codex ``exec --json`` stream, and the
governed lane's dispatcher.
"""
from __future__ import annotations

import errno
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import cost_ledger  # noqa: E402
import gate_runner  # noqa: E402
import harness_run_cost  # noqa: E402
import kimi_gate  # noqa: E402
from gate_runner import GateRunner  # noqa: E402

PROJECT = "vnx-dev"
CODEX_MODEL = "gpt-6-astra"
KIMI_MODEL = "kimi-k3"

# The codex stream a real gate run leaves (tests/fixtures/gate_verdict/
# pr-1869-codex_gate-bare-verdict.ndjson): one shell call, a bare verdict and a
# turn.completed carrying the only usage the stream reports. That usage reports
# input_tokens 40939 WITH cached_input_tokens 33536 — codex's input count
# includes the cached part (measured; see the fixture's line 7).
CODEX_STREAM = (
    VNX_ROOT / "tests" / "fixtures" / "gate_verdict" / "pr-1869-codex_gate-bare-verdict.ndjson"
).read_text(encoding="utf-8")

# The same run from a codex build whose stream carries no cached count at all:
# the input/cache split is then unmeasured, and the total input must never be
# booked as the uncached part.
_UNSPLIT_CODEX_STREAM = CODEX_STREAM.replace(
    '{"type":"turn.completed","usage":{"input_tokens":40939,"cached_input_tokens":33536,'
    '"cache_write_input_tokens":0,"output_tokens":991,"reasoning_output_tokens":786}}',
    '{"type":"turn.completed","usage":{"input_tokens":40939,"output_tokens":991}}',
)
assert _UNSPLIT_CODEX_STREAM != CODEX_STREAM, "the fixture's turn.completed usage line moved"

RUN_START = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)
RUN_END = datetime(2026, 10, 27, 10, 1, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A temporary store: the gate's own records and the real cost ledger both live here."""
    data = tmp_path / "data"
    state = data / "state"
    reports = data / "unified_reports"
    for path in (state / "review_gates" / "requests", state / "review_gates" / "results", reports):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_DATA_DIR", str(data))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    return SimpleNamespace(
        data=data, state=state, reports=reports,
        ledger=data / "events" / "provider_costs.ndjson",
        results=state / "review_gates" / "results",
    )


def _lines(store) -> list:
    """Every cost-ledger run line in the store, in write order."""
    if not store.ledger.exists():
        return []
    return [
        record for record in (
            json.loads(line) for line in store.ledger.read_text(encoding="utf-8").splitlines() if line.strip()
        )
        if record.get("schema") == cost_ledger.RUN_SCHEMA
    ]


def _codex_result(*, stream=CODEX_STREAM, duration=12.5, spawn_refused=None) -> dict:
    """The result ``_run_with_stall_detection`` returns: the CLI boundary this test replaces."""
    if spawn_refused is not None:
        return {
            "status": "failed", "reason": spawn_refused, "reason_detail": "the gate never launched codex",
            "stdout": "", "stderr": "", "duration_seconds": 0.0, "partial_output_lines": 0,
            "runner_pid": 4242, "spawn_refused": spawn_refused,
        }
    return {
        "status": "completed", "stdout": stream, "stderr": "", "duration_seconds": duration,
        "partial_output_lines": 7, "runner_pid": 4242, "returncode": 0,
    }


def _run_codex_gate(store, monkeypatch, *, pr_number=1234, result=None, model=CODEX_MODEL) -> dict:
    """One real codex gate through ``_run_subprocess_path``, with only the spawn replaced."""
    monkeypatch.setattr(gate_runner, "create_gate_worktree", lambda **kw: store.data / "_wt")
    monkeypatch.setattr(gate_runner, "remove_gate_worktree", lambda *a, **kw: None)
    monkeypatch.setattr(GateRunner, "_run_with_stall_detection", lambda self, **kw: result or _codex_result())
    payload = {
        "gate": "codex_gate", "status": "requested", "provider": "codex", "branch": "dispatch/test",
        "pr_number": pr_number, "pr_id": f"PR-{pr_number}", "commit_sha": "abc1234",
        "review_mode": "per_pr", "risk_class": "medium", "changed_files": ["scripts/lib/x.py"],
        "requested_at": "2026-10-27T10:00:00Z",
        "report_path": str(store.reports / f"pr-{pr_number}-codex_gate.md"),
        "prompt": "Review this code",
        # The launch record ``_build_gate_cmd`` stamps; the booking names the
        # model that actually ran from it (ADR-036).
        "codex_launch": {"model": model, "reasoning_effort": "medium"},
    }
    runner = GateRunner(state_dir=store.state, reports_dir=store.reports)
    return runner._run_subprocess_path(
        gate="codex_gate", binary="codex", prompt="Review this code",
        pr_number=pr_number, pr_id=f"PR-{pr_number}", request_payload=payload,
    )


def _kimi_report(*, exit_code=0, input_tokens=0, output_tokens=0, cache_read=0, measured=False) -> str:
    """A kimi gate report in the shape the governed lane writes (frontmatter + verdict body)."""
    body = "Reviewed the diff.\n\n```json\n" + json.dumps(
        {"verdict": "pass", "findings": [], "residual_risk": None}
    ) + "\n```\n"
    return (
        "---\n"
        "schema_version: 1\n"
        "dispatch_id: kimi-gate-pr0-1788000240\n"
        "provider: kimi\n"
        "sub_provider: moonshot\n"
        f"model: {KIMI_MODEL}\n"
        "role: review-gate\n"
        "duration_seconds: 154.095\n"
        f"exit_code: {exit_code}\n"
        "token_usage:\n"
        f"  input: {input_tokens}\n"
        f"  output: {output_tokens}\n"
        f"  cache_read: {cache_read}\n"
        f"token_usage_measured: {'true' if measured else 'false'}\n"
        "---\n"
        "\n" + body
    )


def _run_kimi_gate(store, monkeypatch, report: str) -> tuple:
    """One real kimi gate through ``kimi_gate.main``, with only the lane's dispatcher replaced."""
    diff_file = store.data / "x.diff"
    diff_file.write_text("diff --git a/x b/x\n+ok\n", encoding="utf-8")
    monkeypatch.setattr(kimi_gate, "_make_default_dispatcher", lambda *a, **k: (lambda *a, **k: report))
    rc = kimi_gate.main([
        "--pr", "0", "--diff-file", str(diff_file), "--data-dir", str(store.data),
        "--model", KIMI_MODEL,
    ])
    out = store.results / "pr-0-kimi_gate.json"
    record = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return rc, record


def _warnings(caplog) -> str:
    return " | ".join(r.getMessage() for r in caplog.records if r.name == "harness_run_cost")


# ---------------------------------------------------------------------------
# (a) the codex gate: execution_depth in, one partial line at the registry price
# ---------------------------------------------------------------------------


def test_a_codex_gate_books_one_partial_line_at_the_registry_price(store, monkeypatch):
    """The codex gate ends in ``GateRunner._run_subprocess_path``.

    Its stream reports input 40939 with 33536 of it cached (turn.completed
    ``cached_input_tokens``), so the line books the 7403 uncached part and the
    33536 cache hits; codex's input count includes the cached part (OI-2407).
    No cache-write accounting reaches the stream, so the line says ``partial:``
    and names those two missing fields — and it is still PRICED, because the
    openai registry entries charge input and output only. Codex bills flat on
    the operator's login: 0 at the margin, the list price as
    ``api_equivalent_usd``.
    """
    result = _run_codex_gate(store, monkeypatch)
    assert result["status"] == "completed", result.get("reason_detail")

    (line,) = _lines(store)
    assert line["run_id"] == f"{PROJECT}:codex_gate:PR-1234:1"
    assert line["role"] == "review" and line["provider"] == "codex"
    assert line["model"] == CODEX_MODEL
    assert line["tokens"] == {
        "input_uncached": 7403, "cache_hit": 33536, "cache_write_5m": None,
        "cache_write_1h": None, "output": 991,
    }
    assert line["usage_completeness"] == "partial:cache_write_1h,cache_write_5m"
    assert line["billing"] == "subscription" and line["cost_usd"] == 0.0
    # The ONE pricing function priced it; no second table exists (ADR-043).
    assert line["cost_source"].startswith("price_table@")
    assert line["api_equivalent_usd"] > 0


def test_a_codex_gate_stream_without_a_cached_count_books_the_split_unknown(store, monkeypatch, caplog):
    """OI-2407 acceptance 4, second half: the gate path never books total input as uncached.

    The gate's ``ExecutionDepth`` is built from ``turn.completed``. A stream that
    carries input and output but no ``cached_input_tokens`` cannot say how much
    of that input was cached, so both split fields are ``None`` with a reason.
    """
    with caplog.at_level(logging.WARNING, logger="harness_run_cost"):
        result = _run_codex_gate(store, monkeypatch, result=_codex_result(stream=_UNSPLIT_CODEX_STREAM))
    assert result["status"] == "completed", result.get("reason_detail")

    (line,) = _lines(store)
    assert line["tokens"]["input_uncached"] is None
    assert line["tokens"]["cache_hit"] is None
    assert line["tokens"]["output"] == 991
    assert line["usage_completeness"] == "partial:cache_hit,cache_write_1h,cache_write_5m,input_uncached"
    assert harness_run_cost.INPUT_CACHE_SPLIT_UNMEASURED in _warnings(caplog)


def test_the_gate_depth_carries_the_cached_part_it_was_reported(store, monkeypatch):
    """The depth a gate records keeps the cached count as its own field, and ``None`` when absent.

    ``cache_read_tokens`` is what makes the split above possible: the gate
    measures it off ``turn.completed``, and a stream that reports no cached
    count leaves it ``None`` — never a zero that would price every input token
    as uncached.
    """
    import gate_depth

    measured = gate_depth.measure_execution_depth(CODEX_STREAM)
    assert (measured.input_tokens, measured.cache_read_tokens, measured.output_tokens) == (40939, 33536, 991)

    unmeasured = gate_depth.measure_execution_depth(_UNSPLIT_CODEX_STREAM)
    assert (unmeasured.input_tokens, unmeasured.cache_read_tokens, unmeasured.output_tokens) == (40939, None, 991)

    # A malformed count is unmeasured, never an invented zero — and it does not
    # abort the measurement of the rest of the line.
    malformed = gate_depth.measure_execution_depth(
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":"lots","output_tokens":5}}'
    )
    assert (malformed.input_tokens, malformed.cache_read_tokens, malformed.output_tokens) == (100, None, 5)
    assert "cache_read_tokens" in malformed.to_dict()


# ---------------------------------------------------------------------------
# (b) the kimi gate: no usage reported is said out loud, never booked as 0
# ---------------------------------------------------------------------------


def test_a_kimi_gate_run_that_reports_no_usage_books_the_documented_unavailable(store, monkeypatch):
    """kimi-cli 1.46.0's stream carries no usage accounting (ADR-043 §8).

    The governed lane's tail is where a kimi gate ends, and the seat's report is
    its only usage report. This report is stamped ``token_usage_measured:
    false`` beside the lane's placeholder zeros — the shape 502 of 766 measured
    kimi reports carry — so the line must say the run is unknown, with no
    amount, rather than price three zeros.
    """
    rc, record = _run_kimi_gate(store, monkeypatch, _kimi_report(measured=False))
    assert rc == 0 and record["status"] == "pass"

    (line,) = _lines(store)
    assert line["provider"] == "kimi" and line["role"] == "review"
    assert line["usage_completeness"] == "unknown:kimi_cli_no_usage"
    assert line["cost_source"] == "unavailable:kimi_cli_no_usage"
    assert line["cost_usd"] == 0.0 and line["api_equivalent_usd"] is None
    assert all(value is None for value in line["tokens"].values()), "a zero here would be an invention"
    assert line["run_id"].startswith(f"{PROJECT}:kimi_gate:kimi-gate-pr0-")


def test_a_kimi_gate_run_that_measured_its_tokens_books_them(store, monkeypatch):
    """The other half of the same contract: when the lane DID measure, book what it measured."""
    rc, record = _run_kimi_gate(
        store, monkeypatch,
        _kimi_report(input_tokens=42110, output_tokens=4223, cache_read=39424, measured=True),
    )
    assert rc == 0 and record["status"] == "pass"

    (line,) = _lines(store)
    assert line["tokens"]["input_uncached"] == 42110
    assert line["tokens"]["cache_hit"] == 39424
    assert line["tokens"]["output"] == 4223
    assert line["usage_completeness"] == "partial:cache_write_1h,cache_write_5m"
    assert line["cost_source"].startswith("price_table@")
    assert line["cost_usd"] == 0.0 and line["billing"] == "subscription"


def test_a_reprocess_run_calls_no_model_and_books_no_line(store, monkeypatch):
    """``--reprocess`` re-reads a report; it never calls the lane, so it books nothing."""
    rc, record = _run_kimi_gate(store, monkeypatch, _kimi_report(measured=True, input_tokens=10, output_tokens=2))
    assert rc == 0 and len(_lines(store)) == 1
    dispatch_id = record["dispatch_id"]
    store.ledger.unlink()

    diff_file = store.data / "x.diff"
    monkeypatch.setattr(kimi_gate, "_make_default_dispatcher", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a reprocess run must not call the lane")))
    rc = kimi_gate.main([
        "--pr", "0", "--reprocess", dispatch_id, "--data-dir", str(store.data), "--model", KIMI_MODEL,
    ])
    assert rc in (0, 1), rc
    assert _lines(store) == [], "a run that called no model books no cost line"


# ---------------------------------------------------------------------------
# (c) every attempt is a line: a refire is attempt 2, a refused spawn is a zero
# ---------------------------------------------------------------------------


def test_a_refire_is_a_second_line_with_attempt_two(store, monkeypatch):
    """Two gate runs on the same PR are two attempts of one ref, never one overwritten line."""
    _run_codex_gate(store, monkeypatch)
    _run_codex_gate(store, monkeypatch)

    first, second = _lines(store)
    assert first["run_id"] == f"{PROJECT}:codex_gate:PR-1234:1"
    assert second["run_id"] == f"{PROJECT}:codex_gate:PR-1234:2"
    assert first["record_id"] != second["record_id"]
    assert first["tokens"] == second["tokens"]


def test_a_refused_spawn_is_one_zero_line_with_spawn_refused(store, monkeypatch):
    """A gate that never launched codex still books its attempt — as a refusal, not as unknown usage."""
    result = _run_codex_gate(store, monkeypatch, result=_codex_result(spawn_refused="codex_model_unresolved"))
    assert result["status"] == "unavailable"

    (line,) = _lines(store)
    assert line["usage_completeness"] == "complete"
    assert line["cost_source"] == "unavailable:spawn_refused"
    assert line["cost_usd"] == 0.0 and line["api_equivalent_usd"] is None
    assert all(value == 0 for value in line["tokens"].values())


# ---------------------------------------------------------------------------
# (d) fail-open: a ledger that raises never touches the verdict, but is loud
# ---------------------------------------------------------------------------


def test_a_ledger_write_that_raises_does_not_change_the_gate_verdict(store, monkeypatch, caplog):
    """The verdict and the exit code are identical with and without a broken ledger.

    Two doors book this run: K2b at the lane's measured end
    (``cost_ledger.record_next_attempt``) and, since K3-2b, the outcome commit
    as a fallback (``cost_ledger.record_once``). Both are broken here, so the
    test still measures what it always measured — a ledger that cannot be
    written never costs a verdict — across the whole booking surface.
    """
    rc_healthy, healthy = _run_kimi_gate(store, monkeypatch, _kimi_report(measured=False))
    assert rc_healthy == 0 and healthy["status"] == "pass"
    store.ledger.unlink()

    def _explode(*_args, **_kwargs):
        raise cost_ledger.CostLedgerWriteError("the ledger and its spool are both gone")

    monkeypatch.setattr(cost_ledger, "record_next_attempt", _explode)
    monkeypatch.setattr(cost_ledger, "record_once", _explode)
    with caplog.at_level(logging.WARNING, logger="harness_run_cost"):
        rc_broken, broken = _run_kimi_gate(store, monkeypatch, _kimi_report(measured=False))

    assert rc_broken == rc_healthy == 0
    assert broken["status"] == "pass" and broken["reason"] == healthy["reason"]
    assert _lines(store) == [], "nothing was written, and the gate still decided"
    warnings_ = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "harness_run_cost"
    ]
    assert warnings_, "a failed booking is never silent"
    for warning in warnings_:
        message = warning.getMessage()
        assert "kimi" in message and "review" in message
        assert "CostLedgerWriteError" in message, "a failed booking is never silent"
        assert "kimi-gate-pr0-" in message, "the warning names the run it could not book"


# ---------------------------------------------------------------------------
# (e) fail-open on every booking error: a cost line never costs a verdict
# ---------------------------------------------------------------------------


def _permission_denied(*_args, **_kwargs):
    """The registry/ledger read ``record_next_attempt`` propagates unchanged (an OSError)."""
    raise PermissionError(errno.EACCES, "Permission denied", "/store/state/provider_costs.ndjson")


def test_a_registry_read_error_returns_none_with_a_reason_and_one_warning(store, monkeypatch, caplog):
    """An OSError reading the ledger is not a CostLedger* error, and it must not escape either.

    ``cost_ledger.record_next_attempt`` propagates an OSError from reading the
    registry unchanged; a booker that only catches the ledger's own errors lets
    it through, and a gate that books before recording its verdict then has no
    verdict at all.
    """
    run = harness_run_cost.cli_run(
        provider=harness_run_cost.CODEX_PROVIDER, model=CODEX_MODEL,
        started=RUN_START, ended=RUN_END,
        # A complete codex usage (the cache count is explicit) so the only
        # warning this test sees is the booking refusal itself.
        token_usage={"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 0},
    )
    monkeypatch.setattr(cost_ledger, "record_next_attempt", _permission_denied)
    with caplog.at_level(logging.WARNING, logger="harness_run_cost"):
        record, reason = harness_run_cost.record_codex_run(
            run, project_id=PROJECT, producer="codex_gate", ref="PR-1234",
            ledger_path=store.ledger,
        )

    assert record is None and "PermissionError" in reason
    (warning,) = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "harness_run_cost"
    ]
    message = warning.getMessage()
    assert "codex" in message and "review" in message and "PR-1234" in message
    assert "PermissionError" in message, "a failed booking is never silent"


def test_a_permission_error_reading_the_ledger_does_not_change_the_codex_gate_verdict(store, monkeypatch, caplog):
    """The codex gate books BEFORE it records its verdict, so an escaping error would drop the verdict."""
    monkeypatch.setattr(cost_ledger, "record_next_attempt", _permission_denied)
    with caplog.at_level(logging.WARNING, logger="harness_run_cost"):
        result = _run_codex_gate(store, monkeypatch)

    assert result["status"] == "completed", result.get("reason_detail")
    # The canonical slot for a gate run that carries a pr_id (gate_recorder.result_file_path).
    recorded = json.loads((store.results / "pr1234-codex_gate-contract.json").read_text(encoding="utf-8"))
    assert recorded["status"] == result["status"], "the gate verdict was still recorded"
    assert _lines(store) == [], "the booking failed, and the gate still decided"
    (warning,) = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "harness_run_cost"
    ]
    assert "PermissionError" in warning.getMessage()
