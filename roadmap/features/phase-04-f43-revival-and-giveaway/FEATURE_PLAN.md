# Feature: Phase 4 — F43 Context Rotation Revival + Community Giveaway

**Status**: Draft
**Priority**: P0
**Branch**: `feature/phase-04-f43-revival-and-giveaway`
**Risk-Class**: high (W6A); medium-low (W6B-D); content-only (W6E)
**Merge-Policy**: human
**Review-Stack**: gemini_review (per PR); codex_gate + claude_github_optional on W6A (high-risk feature-end revival)
**Source**: ROADMAP.md Phase 4; ADR-002 (F43 packaging); existing `feat/f43-context-rotation-headless` branch (`ef80c0c`)

Primary objective:
Revive the parked F43 context-rotation work into `main`, then carve it out as a standalone PyPI module per ADR-002. Phase 4 has two halves: (1) a high-risk rebase (W6A) of ~750 LOC against post-W3J subprocess refactor; (2) a packaging + giveaway track (W6B-E) that turns the now-merged module into VNX's first community deliverable.

## Dependency Flow
```text
W6A (Revive F43 into main)              [HIGH-RISK; gemini + codex + claude_github_optional]
  -> W6B (Carve out package within VNX) [low-risk refactor; gemini]
       -> W6C (Separate repo + sync)    [low-risk; gemini]
            -> W6D (PyPI publish)       [low-risk; gemini]
                 -> W6E (Launch posts)  [content; no model worker]
```

W6A is the gating wave. W6B-E are sequential and can only begin once F43 is live in `main` and its 501-LOC test suite is green.

## PR-W6A: Revive F43 Context Rotation Into Main
**Track**: A
**Priority**: P0
**Complexity**: High
**Risk**: High
**Skill**: @backend-developer
**Requires-Model**: opus
**Risk-Class**: high
**Merge-Policy**: human
**Review-Stack**: gemini_review,codex_gate,claude_github_optional
**Estimated Time**: 2-3 days
**Dependencies**: []

### Description
Rebase `feat/f43-context-rotation-headless` (commit `ef80c0c`) onto current `main` and re-apply F43 edits over the W1A subprocess_dispatch facade. Conflicts are likely because the underlying subprocess module was refactored after F43 was parked. Run all repo tests + the original 501-LOC F43 test suite. Opus required: rebase-heavy work where conflict resolution decisions affect runtime behavior in concurrency-sensitive code paths (subprocess lifecycle, event drainer, handover summaries).

This wave is the feature-end of the Phase 4 *high-risk* half. Per operator policy, the review stack carries `codex_gate` AND `claude_github_optional` (both required gates).

### Scope
- Pre-flight: snapshot the parked branch state; capture original F43 test suite (~501 LOC) under `tests/feature/f43_legacy/` so it runs against the rebased code.
- Rebase `feat/f43-context-rotation-headless` onto `main` post-W3J.
- Re-apply F43 edits over current `subprocess_dispatch.py` / `subprocess_adapter.py` / `subprocess_dispatch_internals/`.
- Resolve conflicts and document each one (file + chosen resolution + reason) in PR description.
- Public API frozen: `Tracker.update(event)`, `should_rotate(tracker)`, `build_handover(tracker, last_user_message)`.
- All edits must remain stdlib-only (zero third-party deps in the rotation logic).

### Files to Create/Modify
- Likely modify: `scripts/lib/subprocess_dispatch.py`, `scripts/lib/subprocess_adapter.py`, `scripts/lib/subprocess_dispatch_internals/*.py`
- Likely create: `scripts/lib/context_rotation/tracker.py`, `scripts/lib/context_rotation/rotation.py`, `scripts/lib/context_rotation/handover.py`
- Tests: re-imported `tests/feature/f43_legacy/*` (501 LOC) + new conflict-resolution regression tests
- PR description: mandatory "Conflict Resolution Log" section

### Success Criteria
- Rebased branch passes the full 501-LOC original F43 test suite.
- All repo tests green (no regressions in subprocess_adapter, drainer, dispatcher).
- Public API unchanged (Tracker / should_rotate / build_handover signatures stable).
- Conflict-resolution log filed with PR describing every touched file and chosen resolution.
- Headless workers in long missions trigger context rotation when token budget exceeds threshold (manual integration verification).

### Test Plan
- **Pre-merge (mandatory)**: Run the original 501-LOC F43 test suite against the rebased branch; assert 100% pass. If any test fails, do NOT merge — fix-up commits required.
- **Unit**: Each public API function has at least one direct test from the original suite. Add new unit tests for any case the rebase exposed that the legacy suite didn't cover.
- **Integration (boots real subprocess)**: Spawn a long-running headless dispatch that exceeds `should_rotate` threshold; assert `build_handover` produces a non-empty summary; assert rotated subprocess receives the handover as its first user message.
- **Conflict-resolution evidence (PR-blocking)**: PR description MUST contain a list of every file where a conflict occurred, with the chosen resolution and a one-line rationale. Reviewer (codex_gate + claude_github) must verify this list matches `git log` of the rebase.
- **Negative**: Trigger context rotation while subprocess is mid-tool-use; assert no event loss, archive flushed cleanly, handover includes the in-flight tool result.
- **Smoke**: `python3 -c "from scripts.lib.context_rotation import Tracker, should_rotate, build_handover; print('ok')"` on a fresh clone.

