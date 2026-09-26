"""Availability is measured on reachability, not presence (OI-1454, OI-1507).

Measured 2026-08-23: three of four reader adapters reported ``available=True``
while none could answer a call (codex quota spent, litellm key 401). A fallback
that picks on that chooses a dead seat with full confidence.

What these tests hold:

* codex present but quota exhausted is NOT available, and says why;
* litellm importable but its key rejected is NOT available;
* a provider nobody has asked is ``unmeasured``, never ``reachable``;
* the review-gate seat walk passes over a seat whose provider is recorded
  unreachable, and reports the reason, instead of asking a seat that cannot
  give a verdict;
* a record expires: past its window the provider reads as unmeasured and is
  asked again;
* no key ever reaches a stored record or a log line.
"""
from __future__ import annotations

import contextlib
import importlib
import json
import logging
import subprocess
import sys
import types
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import provider_reachability as pr
from provider_reachability import ReachabilityState as State

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"


@pytest.fixture(autouse=True, scope="module")
def _scripts_dir_importable():
    """conftest puts scripts/lib on the path; the gate manager and the receipt
    writer live one level up in scripts/."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    yield
    sys.path.remove(str(SCRIPTS_DIR))

_CODEX_QUOTA_TEXT = (
    "Subprocess exited with code 1: You've hit your usage limit. Upgrade to Pro "
    "or try again at Sep 29th."
)
_OPENROUTER_401_TEXT = 'Error code: 401 - {"error": {"message": "API key expired", "code": 401}}'
_DEEPSEEK_402_TEXT = "Error code: 402 - Insufficient Balance"


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    """An isolated state dir that ambient resolution also lands on."""
    data_dir = tmp_path / ".vnx-data"
    state = data_dir / "state"
    state.mkdir(parents=True)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    return state


def _record_of(state: Path, provider: str) -> dict:
    return json.loads((state / pr.STATE_SUBDIR / f"{provider}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The record itself
# ---------------------------------------------------------------------------


class TestUnmeasuredIsNotReachable:
    def test_a_provider_nobody_asked_is_unmeasured(self, state_dir):
        status = pr.get("codex", state_dir=state_dir)
        assert status.state is State.UNMEASURED
        assert status.is_reachable is False
        assert status.is_known_unreachable is False

    def test_unmeasured_stays_askable(self, state_dir):
        # Not proven dead is usable; otherwise nothing would ever produce the
        # first outcome that measures it.
        assert pr.get("codex", state_dir=state_dir).is_usable is True

    def test_only_a_fresh_outcome_reads_as_reachable(self, state_dir):
        pr.record_success("codex", source="test", state_dir=state_dir)
        assert pr.get("codex", state_dir=state_dir).is_reachable is True

    def test_unmeasured_describes_itself_as_unmeasured(self, state_dir):
        assert "unmeasured" in pr.get("codex", state_dir=state_dir).describe()


class TestOutcomesAreRecordedWithTheirReason:
    def test_quota_text_records_quota_exhausted(self, state_dir):
        pr.record_failure("codex", _CODEX_QUOTA_TEXT, source="gate_result:codex_gate", state_dir=state_dir)
        status = pr.get("codex", state_dir=state_dir)
        assert status.state is State.UNREACHABLE
        assert status.reason == pr.REASON_QUOTA_EXHAUSTED
        assert "usage limit" in status.detail
        assert status.source == "gate_result:codex_gate"

    def test_credential_refusal_records_auth_401(self, state_dir):
        pr.record_failure("litellm", _OPENROUTER_401_TEXT, source="adapter:litellm", state_dir=state_dir)
        assert pr.get("litellm", state_dir=state_dir).reason == pr.REASON_AUTH_401

    def test_spent_balance_records_insufficient_balance(self, state_dir):
        pr.record_failure("deepseek-harness", _DEEPSEEK_402_TEXT, source="test", state_dir=state_dir)
        assert pr.get("deepseek-harness", state_dir=state_dir).reason == pr.REASON_INSUFFICIENT_BALANCE

    def test_describe_says_why_and_where_it_was_seen(self, state_dir):
        pr.record_failure("codex", _CODEX_QUOTA_TEXT, source="gate_result:codex_gate", state_dir=state_dir)
        line = pr.get("codex", state_dir=state_dir).describe()
        assert "quota_exhausted" in line
        assert "gate_result:codex_gate" in line
        assert "expires" in line

    @pytest.mark.parametrize("text", [
        "timeout after 120s",
        "Subprocess exited with code 139 (segfault)",
        "processed 401 lines of the diff and 429 tokens",
        "the reviewer flagged unauthorized access checks in the diff",
        "",
    ])
    def test_a_failure_that_says_nothing_about_reachability_changes_nothing(self, state_dir, text):
        assert pr.record_failure("codex", text, source="test", state_dir=state_dir) is None
        assert pr.get("codex", state_dir=state_dir).state is State.UNMEASURED

    def test_a_later_success_replaces_an_unreachable_record(self, state_dir):
        pr.record_failure("codex", _CODEX_QUOTA_TEXT, source="test", state_dir=state_dir, measured_at=1000.0, now=1000.0)
        pr.record_success("codex", source="test", state_dir=state_dir, measured_at=1060.0, now=1060.0)
        assert pr.get("codex", state_dir=state_dir, now=1061.0).is_reachable is True


class TestRecordsExpire:
    def test_an_unreachable_record_expires_into_unmeasured_not_reachable(self, state_dir):
        pr.record_failure("codex", _CODEX_QUOTA_TEXT, source="test", state_dir=state_dir, measured_at=1000.0, now=1000.0)
        ttl = pr.unreachable_ttl_seconds()
        assert pr.get("codex", state_dir=state_dir, now=1000.0 + ttl - 1).is_known_unreachable is True
        expired = pr.get("codex", state_dir=state_dir, now=1000.0 + ttl + 1)
        assert expired.state is State.UNMEASURED
        assert expired.is_reachable is False
        assert expired.is_usable is True
        assert expired.reason == "expired"

    def test_a_reachable_record_expires_into_unmeasured(self, state_dir):
        pr.record_success("codex", source="test", state_dir=state_dir, measured_at=1000.0, now=1000.0)
        assert pr.get("codex", state_dir=state_dir, now=1000.0 + pr.REACHABLE_TTL_SECONDS - 1).is_reachable is True
        expired = pr.get("codex", state_dir=state_dir, now=1000.0 + pr.REACHABLE_TTL_SECONDS + 1)
        assert expired.state is State.UNMEASURED
        assert expired.is_reachable is False

    def test_the_unreachable_window_is_the_quota_recovery_contract(self):
        from incident_taxonomy import IncidentClass, get_cooldown_seconds

        assert pr.unreachable_ttl_seconds() == get_cooldown_seconds(IncidentClass.PROVIDER_QUOTA_EXHAUSTED, 0)


class TestAnOlderObservationNeverOverrulesAFresherOne:
    def test_rewriting_an_old_record_does_not_refresh_its_age(self, state_dir):
        pr.record_failure("codex", _CODEX_QUOTA_TEXT, source="first", state_dir=state_dir, measured_at=1000.0, now=1000.0)
        # A takeover annotation re-books the same outcome later, with the ORIGINAL time.
        again = pr.record_failure("codex", _CODEX_QUOTA_TEXT, source="rewrite", state_dir=state_dir,
                                  measured_at=1000.0, now=5000.0)
        assert again is None
        record = _record_of(state_dir, "codex")
        assert record["measured_at"] == 1000.0
        assert record["source"] == "first"

    def test_a_stale_success_does_not_hide_a_fresher_refusal(self, state_dir):
        pr.record_failure("codex", _CODEX_QUOTA_TEXT, source="test", state_dir=state_dir, measured_at=2000.0, now=2000.0)
        pr.record_success("codex", source="test", state_dir=state_dir, measured_at=1500.0, now=2001.0)
        assert pr.get("codex", state_dir=state_dir, now=2002.0).is_known_unreachable is True


class TestFailingClosedNeverTowardsReachable:
    def test_a_corrupt_record_reads_unmeasured_and_is_loud(self, state_dir, caplog):
        path = state_dir / pr.STATE_SUBDIR / "codex.json"
        path.parent.mkdir(parents=True)
        path.write_text("{ not json", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="provider_reachability"):
            status = pr.get("codex", state_dir=state_dir)
        assert status.state is State.UNMEASURED
        assert status.is_reachable is False
        assert status.reason == pr.REASON_UNREADABLE_RECORD
        assert any("codex" in r.getMessage() and "unmeasured" in r.getMessage() for r in caplog.records)

    def test_a_record_missing_a_field_reads_unmeasured(self, state_dir):
        path = state_dir / pr.STATE_SUBDIR / "codex.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"provider": "codex", "state": "reachable"}), encoding="utf-8")
        assert pr.get("codex", state_dir=state_dir).state is State.UNMEASURED

    def test_an_invalid_provider_key_is_refused_loudly(self, state_dir, caplog):
        with caplog.at_level(logging.WARNING, logger="provider_reachability"):
            assert pr.record_success("Not A Key!", source="test", state_dir=state_dir) is None
        assert any("Not A Key!" in r.getMessage() for r in caplog.records)
        assert not (state_dir / pr.STATE_SUBDIR).exists()

    def test_an_unknown_reason_is_refused_not_stored(self, state_dir):
        assert pr.record_unreachable("codex", "vibes", "x", source="test", state_dir=state_dir) is None
        assert pr.get("codex", state_dir=state_dir).state is State.UNMEASURED


class TestNoKeyEverReachesARecordOrALog:
    def test_a_key_shaped_token_is_scrubbed_from_the_stored_detail(self, state_dir):
        text = "Error code: 401 invalid api key sk-abcdef0123456789abcdef rejected"
        pr.record_failure("litellm", text, source="test", state_dir=state_dir)
        stored = (state_dir / pr.STATE_SUBDIR / "litellm.json").read_text(encoding="utf-8")
        assert "sk-abcdef0123456789abcdef" not in stored
        assert "[redacted]" in stored

    def test_a_secret_this_process_holds_is_scrubbed_even_when_it_has_no_known_shape(self, state_dir, monkeypatch, caplog):
        secret = "Zq9-plainlookingsecretvalue-77"
        monkeypatch.setenv("SOME_PROVIDER_API_KEY", secret)
        text = f"Error code: 401 - key {secret} is not authenticated"
        with caplog.at_level(logging.DEBUG):
            pr.record_failure("litellm", text, source="test", state_dir=state_dir)
        stored = (state_dir / pr.STATE_SUBDIR / "litellm.json").read_text(encoding="utf-8")
        assert secret not in stored
        assert all(secret not in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Adapters: codex present but quota exhausted, litellm importable but 401
# ---------------------------------------------------------------------------


class TestAdaptersMeasureReachabilityNotPresence:
    def _codex(self):
        from adapters.codex_adapter import CodexAdapter

        return CodexAdapter("T3")

    def test_codex_present_but_quota_exhausted_is_not_available_and_says_why(self, state_dir):
        pr.record_failure("codex", _CODEX_QUOTA_TEXT, source="gate_result:codex_gate", state_dir=state_dir)
        adapter = self._codex()
        with patch("adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"):
            assert adapter.is_present() is True
            assert adapter.is_available() is False
            status = adapter.reachability()
        assert status.state is State.UNREACHABLE
        assert status.reason == pr.REASON_QUOTA_EXHAUSTED
        assert "usage limit" in status.describe()

    def test_codex_present_and_unmeasured_is_not_reported_as_reachable(self, state_dir):
        adapter = self._codex()
        with patch("adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"):
            status = adapter.reachability()
            available = adapter.is_available()
        assert status.state is State.UNMEASURED
        assert status.is_reachable is False
        assert available is True  # askable: nobody has proven it dead

    def test_codex_absent_is_unreachable_whatever_a_record_says(self, state_dir):
        pr.record_success("codex", source="test", state_dir=state_dir)
        adapter = self._codex()
        with patch("adapters.codex_adapter.shutil.which", return_value=None):
            status = adapter.reachability()
            available = adapter.is_available()
        assert available is False
        assert status.reason == pr.REASON_NOT_PRESENT

    def test_litellm_importable_but_key_rejected_is_not_available(self, state_dir):
        from adapters.litellm_adapter import LiteLLMAdapter

        pr.record_failure("litellm", _OPENROUTER_401_TEXT, source="adapter:litellm", state_dir=state_dir)
        adapter = LiteLLMAdapter("T1")
        with patch.dict(sys.modules, {"litellm": types.ModuleType("litellm")}):
            assert adapter.is_present() is True
            assert adapter.is_available() is False
            assert adapter.reachability().reason == pr.REASON_AUTH_401

    def test_claude_and_gemini_follow_the_same_rule(self, state_dir):
        from adapters.claude_adapter import ClaudeAdapter
        from adapters.gemini_adapter import GeminiAdapter

        pr.record_failure("claude", "Error code: 401 authentication_error", source="test", state_dir=state_dir)
        pr.record_failure("gemini", "429 exceeded your current quota", source="test", state_dir=state_dir)
        with patch("adapters.claude_adapter.shutil.which", return_value="/usr/bin/claude"), \
                patch("adapters.gemini_adapter.shutil.which", return_value="/usr/bin/gemini"):
            assert ClaudeAdapter("T1").is_available() is False
            assert GeminiAdapter("T3").is_available() is False

    def test_ollama_is_measured_live_and_reports_the_probe_as_its_source(self):
        from adapters.ollama_adapter import OllamaAdapter

        adapter = OllamaAdapter("T3")
        with patch("adapters.ollama_adapter.urllib.request.urlopen", side_effect=OSError("connection refused")):
            status = adapter.reachability()
            available = adapter.is_available()
        assert available is False
        assert status.state is State.UNREACHABLE
        assert status.reason == "endpoint_unreachable"
        assert status.source == "probe:ollama"

    def test_an_adapter_without_is_present_fails_loudly_rather_than_claiming_presence(self):
        from provider_adapter import Capability, ProviderAdapter

        class Bare(ProviderAdapter):
            def name(self):
                return "bare"

            def capabilities(self):
                return {Capability.REVIEW}

            def execute(self, instruction, context):
                raise NotImplementedError

            def stream_events(self, instruction, context):
                return iter([])

            def is_available(self):
                return True

        with pytest.raises(NotImplementedError, match="is_present"):
            Bare().reachability()


class TestAdapterCallsFeedTheRecord:
    def test_a_codex_run_refused_on_quota_marks_codex_unreachable(self, state_dir):
        from adapters.codex_adapter import CodexAdapter
        from provider_spawns.codex_spawn import CodexSpawnResult

        refused = CodexSpawnResult(
            returncode=1, completion_text=_CODEX_QUOTA_TEXT, events_written=0, session_id=None, timed_out=False,
        )
        adapter = CodexAdapter("T3")
        with patch("adapters.codex_adapter.spawn_codex", return_value=refused), \
                patch("adapters.codex_adapter.shutil.which", return_value="/usr/bin/codex"):
            assert adapter.is_available() is True
            result = adapter.execute("review", {"changed_files": []})
            assert result.status == "failed"
            assert adapter.is_available() is False
        assert pr.get("codex", state_dir=state_dir).source == "adapter:codex"

    def test_a_codex_run_that_timed_out_marks_nothing(self, state_dir):
        from adapters.codex_adapter import CodexAdapter
        from provider_spawns.codex_spawn import CodexSpawnResult

        timed_out = CodexSpawnResult(
            returncode=-9, completion_text="", events_written=0, session_id=None, timed_out=True,
            error="total_deadline exceeded",
        )
        with patch("adapters.codex_adapter.spawn_codex", return_value=timed_out):
            CodexAdapter("T3").execute("review", {"changed_files": []})
        assert pr.get("codex", state_dir=state_dir).state is State.UNMEASURED

    def test_an_answered_codex_run_confirms_it(self, state_dir):
        from adapters.codex_adapter import CodexAdapter
        from provider_spawns.codex_spawn import CodexSpawnResult

        answered = CodexSpawnResult(
            returncode=0, completion_text="findings", events_written=3, session_id="s", timed_out=False,
        )
        with patch("adapters.codex_adapter.spawn_codex", return_value=answered):
            CodexAdapter("T3").execute("review", {"changed_files": []})
        assert pr.get("codex", state_dir=state_dir).is_reachable is True

    def test_a_litellm_run_rejected_with_401_marks_litellm_unreachable(self, state_dir):
        from adapters.litellm_adapter import LiteLLMAdapter
        from provider_spawns.litellm_spawn import LiteLLMSpawnResult

        rejected = LiteLLMSpawnResult(
            returncode=1, completion_text="", events_written=0, session_id=None, timed_out=False,
            error=_OPENROUTER_401_TEXT,
        )
        adapter = LiteLLMAdapter("T1")
        with patch("provider_spawns.litellm_spawn.spawn_litellm", return_value=rejected), \
                patch.dict(sys.modules, {"litellm": types.ModuleType("litellm")}):
            assert adapter.is_available() is True
            assert adapter.execute("hi", {}).status == "failed"
            assert adapter.is_available() is False
        assert pr.get("litellm", state_dir=state_dir).reason == pr.REASON_AUTH_401


# ---------------------------------------------------------------------------
# Classifier providers share the record with the lanes they ride
# ---------------------------------------------------------------------------


class TestClassifierProvidersReadTheSharedRecord:
    def test_the_codex_classifier_is_unavailable_once_codex_is_recorded_unreachable(self, state_dir):
        from classifier_providers.codex_provider import CodexProvider

        provider = CodexProvider()
        with patch("classifier_providers.codex_provider.shutil.which", return_value="/usr/bin/codex"):
            assert provider.is_available() is True
            pr.record_failure("codex", _CODEX_QUOTA_TEXT, source="gate_result:codex_gate", state_dir=state_dir)
            assert provider.is_available() is False
            assert provider.reachability().reason == pr.REASON_QUOTA_EXHAUSTED

    def test_the_deepseek_classifier_shares_the_deepseek_lane_key(self, state_dir, monkeypatch):
        from classifier_providers.deepseek_provider import DeepSeekProvider

        monkeypatch.setenv("DEEPSEEK_API_KEY", "placeholder-not-a-real-key")
        provider = DeepSeekProvider()
        with patch("classifier_providers.deepseek_provider.shutil.which", return_value="/usr/bin/claude"):
            assert provider.is_available() is True
            pr.record_failure("deepseek-harness", _DEEPSEEK_402_TEXT, source="test", state_dir=state_dir)
            assert provider.is_available() is False

    def test_a_missing_key_is_still_not_available(self, state_dir, monkeypatch):
        from classifier_providers.deepseek_provider import DeepSeekProvider

        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with patch("classifier_providers.deepseek_provider.shutil.which", return_value="/usr/bin/claude"):
            assert DeepSeekProvider().is_available() is False


# The docs (provider_reachability, smart_router.availability, DISPATCH_RULES,
# CHANGELOG) name three writers: gate results, provider-lane dispatches and the
# codex/litellm adapters. Classifier providers only read. Recording from inside
# classify() would resolve the state dir through a git subprocess on every call,
# so this class fails the moment a classifier turns into a writer, and the docs
# cannot drift back to saying it is one without it.
_RECORD_WRITERS = ("_write", "record_unreachable", "record_failure", "record_success")

# (classifier module, class, reachability key the class reads)
_CLI_CLASSIFIERS = [
    ("classifier_providers.haiku_provider", "HaikuProvider", "claude"),
    ("classifier_providers.codex_provider", "CodexProvider", "codex"),
    ("classifier_providers.gemini_provider", "GeminiProvider", "gemini"),
    ("classifier_providers.deepseek_provider", "DeepSeekProvider", "deepseek-harness"),
]


@contextlib.contextmanager
def _spy_on_every_writer():
    """Replace each way this module can persist a record, and the state-dir
    resolver, with a spy. Yields ``{name: spy}``."""
    with contextlib.ExitStack() as stack:
        yield {
            name: stack.enter_context(patch.object(pr, name))
            for name in (*_RECORD_WRITERS, "_resolve_state_dir")
        }


def _assert_classify_left_the_record_alone(spies, state_dir, key):
    for name, spy in spies.items():
        assert not spy.called, f"classify() called provider_reachability.{name}"
    assert not (state_dir / pr.STATE_SUBDIR).exists()
    assert pr.get(key, state_dir=state_dir).state is State.UNMEASURED


class TestClassifierProvidersNeverWriteTheRecord:
    @pytest.mark.parametrize("module_name,class_name,key", _CLI_CLASSIFIERS, ids=[c[1] for c in _CLI_CLASSIFIERS])
    @pytest.mark.parametrize(
        "returncode,stdout,stderr",
        [(1, "", _CODEX_QUOTA_TEXT), (1, "", _DEEPSEEK_402_TEXT), (0, '{"ok": true}', "")],
        ids=["quota_refused", "balance_refused", "answered"],
    )
    def test_a_cli_classifier_call_writes_no_record(
        self, state_dir, monkeypatch, module_name, class_name, key, returncode, stdout, stderr,
    ):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "placeholder-not-a-real-key")
        provider = getattr(importlib.import_module(module_name), class_name)()
        completed = subprocess.CompletedProcess(args=["cli"], returncode=returncode, stdout=stdout, stderr=stderr)
        with _spy_on_every_writer() as spies, patch(f"{module_name}.subprocess.run", return_value=completed):
            result = provider.classify("prompt")
        assert (result.error is not None) == (returncode != 0), "the stub must reach classify()'s real code"
        _assert_classify_left_the_record_alone(spies, state_dir, key)

    def test_a_refused_ollama_classify_writes_no_record(self, state_dir):
        from classifier_providers.ollama_provider import OllamaProvider

        with _spy_on_every_writer() as spies, patch(
            "classifier_providers.ollama_provider.urllib.request.urlopen",
            side_effect=urllib.error.URLError(_CODEX_QUOTA_TEXT),
        ):
            result = OllamaProvider().classify("prompt")
        assert result.error is not None
        _assert_classify_left_the_record_alone(spies, state_dir, "ollama")

    def test_an_answered_ollama_classify_writes_no_record(self, state_dir):
        from classifier_providers.ollama_provider import OllamaProvider

        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = json.dumps({"response": '{"ok": true}'}).encode("utf-8")
        with _spy_on_every_writer() as spies, patch(
            "classifier_providers.ollama_provider.urllib.request.urlopen", return_value=response,
        ):
            result = OllamaProvider().classify("prompt")
        assert result.error is None
        _assert_classify_left_the_record_alone(spies, state_dir, "ollama")


class TestASkippedProviderSaysWhy:
    def test_the_reason_names_the_recorded_cause_and_where_it_was_seen(self, state_dir):
        from classifier_providers.base import describe_unavailable
        from classifier_providers.codex_provider import CodexProvider

        pr.record_failure("codex", _CODEX_QUOTA_TEXT, source="gate_result:codex_gate", state_dir=state_dir)
        with patch("classifier_providers.codex_provider.shutil.which", return_value="/usr/bin/codex"):
            line = describe_unavailable(CodexProvider())
        assert "quota_exhausted" in line
        assert "gate_result:codex_gate" in line

    def test_a_provider_that_carries_no_record_gets_a_plain_answer(self):
        from classifier_providers.base import ClassifierProvider, describe_unavailable

        class Duck:
            def is_available(self):
                return False

        class NoPresence(ClassifierProvider):
            name = "nopresence"

            def classify(self, prompt, _max_tokens=1500):
                raise NotImplementedError

            def is_available(self):
                return False

        assert describe_unavailable(Duck()) == "not available"
        assert describe_unavailable(NoPresence()) == "not available"

    def test_the_scout_logs_why_it_passed_over_a_provider(self, state_dir, caplog):
        import scout_prepass

        pr.record_failure("codex", _CODEX_QUOTA_TEXT, source="gate_result:codex_gate", state_dir=state_dir)
        with patch("classifier_providers.codex_provider.shutil.which", return_value="/usr/bin/codex"), \
                caplog.at_level(logging.DEBUG, logger="scout_prepass"):
            outcome = scout_prepass._invoke_scout_model("prompt", "codex")
        assert outcome == (None, "", 0.0, 0)
        assert any("unavailable" in r.getMessage() and "quota_exhausted" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Gate results are real outcomes: they feed the record
# ---------------------------------------------------------------------------


@pytest.fixture
def gate_dirs(state_dir):
    requests_dir = state_dir / "review_gates" / "requests"
    results_dir = state_dir / "review_gates" / "results"
    requests_dir.mkdir(parents=True)
    results_dir.mkdir(parents=True)
    return requests_dir, results_dir


def _fail_gate(state_dir, gate_dirs, *, gate, reason, detail, pr_number=700):
    import gate_recorder as rec

    requests_dir, results_dir = gate_dirs
    with patch("gate_recorder.emit_governance_receipt"), patch("gate_recorder.publish_forge_check_run"):
        return rec.record_failure(
            gate=gate, pr_number=pr_number, pr_id="",
            result={"reason": reason, "reason_detail": detail, "duration_seconds": 3.0,
                    "partial_output_lines": 0, "runner_pid": 1},
            request_payload={"gate": gate, "dispatch_id": "d-1", "commit_sha": "a" * 40},
            requests_dir=requests_dir, results_dir=results_dir,
        )


class TestGateResultsFeedTheRecord:
    def test_a_quota_refusal_booked_on_a_gate_marks_the_provider_unreachable(self, state_dir, gate_dirs):
        _fail_gate(state_dir, gate_dirs, gate="codex_gate", reason="exit_nonzero", detail=_CODEX_QUOTA_TEXT)
        status = pr.get("codex", state_dir=state_dir)
        assert status.state is State.UNREACHABLE
        assert status.reason == pr.REASON_QUOTA_EXHAUSTED
        assert status.source == "gate_result:codex_gate"

    def test_the_key_is_the_provider_behind_the_gate_not_the_gate(self, state_dir, gate_dirs):
        _fail_gate(state_dir, gate_dirs, gate="glm_gate", reason="harness_lane_exit_nonzero",
                   detail="402 requires more credits, add more credits")
        assert pr.get("glm-harness", state_dir=state_dir).reason == pr.REASON_INSUFFICIENT_BALANCE
        assert pr.get("glm_gate", state_dir=state_dir).state is State.UNMEASURED

    def test_a_timeout_says_nothing_about_reachability(self, state_dir, gate_dirs):
        _fail_gate(state_dir, gate_dirs, gate="codex_gate", reason="timeout",
                   detail="stalled; last output: You've hit your usage limit")
        assert pr.get("codex", state_dir=state_dir).state is State.UNMEASURED

    def test_a_decided_verdict_confirms_the_provider(self, state_dir, gate_dirs):
        import gate_recorder as rec

        _requests_dir, results_dir = gate_dirs
        payload = {
            "gate": "codex_gate", "pr_number": 710, "status": "pass",
            "contract_hash": "c0ffee", "report_path": "unified_reports/r.md",
            "dispatch_id": "d-2", "blocking_findings": [], "recorded_at": "2026-09-26T10:00:00Z",
        }
        with patch("gate_recorder.emit_governance_receipt"), patch("gate_recorder.publish_forge_check_run"):
            rec.write_result_guarded(results_dir / "pr-710-codex_gate.json", payload, gate="codex_gate", pr_ref="710")
        status = pr.get("codex", state_dir=state_dir, now=datetime(2026, 9, 26, 10, 1, tzinfo=timezone.utc).timestamp())
        assert status.is_reachable is True
        assert status.measured_at == datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc).timestamp()

    def test_a_pass_without_its_evidence_trail_confirms_nothing(self, state_dir, gate_dirs):
        import gate_recorder as rec

        _requests_dir, results_dir = gate_dirs
        payload = {"gate": "codex_gate", "pr_number": 711, "status": "pass", "contract_hash": "",
                   "report_path": "", "dispatch_id": "d-3", "blocking_findings": []}
        with patch("gate_recorder.emit_governance_receipt"), patch("gate_recorder.publish_forge_check_run"):
            rec.write_result_guarded(results_dir / "pr-711-codex_gate.json", payload, gate="codex_gate", pr_ref="711")
        assert pr.get("codex", state_dir=state_dir).state is State.UNMEASURED

    def test_re_annotating_a_refusal_does_not_refresh_its_age(self, state_dir, gate_dirs):
        """The takeover annotation rewrites the record. It must carry the
        original time, or every takeover round would extend the seat's exile."""
        import gate_recorder as rec

        _requests_dir, results_dir = gate_dirs
        booked = _fail_gate(state_dir, gate_dirs, gate="codex_gate", reason="exit_nonzero", detail=_CODEX_QUOTA_TEXT,
                            pr_number=720)
        first = _record_of(state_dir, "codex")["measured_at"]
        rewritten = dict(booked, takeover=True, takeover_from="codex_gate")
        with patch("gate_recorder.emit_governance_receipt"), patch("gate_recorder.publish_forge_check_run"):
            rec.write_result_guarded(results_dir / "pr-720-codex_gate.json", rewritten, gate="codex_gate", pr_ref="720")
        assert _record_of(state_dir, "codex")["measured_at"] == first


