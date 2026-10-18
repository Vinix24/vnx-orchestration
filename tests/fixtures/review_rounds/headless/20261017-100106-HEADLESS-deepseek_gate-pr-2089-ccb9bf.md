# deepseek_gate — Headless Gate Report

**PR**: 2089
**Branch**: dispatch/20261017-cr-e-export
**Gate**: deepseek_gate
**Generated**: 2026-10-17T10:06:54Z

---

## Findings

Blocking count: 0
Advisory count: 6

### Advisory

- The leak scan runs on the working tree after `git commit`, so a target-repo hook that rewrites an exported file makes the scrub check bytes that differ from the committed blob, and the exporter still exits 0 leaving the clone dirty (reproduced with a pre-commit hook that rewrites context-rotation/hooks/run.sh). Scanning the committed blobs (or re-checking cleanliness after commit) would close the gap. (severity: warning)
- `cells()` splits on every `|`, so a template row containing an escaped `\|` is counted as extra columns and the new Tools row gets extra empty cells (misaligned table). (severity: info)
- The 'row already present' check scans every table line in the whole README, not just the `## Tools` section; a `context-rotation/` mention in an unrelated table suppresses adding the Tools row entirely. (severity: info)
- `json.loads(new)` is outside the `try`, so a merge that yields invalid JSON exits with a Python traceback instead of the intended 'cannot merge into <file>' message; it still fails closed via the shell's `|| die`. (severity: info)
- The README says a hit on a line 'the base already had' is marked pre-existing, but nothing is passed as the base for files inside the exported folder, so every hit there is reported as new. Behavior is safe (the export still fails), but narrower than documented; the script's own comment says 'toolkit-level line'. (severity: info)
- The content-change guard uses `diff -r`, which ignores file modes: an executable-bit-only change without a version bump is accepted. (severity: info)
