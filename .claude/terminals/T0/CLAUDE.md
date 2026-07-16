<!--
  Thin T0 pointer. The canonical orchestrator role lives in role-orchestrator.md
  and is identical across the whole fleet — synced by `vnx role sync`.

  DO NOT edit role-orchestrator.md inside a consumer project: edit it in the VNX
  keystone (github.com/Vinix24/vnx-orchestration) and run `vnx role sync` to
  propagate. Project-specific context goes BELOW the import, in its own section,
  and is the only sanctioned per-project deviation.
-->

@role-orchestrator.md

<!--
  Skill body imported passively because the skill is intentionally not
  model-invocable (A-4, disable-model-invocation: true). Content-in-context
  replaces the old `load @t0-orchestrator` Skill-tool call.
-->

@../../skills/t0-orchestrator/SKILL.md
