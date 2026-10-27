---
name: refactor-safe
description: Behaviour-preserving refactoring under a test net — characterization before any move, the eleven seams where a move silently changes behaviour, small reversible steps, and the proof a refactor PR carries. Use when splitting a module, moving a function, or untangling near-duplicate implementations, and before anyone calls a refactor "no behaviour change".
allowed-tools: [Read, Grep, Glob, Edit, MultiEdit, Write, Bash]
paths: ["scripts/**", "tests/**"]
---

# Refactor Safe

A refactor changes structure without changing behaviour. That sentence is a
claim; this skill is how the claim gets evidence. If you cannot name the test
that would fail when the behaviour moves, you are not refactoring — you are
rewriting with the old name kept.

Source: the 2026-08-03 six-seat split benchmark. `dsflash` D.1/D.3/E is the
richest; `dspro` adds the near-duplicate unification warning; `codex` adds the
measured-versus-assumed rule.

## The behaviour-preservation contract

State the contract before you touch a file:

- Behaviour is the union of observable outputs, side effects, exceptions, log
  records, exit codes, receipt fields, file paths and call order — not just the
  return value.
- A move is allowed only when a test that already runs the behaviour stays
  green, or a characterization test written on the old code stays green after.
- "No behaviour change" is a claim in the PR body with a run behind it. An
  unrun claim is a guess.

## Characterization before any move

Write these four layers first, on the unchanged code, and see all four green.
This is a test-only step: no production edit in the same commit.

1. **Import surface.** Assert every public name the consumers import still
   imports from the module they import it from. ImportError is a loud failure,
   which makes this the cheapest reliable net.
2. **Patch binding.** For each frequently patched name: patch it, call through
   the code path, and `assert mock.called`. A patch that hits nothing leaves
   the test green while the real function runs (see rule 1).
3. **Path resolution.** Characterize how data, state and project paths resolve
   under the env overrides and without them. After a move, assert the
   resolution chain is unchanged, not that a particular function exists.
4. **Output shape.** Freeze the byte shape of deterministic outputs: token and
   cost extraction, frontmatter keys, launch command strings, completion
   footers, lane keys, verification dicts. These become the per-step proof.

## The eleven seams

A move can change behaviour silently at each of these. Walk the checklist for
every moved symbol.

1. **Monkeypatch namespace** — `patch("old.module.name")` stops intercepting
   once the call site imports the name into a new namespace.
2. **Sibling imports** — modules that add their own directory to `sys.path` and
   import siblings at module scope break when moved into a subpackage.
3. **`__file__`-derived paths** — a parent-count that lands on `scripts/` from
   the old depth lands somewhere else from the new one; no exception, wrong dir.
4. **Defensive fallback blocks** — a `try: import ... except ImportError:`
   fallback defines different behaviour than the real import; move the block
   with its consumer or the fallback branch disappears.
5. **Logger identity** — `logging.getLogger(__name__)` changes logger when the
   module changes; log filters and `patch("module.logger")` silently miss.
6. **Call-time name lookup** — defaults and globals referenced at call time
   (`builder or _default`, a module constant) must be imported where they are
   looked up, not where they are defined.
7. **Cross-module runtime imports** — imports done inside a function point at
   the old module until updated; only the tests that reach that path notice.
8. **Freeze-test allowlists** — an AST guard that permits direct coupling only
   in listed files must list the new file in the same step.
9. **Near-duplicates that differ** — two similar functions are not one
   function; check every branch before consolidating (a "cleanup" can add or
   drop a field).
10. **Attribute contracts** — data passed on a shared object (e.g. `args._x`)
    survives a move only if both readers and writers move together or agree.
11. **CLI entry point** — a module run as a script must keep `main` and
    `__main__` at the same path; add a `--help` smoke test.

## Small reversible steps, and a flag where behaviour must switch

- One commit per step, each independently shippable and revertable. Order from
  leaf functions (few callers) to orchestrators (everything).
- A pure move needs no flag. When a step changes behaviour — a consolidation, a
  new default, a renamed field — put it behind a **feature flag**, keep the old
  path as the default, and test both paths. Ship the switch separately from the
  code that it switches.
- Update patch targets, allowlists and call sites in the same commit as the
  move. A follow-up commit leaves the tree green for the wrong reason.

## Two rules this repo learned the hard way

### Rule 1 — patch the consumer namespace, not the source module

`from X import Y` binds `Y` in the importing module at import time.
`patch("X.Y")` rebinds only `X`; the consumer keeps the original. In this repo
three tests patched the source module for years and passed for the wrong
reason, until a CI change made the real function return data. Measured repeat
offenders in the split benchmark: `_enrich_instruction` (27 patches),
`_emit_governance` (20), `_create_provider_worktree` (16). Patch where the name
is looked up, and assert the mock was called.

### Rule 2 — a partial test sweep is a different environment

Some tests pass only next to other files; some pass only solo. Measured: the
integration directory alone showed 7 failures while the full CI sweep on the
same commit was green, and three files that looked order-dependent were
actually reading an env var present in one shell and absent in CI. Never call a
failure "pre-existing" from a solo or per-directory run. Compare the same sweep
composition on the base commit.

## What proof a refactor PR carries

- A **red run on the old head**: the new test fails there and passes after, and
  the report names the old commit. For a pure move the characterization suite is
  green on the old head; that green run is the baseline the move must preserve.
- **Seam tests written green on the old code first**, then kept green across the
  move. A seam test written after the move proves nothing about the seam.
- **Existing tests unchanged** unless the refactor deliberately changed the
  contract; if a test must change, that is a behaviour change and needs a flag.
- The run command and pass/fail counts, not the word "green".

## Do not

- Do not consolidate near-duplicates "while you are there" in a move commit.
- Do not trust a green patch test without `assert mock.called`.
- Do not call a solo failure pre-existing, and do not skip the base comparison.