### Quality Gate
`gate_pr_w6a_f43_revival`:
- [ ] Original 501-LOC F43 test suite is green.
- [ ] No regressions in any other repo test.
- [ ] PR description contains a complete conflict-resolution log (file + resolution + reason).
- [ ] Public API surface is unchanged.
- [ ] Codex gate verdict: pass (final mode).
- [ ] Gemini review verdict: pass.
- [ ] Claude GitHub review (optional) acknowledged or skipped per env config.
- [ ] Manual long-mission rotation test produces a valid handover document.

## PR-W6B: Carve Out `context_rotation` Package
**Track**: A
**Priority**: P0
**Complexity**: Medium
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 1 day
**Dependencies**: [PR-W6A]

### Description
Reorganize the now-revived F43 code into a clean `scripts/lib/context_rotation/` package with frozen public API per ADR-002. Mechanical refactor; Sonnet is sufficient.

### Scope
- Move all rotation files into `scripts/lib/context_rotation/__init__.py` plus submodules.
- Public API: `from scripts.lib.context_rotation import Tracker, should_rotate, build_handover`.
- Internal modules underscore-prefixed (`_state.py`, `_summary.py`).
- Add `__all__` declaration restricting public surface.
- Confirm zero third-party imports in the package (stdlib only).
- Add module-level docstring describing public API + invariants.

### Files to Create/Modify
- Create: `scripts/lib/context_rotation/__init__.py`, `scripts/lib/context_rotation/tracker.py`, `scripts/lib/context_rotation/rotation.py`, `scripts/lib/context_rotation/handover.py`
- Move/rename: existing W6A modules into the package
- Update: any caller in `subprocess_dispatch_internals/` to import from the new package path
- Tests: keep existing `tests/feature/f43_legacy/` passing; add `tests/unit/test_context_rotation_public_api.py`

### Success Criteria
- All callers import via `scripts.lib.context_rotation` only.
- `__all__` excludes any internal symbols.
- 501-LOC legacy test suite still green.
- `pip-audit` (or equivalent) reports zero third-party deps in the package.

### Test Plan
- **Unit**: Import every public symbol; assert `__all__` matches; assert no other names are public.
- **Integration**: Re-run all integration tests from W6A; pass unchanged.
- **Smoke**: `python3 -c "from scripts.lib.context_rotation import *; assert {Tracker, should_rotate, build_handover}.issubset(set(dir()))"`.
- **Static**: `grep -r "import context_rotation\|from context_rotation" scripts/` returns zero hits — only the package itself self-references.
- **Stdlib-only**: `python3 -c "import ast, pathlib; [print(p) for p in pathlib.Path('scripts/lib/context_rotation').rglob('*.py') for n in ast.walk(ast.parse(p.read_text())) if isinstance(n, ast.ImportFrom) and n.module not in stdlib_modules]"` returns nothing.

### Quality Gate
`gate_pr_w6b_carve_out`:
- [ ] Package layout matches ADR-002 spec.
- [ ] Public API frozen via `__all__`.
- [ ] Zero third-party imports.
- [ ] 501-LOC legacy suite green.
- [ ] All in-repo callers updated to package import path.

## PR-W6C: Separate Repo + Sync Script
**Track**: A
**Priority**: P1
**Complexity**: Medium
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 0.5 day
**Dependencies**: [PR-W6B]

### Description
Establish the standalone giveaway repo `Vinix24/headless-context-rotation` and a one-way sync script that mirrors the carved-out package from VNX into the giveaway repo. Operator owns GitHub repo creation; this PR delivers the sync script + MIT license + README skeleton. Sonnet is sufficient.

### Scope
- Create `scripts/maintenance/sync_context_rotation_module.py` (~150 LOC):
  - rsync-style copy of `scripts/lib/context_rotation/**` into a target path.
  - Strips internal-only docstrings; rewrites import paths from `scripts.lib.context_rotation` to top-level `headless_context_rotation`.
  - Writes a footer to README noting the source repo + commit hash.
- New `templates/giveaway/headless-context-rotation/` skeleton: `LICENSE` (MIT), `README.md`, `pyproject.toml` stub (no PyPI publish yet).
- CLI: `python3 scripts/maintenance/sync_context_rotation_module.py --target /path/to/giveaway-repo`.

### Files to Create/Modify
- Create: `scripts/maintenance/sync_context_rotation_module.py`
- Create: `templates/giveaway/headless-context-rotation/{LICENSE,README.md,pyproject.toml}`
- Tests: `tests/unit/test_context_rotation_sync.py`, `tests/integration/test_sync_to_tempdir.py`

### Success Criteria
- Sync script produces a buildable Python package in the target dir.
- Import-path rewrites verified by `python -c "import headless_context_rotation"` in the synced target.
- License + README present in target post-sync.

