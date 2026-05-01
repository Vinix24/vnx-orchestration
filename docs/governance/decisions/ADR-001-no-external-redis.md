# ADR-001 — No External Redis (or Other Always-On Network Daemons)

**Status:** Accepted
**Date:** 2026-05-01
**Decided by:** Operator (Vincent van Deth)
**Resolves:** OI-1083 — H3/H4 rate limiters in-memory per worker, no Redis backing

## Context

VNX dispatch flows include in-process rate limiters (H3/H4) that exist independently per worker subprocess. Multiple workers each enforce their own counter — global rate limits across the whole VNX instance are effectively unenforced.

A common solution is to back the rate limiter with **Redis** (or any shared in-memory store) so all workers see the same counter. This was raised as OI-1083 with the implicit suggestion to adopt Redis.

## Decision

**VNX will not depend on Redis (or any other always-on network daemon) for its core runtime.**

In-memory per-worker rate limiting stays as the default. If global rate limits are needed in the future, the implementation must use one of:

- A SQLite-backed counter under `$VNX_DATA_DIR/state/`
- A file-locked counter in `$VNX_DATA_DIR/state/rate_limits/`
- A short-lived shared-memory segment (POSIX `mmap`) within a single host

…not a network daemon.

## Reasoning

VNX is a **local-first, single-host orchestration system.** Its core architecture properties:

1. **Zero-install operator experience.** A user clones the repo, runs `vnx init`, and the system works. Adding Redis as a dependency means: "first install Redis, configure it, run it, monitor it" before any value lands. That breaks the value proposition.

2. **Self-contained runtime state.** Everything in `.vnx-data/` is recoverable from disk. Killing the dispatcher and restarting it does not lose state. A Redis dependency means another process to babysit, another failure mode (Redis down → all dispatches blocked), another thing to back up.

3. **Multi-project isolation already solved without Redis.** W4G PR #388 established `project_scope` helpers — sockets, locks, and tmpfiles all route through `$VNX_DATA_DIR`. Adding Redis would re-introduce a shared global namespace that contradicts this.

4. **No operational footprint.** VNX must run on a developer laptop with the same semantics as on a server. Redis on a laptop = systemd unit, port assignment, password rotation. None of that is in scope.

5. **Per-worker rate limits are sufficient for VNX's actual scale.** VNX runs 3-4 worker terminals. Per-worker counters that are roughly synchronized via NDJSON-event timestamps are operationally indistinguishable from a global counter at this scale. We are not Twitter.

## Consequences

### Accepted

- H3/H4 rate limits remain per-worker. Brief over-shoot (sum across workers > nominal limit) is tolerated.
- If we later need a hard global limit, we adopt SQLite-backed counters (already used elsewhere in VNX).
- New OIs that propose Redis or similar (Memcached, etcd, Consul) are auto-rejected with a link to this ADR.

### Rejected

- Redis as a dependency.
- Memcached, KeyDB, Dragonfly, or any other Redis-protocol service.
- etcd / Consul / ZooKeeper for coordination.
- Any always-on network daemon outside the VNX repo.

## Implementation note

OI-1083 is closed as **wontfix-by-design**, with a link to this ADR. No code changes.

## See also

- W4G PR #388 — cross-project isolation via `project_scope`
- W4C PR #380 — singleton lock race + session_ids refresh (in-process coordination)
- `scripts/lib/python_singleton.py` — pattern for single-host coordination without a daemon
