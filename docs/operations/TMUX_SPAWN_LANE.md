# Tmux-Spawn Dispatch Lane (removed)

The tmux-spawn dispatch lane was removed on 2026-09-18. Nothing dispatches through it any more.
This note stays so that old links, ADRs and investigation notes land on the reason instead of a 404.
The last commit that still contained the lane is `6ad8f587`; `git show 6ad8f587:scripts/lib/tmux_interactive_dispatch.py` reads it.

## What was removed

- `scripts/lib/tmux_interactive_dispatch.py`, the leaseless ephemeral lane (lane id `claude_tmux_subscription`)
- the `force_tmux` / `force_tmux_reason` fields of `DispatchSpec`, validate Rule 12b and 12c, and the `--force-tmux` / `--force-tmux-reason` flags
- the `VNX_ALLOW_TMUX_LANE` emergency brake and its `claude-tmux-lane` row in `docs/core/SUBSYSTEMS.md`
- the `tmux` adapter of the deprecated raw-file form (`vnx dispatch <file.md>`), which was its default. The form itself still works with no flag and now defaults to the `subprocess` adapter. Naming `tmux` explicitly (`--adapter tmux`, an `Adapter: tmux` header or `VNX_ADAPTER=tmux`) is refused with a message that says why

## Why

The lane existed to keep claude workers on the subscription while headless `claude -p` was believed to be API-metered after the June 2026 cutover. That never happened. Anthropic does not bill headless outside the subscription (measured 2026-08-11 from the auth state, confirmed by the operator on 2026-09-18), so the lane had no billing reason left.

It had also been unreachable through the door since 2026-09-12, when `validate()` started refusing `force_tmux` unless `VNX_ALLOW_TMUX_LANE=1`, and nothing set that variable.

## What claude dispatches use now

`claude_headless` (`dispatch_envelope.run_envelope_headless_plan`) is the only claude lane. `dispatch_plan.resolve_claude_lane()` returns it for every `provider=claude` spec. Lane selection, billing and the model rows are in `docs/core/DISPATCH_RULES.md` §5 and §8.

A staged spec that was written before the removal and still carries `force_tmux: true` does not crash. `dispatch_cli.load_spec` ignores the field, prints a warning to stderr with the reason the spec carried, and the dispatch runs on `claude_headless`.

## The rollback hatch

The raw-file form is the documented rollback hatch (`VNX_DISPATCH_LEGACY=1`): it is how work gets out when the door itself is broken. It therefore keeps working with no extra flag. `vnx dispatch <file.md>` runs on the subprocess lane where it used to default to tmux, and it prints the same ADR-025 deprecation warning as before. Only the adapter changed. The form still leaves in 1.x per ADR-025; this removal does not change that.

## What stayed

The removal is limited to the lane. Tmux session management is untouched:

- `scripts/lib/tmux_adapter.py` (`TmuxAdapter`), which routes terminal-pinned dispatches, and `scripts/lib/tmux_worktree.py`
- the signalling and session hooks under `scripts/hooks/` (`tmux_signal_*`, `session_*`)
- the worker-permission relay (`scripts/lib/worker_permission_relay.py`, `scripts/permission_relay_cli.py`). Its tmux transport moved to `scripts/lib/tmux_command_runner.py`
- the account-wide serialization lock. Its class keeps the historical name `claude-tmux` and is now held by the headless lane (`VNX_TMUX_MAX_CONCURRENT`)
