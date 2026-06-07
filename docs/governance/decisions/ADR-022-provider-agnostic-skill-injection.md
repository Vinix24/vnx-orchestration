# ADR-022 — Provider-Agnostic Skill Injection via Structured Plain-Text Prompt

**Status:** Accepted
**Date:** 2026-06-06
**Decided by:** Operator (Vincent van Deth)
**References:** ADR-015 (wave7 litellm path-b), ADR-016 (unified event shape), bench-v2 field-tests, Anthropic Issues #63390 + #64153

## Context

The field-tests benchmark (2026-06-04/05) compared 6 model lanes — claude (opus-4-8/4-7, sonnet-4-6), deepseek-v4-pro-harness, kimi-k2-6, kimi-k2-0905 — across 9 coding tasks. Mid-analysis the operator identified a methodology flaw: the lanes were not receiving identical context.

- Claude lanes (tmux-spawn + subprocess) inject skill content via the dispatcher's `_inject_skill_context`.
- DeepSeek-harness inherits the same injection because it runs through the `claude` binary with a DeepSeek API key.
- Kimi (kimi-CLI) and Codex received only the intelligence section (ADRs), no skill content.

This made the leaderboard an apples-to-pears comparison: Kimi scored 4.94 median *without* its specialist-skill context while Claude got the full library. Any routing decision built on that data ("use Kimi instead of Claude for skill-routed work") would be unsound.

Three mechanisms existed for delivering skills, each provider-specific:
- Claude Code: runtime `Skill()` tool + `.claude/skills/`
- Kimi CLI: native `--skills-dir` flag
- Codex CLI: no `--skills-dir`; reads `.agents/skills/` from cwd, or AGENTS.md, or plugins

Maintaining three injection paths means three divergence points and three things to keep in sync per skill edit.

## Decision

All worker lanes receive skills through **one provider-agnostic mechanism**: a structured plain-text prompt built by `scripts/lib/skill_prefix.py` and applied uniformly in `lane_adapter.dispatch()`, regardless of provider.

No reliance on `/skill` slash-commands, `$skill` syntax, `--skills-dir` flags, or runtime `Skill()` tools. The skill content is read from one SSOT folder (`.claude/skills/`), frontmatter stripped, and composed into the prompt.

### Prompt structure

The operator's instruction (the T0 dispatch) sits in the **middle** of a layered frame, not at the end as a trailing block:

```
# YOUR ROLE AND METHODOLOGY
<SKILL.md body — full markdown, frontmatter stripped>
<one per skill, separated by horizontal rules for multi-skill>

# YOUR ASSIGNMENT
<T0 dispatch instruction>

# SKILL RESOURCES (use Read/Bash on demand)
Skill folder: <absolute path>
References (use Read on demand):
- references/<file> (<size>) — <first-line summary>
Scripts (use Bash on demand):
- scripts/<file> — <first-line summary>

# CLOSING
Apply the methodology above to your assignment. Read references or run
scripts when relevant. Follow the completion protocol your dispatcher provided.
```

### Resource index, not resource inlining

`references/` and `scripts/` subfolders are **indexed** (path + size + first-line summary), not inlined. The worker reads or runs them on demand via Read/Bash tools. This preserves the on-demand loading behaviour the Claude-native `Skill()` flow gave for free, without pre-loading every reference file into context.

## Rationale

1. **Eliminates annotation divergence.** Plain text is understood by every text-LLM. No per-CLI flag syntax to track.
2. **One SSOT folder, one function.** A skill edit propagates to all providers automatically. No three-way sync.
3. **Token cost is neutral at point of use.** A runtime `Skill()` invocation loads the same ~750-token SKILL.md body the moment it fires; the plain-prepend loads it up front. For dispatches where T0 pre-routes the specialist (the normal VNX pattern), the skill is always used, so there is no waste. Measured: security-engineer structured prompt = ~1112 tokens.
4. **Deterministic + auditable.** The operator sees exactly what was injected. No runtime-tool indirection obscuring which skill content reached the model.
5. **Instruction-in-the-middle reads better.** Layered context (role → task → resources → closing) outperforms one long block preceding a terse trailing instruction.

## Trade-offs accepted

