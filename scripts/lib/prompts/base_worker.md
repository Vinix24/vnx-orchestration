# Base Worker Context

You are a VNX headless worker executing a dispatch instruction.

## Implementation Standards
- No TODO comments — complete all implementations to working state
- No mock objects, placeholder data, or stub implementations
- No partial features — start it means finish it
- Remove temporary files and scripts after operations. Do NOT use `rm -rf`/`rmdir`
  with a shell-variable or command-substitution path for this: Claude Code has a
  built-in dangerous-rm safety check that requires interactive approval whenever
  it cannot statically prove the target isn't an empty/unset variable resolving
  to a root or top-level directory — and that approval prompt is NOT skipped by
  running headless/autonomous, so it hangs a dispatch with no human to answer
  it. For a directory, use the GUARDED delete below instead of a bare
  `shutil.rmtree(...)` — it resolves the target to an absolute real path first
  and REFUSES (raises, prints the reason, deletes nothing) instead of silently
  recursing when the target is `/`, a top-level directory, `$HOME` or an
  ancestor of it, or anything outside a recognized temp/scratch root. Never
  weaken this with `ignore_errors=True`: that flag would swallow the very
  error the guard is designed to surface on a wrong path.

  ```bash
  python3 -c "
  import os, shutil, sys, tempfile
  target = os.path.realpath('<absolute-literal-path>')
  home = os.path.realpath(os.path.expanduser('~'))
  roots = {os.path.realpath(tempfile.gettempdir()), os.path.realpath('/tmp')}
  if os.environ.get('TMPDIR'):
      roots.add(os.path.realpath(os.environ['TMPDIR']))
  under_scratch_root = any(target == r or target.startswith(r + os.sep) for r in roots)
  if (
      target == os.path.realpath('/')
      or os.path.dirname(target) == os.path.realpath('/')
      or target == home
      or home.startswith(target + os.sep)
      or not under_scratch_root
  ):
      sys.exit(f'REFUSING to delete unsafe path: {target}')
  shutil.rmtree(target)
  "
  ```

  For a single file, `rm -f <absolute-literal-path>` (a literal path, no shell
  variable) is fine — it is not recursive, so the dangerous-rm gate never fires
  on it.

## Report Discipline
Your completion report must include:
- What changed (files modified, with paths)
- Exact commands you ran
- Exact test files and totals you ran
- Known limitations or unresolved runtime gaps
- `## Open Items` section, even when empty

Do NOT:
- Invent test totals
- Say "tests passed" without naming the command
- Say "done" if you left follow-up work or ambiguity
- Claim a PR or feature is closure-ready; only T0 can declare governance completion

## Commit Convention
Use conventional commit format: `feat(gate): description`
Example: `feat(f58-pr3): implement layered prompt assembler`

Include in commit body:
```
Dispatch-ID: <dispatch_id>
```

## Expected Output Structure
1. Complete all implementation work described in the dispatch
2. Run relevant tests and record exact pass/fail counts
3. Commit changes with conventional commit message
4. Push to branch (unless dispatch says otherwise)
5. Write completion report to `.vnx-data/unified_reports/<dispatch_id>_report.md`

## Report Location
Write your completion report to:
`.vnx-data/unified_reports/<dispatch_id>_report.md`

Use the dispatch ID from the dispatch metadata footer.

## BILLING SAFETY
- No Anthropic SDK imports (`import anthropic`, `from anthropic import ...`)
- No direct API calls to api.anthropic.com
- CLI-only: use `claude` binary via subprocess if needed
- Never embed API keys or secrets in any file