# ---------------------------------------------------------------------------
# The seat walk: skip a seat known unreachable, and report why
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(state_dir, tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    data_dir = state_dir.parent
    for d in (state_dir / "review_gates" / "requests", state_dir / "review_gates" / "results",
              data_dir / "unified_reports"):
        d.mkdir(parents=True, exist_ok=True)
    project_root.mkdir(exist_ok=True)
    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(data_dir / "unified_reports"))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    monkeypatch.delenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", raising=False)
    monkeypatch.chdir(project_root)
    import review_gate_manager as rgm

    return rgm.ReviewGateManager()


def _walk(manager, monkeypatch, stack, pr_number):
    """Run the REAL seat walk; only the leaf that would actually ask a provider is recorded."""
    asked: list = []

    def leaf(gate, pr, branch, risk_class, changed_files, mode, dispatch_id):
        asked.append(gate)
        return {"gate": gate, "status": "requested", "pr_number": pr}

    monkeypatch.setattr(manager, "_dispatch_one_review", leaf)
    with patch("governance_receipts.emit_governance_receipt"):
        result = manager.request_reviews(
            pr_number=pr_number, branch="fix/x", review_stack=stack, risk_class="medium",
            changed_files=["scripts/foo.py"], mode="per_pr", dispatch_id="reach-test",
        )
    return asked, result["requested"]


