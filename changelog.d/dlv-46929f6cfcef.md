- Test suite (track `test-suite-kosten`, TSK-7): `tests/test_t0_rotate_spawn.py` runs the real
  spawner once per script behaviour instead of once per test. Two module-scoped rigs — a plain
  spawn and a spawn with an active `/goal` delivered into the transcript — carry ten test
  functions, each keeping its own assertions on `calls.ndjson`, the receipts and the files, and
  each group gets its own `tmp_path_factory` directory. Script runs during the module drop from
  97 to 90; no test is deleted and no assertion is weakened. A test that depends on a stub switch
  (`STUB_EAT_FIRST_GOAL_ENTER`) keeps a run with exactly that switch and now also proves, on a
  monkeypatched rig without the branch, that the enter-only assertion fails there.