### Test Plan
- **Unit**: AST-based path rewrite handles `from scripts.lib.context_rotation import X`, `from scripts.lib.context_rotation.handover import Y`, and module-relative imports.
- **Integration**: Sync to a `tempfile.mkdtemp()` target; run `python -m unittest` in that target against the legacy 501-LOC suite (relocated); pass.
- **Smoke**: After sync, `pip install -e <target>` succeeds and `python -c "from headless_context_rotation import Tracker"` works.
- **Negative**: Sync into a non-empty non-package dir; assert refusal with structured error.

### Quality Gate
`gate_pr_w6c_sync_repo`:
- [ ] Sync script produces a valid standalone package.
- [ ] Import paths rewritten correctly across all modules.
- [ ] LICENSE (MIT) + README in target post-sync.
- [ ] Legacy 501-LOC test suite passes against synced target.

## PR-W6D: PyPI Publish Workflow
**Track**: A
**Priority**: P1
**Complexity**: Low
**Risk**: Low
**Skill**: @backend-developer
**Requires-Model**: sonnet
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review
**Estimated Time**: 0.5 day
**Dependencies**: [PR-W6C]

### Description
Author `pyproject.toml` + GitHub Actions release workflow + first version tag for the standalone giveaway. Operator owns the PyPI account + secret rotation. Sonnet is sufficient: well-trodden territory.

### Scope
- Finalize `templates/giveaway/headless-context-rotation/pyproject.toml` (PEP 621) with version `0.1.0`, MIT license, Python>=3.10 marker, no install_requires.
- New `templates/giveaway/headless-context-rotation/.github/workflows/release.yml`:
  - Trigger: `push: tags: ['v*']`.
  - Steps: build sdist + wheel, run tests, `pypa/gh-action-pypi-publish@release/v1` with trusted publishing.
- Document operator-owned setup in package README: PyPI trusted-publisher config, first-tag instructions.

### Files to Create/Modify
- Modify: `templates/giveaway/headless-context-rotation/pyproject.toml`
- Create: `templates/giveaway/headless-context-rotation/.github/workflows/release.yml`
- Create: `templates/giveaway/headless-context-rotation/CONTRIBUTING.md` (minimal; license, code of conduct pointer, PR guidelines)
- Tests: `tests/unit/test_pyproject_metadata.py` (parses + validates required fields)

### Success Criteria
- `python -m build` against the synced target produces a sdist + wheel.
- `twine check dist/*` reports no errors.
- Workflow YAML lints clean (`actionlint`).

### Test Plan
- **Unit**: Parse pyproject.toml; assert version + license + python_requires + name + readme present and valid.
- **Integration**: After W6C sync, `python -m build` in the synced target; assert dist artifacts; `twine check dist/*` passes.
- **Smoke**: Lint the workflow with `actionlint`; install the produced wheel into a fresh venv; `from headless_context_rotation import Tracker` works.
- **Negative**: With wrong python_requires, assert `python -m build` fails (sanity).

### Quality Gate
`gate_pr_w6d_pypi`:
- [ ] `pyproject.toml` validates against PEP 621.
- [ ] Workflow lints with `actionlint`.
- [ ] Local build produces a clean sdist + wheel passing `twine check`.
- [ ] CONTRIBUTING.md present with license + PR guidelines.

## PR-W6E: Launch Posts (Content)
**Track**: B
**Priority**: P2
**Complexity**: Low
**Risk**: Low
**Skill**: operator (T0 drafts)
**Requires-Model**: n/a (no worker dispatch — content authored by operator with T0 assist)
**Risk-Class**: low
**Merge-Policy**: human
**Review-Stack**: gemini_review (light copy review only)
**Estimated Time**: 0.5 day
**Dependencies**: [PR-W6D]

### Description
Reddit r/LocalLLaMA + r/ClaudeAI + Hacker News "Show HN" + LinkedIn announcement drafts. Operator-owned. T0 drafts; operator reviews, edits, publishes. No code worker dispatched. Tracked here so the giveaway closeout has an explicit landing.

### Scope
- Draft `claudedocs/launch/2026-XX-headless-context-rotation-reddit-localllama.md`
- Draft `claudedocs/launch/2026-XX-headless-context-rotation-reddit-claudeai.md`
- Draft `claudedocs/launch/2026-XX-headless-context-rotation-show-hn.md`
- Draft `claudedocs/launch/2026-XX-headless-context-rotation-linkedin.md`
- Each draft: 200-400 words, links to PyPI + GitHub repo + ADR-002.

### Files to Create/Modify
- Create: 4 markdown drafts under `claudedocs/launch/`

### Success Criteria
- All 4 drafts written, operator-approved, ready to copy-paste into target platforms.
- Each draft links the PyPI page + GitHub repo + license.

### Test Plan
- **Unit**: n/a (content).
- **Integration**: Operator dry-runs each draft (no submission yet).
- **Smoke**: Markdown renders correctly in GitHub preview.

### Quality Gate
`gate_pr_w6e_launch_posts`:
- [ ] All 4 platform drafts present.
- [ ] Each draft links PyPI + GitHub + license.
- [ ] Operator has reviewed and approved tone for each platform.
- [ ] PyPI page is live (W6D dependency satisfied) before posting.