class TestSeatWalkSkipsAnUnreachableSeat:
    def test_an_unmeasured_seat_is_asked_as_before(self, manager, state_dir, monkeypatch):
        asked, requested = _walk(manager, monkeypatch, ["codex_gate"], 901)
        assert asked == ["codex_gate"]
        assert "takeover" not in requested[0]

    def test_a_seat_whose_provider_is_out_of_quota_is_passed_over_and_the_reason_reported(
        self, manager, state_dir, monkeypatch,
    ):
        pr.record_failure("codex", _CODEX_QUOTA_TEXT, source="gate_result:codex_gate", state_dir=state_dir)
        asked, requested = _walk(manager, monkeypatch, ["codex_gate"], 902)

        assert asked == ["kimi_gate"], "the walk must not ask the seat that cannot give a verdict"
        seat = requested[0]
        assert seat["takeover"] is True
        assert seat["takeover_from"] == "codex_gate"
        hop = seat["takeover_path"][0]
        assert hop["gate"] == "codex_gate"
        assert hop["reason"] == pr.REASON_QUOTA_EXHAUSTED
        assert hop["status"] == "unreachable"
        # WHY, and where it was seen, in the canonical field a reader looks at.
        assert "quota_exhausted" in seat["failure_reason"]
        assert "usage limit" in seat["failure_reason"]
        assert "gate_result:codex_gate" in seat["failure_reason"]
        assert "kimi_gate substituted as reader" in seat["failure_reason"]

    def test_the_walk_keeps_going_past_every_seat_known_unreachable(self, manager, state_dir, monkeypatch):
        pr.record_failure("codex", _CODEX_QUOTA_TEXT, source="test", state_dir=state_dir)
        pr.record_failure("kimi", "403 access_terminated_error: reached your usage limit", source="test",
                          state_dir=state_dir)
        asked, requested = _walk(manager, monkeypatch, ["codex_gate"], 903)
        assert asked == ["glm_gate"]
        assert [h["gate"] for h in requested[0]["takeover_path"]] == ["codex_gate", "kimi_gate"]

    def test_a_credential_refusal_is_reported_as_auth_not_as_quota(self, manager, state_dir, monkeypatch):
        pr.record_failure("kimi", "Error code: 401 - invalid api key", source="test", state_dir=state_dir)
        asked, requested = _walk(manager, monkeypatch, ["kimi_gate"], 904)
        assert asked == ["glm_gate"]
        assert requested[0]["takeover_path"][0]["reason"] == pr.REASON_AUTH_401

    def test_when_every_seat_is_unreachable_the_chain_ends_named_and_asks_nobody(self, manager, state_dir, monkeypatch):
        pr.record_failure("codex", _CODEX_QUOTA_TEXT, source="test", state_dir=state_dir)
        pr.record_failure("kimi", "Error code: 401 - invalid api key", source="test", state_dir=state_dir)
        pr.record_failure("glm-harness", "402 requires more credits", source="test", state_dir=state_dir)
        pr.record_failure("deepseek-harness", _DEEPSEEK_402_TEXT, source="test", state_dir=state_dir)

        asked, requested = _walk(manager, monkeypatch, ["codex_gate"], 905)

        assert asked == []
        seat = requested[0]
        assert seat["status"] == "chain_exhausted"
        assert seat["reason"] == "takeover_chain_exhausted"
        for gate, reason in (("codex_gate", "quota_exhausted"), ("kimi_gate", "auth_401"),
                             ("glm_gate", "insufficient_balance"), ("deepseek_gate", "insufficient_balance")):
            assert f"{gate} unavailable ({reason})" in seat["reason_detail"]

    def test_an_expired_record_is_not_a_reason_to_skip_the_seat(self, manager, state_dir, monkeypatch):
        long_ago = datetime.now(timezone.utc).timestamp() - pr.unreachable_ttl_seconds() - 60
        pr.record_failure("codex", _CODEX_QUOTA_TEXT, source="test", state_dir=state_dir,
                          measured_at=long_ago, now=long_ago)
        asked, requested = _walk(manager, monkeypatch, ["codex_gate"], 906)
        assert asked == ["codex_gate"]
        assert "takeover" not in requested[0]

    def test_a_seat_that_already_answered_on_this_head_is_not_overruled_by_the_provider_record(
        self, manager, state_dir, monkeypatch,
    ):
        head = "a" * 40
        result = {"gate": "codex_gate", "pr_number": 907, "status": "pass", "contract_hash": "c0ffee",
                  "report_path": "unified_reports/r.md", "dispatch_id": "d-9", "blocking_findings": [],
                  "commit_sha": head, "recorded_at": "2026-09-26T10:00:00Z"}
        (state_dir / "review_gates" / "results" / "pr-907-codex_gate.json").write_text(
            json.dumps(result), encoding="utf-8")
        pr.record_failure("codex", _CODEX_QUOTA_TEXT, source="test", state_dir=state_dir)
        monkeypatch.setattr("gate_request_handler.get_pr_head_sha", lambda _pr: head)

        asked, requested = _walk(manager, monkeypatch, ["codex_gate"], 907)

        assert asked == ["codex_gate"], "the PR's own answer from this seat decides, not the provider record"
        assert "takeover" not in requested[0]

    def test_a_record_for_another_provider_skips_nothing(self, manager, state_dir, monkeypatch):
        pr.record_failure("glm-harness", "402 requires more credits", source="test", state_dir=state_dir)
        asked, _requested = _walk(manager, monkeypatch, ["codex_gate"], 908)
        assert asked == ["codex_gate"]

    def test_a_gate_outside_the_takeover_chain_is_dispatched_as_before(self, manager, state_dir, monkeypatch):
        pr.record_failure("gemini", "429 exceeded your current quota", source="test", state_dir=state_dir)
        asked, requested = _walk(manager, monkeypatch, ["gemini_review"], 909)
        assert asked == ["gemini_review"]
        assert "takeover" not in requested[0]


