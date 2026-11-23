# VNX Cost Tracking Guide

**Verified against `origin/main`** at a77a74564 on 2026-11-12.
**Status**: shipped. The cost ledger is the system of record. The receipt-based `cost-report` is an older, narrower reader that still runs.

## Two surfaces

| Surface | Command | Reads | Use it for |
|---|---|---|---|
| Cost ledger ([ADR-043](../governance/decisions/ADR-043-het-kostengrootboek.md)) | `vnx costs report` | `<central store>/events/provider_costs.ndjson` | Spend per model, role, day or provider across dispatches, gates, panels and intelligence calls |
| Receipt report | `./bin/vnx cost-report` (`scripts/cost_tracker.py`) | `t0_receipts.ndjson` plus session transcripts | Tokens and a price estimate per receipt, for the models in its price table |

Cost is never invented. A price that is not known is `null` or `unavailable:<reason>`, listed apart and never summed as 0.

## The cost ledger

One line per run, in the shape `provider_cost_run/1`. A run is one start of one model for one purpose: a dispatch attempt, a gate run, a plan-gate seat, a panel leg, an intelligence call. A refire is a new attempt and so a new run.

Properties that matter to a reader:

- **Append-only, one lock.** Every writer and reader takes `events/provider_costs.lock` first (`provider_costs.ledger_lock`). A write that cannot reach the ledger goes to a spool (`events/provider_costs.spool/`) and `vnx costs replay` brings it back.
- **Unknown is not zero.** Token fields are an integer or `null`. `cost_usd` carries a `cost_source`: `provider_reported`, `price_table@<version>` or `unavailable:<reason>`.
- **Prices come from the model registry** (`scripts/lib/providers/wave7_models.yaml`, [ADR-040](../governance/decisions/ADR-040-price-versions-in-the-model-registry.md)), not from a table in the reader. A model without a price books `unavailable:no_price:<model>`.
- **Subscription is not metered spend.** A run on a subscription books `cost_usd: 0` with its list price in `api_equivalent_usd`. A run on an own API key books `billing: metered` and real dollars.
- **Corrections replace, never edit.** A `correction` line names the `record_id` it supersedes. Per `run_id` only the last line counts.

```bash
vnx costs report [--by model|role|day|provider] [--since YYYY-MM-DD] [--json] [--ledger PATH]
vnx costs replay [--json]            # bring the spool back into the ledger
vnx costs observe [--json] [--coverage] [--ledger PATH]   # measure each provider's balance (a calibration, never a spend)
vnx costs backfill [--since YYYY-MM-DD] [--harness-root PATH ...] [--dry-run | --apply]
```

`backfill` defaults to `--dry-run`. `report` exits 0 with a report (an incomplete one says so), 1 for an unreadable ledger and 2 for a usage error. The command surface is `vnx_cli/commands/costs.py`; the machinery is `scripts/cost_ledger_{report,replay,observe,backfill}.py`.

## The receipt report

```bash
./bin/vnx cost-report --human                      # human-readable
python3 scripts/cost_tracker.py                    # JSON on stdout, also written to cost_metrics.json
python3 scripts/cost_tracker.py \
    --receipts $VNX_STATE_DIR/t0_receipts.ndjson \
    --output /tmp/my_metrics.json
```

Without `--human` the output is JSON. The default output file is `$VNX_STATE_DIR/cost_metrics.json`.

### What it counts

Only completion lines book tokens, and one witness per dispatch attempt (`_cost_lines`, OI-2468): the legacy `task_complete` and the v3 `dispatch_finished` of the same attempt are booked once. A `dispatch_finished` carries no usage, so its tokens come from the governance record of the same attempt. No such record means unknown tokens, never zero.

For each line the reader tries the session transcript first and falls back to token fields already in the receipt.

