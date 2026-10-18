## What the guard does not do

The plugin keeps the project's `.claude/rotation/` for itself. That directory holds the handovers, the markers and the per-session state that decides what the force and hard bands deny.

In every band, also below the warn threshold, in plan mode, after a release and inside a subagent, a Write, Edit, MultiEdit or NotebookEdit there is denied, unless it writes the session's own handover. Such a denial never counts toward `hard_max_denials`. Where a write lands is judged by the real directories, so a `..`, a symlink or a name in another case does not get one in. When that check itself fails, for example on a directory it cannot read, the call goes ahead and the failure is logged in `.claude/rotation/errors.log`.

That is all it checks. A shell command (the Bash tool) runs with your rights and can change any file you can, `.claude/rotation/` included. The plugin does not parse shell commands. Nor does it look at what other tools write, such as the tools of an MCP server. Typing `/context-rotation:rotate release` is the only release the plugin offers.
