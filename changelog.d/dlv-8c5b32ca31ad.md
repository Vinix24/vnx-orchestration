- Gate engine (GE-2b): `scripts/codex_final_gate.py` no longer carries the Codex prompt
  renderer. `render_codex_prompt` and its four `_format_*` helpers are gone; only tests
  called them, and no script, hook or workflow did. The module keeps `enforce_codex_gate`
  and the helpers it calls, which is all `scripts/closure_verifier.py` imports. 435 lines
  became 248; the removed symbol is listed in
  `scripts/ci/retired_paths.d/dlv-8c5b32ca31ad.txt`.
