# deepseek_gate — Headless Gate Report

**PR**: 2080
**Branch**: dispatch/20261016-cr-b1-state-inject
**Gate**: deepseek_gate
**Generated**: 2026-10-16T18:31:57Z

---

## Findings

Blocking count: 0
Advisory count: 2

### Advisory

- The B1 entry claims every name and path from a marker, including the transcript position, 'must be the one its own session id and kind give', but _position_problem (lib/rotation_state.py:326) only shape-checks the transcript (absolute path, int size, str/None uuid). Doc over-claims relative to code; not a security input, so non-blocking. (severity: info)
- trim_archive can delete the archive copy that claimed_bytes() relies on as the recovery record for an unfinished claim. Narrow window (more than archive_keep newer injections plus a later rewrite of the original), and the original handover usually still exists, so it is normally still resumable. (severity: info)
