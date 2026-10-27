# OSS harnesses for the T0 role: coupling map, door measurement, candidate matrix

Dispatch-ID: dlv-6449538590c3-r2
**Model**: deepseek-flash
**Provider**: deepseek-harness

Track `oss-harness-orchestrator-interchange` (golf 3, research). Read-only on code: this file and `changelog.d/dlv-6449538590c3-r2.md` are the only changes.

**Measured on:** head `164d48ad` (`docs(store-path-parity): runbook for the legacy XDG store migration (#2279)`, 2026-10-27). Every `file:line` in this document was read on that head.

**This document is internal.** It makes no claim that VNX works with any harness, no claim that any harness can carry the T0 role, and no external claim at all. Nothing in this track ran a live T0 on another harness. A proef was not run; the only executed measurements are the `--help`/local-package/repo-code reads in section 3 and the two dry-runs plus one control in section 2, all against a throwaway store under `$TMPDIR`.

## 0. Question and method

The question (plan `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/oss-harness-orchestrator-interchange.md`, "Doel"): is the value of VNX the governed dispatch and receipt layer, and could T0 be harness-agnostic? Four deliverables: (1) a measured coupling map, (2) proof that the door's plan does not depend on the Claude Code environment, (3) a per-candidate capability row, (4) an adapter order and an ADR draft.

Method and evidence rules:

- **Coupling map:** `file:line` on the head above, read with `sed`/`grep`. A coupling is named by the contract it uses (hook JSON, tool namespace, tmux/pane semantics), not by the script that happens to implement it.
- **Candidate matrix:** the six mandatory T0 capabilities from ADR-042 (see section 3 for why those six). A cell is filled only from (a) the installed CLI's `--help`, (b) the installed CLI's local package docs, or (c) repo code. **A cell without one of those three sources says "niet gemeten".** No cell is filled from memory.
- **Door measurement:** one spec staged with `stage_spec_bundle` into a throwaway store, then `vnx dispatch <id> --dry-run` three times (ambient shell, strict `env -i`, and a control that re-adds only the two non-Claude variables). Never fired.

## 1. Coupling map: where the T0 role depends on Claude Code today

Eight coupling points, in the order the dispatch names them. "Kind" is the contract that would have to be reimplemented or shimmed by another harness; a shell script that merely *contains* the logic is not itself a hard coupling if another harness can run it.

