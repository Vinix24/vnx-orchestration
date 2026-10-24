- Golf 1, C2 (track `golf-1-opruimen`): removed the three `tmux_signal_*` hooks of the retired tmux-spawn lane
  (`tmux_signal_prompt_received.sh`, `tmux_signal_session_ready.sh`, `tmux_signal_stop_receipt.sh`), the
  tool-call signal tails of `pretooluse_block_raw_claude_spawn.sh` and `pretooluse_block_subagent.sh`, and the
  `VNX_DISPATCH_ID` guards of `session_reconcile_autoclose.sh` and `session_reconcile_cleanup.sh`. Nothing exports
  `VNX_TMUX_SIGNAL_DIR` or sets `VNX_DISPATCH_ID` in a headless dispatch, so none of them ran. The hooks stay
  registered in a project's `.claude/settings.json` until its SessionStart, UserPromptSubmit and Stop entries for
  `tmux_signal_*` are removed by hand; a project that pins this release must remove them, or every prompt reports
  a hook error. `VNX_TMUX_SIGNAL_DIR` itself stays: `enrichment.py`, `envelope_govern.py` and `provider_dispatch.py`
  still read it. The four removed paths are guarded by `scripts/ci/retired_paths.d/dlv-209d2bcb21d2.txt`.
