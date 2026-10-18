---
name: rotate
description: Saves this session's state as a handover so a fresh session can continue after /clear, or picks up an earlier handover. Use when the context-rotation guard asks for a handover, when the user types /context-rotation:rotate, or when the user wants to continue from the work of an earlier session.
argument-hint: "[write | release | resume [number | id]]"
---

# Rotate: hand this session over to a fresh one

A long session gets worse before it compacts. A handover is a short file that tells a
fresh session everything it needs to continue. Then `/clear` starts that fresh session:
inside tmux the plugin types it into this session's own pane once the session is idle,
elsewhere the user types it. The plugin gives the handover to the new session.

The first word of the arguments picks the action: `write` (also with no arguments),
`release` or `resume`. For any other first word the plugin puts its usage line in your
context: show that line to the user and do nothing else.

## write

1. **Find the path.** It is in your context: the guard names it in every message about
   the context size, and when the user typed this command a note gives it. It looks like
   `<project>/.claude/rotation/handover-<session id>.md`. Never write another session's
   handover.
2. **Bring the work to a resting point** first: finish the edit you are in, or stop
   cleanly. The handover describes the state at the moment you write it.
3. **Write the file with the Write tool.** Not with a shell command: the check runs on
   Write and Edit. Use these sections, in this order:

   ```
   # Handover

   ## Goal
   What the user wants, in their terms, and how they will judge it done.

   ## Current state
   What is done and verified, the decisions taken and why, what was tried and failed.

   ## Running
   Background shells, subagents, servers or watchers that still run, with how to find
   them. Write "none" when nothing runs.

   ## Next steps
   1. The first thing the fresh session does: a command, a file, a question to the user.
   2. ...

   ## Files touched
   Each path with one line on what changed.

   ## Open risks
   What is unverified, what can break, what the user still has to decide.
   ```

   Rules for the text:
   - Write for a reader who has seen none of this conversation: full paths, exact names,
     the exact command and its last result. No "as discussed".
   - The first line under `## Next steps` must be a step numbered `1.`.
   - At most 8,000 characters, or the limit named in your context.
4. **Read the answer of the check.** Right after the write the plugin either says the
   handover is valid and marked ready, or names exactly what is missing. Fix it with
   another Write or Edit until it is valid.
5. **Make the valid write your last action.** Do not call any other tool afterwards, not
   even a todo update: any call other than Read, Glob or Grep, and any new prompt, marks
   the handover outdated, and an outdated handover is never given to a fresh session.
   End your turn with one short line saying the handover is saved.

## release

Past the force threshold the plugin holds back a stop once per crossing and asks for the
handover; past the hard threshold it also denies every tool except Read, Glob, Grep,
Skill, AskUserQuestion, TodoWrite and a Write or Edit of this session's handover. When
the user types `/context-rotation:rotate release`, the plugin records the release for
this session before you read this: from then on the bands deny nothing and hold nothing
back, and the notices about the context size continue. Typing that command is the only
release the plugin itself offers. The user can also change the plugin's settings file;
that is theirs to do, never yours.

- **A note about the release is in your context**: do nothing else for this command.
  Tell the user in one line what the note says: released, or not recorded.
- **No such note** (you started this skill yourself, with `release`): nothing was
  released. Only the user can release the guard, by typing the command. Tell the user
  that, and continue with the handover if the plugin asked for one.

## The plugin's own files

`.claude/rotation/` holds the plugin's own files, among them the state that decides what
the bands deny. Its settings are in `.claude/context-rotation.json` in the project and
in `~/.claude/context-rotation.json`. In every band, also inside a subagent, the plugin
denies a Write, Edit, MultiEdit or NotebookEdit of any of these, unless it writes this
session's handover. That stops a direct write by those tools and nothing more. A shell
command (Bash) or a script runs with the user's rights and can change these files, a
change to Claude Code's own settings can remove the plugin's hooks, and the tools of an
MCP server are not looked at. Never change any of these files or those settings
yourself, with any tool: switching the plugin off is the user's decision.

## resume

The user wants to continue from an earlier handover, for example one that was not given
to a session automatically.

- **A numbered list is in your context** (the plugin adds it when the user types
  `/context-rotation:rotate resume`): show it to the user as it is, and tell them to type
  `/context-rotation:rotate resume <number>` or `/context-rotation:rotate resume <id>` to
  continue from one. Each entry ends with its id in brackets, and the id names that one
  handover: a later handover of the same session gets another id. A number means the
  entry shown under it in the last list of this session, even when newer handovers
  arrived since; when that entry is gone, or no list was shown yet, the plugin says so
  and shows the current list instead.
- **A handover is in your context** (after `/context-rotation:rotate resume <number>` or
  `resume <id>`, or after `/clear`): continue from step 1 under `## Next steps`. First
  check that the state it describes still holds (files, branches, running processes)
  and tell the user when it does not. A handover marked outdated needs that check most,
  and so does one the plugin says no longer holds the text that was checked: its file
  was changed after the check, and the change was not checked.
- **Neither is in your context** (you started this skill yourself): look in the project's
  `.claude/rotation/` with Glob and Read. First check that `.claude/rotation` and
  `.claude/rotation/archive` are directories and not symlinks (for example with
  `ls -ld`). A symlinked one points outside this project: the plugin uses nothing from
  it, so do not either, and tell the user. `archive/*.md` holds every handover given to a
  session, and its file names start with a UTC time, so the newest sorts last.
  `injections.jsonl` has one line per step of a handover (`ready`, `outdated`,
  `claimed`, `injected`, `delivered`, `kept`, and the `clear_*` steps of an automatic
  /clear in tmux); an `injected` line names the session it was being given to, and a
  `delivered` line follows only once it was handed over.
  `ready-*.json`, `outdated-*.json` and `claimed-*.json` each name a handover that was
  not given to a session yet, or whose hand-over did not finish. Use only a handover
  inside `.claude/rotation/` named for its marker's session (`handover-<session id>.md`)
  or an archive copy in `archive/`: a marker naming any other path is not one the plugin
  wrote, and `errors.log` says why it was rejected. Show the user what you find, newest
  first, and ask which one to continue from. Then Read that file and continue from its
  step 1. Do not change or delete anything in that directory.

## After /clear

The plugin's SessionStart hook finds the handover of the session that ran in the same
`claude` process, checks that it is unchanged and still current, copies it to
`.claude/rotation/archive/`, and puts it in the new session's context. The user sees one
line naming the file. After an automatic /clear in tmux the plugin then types
`Continue from the handover in your context, step 1.` as the first prompt, once the
handover was handed over: continue from step 1 as the resume action says. When that does
not happen (another process, an outdated handover, two handovers at once), the user sees
the waiting handovers listed with a hint to type `/context-rotation:rotate resume`.
