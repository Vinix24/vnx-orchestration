## What the guard does not do

The guard stops one thing: a direct write by Claude's file tools (Write, Edit, MultiEdit and NotebookEdit) to the plugin's own files. Those are the project's `.claude/rotation/` and the two settings files. `.claude/rotation/` holds the handovers, the markers and the per-session state that decides what the force and hard bands deny. The settings are in `.claude/context-rotation.json` in the project and in `~/.claude/context-rotation.json`, and the plugin reads the first of the two that exists.

Such a write is denied in every band, also below the warn threshold, in plan mode, after a release and inside a subagent. The one exception is a write of the session's own handover. A denial of this kind never counts toward `hard_max_denials`. Where a write lands is judged by the real directories, so a `..`, a symlink or a name in another case does not get one in. Each way of reading the path is judged on its own, and one that lands on a plugin file is enough for a denial. Only when none of them does and one could not be judged, for example on a directory the hook cannot read, does the call go ahead. The failure is then logged in `.claude/rotation/errors.log`.

It is no lock against a model that sets out to switch the plugin off. A shell command (the Bash tool) or a script runs with your rights and can change any of these files. The plugin does not parse shell commands. A change to Claude Code's own settings can remove the plugin's hooks. The tools of an MCP server are not looked at. None of that is stopped.

Typing `/context-rotation:rotate release` is the only release the plugin itself offers. You can always also edit the settings file yourself.
