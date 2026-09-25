#!/usr/bin/env bash
# vnx_context_monitor.sh — retired worker context monitor, kept as a silent no-op.
#
# This hook used to read $VNX_STATE_DIR/context_window_T{1,2,3}.json and ask a T1-T3 worker
# for a rotation handover at 65% context. Nothing has written that file for a long time (its
# only writer was an F43-branch adapter feature that never reached main; see the skipped test
# in tests/test_headless_context_rotation.py), so every branch below the file check was dead.
#
# The dead branch is removed rather than rewired to the new transcript measurement
# (scripts/lib/t0_context_budget.py) on purpose: operator decision 2026-09-25 puts context
# rotation on T0 ONLY, never on workers, and the T0 enforcement lives in
# scripts/hooks/t0_context_guard.py.
#
# The file itself stays because consumer settings still pin it (SEOcrawler_v2
# .claude/settings.json, via .claude/vnx-system/hooks/vnx_context_monitor.sh). A deleted hook
# target makes Claude Code print a hook error on every tool call; a silent exit 0 does not.
# Drop it once no consumer pin points here (`vnx doctor` hook-pin check).
cat >/dev/null
exit 0
