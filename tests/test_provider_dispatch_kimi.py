"""Tests for the kimi provider routing in provider_dispatch.py (Wave 7.7)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_LIB_DIR = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)


def _build_args(**overrides):
    """Build a minimal argparse.Namespace for dispatch tests."""
    import argparse

    defaults = {
        "provider": "kimi",
        "terminal_id": "T1",
        "dispatch_id": "test-dispatch-kimi-01",
        "instruction": "Say hi",
        "model": "default",
        "max_retries": 3,
        "no_auto_commit": False,
        "gate": "",
        "dispatch_paths": "",
        "pr_id": None,
        "role": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestProviderDispatchKimiArgParser(unittest.TestCase):
    def test_provider_kimi_accepted_in_arg_parser(self):
        import provider_dispatch as pd

        parser = pd._build_parser()
        args = parser.parse_args([
            "--provider", "kimi",
            "--terminal-id", "T1",
            "--dispatch-id", "d1",
            "--instruction", "test",
        ])
        self.assertEqual(args.provider, "kimi")

    def test_parser_help_mentions_kimi(self):
        import provider_dispatch as pd

        parser = pd._build_parser()
        help_text = parser.format_help()
        self.assertIn("kimi", help_text)


class TestDispatchKimiSuccess(unittest.TestCase):
    def _make_success_result(self):
        from provider_spawns.kimi_spawn import KimiSpawnResult

        return KimiSpawnResult(
            returncode=0,
            completion_text="Hello from Kimi!",
            events_written=3,
            session_id=None,
            timed_out=False,
            stopped_early=False,
            token_usage={"input_tokens": 50, "output_tokens": 20, "cache_read_tokens": 0, "cache_creation_tokens": 0},
            error=None,
            event_writer_failures=0,
        )

    def test_dispatch_kimi_emits_receipt_with_provider_kimi(self):
        import provider_dispatch as pd

        args = _build_args()
        result = self._make_success_result()

        with patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=result), \
             patch("event_store.EventStore", return_value=MagicMock()), \
             patch("governance_emit.emit_dispatch_receipt") as mock_receipt, \
             patch("governance_emit.emit_unified_report") as mock_report:
            mock_receipt.return_value = Path("/tmp/receipts.ndjson")
            mock_report.return_value = Path("/tmp/report.md")
            exit_code = pd._dispatch_kimi(args)

        self.assertEqual(exit_code, 0)
        mock_receipt.assert_called_once()
        # provider is always passed as keyword arg
        call_kwargs = mock_receipt.call_args.kwargs
        self.assertEqual(call_kwargs.get("provider"), "kimi")

    def test_dispatch_kimi_emits_unified_report(self):
        import provider_dispatch as pd

        args = _build_args()
        result = self._make_success_result()

        with patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=result), \
             patch("event_store.EventStore", return_value=MagicMock()), \
             patch("governance_emit.emit_dispatch_receipt") as mock_receipt, \
             patch("governance_emit.emit_unified_report") as mock_report:
            mock_receipt.return_value = Path("/tmp/receipts.ndjson")
            mock_report.return_value = Path("/tmp/report.md")
            pd._dispatch_kimi(args)

        mock_report.assert_called_once()

    def test_dispatch_kimi_failure_emits_receipt_with_status_failure(self):
        import provider_dispatch as pd
        from provider_spawns.kimi_spawn import KimiSpawnResult

        args = _build_args()
        fail_result = KimiSpawnResult(
            returncode=1,
            completion_text="",
            events_written=0,
            session_id=None,
            timed_out=False,
            error="kimi exited with code 1",
        )

        with patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=fail_result), \
             patch("event_store.EventStore", return_value=MagicMock()), \
             patch("governance_emit.emit_dispatch_receipt") as mock_receipt, \
             patch("governance_emit.emit_unified_report") as mock_report:
            mock_receipt.return_value = Path("/tmp/receipts.ndjson")
            mock_report.return_value = Path("/tmp/report.md")
            exit_code = pd._dispatch_kimi(args)

        self.assertEqual(exit_code, 1)
        mock_receipt.assert_called_once()
        call_kwargs = mock_receipt.call_args.kwargs
        self.assertEqual(call_kwargs.get("status"), "failure")

    def test_event_store_init_failure_returns_nonzero(self):
        import provider_dispatch as pd

        args = _build_args()

        with patch("event_store.EventStore", side_effect=Exception("db unavailable")), \
             patch("governance_emit.emit_dispatch_receipt") as mock_receipt, \
             patch("governance_emit.emit_unified_report") as mock_report:
            exit_code = pd._dispatch_kimi(args)

        self.assertNotEqual(exit_code, 0)
        # No success receipt may be emitted when audit sink is unavailable
        for call in mock_receipt.call_args_list:
            self.assertNotEqual(call.kwargs.get("status"), "success")

    def test_event_writer_failures_returns_nonzero_with_success_receipt(self):
        """event_writer_failures signals an audit gap: exit_code=2, status='success'.

        The dispatch itself completed correctly (worker produced output); the
        event_writer_failures field flags an ADR-005 audit-trail gap, not a
        dispatch failure.  All handlers (codex, gemini, litellm, kimi) consistently
        emit status='success' with return code 2 for this case.
        """
        import provider_dispatch as pd
        from provider_spawns.kimi_spawn import KimiSpawnResult

        args = _build_args()
        audit_gap_result = KimiSpawnResult(
            returncode=0,
            completion_text="done",
            events_written=5,
            session_id=None,
            timed_out=False,
            stopped_early=False,
            token_usage={"input_tokens": 100, "output_tokens": 40, "cache_read_tokens": 0, "cache_creation_tokens": 0},
            error=None,
            event_writer_failures=3,
        )

        with patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=audit_gap_result), \
             patch("event_store.EventStore", return_value=MagicMock()), \
             patch("governance_emit.emit_dispatch_receipt") as mock_receipt, \
             patch("governance_emit.emit_unified_report") as mock_report:
            mock_receipt.return_value = Path("/tmp/receipts.ndjson")
            mock_report.return_value = Path("/tmp/report.md")
            exit_code = pd._dispatch_kimi(args)

        # exit_code=2 signals the audit gap to the caller — not zero, not one.
        self.assertEqual(exit_code, 2)
        mock_receipt.assert_called_once()
        call_kwargs = mock_receipt.call_args.kwargs
        # Dispatch outcome is success; the audit gap is reported via exit_code=2.
        self.assertEqual(call_kwargs.get("status"), "success")


class TestComputeKimiCost(unittest.TestCase):
    def test_cost_computed_when_usage_present(self):
        import provider_dispatch as pd

        token_usage = {"input": 1_000_000, "output": 500_000, "cache_hit": 0}
        # Without registry (isolated): returns None gracefully
        with patch("provider_dispatch._compute_kimi_cost") as mock_cost:
            mock_cost.return_value = 0.00185
            cost = pd._compute_cost("kimi", "kimi-default", token_usage)
        self.assertEqual(cost, 0.00185)

    def test_compute_kimi_cost_returns_none_on_zero_usage(self):
        import provider_dispatch as pd

        cost = pd._compute_kimi_cost("kimi-default", {"input": 0, "output": 0, "cache_hit": 0})
        self.assertIsNone(cost)

    def test_compute_kimi_cost_returns_none_when_registry_missing(self):
        import provider_dispatch as pd

        with patch("provider_dispatch._compute_kimi_cost", wraps=pd._compute_kimi_cost):
            # Patch load to raise FileNotFoundError
            with patch("providers.provider_registry.load", side_effect=FileNotFoundError):
                cost = pd._compute_kimi_cost("kimi-default", {"input": 100, "output": 50, "cache_hit": 0})
        self.assertIsNone(cost)

    def test_extract_token_usage_kimi_uses_input_output_keys(self):
        import provider_dispatch as pd
        from provider_spawns.kimi_spawn import KimiSpawnResult

        result = KimiSpawnResult(
            returncode=0,
            completion_text="",
            events_written=1,
            session_id=None,
            timed_out=False,
            token_usage={"input_tokens": 300, "output_tokens": 120, "cache_read_tokens": 5, "cache_creation_tokens": 0},
        )
        usage = pd._extract_token_usage(result, "kimi")
        self.assertEqual(usage["input"], 300)
        self.assertEqual(usage["output"], 120)
        self.assertEqual(usage["cache_hit"], 5)
        self.assertNotIn("unavailable", usage)

    def test_extract_token_usage_kimi_no_usage_reported_is_marked_unavailable(self):
        """kimi-cli stream-json reports no usage event -> token_usage stays None on the
        spawn result. The receipt must record this as explicitly unavailable, not 0/0
        (a bare 0/0 would be indistinguishable from a real zero-token dispatch)."""
        import provider_dispatch as pd
        from provider_spawns.kimi_spawn import KimiSpawnResult

        result = KimiSpawnResult(
            returncode=0,
            completion_text="done",
            events_written=1,
            session_id=None,
            timed_out=False,
            token_usage=None,
        )
        self.assertFalse(result.token_usage_measured)
        usage = pd._extract_token_usage(result, "kimi")
        self.assertEqual(usage["input"], 0)
        self.assertEqual(usage["output"], 0)
        self.assertTrue(usage["unavailable"])
        # Cost computation must treat unavailable as not-billable, not $0-confirmed.
        self.assertIsNone(pd._compute_cost("kimi", "kimi-default", usage))



class TestComputeKimiCostRefusesToGuess(unittest.TestCase):
    """OI-1361: an unknown kimi model must yield None, never a fabricated price.

    ``_compute_kimi_cost`` ended on ``next(iter(cfg.models.values()), None)``, so any
    model name the registry does not know inherited the price of whichever entry
    happened to sit FIRST in the kimi_cli section — today ``kimi-k3`` at 3.00 in /
    15.00 out. That invented number was then written to the receipt as a confirmed
    ``cost_usd``, indistinguishable from a real one.

    The sibling lookup ``_load_pricing_from_registry`` had exactly this construction
    removed in PR #1609 (OI-1355) and now logs "miss, not a fabricated price". This is
    the same fix in the handler that was left behind.
    """

    #: One MTok in and one MTok out, so cost_usd equals the per-MTok prices summed.
    ONE_MTOK = {"input": 1_000_000, "output": 1_000_000, "cache_hit": 0}

    def test_unknown_model_returns_none_instead_of_the_first_entry_price(self):
        import provider_dispatch as pd

        # kimi-k3 is first in the section (3.00 + 15.00), so the old guess was 18.0.
        self.assertIsNone(pd._compute_kimi_cost("kimi-k9-does-not-exist", self.ONE_MTOK))

    def test_unknown_model_is_not_priced_as_any_registry_entry(self):
        """Stronger than the previous test: the miss must not equal ANY known price,
        so a future reordering of the section cannot make this test pass by accident."""
        import provider_dispatch as pd
        from providers import provider_registry as _reg

        cfg = _reg.load().get("kimi_cli")
        every_price = {
            round(m.cost_input_per_mtok + m.cost_output_per_mtok, 8)
            for m in cfg.models.values()
        }
        cost = pd._compute_kimi_cost("totally-not-a-kimi-model", self.ONE_MTOK)
        self.assertIsNone(cost)
        self.assertNotIn(cost, every_price)

    def test_known_models_keep_pricing_exactly(self):
        """Every registry key that names a real model prices as itself.

        Keys that the canonical resolver treats as a bare ALIAS are skipped and covered
        by test_alias_key_prices_as_the_model_it_aliases below — asserting an alias
        prices as its own registry row would be asserting the bug."""
        import provider_dispatch as pd
        from providers import provider_registry as _reg

        cfg = _reg.load().get("kimi_cli")
        for key, entry in cfg.models.items():
            if pd._kimi_resolve_requested_key(key) != key:
                continue  # alias, not a selectable model
            expected = round(entry.cost_input_per_mtok + entry.cost_output_per_mtok, 8)
            with self.subTest(model=key):
                self.assertEqual(pd._compute_kimi_cost(key, self.ONE_MTOK), expected)

    def test_alias_key_prices_as_the_model_it_aliases(self):
        """"kimi-default" is a bare alias for the K3 default (_KIMI_BARE_ALIASES), and
        ALSO exists as its own registry row at a different price. A dispatch naming it
        runs on K3, so it must be priced as K3 — the same-named row is unreachable.

        The unreachable row itself is a registry-hygiene problem, not a pricing one; it
        is reported separately rather than silently deleted here."""
        import provider_dispatch as pd
        from providers import provider_registry as _reg

        cfg = _reg.load().get("kimi_cli")
        self.assertIn("kimi-default", cfg.models, "fixture assumes the shadowed row exists")

        aliased_to = cfg.models[pd._kimi_resolve_requested_key("kimi-default")]
        expected = round(aliased_to.cost_input_per_mtok + aliased_to.cost_output_per_mtok, 8)
        self.assertEqual(pd._compute_kimi_cost("kimi-default", self.ONE_MTOK), expected)

    def test_absent_model_is_priced_as_the_model_that_actually_runs(self):
        """An absent model name must be priced as the model the spawn seam will really
        use, not as the "kimi-default" registry entry.

        This is the expensive half of OI-1361. _kimi_resolve_requested_key (shared by
        the spawn seam, governance labeling and the constraint pre-flight) resolves an
        absent model to the registry's K3 default at 3.00/15.00. _compute_kimi_cost had
        its own rule and charged kimi-default's 0.60/2.50 instead — every kimi dispatch
        without an explicit model was under-reported by a factor 5.8."""
        import os
        from unittest.mock import patch

        import provider_dispatch as pd
        from providers import provider_registry as _reg

        # _kimi_resolve_requested_key consults VNX_KIMI_MODEL. Leaning on that var
        # being absent makes the test depend on the operator's shell; pin it empty.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VNX_KIMI_MODEL", None)
            cfg = _reg.load().get("kimi_cli")
            ran = cfg.models[pd._kimi_resolve_requested_key(None)]
            expected = round(ran.cost_input_per_mtok + ran.cost_output_per_mtok, 8)

            wrong = cfg.models["kimi-default"]
            wrong_price = round(wrong.cost_input_per_mtok + wrong.cost_output_per_mtok, 8)
            self.assertNotEqual(expected, wrong_price, "fixture no longer discriminates")

            for placeholder in ("", None, "default", "sonnet", "kimi", "kimi_cli"):
                with self.subTest(model=placeholder):
                    self.assertEqual(
                        pd._compute_kimi_cost(placeholder, self.ONE_MTOK), expected
                    )

    def test_pricing_key_agrees_with_the_canonical_resolver(self):
        """The pricing handler and the spawn seam must never disagree about which model
        a dispatch is. Two independent resolvers for one question is the defect."""
        import provider_dispatch as pd
        from providers import provider_registry as _reg

        cfg = _reg.load().get("kimi_cli")
        for raw in ("", None, "default", "sonnet", "kimi", "kimi-default", "kimi_cli",
                    "kimi-k3", "kimi-k2-6", "kimi-k2-7"):
            key = pd._kimi_resolve_requested_key(raw)
            entry = cfg.models.get(key)
            if entry is None:
                continue
            expected = round(entry.cost_input_per_mtok + entry.cost_output_per_mtok, 8)
            with self.subTest(model=raw, resolved=key):
                self.assertEqual(pd._compute_kimi_cost(raw, self.ONE_MTOK), expected)

    def test_dated_suffix_resolves_to_its_base_model(self):
        """A real dated model id must still resolve — refusing to guess is not the same
        as refusing to resolve.

        Deliberately uses a suffix of kimi-k2-6, NOT of kimi-k3. A kimi-k3 suffix would
        pass on the broken code too, because the first-entry guess happens to BE kimi-k3
        — the same correct answer reached by the wrong mechanism. Pinning a non-first
        model makes the test discriminate."""
        import provider_dispatch as pd
        from providers import provider_registry as _reg

        k26 = _reg.load().get("kimi_cli").models["kimi-k2-6"]
        expected = round(k26.cost_input_per_mtok + k26.cost_output_per_mtok, 8)
        self.assertEqual(pd._compute_kimi_cost("kimi-k2-6-20260901", self.ONE_MTOK), expected)

    def test_miss_is_logged_as_a_miss(self):
        import logging

        import provider_dispatch as pd

        with self.assertLogs("provider_dispatch", level=logging.WARNING) as captured:
            pd._compute_kimi_cost("kimi-k9-does-not-exist", self.ONE_MTOK)
        joined = "\n".join(captured.output)
        self.assertIn("kimi-k9-does-not-exist", joined)
        self.assertIn("not a fabricated price", joined)

    def test_cli_arg_form_prices_the_model_that_ran(self):
        """The envelope lane stamps adapter_result.model with the CLI ARG, not a
        registry key (envelope_adapters_provider -> envelope_govern -> _compute_cost).

        On main the first-entry guess made "kimi-code/k3" come out right by accident
        and "kimi-code/kimi-for-coding" come out 5.8x too high (18.00 for a model that
        costs 3.10). Both now resolve through the registry's own cli_model_arg field."""
        import provider_dispatch as pd
        from providers import provider_registry as _reg

        cfg = _reg.load().get("kimi_cli")
        priced = 0
        for key, entry in cfg.models.items():
            arg = getattr(entry, "cli_model_arg", None)
            if not arg:
                continue
            expected = round(entry.cost_input_per_mtok + entry.cost_output_per_mtok, 8)
            with self.subTest(cli_arg=arg, registry_key=key):
                self.assertEqual(pd._compute_kimi_cost(arg, self.ONE_MTOK), expected)
            priced += 1
        self.assertGreaterEqual(priced, 2, "fixture expects at least two mapped CLI args")

    def test_cli_arg_form_that_maps_to_nothing_is_still_a_miss(self):
        import provider_dispatch as pd

        self.assertIsNone(pd._compute_kimi_cost("kimi-code/not-a-real-arg", self.ONE_MTOK))

    def test_ambiguous_cli_arg_refuses_to_pick(self):
        """Two registry entries sharing one CLI arg is drift. Picking one would put
        iteration order back in charge of a price, which is the defect being closed."""
        import copy

        import provider_dispatch as pd
        from providers import provider_registry as _reg

        cfg = copy.deepcopy(_reg.load().get("kimi_cli"))
        keys = [k for k, m in cfg.models.items() if getattr(m, "cli_model_arg", None)]
        self.assertGreaterEqual(len(keys), 2)
        shared = cfg.models[keys[0]].cli_model_arg
        object.__setattr__(cfg.models[keys[1]], "cli_model_arg", shared)

        self.assertIsNone(pd._kimi_entry_from_cli_arg(cfg, shared))


    def test_cli_arg_arriving_via_the_env_override_is_priced(self):
        """VNX_KIMI_MODEL can itself hold the CLI-arg form. With no explicit -m the
        canonical resolver returns it unchanged, so the CLI-arg reverse map has to be
        tried on the RESOLVED key too, not only on the raw argument."""
        import os
        from unittest.mock import patch

        import provider_dispatch as pd

        with patch.dict(os.environ, {"VNX_KIMI_MODEL": "kimi-code/k3"}):
            self.assertEqual(pd._kimi_resolve_requested_key(None), "kimi-code/k3")
            expected = pd._compute_kimi_cost("kimi-k3", self.ONE_MTOK)
            self.assertEqual(pd._compute_kimi_cost(None, self.ONE_MTOK), expected)

    def test_cli_arg_match_is_case_insensitive_like_the_spawn_side(self):
        """_kimi_resolve_cli_model_arg compares cli_model_arg case-insensitively. A
        case variant that resolves at spawn must not miss at cost."""
        import provider_dispatch as pd
        from providers import provider_registry as _reg

        cfg = _reg.load().get("kimi_cli")
        arg = next(
            m.cli_model_arg for m in cfg.models.values() if getattr(m, "cli_model_arg", None)
        )
        self.assertEqual(
            pd._compute_kimi_cost(arg.upper(), self.ONE_MTOK),
            pd._compute_kimi_cost(arg, self.ONE_MTOK),
        )

    def test_disabled_models_are_still_priceable(self):
        """Spawn asks "may this run"; cost asks "what did the thing that already ran
        charge". A model disabled after the fact must still price, or real spend is
        lost from the ledger."""
        import provider_dispatch as pd
        from providers import provider_registry as _reg

        cfg = _reg.load().get("kimi_cli")
        disabled = [
            (k, m) for k, m in cfg.models.items() if not getattr(m, "dispatch_allowed", True)
        ]
        self.assertTrue(disabled, "fixture expects at least one disabled kimi model")
        for key, entry in disabled:
            expected = round(entry.cost_input_per_mtok + entry.cost_output_per_mtok, 8)
            with self.subTest(model=key):
                self.assertEqual(pd._compute_kimi_cost(key, self.ONE_MTOK), expected)

    def test_the_miss_warning_names_the_raw_input_too(self):
        """The forensic value of the miss log is the string that actually arrived, not
        only what it resolved to."""
        import logging

        import provider_dispatch as pd

        with self.assertLogs("provider_dispatch", level=logging.WARNING) as captured:
            pd._compute_kimi_cost("kimi-code/not-a-real-arg", self.ONE_MTOK)
        self.assertIn("kimi-code/not-a-real-arg", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