| # | Coupling point | Where on the head | Kind of coupling |
|---|---|---|---|
| 1 | SessionStart injection (primary role + playbook delivery) | `hooks/sessionstart.sh:424`, `:426`, `:482`, `:494`; wired at `.claude/settings.json:62` and `:107`; deployed copy by `scripts/vnx_init.py:487` and `:495` | Claude Code hook protocol: the script writes `{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":...}}` to stdout and Claude Code inlines it before the first prompt. Another harness needs its own start event and context-injection channel. |
| 2 | Skill tool / skill namespace | `.claude/settings.json:4-10` (Skill allowlist), `.claude/terminals/T0/role-orchestrator.md:15` and `:20` (explicitly avoids the Skill tool), `.claude/skills/t0-orchestrator/SKILL.md` (body shipped through the hook, not the tool) | Claude Code tool namespace and skill discovery. The T0 playbook is deliberately *not* model-invocable; the hook is the delivery path. Another harness must deliver the same body through its own start-injection channel. |
| 3 | `/goal` and the rotation spawner | `/goal` semantics at `.claude/skills/horizon/SKILL.md:211` and `:216`; spawner `scripts/t0_rotate_spawn.sh:313`, `:450`, `:460`, `:473`; state keys `scripts/lib/t0_rotation_state.py:16`, `:65`, `:165`; `/goal` handover `scripts/t0_rotation_delivery_job.py:14`; Stop-hook safety net `scripts/hooks/session_stop_rotation.py:19` wired at `.claude/settings.json:150`; prompt guard `scripts/hooks/t0_context_guard.py:173` wired at `.claude/settings.json:40`, `:129`, `:160` | Claude Code session identity and turn semantics: a `session_id`, a JSONL transcript at `transcript_path`, a Stop hook, a `/goal` Stop-hook evaluator, and a pane footer/spinner the spawner reads. The spawner launches `claude --model opus` and types `/clear` plus a kickoff prompt into a tmux pane. |
| 4 | Status line | `packages/vnx-mods/voortgang/hooks/register.tsx:6` and `:43` (`$.ui.status(statusLine(next))`); the `verbruik` mod in the same package | Claude Code plugin UI API. Not a T0 capability in ADR-042; it is an operator affordance layered on Claude Code. |
| 5 | Role file and tri-file | `.claude/terminals/T0/CLAUDE.md:11` (`@role-orchestrator.md` import); `.claude/terminals/T0/role-orchestrator.md:152-155` (tri-file); `bin/vnx:1517` (`cmd_role_sync`), `bin/vnx:1684` (writes `AGENTS.md` / `GEMINI.md`) | Claude Code `CLAUDE.md` discovery by walking up from cwd, plus a sync command that writes three provider-named mirrors. On this head only `CLAUDE.md` and `role-orchestrator.md` exist in `.claude/terminals/T0/`; `AGENTS.md` and `GEMINI.md` are absent in both the repo root and the T0 folder. The tri-file is therefore a *delivery convention* for Codex/Kimi, not a measured load path in this checkout. |
| 6 | Hooks in `.claude/settings.json` | `:14-44` PreToolUse, `:46-60` SessionEnd, `:62-121` SessionStart, `:123-133` UserPromptSubmit, `:135-165` Stop | Claude Code hook schema and matcher semantics. The hook scripts are ordinary bash/python and portable in principle; the wiring, event names and stdin/stdout contracts are Claude Code's. |
| 7 | Auto-mode classifier | `.claude/skills/horizon/SKILL.md:77` ("Without the rule the auto-mode classifier refuses a T0 promote"), `:216` ("without auto-mode every tool call still prompts") | A Claude Code permission/auto-mode engine. Measured: no `statusLine` or auto-mode key exists in `.claude/settings.json` on this head; this is a harness feature referenced by the playbook, not repo configuration. |
| 8 | tmux as transport | `scripts/t0_rotate_spawn.sh:313` (`TMUX`/`TMUX_PANE` required), `:354-356`, `:460`, `:473`; `hooks/vnx_rotate.sh:56-62` and `:138-141`; `scripts/t0_rotation_delivery_job.py:3-4` | tmux itself, not Claude Code. But the rotation's *readiness* checks are Claude Code pane checks: `scripts/t0_rotate_spawn.sh:106` (spinner pattern) and `:487` (footer pattern default `auto mode`). ADR-042 decision 5 keeps tmux as transport; the pane-semantics coupling stays with the claude adapter. |

Notes that matter for a second harness:

- Points 1, 3, 5 (the `CLAUDE.md` half), 6 and 7 are Claude-Code-only by construction.
- Points 2 and 4 are affordances, not capabilities: the playbook reaches T0 through point 1, and the status line is optional.
- Point 8 is reusable if the harness gives an interactive pane; its Claude-specific part is the footer/spinner probe, not tmux.
- ADR-042 already names the direction: hooks, skills and rotation are "de claude-adapter van de T0-rol" in golf 3 (`docs/governance/decisions/ADR-042-harness-contract.md:74`), and a harness may fill the T0 role only if it delivers the six capabilities in the same ADR (`:37-48`).

## 2. The door without the Claude Code environment

### 2.1 Setup: a throwaway store, explicit contract

The plan-gate requirement 1 is binding: staging sets **both** `VNX_DATA_DIR=<throwaway>` and `VNX_DATA_DIR_EXPLICIT=1`, and the report shows with `scripts/lib/vnx_paths.py` that the throwaway store was used.

Throwaway store created by `mktemp -d` (exact path, used for cleanup):

```
/var/folders/q5/n9hzhbvx3zv05t09g426yblh0000gn/T/vnx-oh1-store.jUoIdA
```

Staging (from the worktree, `VNX_DATA_DIR` and `VNX_DATA_DIR_EXPLICIT=1` set; `stage_spec_bundle` writes `<store>/dispatches/pending/<id>/{instruction.md,dispatch-spec.json}`):

