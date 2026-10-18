# deepseek_gate — Headless Gate Report

**PR**: 2080
**Branch**: dispatch/20261016-cr-b1-state-inject
**Gate**: deepseek_gate
**Generated**: 2026-10-16T17:47:26Z

---

## Findings

Blocking count: 0
Advisory count: 3

### Advisory

- Marker-named paths are trusted without containment, and the resume path has no process-identity guard. `finish_claim` unlinks `marker["handover"]` and `handover_injection.resume_pick` reads it (and, for a claimed marker, `marker.get("archive")`) as an arbitrary absolute or relative path taken from a JSON file in `<project>/.claude/rotation/`. `load_marker` only checks version/session_id/sha256/handover, never that the path is inside `<project>/.claude/rotation/`. Reproduced end to end in this checkout: with a hand-planted `ready-plant.json` naming `/tmp/crprobe/secret/elsewhere.txt`, `context_guard` on a UserPromptExpansion `resume 1` returned that file's contents as `hookEventName: UserPromptExpansion` additionalContext and then deleted the file (rc 0, victim file gone). The automatic `/clear` path is not exposed this way: `session_start.current_matches` requires `marker["process"] == process_identity(env)` (the live CLAUDE_PID plus its start time), which a planted file cannot know in advance. The `resume` path has no such check, so a marker file shipped in a cloned repo (tracked files survive the `*` .gitignore) or written by any process running as the user is enough, once the user types the resume command; the listing does print the target path, which is the only mitigation. Same root cause: `read_bytes` opens an unvalidated path, so a marker naming a FIFO or a device blocks or exhausts the hook. Impact is bounded: whoever plants the file already has the user's file permissions, so this is not an escalation, and it needs an explicit user command — hence warning, not blocking. A containment check (os.path.realpath inside rotation_dir) before read/unlink would close it. (severity: warning)
- `expansion_says` calls `hook_runtime.emit(hook_runtime.context_output(...))` without checking the result, and `context_output` returns None when both text and system_message are empty; `emit` would then write a bare `null` document on stdout (json.dumps(None)), which is not a valid hook document. Every current call site passes a non-empty string, so this is latent rather than live, but a future call site with an empty message would silently emit `null` instead of staying silent. (severity: info)
- This registration runs the guard process on every tool call (no matcher, so a python3 spawn plus a settings read plus a marker stat per call), and the same process may wait up to LOCK_TIMEOUT_SECONDS = 5.0 for the project lock and up to PS_TIMEOUT_SECONDS = 2.0 for `ps` in process_start — against the 10 s hook budget the fabric documents for its own guard (scripts/hooks/t0_context_guard.py). Worst case is inside the budget and writes are atomic (mkstemp + os.replace), so nothing corrupts, but the diff carries no measurement of the added per-call latency or of the lock-contention worst case (e.g. session_start's current_matches scanning transcripts while holding the lock). (severity: info)
