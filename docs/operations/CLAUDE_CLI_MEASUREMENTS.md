# Claude CLI measurements

Behaviour of the `claude` CLI that the dispatch lanes depend on, measured per CLI version.

Cell B (track `agents-als-map-fase-1`, point 5): does `claude -p` WITHOUT `--bare` still authenticate through OAuth on the own account, or did `--bare` become the default? The subscription lane (`claude_headless`) starts `claude -p` without `--bare` and breaks if that changes.

Only the T0/operator measures: a builder cannot start `claude -p` (`scripts/hooks/pretooluse_block_raw_claude_spawn.sh`). Run `scripts/probe_claude_cli.sh --append` after every CLI update. It appends one row below.

A row counts as a measurement only when the outcome is `ja` or `nee` AND the last column holds the 64-hex sha256 of the probe output. `vnx doctor` (check `claude-cli`) WARNs when the newest row is not for the installed `claude --version`, and when that row is not a measurement. It names `scripts/probe_claude_cli.sh` as the script that re-measures.

| Date | claude --version | Cell B outcome | Probe output sha256 |
|---|---|---|---|
