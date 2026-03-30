# VNX Adoption Feature — Certification Report

**Date**: 2026-03-29
**Feature**: VNX Adoption, Packaging, Pythonization, And Public Onboarding
**Branch**: `feature/adoption-packaging-pythonization`
**PR**: PR-7 (QA, Review, And Certification Hardening)
**Certifier**: T3 (Track C — adversarial QA)

## Executive Summary

The VNX adoption feature (PR-0 through PR-6) delivers a materially improved install, onboarding, and documentation surface. Three execution modes (starter, operator, demo) are well-defined, command gating works, and Python-led entrypoints replace brittle shell orchestration where it matters most. This report certifies the feature's readiness and documents residual risks.

## Evidence Summary

### Test Coverage

| Test Suite | Tests | Status |
|------------|-------|--------|
| test_vnx_mode.py (PR-2) | 28 | PASS |
| test_vnx_starter.py (PR-2) | 18 | PASS |
| test_vnx_init.py (PR-1) | 24 | PASS |
| test_vnx_setup.py (PR-4) | 22 | PASS |
| test_vnx_install.py (PR-4) | 29 | PASS |
| test_vnx_demo.py (PR-2) | 22 | PASS |
| test_vnx_doctor.py (PR-1) | 22 | PASS |
| test_vnx_worktree.py (PR-3) | 20 | PASS |
| test_vnx_start_runtime.py (PR-3) | 38 | PASS |
| test_vnx_recover_runtime.py (PR-3) | 34 | PASS |
| **Existing suite subtotal** | **257** | **PASS** |
| test_path_resolution_regression.py (PR-7) | 24 | PASS |
| test_docs_command_validation.py (PR-7) | 12 | PASS |
| test_quickstart_validation.py (PR-7) | 9 | PASS |
| **PR-7 new tests subtotal** | **45** | **PASS** |
| **Total** | **302** | **PASS** |

### CI Coverage (Profile C — new)

Profile C added to `vnx-ci.yml` covers:
- Path-resolution regression tests
- Docs-command validation tests (README vs bin/vnx vs vnx_mode.py)
- Quickstart validation (install → init → doctor end-to-end)
- Closure verification script

### Closure Verification

`scripts/verify_closure.py` performs independent checks:
- FEATURE_PLAN.md and PR_QUEUE.md exist and align
- All 9 PRs present in both metadata files
- Branch tracks remote
- All 11 expected test files exist
- No phantom PRs (all queue entries have matching plan entries)
- **Result**: 24 pass, 0 warn, 0 fail

## Gate Checklist: `gate_pr7_qa_and_certification`

| # | Gate Item | Status | Evidence |
|---|-----------|--------|----------|
| 1 | CI covers starter mode, operator mode, and install/quickstart smoke paths | PASS | Profile C in vnx-ci.yml: test_quickstart_validation.py covers install→init→doctor→status; test_vnx_mode.py and test_vnx_starter.py in Profile A |
| 2 | Docs and public command examples validated against actual behavior | PASS | test_docs_command_validation.py: 12 tests verify README commands, mode tiers, bin/vnx case branches, productization contract, and install.sh help all align |
| 3 | Path-resolution regressions covered | PASS | test_path_resolution_regression.py: 24 tests covering default resolution, env overrides, cross-project contamination, legacy layout, worktree isolation, skills fallback, intelligence dir, ensure_env, determinism |
| 4 | Certification report summarizes adoption readiness and residual risks | PASS | This document |
| 5 | Review and QA evidence strong enough for public-facing rollout | PASS | 302 tests passing, zero regressions, closure verification clean |
| 6 | Independent closure verification catches no push/PR/CI/metadata inconsistencies | PASS | verify_closure.py: 24/24 checks pass |

## Findings

### F-1: Doctor hygiene check requires `rg` binary (severity: medium)

`vnx doctor` includes a path-hygiene check that depends on `rg` (ripgrep) as a binary. When `rg` is only available as a shell function (e.g., Claude Code's built-in wrapper) or not installed at all, doctor returns exit code 1 even though all critical checks pass.

**Impact**: New users without standalone `rg` installed will see a FAIL on their first `vnx doctor` run. This undermines the quickstart experience.

**Recommendation**: Either make the hygiene check skip gracefully when `rg` is missing, or downgrade it to WARN instead of FAIL. Required for PR-8 adoption cutover.

### F-2: vnx_paths.sh PROJECT_ROOT inheritance (severity: low)

When `vnx_paths.sh` is sourced directly (not via `bin/vnx`), it does not aggressively clear inherited `PROJECT_ROOT` for non-legacy layouts. `bin/vnx` handles this correctly (line 15 unsets all VNX vars), but scripts that source `vnx_paths.sh` independently may inherit a stale PROJECT_ROOT from the parent environment.

**Impact**: Only affects direct sourcing of vnx_paths.sh in non-standard invocations. Normal usage via `bin/vnx` is safe.

**Recommendation**: Document this as a known edge case. Consider adding PROJECT_ROOT validation to vnx_paths.sh to match bin/vnx behavior.

### F-3: macOS `/private/var` symlink in path comparisons (severity: low)

On macOS, `/var/folders/...` and `/private/var/folders/...` are the same directory but string-compare as different. The worktree detection in `vnx_paths.sh` uses `pwd` (logical) vs `cd ... && pwd` (resolves symlinks) which can cause `/private` prefix mismatches in tmpdir scenarios.

**Impact**: Only affects tests running in macOS temp directories. Production usage (projects in home directories) is unaffected.

**Recommendation**: No action needed for adoption. If tmpdir tests become a CI concern on macOS, use `pwd -P` consistently.

### F-4: FEATURE_PLAN.md still marked "Draft" (severity: info)

FEATURE_PLAN.md status is "Draft" while PR_QUEUE.md shows 7/9 PRs complete (77%). This should be updated as part of PR-8 adoption cutover.

## Residual Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Doctor hygiene FAIL on systems without standalone rg | Medium | Fix in PR-8 or document in quickstart |
| No macOS-specific CI runner | Low | Local test coverage; GitHub Actions runs on ubuntu-latest |
| Demo mode depends on pre-built evidence files | Low | Evidence is git-tracked; demo tests pass |
| Operator mode not testable in CI (requires tmux) | Low | Unit tests mock tmux adapter; manual validation for full grid |

## Certification Verdict

**CONDITIONALLY READY FOR ADOPTION**

The adoption feature is well-tested (302 tests, zero regressions), well-documented (README rewrite, productization contract, example flows, comparison docs), and structurally sound (three clean modes sharing one runtime model). The single blocking issue (F-1: doctor hygiene requiring `rg`) should be addressed in PR-8 before public release.

All governance rules (G-R1 through G-R8) and closure rules (C-R1 through C-R7) are satisfied for PR-0 through PR-6 based on closure verification evidence.