- **No worker-driven runtime skill escalation.** A worker cannot decide mid-run "I need skill X that wasn't in my initial set". For T0-routed dispatch where the specialist is known ahead of time, this is not a loss. If runtime escalation is ever needed, claude-lanes can fall back to the native `Skill()` tool.
- **Possible double-injection on claude lanes.** The claude dispatchers' `_inject_skill_context` may also fire on the `--role <name>` flag, layering skill content twice. Token impact ~1000 extra on subscription = $0. Resolved by disabling dispatcher-side injection when the structured prompt is already present (scheduled with the skill-aware re-bench).

## Consequences

- `scripts/lib/skill_prefix.py` is the canonical injection module. `build_structured_prompt()` is the entry point. Legacy `build_skill_prefix()` / `inject_skill_prefix_into_instruction()` remain for back-compat and will be removed after the skill-aware re-bench validates the structured path.
- `tasks.yaml` declares `skill: <name>` (or `skills: [a, b]`) per task. `run_field_tests.py` reads it and passes `skill_names` to the dispatcher.
- The benchmark leaderboard prior to the skill-aware re-bench carries a disclaimer: Kimi + Codex scored without skill content; skill-pariteit re-measurement pending.
- New providers added later inherit skill injection for free — they only need a lane entry in `models.yaml`; no per-provider skill plumbing.

## Sunset plan — legacy injection

Two legacy paths exist next to `build_structured_prompt()`:

1. `build_skill_prefix()` + `inject_skill_prefix_into_instruction()` in `skill_prefix.py` — the plain-prepend v1 (trailing-instruction layout), superseded by the structured layout.
2. Dispatcher-side `_inject_skill_context` on claude lanes, triggered by `--role <name>` — the source of the accepted double-injection.

Phased removal, gated on evidence:

| Phase | Gate | Action |
|---|---|---|
| 1 (now) | — | Both paths live. Structured prompt is the only path the bench uses; dispatchers untouched. |
| 2 | Skill-aware re-bench (2026-06-08) shows structured path produces role-adoption on all lanes with no composite regression vs the 06-04/05 baseline | Dispatchers skip `_inject_skill_context` when the instruction already carries the structured-prompt markers (`# YOUR ROLE AND METHODOLOGY` … `# CLOSING`). Detection over configuration: no new env flag. |
| 3 | One week of production dispatches (receipts) on phase-2 behaviour without skill-related rework | Delete `build_skill_prefix()` + `inject_skill_prefix_into_instruction()` and their tests. `build_structured_prompt()` is the single path. |
| Rollback | Composite regression or role-adoption failure on any lane | Revert phase 2 commit; legacy paths are intact until phase 3, so rollback is a one-commit revert. |

## Validation

- Unit smoke: security-engineer structured prompt builds correctly (role → assignment → resources → closing markers verified, instruction preserved verbatim).
- End-to-end: codex-gpt-5-4 ran T3-09 with debugger-skill injected via this path (composite 4.38, real report written) — proves a non-claude provider parses and acts on the structured prompt.
- **E2E role-adoption smoke, 2026-06-07** (`runners/skill_smoke.py`): neutral assignment (never says "security") with planted SQL-injection + hardcoded credential + off-by-one; security-engineer skill injected via `build_structured_prompt()`. **6/6 lanes PASS** — every dispatch mechanism delivers the structured prompt, the worker surfaces the security finding (presence-based per F3, not strict ordering), and reproduces the skill's mandatory activation line:

  | Lane | Mechanism | Wallclock | Activation line | Security surfaced |
  |---|---|---:|---|---|
  | claude-opus-4-6 | tmux interactive | 50.8s | yes | yes |
  | claude-sonnet-4-6 | headless subprocess | 28.6s | no (role vocabulary present) | yes |
  | deepseek-v4-pro-harness | claude-harness | 27.2s | yes | yes |
  | deepseek-v4-pro-bare | bare litellm chat | 19.2s | yes | yes |
  | codex-gpt-5-4 | codex CLI | 195.5s | yes | yes |
  | kimi-k2-6 | kimi CLI OAuth | 51.9s | yes | yes |

  kimi was confirmed after its billing-cycle quota reset (2026-06-07): leads with SQL-injection (CVSS 9.8) + hardcoded token, activation line present. All six provider mechanisms verified — provider-agnostic skill injection holds across the full matrix.
- Full cross-lane validation: skill-aware re-bench (all 7 lanes, identical skill context) — pending.
