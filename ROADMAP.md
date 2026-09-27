# VNX Orchestration Roadmap

Current release: `v1.6.6` (see [CHANGELOG.md](./CHANGELOG.md) for the full release history).

This file is a short pointer. The architecture principles and full wave history live in
[docs/manifesto/ROADMAP.md](docs/manifesto/ROADMAP.md). The live, per-feature plan is not a
markdown file at all — it is the maintainer's tracks database (`vnx horizon`, alias
`vnx objective`). The repo-root [`ROADMAP.yaml`](./ROADMAP.yaml) is a generic example of the
machine-readable roadmap format, not the live plan; [`PR_QUEUE.md`](./PR_QUEUE.md) is a view
generated from it by `scripts/build_pr_queue.py`.

## What ships today

- **Dispatch lanes**: `claude_headless` (`claude -p` via envelope) is the only claude lane —
  the tmux-spawn lane was removed 2026-09-18. It runs on the Max subscription, not
  API credits. Codex CLI, Kimi CLI (OAuth), and a LiteLLM bridge (DeepSeek, GLM) round out
  the provider set. Gemini CLI remains a worker lane; it is no longer a review gate.
- **Governance receipts**: append-only NDJSON audit trail, uniform receipt + unified-report
  shape across all providers. A per-append hash-chain is available but off by default.
- **Default review stack**: `codex_gate,kimi_gate` — both subscription reviewers. `glm_gate`
  and `deepseek_gate` are fallback-only, not default.
- **Models**: T0 runs Opus 5.5; build workers (T1/T2/T3) default to Sonnet, with a per-gate
  diff-size ceiling (`gate_lane_contract.max_diff_chars`).
  Scoped worker permissions are the default; `VNX_WORKER_BLANKET_SKIP=1` is the explicit opt-out.
- **Worktree isolation**: the envelope/headless lane creates a per-dispatch git worktree by
  default. `VNX_ISOLATED_WORKTREE=1` only affects the separate `subprocess_dispatch` path.
- **Elastic worker pool**: `vnx pool` CLI, queue-aware + cost-aware scaling, per-worker
  worktree isolation.
- **Install**: `pip install vnx-orchestration` (PyPI) or from a checkout (`pip install -e .`
  / `./bin/vnx`), `vnx init`, `vnx doctor --strict`.

## Guardrails

- Append-only receipt path stays the canonical audit foundation.
- Human approval gates stay default behavior.
- Provider hooks stay optional, never mandatory for core orchestration.
- Explicit contracts and deterministic recovery are preferred over hidden automation.

## Out of scope (for now)

- Hosted SaaS control plane
- Enterprise RBAC/compliance suite
- Fully distributed orchestration across remote machines
- Rewriting core runtime in Rust/Go before current governance objectives are complete

---

Contributions welcome. See [CONTRIBUTING.md](./CONTRIBUTING.md).
