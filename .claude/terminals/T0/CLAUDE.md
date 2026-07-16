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
  t0-orchestrator's SKILL.md body is intentionally NOT `@`-imported here.
  It's not model-invocable (A-4, disable-model-invocation: true), so an
  import used to sit on this line as a passive delivery path — but a
  `@`-import of a file outside this directory tree (.claude/skills/... is a
  sibling, not a descendant, of .claude/terminals/T0/) is classified by
  Claude Code as an "external CLAUDE.md file import" and blocks session
  start on an interactive trust prompt. A fresh autonomous T0 spawn has no
  human to answer that prompt and just hangs (F1, 2026-07-16 live smoke
  test). The playbook body now reaches T0 via the SessionStart hook instead
  (`hooks/sessionstart.sh`, deployed fleet-wide as
  `.claude/hooks/sessionstart.sh` by `vnx init`/`bootstrap_hooks`), which
  injects it as additionalContext before the first prompt — no import, no
  Skill-tool call, no prompt. See role-orchestrator.md's "Mandatory Startup".
-->

