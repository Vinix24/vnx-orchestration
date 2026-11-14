"""Characterisation tests for golf-schoon R26b (follow-up of dlv-702b81a9f505).

Pins the observable behaviour of
``scripts/analysis/worker_scope_enforcement_measure.py::main`` before and after
an extract-method refactor: the full stdout (JSON summary + example blocks),
the exit code and the order of git calls, over real spec files in a temp
pending dir; only the external ``git`` process is patched.

The module is loaded by path under unique names (no basename collision risk).
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
for _p in (_REPO / "scripts", _REPO / "scripts" / "lib"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


measure = _load("_r26_measure", _REPO / "scripts" / "analysis" / "worker_scope_enforcement_measure.py")

# ---------------------------------------------------------------------------
# worker_scope_enforcement_measure.main
# ---------------------------------------------------------------------------

_SPECS = {
    "d-ok": {"role": "technical-writer", "dispatch_paths": []},
    "d-role-out": {"role": "backend-developer", "dispatch_paths": []},
    "d-narrow": {"role": "backend-developer",
                 "dispatch_paths": [{"path": "scripts/a.py", "access": "read_write"},
                                    {"path": "docs/x.md", "access": "read"}]},
    "d-dir": {"role": "backend-developer",
              "dispatch_paths": [{"path": "tests", "access": "write"}]},
    "d-both": {"role": "backend-developer",
               "dispatch_paths": [{"path": "scripts/b.py", "access": "create"}]},
    "d-empty": {"role": "backend-developer", "dispatch_paths": []},
    "d-unlinked": {"role": "backend-developer", "dispatch_paths": []},
}
_COMMITS = {
    "d-ok": {"sha-ok": ["scripts/ok.py"]},
    "d-role-out": {"sha-ro": ["README.md", "scripts/x.py"]},
    "d-narrow": {"sha-n1": ["scripts/a.py"], "sha-n2": ["scripts/c.py"]},
    "d-dir": {"sha-d": ["tests/test_x.py"]},
    "d-both": {"sha-b": ["scripts/b.py", "docs/y.md", "README.md", "scripts/z.py", "x.txt"]},
    "d-empty": {"sha-e": []},
}


@pytest.fixture()
def measure_env(tmp_path, monkeypatch):
    pending = tmp_path / "pending"
    pending.mkdir()
    for did, body in _SPECS.items():
        (pending / did).mkdir()
        (pending / did / "dispatch-spec.json").write_text(
            json.dumps({"dispatch_id": did, **body}), encoding="utf-8")
    (pending / "seat-dir").mkdir()  # plan-gate seat dir, no spec
    (pending / "seat-dir" / "final_prompt.md").write_text("x", encoding="utf-8")
    (pending / "broken").mkdir()
    (pending / "broken" / "dispatch-spec.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(measure, "PENDING_DIR", pending)
    real_load = measure.load_specs

    def ordered_load():  # iterdir order is filesystem-dependent; pin _SPECS order
        got = real_load()
        assert set(got) == set(_SPECS)
        return {did: got[did] for did in _SPECS}

    monkeypatch.setattr(measure, "load_specs", ordered_load)

    calls: list[tuple] = []
    sha_files = {sha: f for shas in _COMMITS.values() for sha, f in shas.items()}

    def fake_git(*args):
        calls.append(args)
        if args[0] == "log":
            out = ""
            for did, shas in _COMMITS.items():
                for sha in shas:
                    out += f"{sha}\x00subject\n\nDispatch-ID: {did}\n\x00"
            return out + "sha-other\x00x\n\nDispatch-ID: not-a-spec\n\x00"
        assert args[0] == "diff-tree"
        return "".join(f + "\n" for f in sha_files[args[-1]])

    monkeypatch.setattr(measure, "_git", fake_git)
    return calls


def test_measure_main_summary_and_exit(measure_env, capsys):
    assert measure.main() == 0
    out = capsys.readouterr().out
    summary = json.loads(out.split("\n\n== concrete")[0])
    assert summary == {
        "dispatch_specs_total": 7,
        "linked_to_commit": 6,
        "unlinked": 1,
        "linked_with_changed_files": 5,
        "no_dispatch_paths": 2,
        "with_dispatch_paths": 3,
        "would_be_blocked_by_flip": 3,
        "not_blocked": 2,
        "blocked_no_dispatch_paths__role_scope_only": 1,
        "blocked_with_paths__dispatch_narrowing": 2,
        "blocked_with_paths__also_outside_role_scope": 1,
        "dispatches_declaring_a_directory_path": 1,
        "role_scope_only__outside": 2,
        "role_scope_only__inside": 3,
        "dispatch_scope_only__declared_total": 3,
        "dispatch_scope_only__outside_literal": 3,
        "dispatch_scope_only__outside_dir_aware": 2,
        "dispatch_scope_only__dir_fix_rescued": 1,
    }
    keys = re.findall(r'^  "([a-z_]+)":', out, re.M)
    assert keys == list(summary)


_MEASURE_TAIL = """
== concrete examples: dispatches the flip would block ==
  d-role-out [backend-developer] declared=[]
      README.md  <role-scope-fail>
  d-narrow [backend-developer] declared=['scripts/a.py']
      scripts/c.py  <dispatch-narrow-fail>
  d-both [backend-developer] declared=['scripts/b.py']
      README.md  <role-scope-fail>
      docs/y.md  <role-scope-fail>
      scripts/z.py  <dispatch-narrow-fail>
      x.txt  <role-scope-fail>

== concrete examples: dispatches outside their ROLE scope (dispatch_paths ignored) ==
  d-role-out [backend-developer] -> README.md
  d-both [backend-developer] -> README.md, docs/y.md, x.txt

== dispatches the directory-matching fix rescues (outside-declaration under literal, inside under dir-aware) ==
  d-dir
"""


def test_measure_main_stdout_tail_and_git_order(measure_env, capsys):
    measure.main()
    out = capsys.readouterr().out
    assert out.endswith("}\n" + _MEASURE_TAIL)
    # git order: one log, then diff-tree per sha in three passes (flip, role, dispatch).
    assert measure_env[0][0] == "log"
    diff_shas = [c[-1] for c in measure_env[1:]]
    flip = [s for d in _COMMITS for s in _COMMITS[d]]
    role = flip
    dispatch = ["sha-n1", "sha-n2", "sha-d", "sha-b"]
    assert diff_shas == flip + role + dispatch


def test_measure_main_empty_pending(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(measure, "PENDING_DIR", tmp_path / "missing")
    monkeypatch.setattr(measure, "_git", lambda *a: "")
    assert measure.main() == 0
    out = capsys.readouterr().out
    assert json.loads(out.split("\n\n== concrete")[0])["dispatch_specs_total"] == 0
    assert out.count("== ") == 3
