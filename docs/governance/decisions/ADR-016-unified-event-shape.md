# ADR-016 — Unified Event Shape via CanonicalEvent

**Status:** Accepted
**Date:** 2026-05-15
**Decided by:** Operator (Vincent van Deth)
**Resolves:** Wave 4.6 PR-4.6.6 — per `claudedocs/wave4.6-provider-dispatch-generalization-design-2026-05-13.md` §Phase 6

## Context

With Wave 4.6 PRs 4.6.2–4.6.5, four provider-specific spawn handlers now exist:
- `claude_spawn.py` — emits via SubprocessAdapter (maps to CanonicalEvent internally)
- `codex_spawn.py` — normalizes via `normalize_codex_event()`
- `gemini_spawn.py` — normalizes via `normalize_gemini_event()`
- `litellm_spawn.py` — normalizes via `normalize_litellm_event()`

Each normalizer produces a `CanonicalEvent` instance, but the mapping logic was distributed across four files with no single enforcement point. Two problems resulted:

1. **No schema enforcement at persistence boundary.** `EventStore.append()` accepted any `CanonicalEvent` instance, including ones with invalid `schema_version` or negative token counts.
2. **No canonical mapper registry.** There was no single place to ask "given a raw provider event, what CanonicalEvent does it produce?" — callers had to know which module to import.

## Decision

**`CanonicalEvent` in `scripts/lib/canonical_event.py` is the single source of truth for the unified event shape and per-provider mapping.**

Concrete changes:

1. **Four per-provider mapper functions** live in `canonical_event.py`:
   - `_from_claude_event(raw, dispatch_id, terminal_id)`
   - `_from_codex_event(raw, dispatch_id, terminal_id)`
   - `_from_gemini_event(raw, dispatch_id, terminal_id)`
   - `_from_litellm_event(raw, dispatch_id, terminal_id, sub_provider)`

2. **`CanonicalEvent.from_provider_event(provider, raw, dispatch_id, terminal_id, sub_provider)`** is the public classmethod that dispatches to the appropriate mapper. Raises `ValueError` for unknown providers.

3. **`CanonicalEvent.validate_shape()`** raises `EventShapeError` (defined in `canonical_event.py`) for:
   - `schema_version != 1`
   - Token counts (`tokens_input`, `tokens_output`, `tokens_cache_read`, `tokens_cache_write`) set to negative integers

4. **`EventStore.append()`** calls `event.validate_shape()` for every `CanonicalEvent` instance before writing, enforcing the schema at the persistence boundary.

5. **New fields added to `CanonicalEvent`** (all optional with defaults for backward compat):
   - `sub_provider: Optional[str]` — for litellm: deepseek, moonshot, zai, bedrock
   - `sequence: int` — monotonic sequence, default 0
   - `schema_version: int` — schema revision, default 1
   - `tokens_input`, `tokens_output`, `tokens_cache_read`, `tokens_cache_write: Optional[int]`
   - `model: Optional[str]`

## Consequences

### Accepted

- Any code that creates a `CanonicalEvent` with `schema_version != 1` or negative token counts will receive `EventShapeError` at `EventStore.append()` time. Callers MUST catch `EventShapeError` and log+abort.
- All new provider integrations MUST implement a mapper function in `canonical_event.py` and register it in `from_provider_event()`.
- `to_dict()` now always includes `schema_version` in its output. Existing callers that check `to_dict()` key sets must account for this.
- Wave 5 PR-5.4/5.6 (dashboard SSE unification) and Wave 7 PR-7.5 (new-provider integration path) can rely on `from_provider_event()` as their single entry point.

### Rejected

- Distributing mapper functions to spawn files — rejected. Centralizing in `canonical_event.py` avoids circular imports and provides a single audit target.
- Late-importing spawn normalizers from `from_provider_event()` — rejected. Self-contained mappers in `canonical_event.py` have no runtime import dependencies on the spawn layer.
- Raising `EventShapeError` for empty `dispatch_id`/`terminal_id` — rejected. Many tests and legacy paths create events without these fields; raising would break backward compat without adding safety at the relevant boundaries.

## Implementation note

- Spawn handlers (`codex_spawn`, `gemini_spawn`, `litellm_spawn`) continue using their own `normalize_*` functions in the hot path. This guarantees byte-identical NDJSON output vs. pre-PR-4.6.6 behavior. The mapper functions in `canonical_event.py` are functionally equivalent for all standard event types.
- The `EventShapeError` class is exported from `canonical_event.py` and re-imported in `event_store.py` to keep the schema definition colocated with the type.

## See also

- ADR-005 — Append-only NDJSON audit ledger (EventStore is the persistence layer this ADR extends)
- ADR-010 — SubprocessAdapter Claude routing (source of claude events)
- `claudedocs/wave4.6-provider-dispatch-generalization-design-2026-05-13.md` §Phase 6
- `scripts/lib/canonical_event.py` — CanonicalEvent, EventShapeError, from_provider_event()
- `scripts/lib/event_store.py` — EventStore.append() enforcement
- Wave 4.6 PR-4.6.2–4.6.5 — spawn handler extraction (prereqs for this ADR)
