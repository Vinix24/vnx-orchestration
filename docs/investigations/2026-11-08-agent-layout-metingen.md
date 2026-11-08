# Agent-layout measurements

Points 3, 4 and 5 of track `agents-als-map-fase-1`. The instrument is `scripts/probe_agent_layout.sh`; the plan is the spec (`## Metingen (punt 3, 4 en 5)` in `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/agents-als-map-fase-1.md`, revision 2) and ADR-050 holds the contracts.

Only the T0/operator runs the CLI cells: a builder cannot start `claude -p` (`scripts/hooks/pretooluse_block_raw_claude_spawn.sh`). Each CLI cell runs on the operator's own account (`~/.claude`), cwd in a probe tree under `$TMPDIR`, throwaway store (`VNX_DATA_DIR=<probe>/data`, `VNX_DATA_DIR_EXPLICIT=1`); sentinels exist only there. Transcripts are read only as paths, types, lengths and hashes, through `scripts/lib/context_manifest.py`. A cell is `ja`, `nee`, or `niet gemeten: <reason>`; a goal point is measured only when its decisive cells gave `ja` or `nee`. Run `scripts/probe_agent_layout.sh --cell all --out <file>`; `--self-check` validates the table and cell L without a CLI.

## Cell results (T0 run 2026-11-08, claude 2.1.319, own account, main `425960e0`)

The T0 ran `env -u CLAUDE_CONFIG_DIR scripts/probe_agent_layout.sh --cell all` and re-measured every cell that gave no clean answer, with a copy of the probe that keeps the raw output. Columns: the probe's own outcome of that run, the T0 re-measurement, the conclusion. Three of the four decisive CLI cells were wrong because of the probe, not the CLI (see "Probe defects" below); this round fixes them.

| Date | Cell | Point | Decisive | Probe outcome (T0 run) | T0 re-measurement | Conclusion |
|---|---|---|---|---|---|---|
| 2026-11-08 | A | 3 | yes | nee: "no Project instruction for the sentinel (seen types: Project,User)" | the transcript scan lists `Project <probe>/A/CLAUDE.md`; the probe compared with `$PROBE_DIR/A/CLAUDE.md`, and `$PROBE_DIR` held `//` because `$TMPDIR` ends in `/` | false `nee`. **The ancestor `CLAUDE.md` is loaded** as a `Project` instruction. |
| 2026-11-08 | C | 3 | no | niet gemeten: "claude exited 1 without a completed call" | not re-measured (`--bare`, not decisive) | **open**. The fixed probe records the class of the failure (`requires_api_key`, `unknown_flag`, `other`). |
| 2026-11-08 | D | 3 | yes | nee: "no AutoMem instruction loaded for this cwd" | `auto_memory: {loaded: False}` in the scan of cell A | **no projects memory for a fresh cwd.** |
| 2026-11-08 | H1 | 4 | yes | niet gemeten: "positive control gave no proof: no_hook_denial (exit 0)" | allowed Write: `tool_result` without `is_error`, `allowed.txt` holds `probe`. Out-of-scope Write: `is_error=True`, "PreToolUse:Write hook error: worker-scope: write target '...H1-outside.txt' ...", file not created | false `niet gemeten`. **Enforced by the worker-scope hook.** |
| 2026-11-08 | H2 | 4 | yes | niet gemeten: same | allowed Write `docs/x.md`: no `is_error`, holds `probe`. `scripts/x.py`: `is_error=True`, "worker-scope: write target 'scripts/x.py' is outside file_write_scope narrowed by this dispatch's declared paths for role 'code-worker'", file not created | false `niet gemeten`. **Enforced by the worker-scope hook**, also when the dispatch narrows to `docs/`. |
| 2026-11-08 | L | 4 | yes | ja: bare lanes `deepseek-harness`, `glm`, `deepseek-classifier` | static | **ja**. |
| 2026-11-08 | B | 5 | yes | ja: "call succeeded and init apiKeySource=none with credential variables unset; OAuth works without --bare" | field name `apiKeySource` confirmed | **ja**. OAuth works without `--bare`. |

Raw probe output of the run: `claudedocs/2026-11-07-naar-1-9/metingen/agent-layout-cells.ndjson`. Re-measurement: `claudedocs/2026-11-07-naar-1-9/metingen/agent-layout-t0-nameting-2026-11-08.md`.

## Probe defects found by the T0 run