# ---------------------------------------------------------------------------
# Provider-lane dispatches and the smart router
# ---------------------------------------------------------------------------


class TestProviderDispatchFeedsTheRecord:
    def _feed(self, state_dir, **kw):
        import provider_dispatch as pd

        base = dict(provider="kimi", status="failed", failure_class=None, reason="", state_dir=state_dir)
        pd._maybe_record_provider_reachability(**{**base, **kw})

    def test_a_successful_dispatch_confirms_the_provider(self, state_dir):
        self._feed(state_dir, provider="kimi", status="success")
        assert pr.get("kimi", state_dir=state_dir).is_reachable is True

    def test_a_credit_exhausted_dispatch_marks_it_unreachable_with_its_reason(self, state_dir):
        self._feed(state_dir, provider="deepseek-harness", failure_class="credit_exhausted", reason="402 Insufficient Balance")
        status = pr.get("deepseek-harness", state_dir=state_dir)
        assert status.reason == pr.REASON_INSUFFICIENT_BALANCE
        assert status.source == "provider_dispatch:deepseek-harness"

    def test_an_auth_rejected_dispatch_marks_it_unreachable_as_auth(self, state_dir):
        self._feed(state_dir, provider="glm-harness", failure_class="auth_rejected", reason="401 API key expired")
        assert pr.get("glm-harness", state_dir=state_dir).reason == pr.REASON_AUTH_401

    def test_a_model_error_or_timeout_says_nothing(self, state_dir):
        self._feed(state_dir, provider="kimi", failure_class="model_error", reason="503 overloaded")
        self._feed(state_dir, provider="kimi", failure_class="timeout", reason="deadline exceeded")
        assert pr.get("kimi", state_dir=state_dir).state is State.UNMEASURED

    def test_a_litellm_sub_provider_route_is_not_modelled(self, state_dir):
        self._feed(state_dir, provider="litellm:deepseek:deepseek-v4-pro", status="success")
        assert not (state_dir / pr.STATE_SUBDIR).exists()


class TestSmartRouterSkipsAnUnreachableLane:
    def test_a_lane_recorded_unreachable_is_not_available_and_says_why(self, state_dir):
        from providers.smart_router.availability import lane_available

        pr.record_failure("codex", _CODEX_QUOTA_TEXT, source="gate_result:codex_gate", state_dir=state_dir)
        ok, reason = lane_available("codex", env={}, state_dir=state_dir)
        assert ok is False
        assert "unreachable" in reason and "quota_exhausted" in reason

    def test_a_lane_nobody_asked_is_still_available(self, state_dir):
        from providers.smart_router.availability import lane_available

        assert lane_available("codex", env={}, state_dir=state_dir) == (True, "available")
