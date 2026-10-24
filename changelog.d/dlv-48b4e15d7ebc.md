- Build roles fail-closed (track `review-stopregel-en-lichte-stoelen`): one shared block, `scripts/lib/prompts/fail_closed_evidence.md`
  (1.1 KB), now reaches every build worker on every lane. Code that reads evidence decides only on positive evidence: missing,
  `None`, malformed or unknown never reads as a pass; match the positive value exactly; fix sibling paths in the same round;
  one blocking test case per missing/`None`/malformed/unknown value. `scripts/lib/fail_closed_evidence.py` appends it through
  `PromptAssembler.assemble` and the legacy `agents/<role>/CLAUDE.md` route of `skill_context`, so `quality-engineer` and
  `system-architect`, which never saw `base_worker.md`, get it too. Exactly once per prompt; reviewer roles get nothing (D1).
  The "Null guards" checklist item of `backend-developer` (prompt role file and both `SKILL.md` copies) is now limited to display
  and formatting code, and `agents/quality-engineer/CLAUDE.md` gained a fail-closed tests section.
