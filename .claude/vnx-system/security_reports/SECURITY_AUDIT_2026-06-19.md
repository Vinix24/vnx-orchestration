# Security Audit — Safe Path Resolver (t4-01)

**Date:** 2026-06-19
**Scope:** `safe_path.py` + `tests/test_safe_path.py` (directory-traversal sandbox)
**Auditor:** security-engineer skill
**Verdict:** PASS — resolver anchors inside base via real-path resolution + component-wise containment; full threat matrix rejected.

## Foundational check (STEP 0)

- **ADR-007** (composite `project_id` keys): NOT applicable — this is a filesystem path-sandbox, no central-DB tables involved. Cited per process; no constraint applies.
- **ADR-005** (NDJSON audit ledger): observability layer; not in scope for the resolver contract itself.
- **Prior incident — A14-PR2** (memory `a14-pr2-pregate-merge-incident`): a path-traversal vulnerability was merged to main pre-gate and required cherry-pick #697. This task is the hardened, structural fix for exactly that class.
- **SECURITY-SWEEP canonical fix pattern**: `Path(base).resolve()` + `is_relative_to`/`relative_to` containment — NOT string `startswith`. Adopted as the authoritative gate.
- **FUT-1 lesson** (2026-05-28): consult priors at design time. Honored — the naive `".." in path` check was explicitly rejected in favour of real-path + component containment.

## Design

The resolver defeats traversal through three layers, none of which is a string match:

1. **Resolve real paths first.** `Path(base).resolve(strict=False)` canonicalises the base (eliminating any `..` or symlinks in the trusted path). The candidate `base / user_path` is then resolved the same way — symlinks are followed for existing components and `..` is lexically collapsed. This defeats symlink escape and parent traversal simultaneously.
2. **Component-wise containment.** After resolution, `resolved.relative_to(base_real)` is the authoritative gate. It raises `ValueError` if `resolved` is not under `base_real`, and it is a *component* comparison — so `/foo/bar` and `/foo/barbaz` are correctly distinguished (string `startswith` would fail here).
3. **Defence-in-depth pre-gates.** Backslash to `/` normalisation (defeats `..\..\secret` on POSIX), null/control-char rejection (defeats NUL truncation), absolute-path early-reject, and a percent-decode re-check gate (realpath alone cannot reject a literal directory named `%2e%2e`; an upstream decoder could turn it into real traversal, so containment is re-run on the decoded form and rejected if *that* would escape).

## Threat matrix — verification

Each row of the task's threat matrix was implemented as an explicit test. All pass.

| # | Threat | Payload(s) | Result |
|---|--------|-----------|--------|
| 1 | Parent traversal | `../etc/passwd`, `a/../../etc/passwd`, 40x `../` | rejected |
| 2 | Absolute paths | `/etc/passwd`, `/` | rejected |
| 3 | Symlink escape | dir-symlink + file-symlink pointing outside base | rejected |
| 4 | Percent-encoded traversal | `%2e%2e%2f`, `..%2f`, `%2E%2E/` | rejected |
| 5 | Backslash / mixed separators | `..\..\secret`, `\windows\system32` | rejected |
| 6 | Null byte / control char | `safe.txt\x00../../etc/passwd`, `\x07` | rejected |
| 7 | Trailing dot/space tricks | `secret. `, `..../` | contained (no escape) |
| 8 | Current-dir noise escape | `./a/./../../etc/passwd` | rejected |
| 9 | Empty / whitespace | ``, `   `, `\t` | rejected |
| 10 | Exactly base vs one-char-out | `.` allowed; `..` rejected | correct |

**Legitimate paths allowed:** `report.pdf`, `sub/dir/file.txt`, `./nested/ok.md`, `a/b/../b/file.txt`, symlink-into-base, `.` (base itself). All resolve without raising.

## Findings

No blocking findings. The resolver meets the "truly anchors inside the base directory" bar — it does not rely on string matching.

### Informational / accepted limitations

- **TOCTOU:** Like every userspace path resolver, there is a time-of-check/time-of-use gap between `resolve_safe` returning and the caller's `open()`. A symlink could be swapped in the window. This is inherent to a path-returning contract and documented in the module docstring. Callers needing TOCTOU-freedom must use `openat`/`O_NOFOLLOW` descend at access time — out of scope for this contract.
- **Double-encoding:** The percent-decode gate runs a single `unquote`. A `%252e` payload decodes to `%2e` (not `..`); rejecting it would require recursive decode, which is itself a denial-of-service vector. The single-decode gate covers the realistic vector (upstream decodes once). Documented in tests.

## Verification evidence

- `python3 -m pytest tests/test_safe_path.py -v` -> **41 passed in 0.13s**
- Direct adversarial probe (20 hand-crafted attacks + 4 positives) -> **20/20 blocked, 4/4 allowed**
- `python3 -m py_compile` -> clean
- No TODO/FIXME/stub/placeholder strings present.

## Open items

None for the resolver itself. If integrated into a web service, the caller MUST:
1. Not re-decode the returned path.
2. Use the returned path verbatim (no further `..` interpretation).
3. Consider `openat`-based descend if TOCTOU is in threat model.