1. **Paths with `//`.** `inspect` compared `endswith("$root/CLAUDE.md")` without normalisation, while the CLI reports the normalised path. `$PROBE_DIR` is now built from `$TMPDIR` without trailing slashes, and `inspect` compares `os.path.realpath` (or the normalised suffix) on both sides.
2. **A successful Write carries no `is_error`.** `stream_facts` required `is_error is False`, so the positive control never gave proof and `why_not` named it `no_hook_denial`. A successful Write is now a `tool_result` for that `tool_use` id with `is_error` absent, `null` or `false` and without the hook's refusal text. `denied` is as strict as before (`is_error` true plus the worker-scope text). `why_not` names what happened: `no_write_attempt`, `no_tool_result`, `write_error_without_hook_text`, `write_result_unclear`, `hook_refused_the_allowed_write`, `allowed_file_not_as_expected`; `no_hook_denial` is only used for the denied call.
3. **`judge_write` gave `ja` after a failed call** (codex warning on #2527 r3). A non-zero exit of either call (timeout and an empty status included) now gives `niet_gemeten` with the exit code, before any other judgement.

Cell L is static. Its raw output names every lane starter with file and line. `--bare` (hooks never fire): `deepseek_harness_spawn.py:155` (`deepseek-harness`), `glm_harness_spawn.py:89` (`glm`), `deepseek_provider.py:101` (the `deepseek` classifier), and the shared builder `harness_config_dir.py:319`. No `--bare` (hooks can fire): `envelope_adapters_claude.py:210` (`claude_headless`), `claude_spawn.py:187` (terminal-pinned claude lane), `claude_gate_lane.py:213` (`claude_gate`).

## Point 3 — an ancestor `CLAUDE.md` and the `projects` memory

Decisive cells: A (does a sentinel `CLAUDE.md` one level above the cwd arrive as a `Project` instruction?) and D (which `auto_memory.path` the CLI loads for that cwd?). C repeats A under `--bare` and is not decisive; it couples to point 5. The T0 run of 2026-11-08 settles both: A shows the ancestor `CLAUDE.md` is loaded as a `Project` instruction (the probe's `nee` was a `//` path defect), D shows no projects memory is loaded for a fresh cwd. C stays open.

## Point 4 — is `allowed_paths` enforced?

Decisive cells: H1 (the registered hook denies a write outside the fallback scope while the allowed positive control lands), H2 (a dispatch narrowing `docs/` denies `scripts/x.py` and allows `docs/x.md`) and L. L closes its half statically: on the `deepseek-harness`, `glm` and `deepseek` classifier lanes a PreToolUse hook never fires, so enforcement there is a measurable `nee` regardless of registration. H1 and H2 are `ja` only when the stream shows a `Write` aimed at the denied path and the hook's refusal (`worker-scope: write target ... is outside`) as its error result, with the denied file absent and the allowed write performed. A missing attempt, a missing refusal, a timeout, a CLI exit or an authentication failure is `niet gemeten` with that reason (`no_write_attempt`, `no_hook_denial`, `timeout`, `cli_exit_<n>`, `auth_failed`). The denied file present is `nee`. The T0 run of 2026-11-08 shows the hook refusal on the out-of-scope Write and the allowed Write performed in both cells: enforced by the worker-scope hook. The probe's `niet_gemeten` came from the `is_error` defect above.

## Point 5 — the `--bare` flag

Decisive cell: B (does `claude -p` without `--bare` authenticate over OAuth on the own account?). B is the same call as `scripts/probe_claude_cli.sh`, whose recurring row lives in `docs/operations/CLAUDE_CLI_MEASUREMENTS.md`; both must agree. B runs with the credential variables unset and is `ja` only when the `system`/`init` event reports `apiKeySource` `none`; a key source or a missing init event is `niet gemeten` (`auth_source_api_key`, `auth_source_unproven`). The T0 run of 2026-11-08 gave `ja`.

## Re-measuring

The decisive cells (A, D, H1, H2, B) are measured again with this fixed probe after the merge, by the T0 on its own account (`~/.claude`): `scripts/probe_agent_layout.sh --cell all --out /tmp/agent-layout-cells.ndjson`. The table above is then updated with the new outcome, its command and the `sha256` of the raw output the probe judged. Until then the conclusions rest on the T0 re-measurement of 2026-11-08. `--self-check` (no `claude`, exit 0) revalidates the table and cell L.
