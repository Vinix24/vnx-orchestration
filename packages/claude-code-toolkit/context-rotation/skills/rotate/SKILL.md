---
name: rotate
description: Saves this session's state as a handover so a fresh session can continue after /clear, or picks up an earlier handover. Use when the context-rotation guard asks for a handover, when the user types /context-rotation:rotate, or when the user wants to continue from the work of an earlier session.
argument-hint: "[write | resume [number]]"
---

# Rotate: hand this session over to a fresh one

A long session gets worse before it compacts. A handover is a short file that tells a
fresh session everything it needs to continue. The user then types `/clear`, and the
plugin gives the handover to the new session.

The first word of the arguments picks the action: `write` (also with no arguments) or
`resume`.

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

## resume

The user wants to continue from an earlier handover, for example one that was not given
to a session automatically.

- **A numbered list is in your context** (the plugin adds it when the user types
  `/context-rotation:rotate resume`): show it to the user as it is, and tell them to type
  `/context-rotation:rotate resume <number>` to continue from one.
- **A handover is in your context** (after `/context-rotation:rotate resume <number>`, or
  after `/clear`): continue from step 1 under `## Next steps`. First check that the state
  it describes still holds (files, branches, running processes) and tell the user when
  it does not. A handover marked outdated needs that check most.
- **Neither is in your context** (you started this skill yourself): look in the project's
  `.claude/rotation/` with Glob and Read. `archive/*.md` holds every handover given to a
  session, and its file names start with a UTC time, so the newest sorts last.
  `injections.jsonl` has one line per handover given, with the session that got it.
  `ready-*.json`, `outdated-*.json` and `claimed-*.json` each name a handover that was
  not given to a session yet, or whose hand-over did not finish. Show the user what you
  find, newest first, and ask which one to continue from. Then Read that file and
  continue from its step 1. Do not change or delete anything in that directory.

## After /clear

The plugin's SessionStart hook finds the handover of the session that ran in the same
`claude` process, checks that it is unchanged and still current, copies it to
`.claude/rotation/archive/`, and puts it in the new session's context. The user sees one
line naming the file. When that does not happen (another process, an outdated handover,
two handovers at once), the user sees the waiting handovers listed with a hint to type
`/context-rotation:rotate resume`.
