# Agent-skill-discovery measurements

The points of the `## Done measurement` table of track `agent-skill-discovery-binding` that a probe cell decides. The instrument is `scripts/probe_agent_skill_discovery.sh` (R7); the plan is the spec (`## Done measurement` in `claudedocs/2026-11-07-naar-1-9/plannen/agent-skill-discovery-binding.md`, revision 2) and ADR-059 holds the contracts. This document is R8: it records the T0 run after R7 merged. The values below are copied from the measurement, never read upward.

Only the T0/operator runs the CLI cells: a builder cannot start `claude -p` or `codex exec` (`scripts/hooks/pretooluse_block_raw_claude_spawn.sh`). Each cell runs in a throwaway git worktree under `$TMPDIR` with the R2 ignore rule and a probe slug that exists only through materialisation, on the operator's own account. A cell is `ja`, `nee` or `niet gemeten: <reason>`. A row counts only with the sha256 of the output judged. The raw JSON lines are `claudedocs/2026-11-07-naar-1-9/metingen/agent-skill-discovery-cells.ndjson`, read only.

## Cell results (T0 run after R7, claude 2.1.319, codex 0.164.0)

The T0 ran `VNX_PROBE_OPERATOR=1 scripts/probe_agent_skill_discovery.sh --run --cell all --out <file>` once after R7 merged. The line carries no repository sha; it is not in the measurement and is not filled in here. Columns are verbatim from the line.

| Cell | Decisive (yes/no) | Outcome (verbatim) | Command (verbatim) | CLI version (verbatim) | sha256 (verbatim) |
|---|---|---|---|---|---|
| C1 | yes | ja | `cd /private/var/folders/q5/n9hzhbvx3zv05t09g426yblh0000gn/T/probe-skill-discovery.1u3Zma/C1 && VNX_AGENT_LAYOUT=on env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN -u CLAUDE_CODE_USE_BEDROCK -u CLAUDE_CODE_USE_VERTEX claude -p 'Reply with the single word OK.' --output-format stream-json --verbose --session-id 1fe0560d-f0a8-42bd-95a5-bb4c5b15daf7 ` | claude 2.1.319; codex 0.164.0 | `022a14b0ee9a6d2f3fdd7e3b95164c1b9d3c870b125819bf7dd66787f66452b8` |
| C2 | no | niet gemeten: cli_exit_1: no stderr | `cd /private/var/folders/q5/n9hzhbvx3zv05t09g426yblh0000gn/T/probe-skill-discovery.1u3Zma/C2 && VNX_AGENT_LAYOUT=on env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN -u CLAUDE_CODE_USE_BEDROCK -u CLAUDE_CODE_USE_VERTEX claude -p 'Reply with the single word OK.' --output-format stream-json --verbose --session-id 22143549-8fcb-4552-bb9b-ddfc0300f5c4 --bare` | claude 2.1.319; codex 0.164.0 | `25b3de486797325870050ccb39ea56d514d98e732c267d4d06a4a08fbd80740c` |
| C3a | yes | ja | `cd /private/var/folders/q5/n9hzhbvx3zv05t09g426yblh0000gn/T/probe-skill-discovery.1u3Zma/C3a && PYTHONPATH=/Users/vincentvandeth/Development/vnx-orchestration/scripts/lib python3 -c "from agent_skill_materialize import materialize_skill_set as m; m('probe-skill-discovery', worktree='/private/var/folders/q5/n9hzhbvx3zv05t09g426yblh0000gn/T/probe-skill-discovery.1u3Zma/C3a', provider='codex', mode='on', dispatch_id='probe-vnx-probe-skill-disc-7b1dd25a', state_dir='/private/var/folders/q5/n9hzhbvx3zv05t09g426yblh0000gn/T/probe-skill-discovery.1u3Zma/data', project_id='probe-skill-discovery')" && ls -1 .agents/skills` | claude 2.1.319; codex 0.164.0 | `f592366a9c9728c12249ca1b9edb4a324dc9db3c6531a49357ee0fa9da4a7ba8` |
| C3b | yes | ja | `cd /private/var/folders/q5/n9hzhbvx3zv05t09g426yblh0000gn/T/probe-skill-discovery.1u3Zma/C3b && codex exec --json --sandbox read-only 'List the name of every skill available to you in this project, one per line, and nothing else.'` | claude 2.1.319; codex 0.164.0 | `ff47f790b8e99d1b618e7c1909b0b40c5b3e6b673410cc564fd5a250e96fb4bc` |

## The DONE point for claude (C1)

Decisive cell: C1. Its outcome is `ja` and its sha256 is `022a14b0ee9a6d2f3fdd7e3b95164c1b9d3c870b125819bf7dd66787f66452b8`. The reason in the line is: `declared set is a subset of the initial listing (1/1 present); by origin: repo=1,user=23,synced=23,namespaced=44,other=14`.

**The point is met on its decisive cell.** C1 is `ja`: the session listing names the materialised slug, so the declared set is a subset of what the CLI saw.

The line also counts the rest per origin: `repo=1`, `user=23`, `synced=23`, `namespaced=44`, `other=14`. The plan row names `repo 27, user 25`. Those numbers do not match. This document records the difference and does not explain it.

Uncertain: the plan row writes 27 and 25 from the repository measurement in ADR-059 section 8; the probe runs in a throwaway worktree with one probe skill. The line itself gives only the counts above.

## The DONE point for codex (C3a and C3b)

Decisive cells: C3a and C3b, both decisive. Both outcomes are `ja`.

- **C3a** (`f592366a9c9728c12249ca1b9edb4a324dc9db3c6531a49357ee0fa9da4a7ba8`): `in a fresh worktree .agents/skills holds exactly the declared set: vnx-probe-skill-disc-7b1dd25a`.
- **C3b** (`ff47f790b8e99d1b618e7c1909b0b40c5b3e6b673410cc564fd5a250e96fb4bc`): `the answer lists vnx-probe-skill-disc-7b1dd25a as an entry and not the negative control`.

**The point is met on its decisive cells.** C3a proves the placement and C3b asks the CLI itself; the negative control is not named. The test half of the plan row falls outside this measurement.

## `--bare` (C2) and the harness lanes

C2 is not decisive. Its outcome is `niet gemeten: cli_exit_1: no stderr`, with sha256 `25b3de486797325870050ccb39ea56d514d98e732c267d4d06a4a08fbd80740c`.

ADR-059 section 12 records the question and the consequence: whether `--bare` disables skill discovery is unmeasured, cell C2 measures it, and if it does then `on` is inert on the harness lanes and the DONE is measured on the non-`--bare` claude lane; `--bare` itself is not touched.

**The question is not answered.** C2 gave `niet gemeten`, so whether `--bare` disables discovery remains unmeasured. C2 is not decisive for a DONE point. The DONE therefore rests on C1, the non-`--bare` claude lane.

## Red run

None. This is a docs-only row, the same named exception as R1: there is no old code to run against. The evidence is the docs checker green, the two docs tests green, and every decisive cell carrying an outcome and a 64-hex sha256.

## Re-measuring

The four cells are measured again with `VNX_PROBE_OPERATOR=1 scripts/probe_agent_skill_discovery.sh --run --cell all --out <file>`; `--self-check` (no CLI, exit 0) validates the table shape and the cell list. The table above is then updated with the new outcome, its command and the sha256 of the output judged.
