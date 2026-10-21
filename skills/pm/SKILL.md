---
name: pm
description: >
  Backward-compat alias for @horizon (this skill was renamed from `pm` to `Horizon` on
  2026-07-05). USE THIS only when invoked directly as `/pm` or via an existing `@pm`
  delegation reference — it redirects immediately to @horizon, the strategic owner of the
  VNX future-state layer (roadmap -> tracks -> deliverables) and the plan-first gate. Prefer
  `@horizon` / `vnx horizon` for anything new; this alias exists only for transition safety.
user-invocable: true
allowed-tools: [Read, Grep, Glob, Bash]
paths: [".vnx-data/**", "claudedocs/**", "ROADMAP.yaml"]
---

# PM — alias for Horizon (renamed 2026-07-05)

`pm` was renamed to **Horizon**. This file exists ONLY as a backward-compat pointer so
existing `/pm` invocations and `@pm` delegation references (e.g. in `planner`) keep
resolving during the transition. It carries no independent instructions.

**Load `@horizon` now and follow its instructions in full instead of this file.** See
`.claude/skills/horizon/SKILL.md` for the actual skill: the strategic owner of the VNX
future-state layer (roadmap -> tracks -> deliverables) and the plan-first gate, driven via
`vnx horizon` (alias `vnx objective`).