```bash
VNX_DATA_DIR="$TMPSTORE" VNX_DATA_DIR_EXPLICIT=1 python3 - "$TMPSTORE" <<'PY'
import sys, json
sys.path.insert(0, "scripts/lib")
from pathlib import Path
import dispatch_bridge
data_dir = Path(sys.argv[1]).resolve()
p = dispatch_bridge.stage_spec_bundle(
    instruction_text="OH-1 dry-run probe: read-only research, no fire.",
    dispatch_id="20261028-000000-oh1-dryrun-probe",
    role="research-analyst",
    target_slot="T1",
    project_id="vnx-dev",
    provider="claude",
    model="sonnet",
    data_dir=data_dir,
)
print("STAGED:", p)
PY
# STAGED: /private/var/folders/.../vnx-oh1-store.jUoIdA/dispatches/pending/20261028-000000-oh1-dryrun-probe/dispatch-spec.json
```

Proof that the resolver honoured the throwaway store (`VNX_DATA_DIR` and the warning name the throwaway path; `VNX_STATE_DIR` in this printout is the ambient value because the shell still exported the real store, which the door overrides from the staged bundle's physical location):

```bash
VNX_DATA_DIR="$TMPSTORE" VNX_DATA_DIR_EXPLICIT=1 python3 scripts/lib/vnx_paths.py
```

```
PROJECT_ROOT=/Users/vincentvandeth/Development/vnx-orchestration
VNX_CANONICAL_ROOT=/Users/vincentvandeth/Development/vnx-orchestration
VNX_DATA_DIR=/private/var/folders/q5/n9hzhbvx3zv05t09g426yblh0000gn/T/vnx-oh1-store.jUoIdA
... (VNX_DB_DIR and the other subdirs print the ambient central paths; the door's own state_dir below is the throwaway)
VNX_STATE_DIR=/Users/vincentvandeth/.vnx-data/vnx-dev/state
```

The door itself proves the store: the first line of both dry-runs is `spec=/var/folders/.../vnx-oh1-store.jUoIdA/dispatches/pending/...`, and `config_runtime` reports `state_dir=/var/folders/.../vnx-oh1-store.jUoIdA/state`.

### 2.2 Dry-run A: ambient shell (Claude Code / harness variables present)

The ambient shell carries `CLAUDECODE=1`, `CLAUDE_CODE_*`, `CLAUDE_CONFIG_DIR`, `VNX_PROJECT_ID=vnx-dev`, and `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` (this dispatch runs on the deepseek-harness). Command:

```bash
VNX_DATA_DIR="$TMPSTORE" VNX_DATA_DIR_EXPLICIT=1 bin/vnx dispatch 20261028-000000-oh1-dryrun-probe --dry-run
```

Output verbatim (exit 0):

```
[dispatch] single-entry gate: spec=/var/folders/q5/n9hzhbvx3zv05t09g426yblh0000gn/T//vnx-oh1-store.jUoIdA/dispatches/pending/20261028-000000-oh1-dryrun-probe/dispatch-spec.json
config_runtime: no runtime_coordination.db under state_dir=/var/folders/q5/n9hzhbvx3zv05t09g426yblh0000gn/T/vnx-oh1-store.jUoIdA/state (project_id=vnx-dev); operator config in project_config will NOT be read this process — reads fall back to env vars / registry defaults only
config_runtime: no runtime_coordination.db under state_dir=/var/folders/q5/n9hzhbvx3zv05t09g426yblh0000gn/T/vnx-oh1-store.jUoIdA/state (project_id=vnx-dev); operator config in project_config will NOT be read this process — reads fall back to env vars / registry defaults only
[dispatch_cli] [stop-conditions] unmeasurable (not a green light, not a refusal): provider_exhausted, repeated_gate_failure_cause
/Users/vincentvandeth/Development/vnx-orchestration/.vnx-data/worktrees/dispatch-dlv-6449538590c3-r2/scripts/lib/vnx_paths.py:674: VNXDataDirMismatchWarning: VNX data-dir mismatch: resolved data-dir /private/var/folders/q5/n9hzhbvx3zv05t09g426yblh0000gn/T/vnx-oh1-store.jUoIdA does not belong to project 'vnx-dev' (expected central dir /Users/vincentvandeth/.vnx-data/vnx-dev). Running with the wrong data directory can write receipts/state into another project's store or a stray repo-local tree. Fix: ensure VNX_PROJECT_ID matches the intended project, add a .vnx-project-id marker in the project root, or stop pinning a repo-local VNX_DATA_DIR / VNX_STATE_DIR. Set VNX_DATA_DIR_GUARD=enforce to abort on this condition, or =off to silence this warning.
  data_dir_guard.check_data_dir_project_id_guard(vnx_data_dir, _state_project_id)
[dispatch_cli] [reachability] lane=claude_headless — no cheap endpoint check available (claude runs on the operator's subscription headless; it has no network endpoint the door can probe, and its auth state is the operator's keychain, not an env key).
[dispatch_cli] DRY RUN — fingerprint: de46ed248ea0-20261028-000000-oh1-dryrun-probe
  dispatch_id:  20261028-000000-oh1-dryrun-probe
  provider:     claude
  model:        sonnet
  lane:         claude_headless
  target_id:    ephemeral
  billing:      api_metered
  requires_mcp: False
  route_reason: smart-router:governance_variant=default gate=codex_gate direction=unchanged; no dispatch paths; task_class=none -> 'code';D11,D3,D1,D2,D4,D5,D6,D7,D8,D9,D10,D12
  task_class:   01_code_generation
  tier:         - -> tier-mid
  [WARN] constraint-warn: claude-headless: Route forbidden: OPENED 2026-08-11 ... (long constraint text, unchanged between runs)
  [WARN] constraint-warn: track_unlinked: dispatch has no track_id (VNX_REQUIRE_DISPATCH_TRACK is OFF; advisory-only)
  [WARN] OI-1158: claude_headless lane isolation is NOT structurally verified by the door. ... (long warning, unchanged between runs)
```

(The two long `[WARN]` bodies are kept in the raw console log; they are byte-identical in A, B and C and carry no environment signal. The full text is reproduced in the dispatch report's Verification section.)

### 2.3 Dry-run B: `env -i`, no `CLAUDE_*`, no `CLAUDECODE`

Command:

```bash
env -i HOME="$HOME" \
  PATH="/opt/homebrew/opt/python@3.12/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin" \
  VNX_DATA_DIR="$TMPSTORE" VNX_DATA_DIR_EXPLICIT=1 \
  bin/vnx dispatch 20261028-000000-oh1-dryrun-probe --dry-run
```

Output verbatim (exit 0):

```
[dispatch] single-entry gate: spec=/var/folders/q5/n9hzhbvx3zv05t09g426yblh0000gn/T//vnx-oh1-store.jUoIdA/dispatches/pending/20261028-000000-oh1-dryrun-probe/dispatch-spec.json
config_runtime: could not resolve a project_id for state_dir=/var/folders/q5/n9hzhbvx3zv05t09g426yblh0000gn/T/vnx-oh1-store.jUoIdA/state; operator config in project_config will NOT be read this process — reads fall back to env vars / registry defaults only
config_runtime: could not resolve a project_id for state_dir=/var/folders/q5/n9hzhbvx3zv05t09g426yblh0000gn/T/vnx-oh1-store.jUoIdA/state; operator config in project_config will NOT be read this process — reads fall back to env vars / registry defaults only
[dispatch_cli] [stop-conditions] unmeasurable (not a green light, not a refusal): provider_exhausted, repeated_gate_failure_cause
/Users/vincentvandeth/Development/vnx-orchestration/.vnx-data/worktrees/dispatch-dlv-6449538590c3-r2/scripts/lib/vnx_paths.py:674: VNXDataDirMismatchWarning: VNX data-dir mismatch: resolved data-dir /private/var/folders/q5/n9hzhbvx3zv05t09g426yblh0000gn/T/vnx-oh1-store.jUoIdA does not belong to project 'vnx-dev' (expected central dir /Users/vincentvandeth/.vnx-data/vnx-dev). ... Set VNX_DATA_DIR_GUARD=enforce to abort on this condition, or =off to silence this warning.
  data_dir_guard.check_data_dir_project_id_guard(vnx_data_dir, _state_project_id)
[dispatch_cli] [reachability] lane=claude_headless — no cheap endpoint check available (claude runs on the operator's subscription headless; it has no network endpoint the door can probe, and its auth state is the operator's keychain, not an env key).
[dispatch_cli] DRY RUN — fingerprint: f08a26482e57-20261028-000000-oh1-dryrun-probe
  dispatch_id:  20261028-000000-oh1-dryrun-probe
  provider:     claude
  model:        sonnet
  lane:         claude_headless
  target_id:    ephemeral
  billing:      subscription
  requires_mcp: False
  route_reason: smart-router:governance_variant=default gate=codex_gate direction=unchanged; no dispatch paths; task_class=none -> 'code';D11,D3,D1,D2,D4,D5,D6,D7,D8,D9,D10,D12
  task_class:   01_code_generation
  tier:         - -> tier-mid
  [WARN] constraint-warn: claude-headless: Route forbidden: OPENED 2026-08-11 ... (identical to A)
  [WARN] constraint-warn: track_unlinked: dispatch has no track_id (VNX_REQUIRE_DISPATCH_TRACK is OFF; advisory-only)
  [WARN] OI-1158: claude_headless lane isolation is NOT structurally verified by the door. ... (identical to A)
```

### 2.4 Diff A vs B

```
$ diff -u /tmp/oh1_dryrun_a.txt /tmp/oh1_dryrun_b.txt
@@ -1,17 +1,17 @@
 [dispatch] single-entry gate: spec=... (same)
-config_runtime: no runtime_coordination.db under state_dir=.../state (project_id=vnx-dev); ...
-config_runtime: no runtime_coordination.db under state_dir=.../state (project_id=vnx-dev); ...
+config_runtime: could not resolve a project_id for state_dir=.../state; ...
+config_runtime: could not resolve a project_id for state_dir=.../state; ...
 [dispatch_cli] [stop-conditions] unmeasurable ... (same)
 ...VNXDataDirMismatchWarning... (same)
 [dispatch_cli] [reachability] lane=claude_headless ... (same)
-[dispatch_cli] DRY RUN — fingerprint: de46ed248ea0-20261028-000000-oh1-dryrun-probe
+[dispatch_cli] DRY RUN — fingerprint: f08a26482e57-20261028-000000-oh1-dryrun-probe
   dispatch_id:  20261028-000000-oh1-dryrun-probe
   provider:     claude
   model:        sonnet
   lane:         claude_headless
   target_id:    ephemeral
-  billing:      api_metered
+  billing:      subscription
   requires_mcp: False
   route_reason: smart-router:... (same)
   task_class:   01_code_generation
   tier:         - -> tier-mid
   [WARN] ... (same)
```

Two lines differ, and only two causes:

1. **`config_runtime` project-id line.** The ambient shell exports `VNX_PROJECT_ID=vnx-dev`; `env -i` drops it, and the throwaway state dir has no `.vnx-project-id` marker, so `project_id_from_state_dir` (`scripts/lib/vnx_paths.py:727`) returns empty. This is a projection warning, not a lane decision; the door still routed the spec from its physical location.
2. **`billing` and therefore the fingerprint.** `dispatch_plan.claude_auth_is_api_metered` (`scripts/lib/dispatch_plan.py:252`) returns true when `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL` is set. The ambient harness sets `ANTHROPIC_BASE_URL`; `env -i` does not. This is auth identity, not a Claude Code variable.

### 2.5 Control C: re-add only the two non-Claude variables

To show the difference is fully explained, one more `env -i` run added **only** `VNX_PROJECT_ID=vnx-dev` and the ambient `ANTHROPIC_BASE_URL` — still no `CLAUDE_*`, no `CLAUDECODE`:

```bash
env -i HOME="$HOME" PATH="..." \
  VNX_DATA_DIR="$TMPSTORE" VNX_DATA_DIR_EXPLICIT=1 VNX_PROJECT_ID=vnx-dev \
  ANTHROPIC_BASE_URL="$ANTHROPIC_BASE_URL" \
  bin/vnx dispatch 20261028-000000-oh1-dryrun-probe --dry-run
```

`diff -u A C` prints nothing (exit 0). The lane plan in A is reproduced byte-for-byte without any Claude Code variable.

### 2.6 Claim, and what it does not prove

**Claim (measured):** the door's *lane plan* — `provider`, `model`, `lane`, `target_id`, `requires_mcp`, `route_reason`, `task_class`, `tier` — does not depend on the Claude Code environment. The two lines that differ between A and B are driven by `VNX_PROJECT_ID` and by Anthropic auth identity (`ANTHROPIC_BASE_URL`), and re-adding only those two, with no `CLAUDE_*`/`CLAUDECODE`, reproduces A exactly.

**What this does not prove:**

- **Not that firing works without Claude Code.** The dry-run compiles a plan and prints it; it spawns nothing. The lane itself (`claude -p` via the envelope) is unchanged and still Claude Code.
- **Not that any lane is harness-agnostic.** A `provider=kimi` or `provider=codex` spec would route through `provider_dispatch.py`; that path was not dry-run here.
- **Not rotation.** The spawner requires tmux and Claude panes and was not exercised.
- **Not hooks.** No hook fired in this measurement; the `env -i` run only proves the plan compiler does not read `CLAUDE_*`/`CLAUDECODE`.
- **Not that `env -i` is representative.** The control shows the two differences are non-Claude, but the ambient shell in this dispatch is the deepseek-harness, not a stock Claude Code session.

## 3. Candidate matrix

### 3.1 Which capabilities, and why

ADR-042 **does** list the mandatory T0 capabilities, in the `T0` column of the table under "Besluit" 2 (`docs/governance/decisions/ADR-042-harness-contract.md:37-48`). Six rows say `ja` for T0; the four rows that say `nee` for T0 are deliberately *not* T0 requirements (non-interactive run, exit code/answer read by the fabric, deadline stop, verdict block). The six columns below are those six `ja` rows, verbatim:

- **C1** — Schrijven en git binnen de werkmap
- **C2** — De permissiegrens van de fabric is af te dwingen (allowlist, sandbox of read-only)
- **C3** — Modelidentiteit vast te stellen uit configuratie of uitvoer, niet geraden
- **C4** — Een interactieve sessie die bij de start de canonieke rol laadt
- **C5** — Een startinjectie van de state (hook of gelijkwaardig)
- **C6** — Contextgebruik te meten of te begrenzen, zodat rotatie kan

Installed locally and therefore measurable: `codex` 0.164.0 (`/Users/vincentvandeth/.local/bin/codex`), `kimi` 1.46.0 (`/Users/vincentvandeth/.local/bin/kimi`).

The kimi package root for every `kimi_cli/...` citation below is `/Users/vincentvandeth/.local/share/uv/tools/kimi-cli/lib/python3.12/site-packages/kimi_cli`. The codex README is `/opt/homebrew/lib/node_modules/@openai/codex/README.md`.

**Not installed:** `opencode`, `aider`, `goose`, `crush` (checked with `command -v` and `/opt/homebrew/bin`). Rows: codex CLI, kimi CLI, OpenCode, Aider. Aider is the "at least one more OSS harness"; OpenCode is the OSS harness the plan names.

### 3.2 Matrix

Legend: **covered** / **partly** / **not covered** / **niet gemeten**. Every filled cell names its source inline; a cell without a source is `niet gemeten`.

| Candidate | C1 write+git | C2 permission boundary | C3 model identity | C4 role at start | C5 start injection | C6 context measurable |
|---|---|---|---|---|---|---|
| **codex CLI** 0.164.0 | covered — `codex --help` (`-s/--sandbox workspace-write`, `--worktree`, `--add-dir`) | covered — `codex --help` (`-s read-only\|workspace-write\|danger-full-access`, `-a/--ask-for-approval`, `--approve-for-me`) | covered — `codex --help` (`-m/--model`, `-c model=`, `~/.codex/config.toml`); `scripts/lib/codex_wrapper.py:72-74` | partly — repo tri-file writes `AGENTS.md` for Codex (`.claude/terminals/T0/role-orchestrator.md:152-155`, `bin/vnx:1684`); no local codex help/doc line confirms the load at session start | niet gemeten — `codex --help` shows hook trust (`--dangerously-bypass-hook-trust`) and rules (`--ignore-rules`), but no event list for a start injection | covered — repo `scripts/lib/codex_wrapper.py:28-52` parses `input_tokens`/`output_tokens` from `codex exec --json` |
| **kimi CLI** 1.46.0 | covered — `kimi --help` (`-w/--work-dir`, `--add-dir`, `--yolo` auto-approves tool calls) | partly — `kimi --help` (`--plan`, `--yolo`, `--afk`); local package `kimi_cli/approval_runtime/models.py:1-25` only has approve / approve-for-session / reject, no sandbox or allowlist | covered — `kimi --help` (`-m/--model`, `--config-file`); `scripts/lib/kimi_wrapper.py:44` passes `-m` | covered — local package `kimi_cli/agents/default/system.md:101-123` and `CHANGELOG.md:177` (hierarchical `AGENTS.md` loading) | covered — local package `kimi_cli/hooks/config.py:6-18` lists `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, `SessionEnd`, `PreCompact`, `Notification` | covered — repo `scripts/lib/kimi_wrapper.py:43-76` parses `usage_complete` prompt/completion tokens from `--output-format stream-json` |
| **OpenCode** | niet gemeten | niet gemeten | niet gemeten | niet gemeten | niet gemeten | niet gemeten |
| **Aider** | niet gemeten | niet gemeten | niet gemeten | niet gemeten | niet gemeten | niet gemeten |

OpenCode and Aider: not on `PATH`, no local package or docs on this machine, and no adapter code in the repo (`grep -rniI 'opencode\|aider'` finds only ADR prose and a `repo_map.py` comment, no executable integration). Every cell is therefore "niet gemeten" — installing either would be the missing measurement, not a conclusion.

### 3.3 Count

Four rows × six columns = 24 cells. **Measured: 11** (codex 5, kimi 6). **Niet gemeten: 13** (codex 1, OpenCode 6, Aider 6). No cell is "not covered": no measured capability is absent, but a partly or unmeasured cell is not a pass.

Per-cell evidence notes:

- **codex C4 partly.** The repo's role-sync writes an `AGENTS.md` mirror for Codex (`bin/vnx:1684`) and the canonical role says a Codex session may run under `AGENTS.md` (`.claude/terminals/T0/role-orchestrator.md:152-155`). That is the *delivery intent*, not a local measurement of codex's load behaviour: `codex --help` and the installed README (`/opt/homebrew/lib/node_modules/@openai/codex/README.md`) name neither `AGENTS.md` nor a start-time instruction file. Hence "partly", not "covered".
- **codex C5 niet gemeten.** Hook flags exist, but an event list (in particular a SessionStart-equivalent that can inject context for the state) is not in local help or docs. This is the single unmeasured capability that matters most for a T0 adapter.
- **kimi C2 partly.** The approval runtime gates tool calls and `--yolo`/`--afk` bypass it; `--plan` starts in plan mode. Neither is an allowlist or a filesystem sandbox in the `--help` surface, and `approval_runtime/models.py` has no policy/allowlist type. Enforcement would have to be built or measured.
- **kimi C6 covered via repo code, not `--help`.** `scripts/lib/kimi_wrapper.py:44` runs `kimi --print --output-format stream-json --yolo -m <model>` and `:43-76` parses the usage event. This is a measured integration already in production (review seat), which is stronger evidence than a help string.
- **kimi C4 covered.** The local package's default system prompt describes hierarchical `AGENTS.md` loading with a 32 KiB budget (`kimi_cli/agents/default/system.md:101-123`, `CHANGELOG.md:177`). That is the same mechanism the tri-file targets.
- **OpenCode/Aider.** No local source at all. Per the plan-gate requirement 3, these cells cannot carry an adapter order.

## 4. Recommendation and ADR draft

### 4.1 Adapter order for golf 3

Measured order, strongest evidence first:

1. **kimi CLI.** 6 of 6 capabilities measured; 5 covered and only C2 partly. It already runs as a VNX review seat through `scripts/lib/kimi_wrapper.py`, so C3 and C6 are proven in production wiring, and C4/C5 have a local hook and `AGENTS.md` implementation. Source cells: kimi C1, C3 (`kimi --help`), C4 (`kimi_cli/agents/default/system.md:101-123`), C5 (`kimi_cli/hooks/config.py:6-18`), C6 (`scripts/lib/kimi_wrapper.py:43-76`), C2 partly (`kimi --help`, `kimi_cli/approval_runtime/models.py:1-25`).
2. **codex CLI.** 5 of 6 measured; C1, C2, C3 and C6 covered, C4 partly, C5 unmeasured. It is close, but the T0 role cannot ship on an unmeasured start injection. The missing measurement is named: does codex fire a start-of-session hook (or equivalent) that can inject context, and does it load `AGENTS.md` at interactive start? Source cells: codex C1, C2, C3 (`codex --help`), C6 (`scripts/lib/codex_wrapper.py:28-52`), C4 partly (`bin/vnx:1684`).
3. **OpenCode.** 0 of 6 measured; it cannot be ordered on evidence. It belongs last not because it is weak but because nothing about it is known locally. The missing measurement: install OpenCode and capture `opencode --help`, its plugin/hook documentation and its token-usage output; then fill the same six cells.

This differs from the plan's listing order (codex, kimi, OpenCode). The plan order is not the measured order: **kimi has the broader measured surface**. The plan-gate requirement 3 forbids letting "niet gemeten" cells carry the order, and codex's single missing cell (C5) is exactly a T0 capability.

If the operator wants codex first for reasons outside these cells (for example the review-stack symmetry with `codex_gate`), that is a policy choice, not a measurement. The report does not make it.

### 4.2 ADR draft (in the report body, not in `docs/governance/decisions/`)

**Title:** T0 harness adapters are admitted by a measured six-capability gate

**Context.** T0 today couples to Claude Code at eight points (section 1): a SessionStart hook that injects the role and playbook, a Skill namespace it deliberately avoids, a `/goal` and rotation path built on Claude session identity and transcripts, a plugin status line, `CLAUDE.md` discovery plus a tri-file convention, a hook schema, an auto-mode permission classifier, and tmux pane semantics. ADR-042 already split harness from model and listed six capabilities a harness must deliver for the T0 role. The door's lane plan was measured independent of the Claude Code environment (section 2), but a T0 adapter is not the door. The candidate matrix measured 11 of 24 cells (section 3); the rest are "niet gemeten".

**Decision.**

1. Golf 3 builds T0 adapters in the measured order **kimi → codex → OpenCode**. OpenCode stays provisional until its six cells are measured; no adapter is admitted on unmeasured cells.
2. An adapter is **ready** only when all six ADR-042 T0 capabilities are measured as *covered*, or a *partly* cell is accepted explicitly with the gap written down. `niet gemeten` is never a pass. Each cell records its source (CLI `--help`, local package docs, or repo code).
3. The adapter owns spawn, permission scope, model-identity readout, start-of-session injection and context measurement. The door, worktree isolation, report contract, ledger, receipt pipeline and review/merge gates stay harness-agnostic (ADR-042 decisions 3 and 4).
4. Status line and auto-mode classifier are **not** T0 capabilities. They stay in the claude adapter or are dropped; they must not appear in the admission gate.
5. The claude adapter is the existing behaviour, unchanged: hooks, skills, SessionStart injection, `/goal` rotation and tmux stay as they are, behind the adapter boundary.

**Consequences.**

- Positive: the order is evidence-driven, the missing measurements are named, and a partly cell cannot silently become a shipped gap.
- Positive: the six capabilities are already an accepted ADR; this decision only makes them the admission gate and fixes the order.
- Negative: kimi first departs from the plan's nominal order; the operator may override on policy, and the report then no longer carries the order.
- Negative: codex cannot ship until C5 is measured; that is work, not a documentation gap.
- Cost: OpenCode needs an install and a measurement pass before it can even be ranked; no live T0 on the project, per the track's hard boundary.

## 5. Boundary of this report

- Internal only; no external claim. It does not say VNX "works with" any harness.
- No live T0 ran on another harness on this project.
- Nothing here writes to `~/.claude`, `~/.claude-salesminds`, or a consumer repo.
- The dry-run evidence covers the plan compiler only. Firing, the lanes themselves, rotation and hooks were not measured (section 2.6).
- The matrix's "covered" means "the local source shows the capability exists"; it does not mean an adapter has been built or tested.
