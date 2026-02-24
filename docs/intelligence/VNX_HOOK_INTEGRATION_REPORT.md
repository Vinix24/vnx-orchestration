# VNX Hook Integration Report - Context Rotation v2.4

**Status**: Evidence-backed (probe + logs)
**Date**: 2026-02-23
**Scope**: Hook payload probe (T-P60), hook event logging, receipt payload shape, deployment decision on settings scope.

---

## Summary

- **Stop hook payload probe (T-P60)** captured valid payloads from both **T0** and **T1**, confirming hook invocation in terminal context and payload shape.
- **Decision**: **Root-level hook registration** is approved for rollout (no terminal-specific hook registration required), based on the T-P60 probe evidence.
- **Receipts** for `context_rotation` are **informational** only; T0 does not need to take action solely based on these receipts.
- **Public rollout** remains **opt-in / experimental** via `VNX_CONTEXT_ROTATION_ENABLED=1`.

---

## Evidence Used (Paths)

- `$PROJECT_ROOT` = `/Users/vincentvandeth/Development/SEOcrawler_v2`
- `$TEST_ROOT` = `/tmp/vnx-rotation-test`
- Hook payload probe log: `$PROJECT_ROOT/.vnx-data/logs/hook_payload_probe.log`
- Hook events log (test env): `$TEST_ROOT/vnx-data/logs/hook_events.log`
- Test receipts file (test env): `$TEST_ROOT/test_receipts.ndjson`
- Stop hook unit-test summary (T-U09..T-U15): `$PROJECT_ROOT/.vnx-data/logs/cr2_tu09_tu15_context_monitor.log`

---

## T-P60 Probe Outcome (Settings Scope)

**Finding**: Stop hook payloads were captured for **T1** (and **T0**), with `cwd` and `PWD` reflecting terminal directories. This confirms that the configured hooks execute in terminal context as expected.

**Decision**: Register hooks at **root settings** (`.claude/settings.json`). Terminal-specific settings are not required for the context rotation hooks.

**Evidence**:
- `$PROJECT_ROOT/.vnx-data/logs/hook_payload_probe.log` shows Stop hook payload for `PWD: .../.claude/terminals/T1` with a valid JSON payload.
- The same log shows Stop hook payload from `T0` (baseline verification).

---

## Payload Format (Stop Hook)

Observed fields include:
- `session_id`, `transcript_path`, `cwd`, `permission_mode`
- `hook_event_name` (Stop)
- `stop_hook_active`
- `last_assistant_message`

This aligns with the expected payload usage in `vnx_context_monitor.sh` for context usage logic and the `stop_hook_active` loop-prevention guard.

---

## Receipt Treatment (T0)

`context_rotation` receipts are **informational** and do **not** require T0 action. T0 decisions remain based on dispatch receipts and explicit work artifacts.

---

## Public Rollout Mode

- **Mode**: Experimental / opt-in
- **Activation**: `VNX_CONTEXT_ROTATION_ENABLED=1`
- **Default**: Off (no-op)
- **Compatibility**: Backward-compatible for public repos and existing workflows

---

## Known Limitations / Notes

- T-P60 proves hook invocation in terminal context, not absolute precedence if a terminal overrides settings. No terminal override case was asserted in the probe log.
- Hook event logging and receipt generation validated in the test sandbox only.