| Status | Meaning |
|---|---|
| `resolved` | usage extracted from the transcript |
| `no_session_id` | the receipt carries no session id |
| `transcript_not_found` | `find_transcript` found no `<session_id>.jsonl` under `~/.claude/sessions/`, `~/.gemini/sessions/` or `~/.codex/sessions/` |
| `no_usage_data` | the transcript has no usage entries |
| `error` | parsing the transcript raised |
| `fallback_to_receipt` | token fields already in the receipt were used |

`extract_usage_from_transcript` reads three shapes: `usage.input_tokens` / `usage.output_tokens` (Claude Code), `usage.prompt_tokens` / `usage.completion_tokens` (Codex) and `usageMetadata.promptTokenCount` / `usageMetadata.candidatesTokenCount` (Gemini).

### Price table and its limit

`MODEL_PRICING` in `scripts/cost_tracker.py` (USD per 1M tokens, dated 2026-03-23):

| Model | Input | Output |
|-------|-------|--------|
| claude-opus-4.6, claude-opus-4.5 | 15.00 | 75.00 |
| claude-sonnet-4.6, claude-sonnet-4.5 | 3.00 | 15.00 |
| gpt-5.3-codex | 2.00 | 16.00 |
| gpt-5.2-codex | 1.75 | 14.00 |
| gpt-5.1-codex | 1.25 | 10.00 |
| gpt-5.1-codex-mini | 0.25 | 2.00 |
| gemini-pro | 0.50 | 1.50 |
| gemini-flash | 0.10 | 0.30 |

A model that is not in this table gets an unknown cost (`_estimate_cost` returns `None`), and its receipts count under "unknown cost". The table has no Claude 5 entries, no Kimi, no DeepSeek and no GLM. For those, read the ledger.

## Session resolution

Deterministic only. A heuristic such as "the latest transcript" mis-attributes work when two terminals finish within a second of each other. `_resolve_session_id` (`scripts/lib/append_receipt_internals/session_resolver.py`) takes the first of:

1. A session id in the receipt: `metadata.session_id`, `metadata.session` or the top-level `session_id`. `scripts/report_parser.py` fills it from a `**Session**: <id>` or `Session: <id>` line in the report.
2. The per-terminal file `$VNX_STATE_DIR/current_session_<terminal>`.
3. The provider environment variable (`CLAUDE_SESSION_ID`, `GEMINI_SESSION_ID`, `CODEX_SESSION_ID`, `KIMI_SESSION_ID`). A hit is written to the per-terminal file.
4. The provider's `~/.<provider>/sessions/current` file.
5. `unknown`.

A receipt carries `session_id`, `model` and `provider` at the top level ([ADR-035](../governance/decisions/ADR-035-receipt-v2.md) section 4). The cost report still reads the nested `session{}` object of a pre-cutover receipt as a fallback.

## Troubleshooting

- **`receipts file not found`**: check that `$VNX_STATE_DIR/t0_receipts.ndjson` exists or pass `--receipts`.
- **Invalid NDJSON lines**: skipped and counted in `invalid_receipt_lines`.
- **High `no_session_id`**: the report carried no session id and steps 2 to 4 above found none. Add `**Session**: <id>` to the report.
- **High `transcript_not_found`**: the transcript is not at the path `find_transcript` searches. Check it with `ls ~/.claude/sessions/<session_id>.jsonl`.
- **`jq` check on a receipt file**: `jq -c 'select(.session_id) | {dispatch_id, session_id}' $VNX_STATE_DIR/t0_receipts.ndjson | head -5`.

## Related

- [ADR-043](../governance/decisions/ADR-043-het-kostengrootboek.md): the ledger. [ADR-040](../governance/decisions/ADR-040-price-versions-in-the-model-registry.md): prices in the registry.
- [RECEIPT_PIPELINE.md](../operations/RECEIPT_PIPELINE.md) and [11_RECEIPT_FORMAT.md](../core/11_RECEIPT_FORMAT.md): receipts.
- `scripts/report_parser.py`: session extraction from reports.
