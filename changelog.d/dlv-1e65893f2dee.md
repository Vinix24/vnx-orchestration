- T0 context trim (track `t0-context-trim`, F2): the fabric-reference skill now names the
  absolute-path convention in Gotchas — every Bash call starts in the session's start folder, so a
  T0 writes from `${VNX_HOME:-$(git rev-parse --show-toplevel)}` or an absolute path, and the start
  folder is not moved to the project root because role-file discovery walks up from cwd — and adds
  two runbooks: staging and firing a dispatch from a script (`stage_spec_bundle` with a `Path`
  `data_dir`, no `--gate` on the stage-only CLI, `pr_id`/`work_ref`/`base_ref` for a follow-up
  round, the door fired in its own tmux window) and context rotation (the 400K/500K/600K bands and
  the hook that enforces them, `/build-log wrap` writing `daily-log/handoff.md` with
  `## Actief /goal`, `scripts/t0_rotate_spawn.sh`, and a handed-over `/goal` longer than about 650
  characters arriving as a pasted block that is not executed — OI-2314). The shipped
  `skills/fabric-reference/SKILL.md` and the checked-in `.claude/skills/fabric-reference/SKILL.md`
  are byte-identical again; the latter was one line behind on the `VNX_STATE_DIR` pin.
