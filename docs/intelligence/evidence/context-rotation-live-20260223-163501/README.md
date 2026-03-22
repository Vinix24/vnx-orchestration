# VNX Context Rotation Live Evidence Bundle (T1)

- Captured: 2026-02-23 16:35:01 UTC
- Scenario: Forced live validation by writing a test `T1-ROTATION-HANDOVER` file from T1
- Result: Core rotation pipeline triggered (`PostToolUse` detector -> tmux rotator -> `/clear` -> continuation prompt paste)
- Notes: Continuation prompt paste succeeded; Enter timing/UI readiness showed a minor race (fallback delay path used)

## Included files
- `hook_events.snippet.log`
- `vnx_rotate_T1.snippet.log`
- `t0_receipt_context_rotation.snippet.ndjson`
- `handover_test_T1.md`
- `evidence_summary.md`
