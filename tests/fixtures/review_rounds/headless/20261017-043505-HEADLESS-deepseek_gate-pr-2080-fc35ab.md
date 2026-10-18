# deepseek_gate — Headless Gate Report

**PR**: 2080
**Branch**: dispatch/20261016-cr-b1-state-inject
**Gate**: deepseek_gate
**Generated**: 2026-10-17T04:43:39Z

---

## Findings

Blocking count: 0
Advisory count: 5

### Advisory

- finish_claim is documented 'Never raises' so the hook keeps exit 0 after the injection was emitted, but project_lock(project_dir) can raise OSError from ensure_gitignore (os.makedirs/os.open) or os.open('.lock') (e.g. read-only rotation dir, or .lock replaced by a directory); only LockTimeout is caught, so the hook exits 1 after emitting and Claude Code can discard the output that carries the handover. Wrap the whole locked body in except OSError (log via log_error) instead of only LockTimeout. (severity: warning)
- trim_archive's docstring promises 'never raises', but the locked body is outside the guarded region: project_lock can raise OSError or LockTimeout, and os.unlink(name, dir_fd=descriptor) re-raises anything but FileNotFoundError (EACCES/EPERM on a read-only archive dir). session_start.run calls trim_archive before inject_on_clear (session_start.py:89 vs :93), so such an error exits the hook with 1 and skips both the /clear injection and the waiting-handovers notice for that start. Catch OSError and LockTimeout around the locked region and return 0. (severity: warning)
- When .claude/rotation/archive is a symlink the trim refuses to operate there (deliberately, with one stderr line per SessionStart), but archive_handover still writes copies through that symlink via path-based os.makedirs/mkstemp, so archive copies accumulate without bound with only the warning; consider refusing to archive through a symlinked archive directory or counting such copies in the trim. (severity: info)
- survey's docstring says ready and outdated markers are listed 'newest first within each group', but the implementation merges both kinds into one waiting list and sorts the merged list by written_at, so the stated grouping does not match the behavior; no caller depends on the distinction, so this is a doc/behavior mismatch only. (severity: info)
- resume re-derives the numbered list at pick time; if a handover appears between the list shown to the user and the user typing /rotate resume <n>, the number can select a different handover than the one displayed (recoverable and visible in the injection, but echoing the origin session id alongside each number would make the pick unambiguous). (severity: info)
