# Agent-layout measurements

Points 3, 4 and 5 of track `agents-als-map-fase-1`. The instrument is `scripts/probe_agent_layout.sh`; the plan is the spec (`## Metingen (punt 3, 4 en 5)` in `claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/agents-als-map-fase-1.md`, revision 2) and ADR-050 holds the contracts.

Only the T0/operator runs the CLI cells: a builder cannot start `claude -p` (`scripts/hooks/pretooluse_block_raw_claude_spawn.sh`). Each CLI cell runs on the operator's own account (`~/.claude`), cwd in a probe tree under `$TMPDIR`, throwaway store (`VNX_DATA_DIR=<probe>/data`, `VNX_DATA_DIR_EXPLICIT=1`); sentinels exist only there. Transcripts are read only as paths, types, lengths and hashes, through `scripts/lib/context_manifest.py`. A cell is `ja`, `nee`, or `niet gemeten: <reason>`; a goal point is measured only when its decisive cells gave `ja` or `nee`. Run `scripts/probe_agent_layout.sh --cell all --out <file>`; `--self-check` validates the table and cell L without a CLI.

## Cell results (round 2026-11-08, before the T0 run)

| Date | Cell | Point | Decisive | Setup / command | Output sha256 | Outcome |
|---|---|---|---|---|---|---|
| 2026-11-08 | A | 3 | yes | cwd `$PROBE/A/klant`, sentinel `$PROBE/A/CLAUDE.md`; `claude -p 'Reply with OK.' --output-format text --session-id <uuid>` | - | niet gemeten: T0-run volgt na de merge |
| 2026-11-08 | C | 3 | no | as A with `--bare` | - | niet gemeten: T0-run volgt na de merge |
| 2026-11-08 | D | 3 | yes | as A without a sentinel; `auto_memory.path` from the session transcript | - | niet gemeten: T0-run volgt na de merge |
| 2026-11-08 | H1 | 4 | yes | probe repo, `--settings <probe-settings.json>` registers `scripts/hooks/pretooluse_worker_scope_enforce.sh`, `VNX_ENFORCE_WORKER_PERMISSIONS=1`; Write `allowed.txt` and `$PROBE/H1-outside.txt`, both calls with `--output-format stream-json --verbose` | - | niet gemeten: T0-run volgt na de merge |
| 2026-11-08 | H2 | 4 | yes | as H1 plus `VNX_DISPATCH_PATHS='["docs/"]'`; Write `docs/x.md` and `scripts/x.py`, both calls with `--output-format stream-json --verbose` | - | niet gemeten: T0-run volgt na de merge |
| 2026-11-08 | L | 4 | yes | `scripts/probe_agent_layout.sh --cell L` (static `grep -nF` over the lane starters in `scripts/`) | `ffbc6bb2fb19a015c51c4d71c620fabf66dc28ae10c6a89e371f9d38a9ed845f` | ja |
| 2026-11-08 | B | 5 | yes | cwd `$PROBE/B/klant`; `env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN -u CLAUDE_CODE_USE_BEDROCK -u CLAUDE_CODE_USE_VERTEX claude -p 'Reply with the single word OK.' --output-format stream-json --verbose` (no `--bare`) | - | niet gemeten: T0-run volgt na de merge |

Cell L is the only cell this round could measure. Its raw output names every lane starter with file and line. `--bare` (hooks never fire): `deepseek_harness_spawn.py:155` (`deepseek-harness`), `glm_harness_spawn.py:89` (`glm`), `deepseek_provider.py:101` (the `deepseek` classifier), and the shared builder `harness_config_dir.py:319`. No `--bare` (hooks can fire): `envelope_adapters_claude.py:210` (`claude_headless`), `claude_spawn.py:187` (terminal-pinned claude lane), `claude_gate_lane.py:213` (`claude_gate`).

## Point 3 — an ancestor `CLAUDE.md` and the `projects` memory

Decisive cells: A (does a sentinel `CLAUDE.md` one level above the cwd arrive as a `Project` instruction?) and D (which `auto_memory.path` the CLI loads for that cwd?). C repeats A under `--bare` and is not decisive; it couples to point 5. A and D are `niet gemeten` until the T0 run, so point 3 is not yet closed.

## Point 4 — is `allowed_paths` enforced?

Decisive cells: H1 (the registered hook denies a write outside the fallback scope while the allowed positive control lands), H2 (a dispatch narrowing `docs/` denies `scripts/x.py` and allows `docs/x.md`) and L. L closes its half statically: on the `deepseek-harness`, `glm` and `deepseek` classifier lanes a PreToolUse hook never fires, so enforcement there is a measurable `nee` regardless of registration. H1 and H2 are `ja` only when the stream shows a `Write` aimed at the denied path and the hook's refusal (`worker-scope: write target ... is outside`) as its error result, with the denied file absent and the allowed write performed. A missing attempt, a missing refusal, a timeout, a CLI exit or an authentication failure is `niet gemeten` with that reason (`no_write_attempt`, `no_hook_denial`, `timeout`, `cli_exit_<n>`, `auth_failed`). The denied file present is `nee`. Until the T0 run both are `niet gemeten`.

## Point 5 — the `--bare` flag

Decisive cell: B (does `claude -p` without `--bare` authenticate over OAuth on the own account?). B is the same call as `scripts/probe_claude_cli.sh`, whose recurring row lives in `docs/operations/CLAUDE_CLI_MEASUREMENTS.md`; both must agree. B runs with the credential variables unset and is `ja` only when the `system`/`init` event reports `apiKeySource` `none`; a key source or a missing init event is `niet gemeten` (`auth_source_api_key`, `auth_source_unproven`). Until the T0 run B is `niet gemeten`.

## Re-measuring

The T0 re-runs `scripts/probe_agent_layout.sh --cell all --out /tmp/agent-layout-cells.ndjson` after the merge and replaces each row above with the measured outcome, its command and the `sha256` of the raw output the probe judged. `--self-check` (no `claude`, exit 0) revalidates the table and cell L.
