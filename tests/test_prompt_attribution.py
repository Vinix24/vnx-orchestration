"""Prompt attribution (ADR-056, zelfst PR-8b): real functions and the real hook script, tmp stores."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    import prompt_attribution
except ImportError:  # the red run on the old code: the module does not exist yet
    prompt_attribution = None

import lifecycle_count

HOOK = _ROOT / "scripts" / "hooks" / "log_prompt_time.py"
PID = "p-test"
SECRET = "geheime prompttekst 42"


@pytest.fixture(autouse=True)
def _isolated_prompt_home(tmp_path, monkeypatch):
    """Even attribution-only tests must never inspect the operator's state."""
    isolated = tmp_path / "isolated-home"
    monkeypatch.setenv("HOME", str(isolated))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: isolated))


def _registered_prompt_hook():
    settings = json.loads((_ROOT / ".claude" / "settings.json").read_text())
    groups = settings["hooks"]["UserPromptSubmit"]
    assert len(groups) == 1
    assert groups[0]["matcher"] == ""
    hooks = groups[0]["hooks"]
    assert len(hooks) == 1
    assert hooks[0]["type"] == "command"
    assert "scripts/hooks/log_prompt_time.py" in hooks[0]["command"]
    return hooks[0]


def test_repo_registers_exactly_one_prompt_logger():
    _registered_prompt_hook()


def test_registered_prompt_hook_timeout_is_at_most_five_seconds():
    timeout = _registered_prompt_hook()["timeout"]
    assert type(timeout) is int and 0 < timeout <= 5


@pytest.mark.parametrize("with_script", [False, True], ids=["missing-script", "real-script"])
def test_registered_prompt_command_is_silent_and_nonblocking(home, with_script):
    home_dir, state = home
    repo = home_dir / "repo with spaces"
    cwd = repo / "nested"
    cwd.mkdir(parents=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(home_dir), "VNX_PROJECT_ID": PID,
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True, capture_output=True)
    if with_script:
        (repo / "scripts").symlink_to(_ROOT / "scripts", target_is_directory=True)
    assert (repo / "scripts" / "hooks" / "log_prompt_time.py").is_file() is with_script
    result = subprocess.run(
        shlex.split(_registered_prompt_hook()["command"]), cwd=cwd, env=env,
        input=json.dumps({"session_id": "registered", "cwd": str(cwd), "prompt": SECRET}),
        capture_output=True, text=True, timeout=5, check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    ledger = state / "prompt_attribution.ndjson"
    if with_script:
        lines = ledger.read_text().splitlines()
        assert len(lines) == 1 and SECRET not in lines[0]
        assert json.loads(lines[0])["session_id"] == "registered"
    else:
        assert not ledger.exists()


def _attribute(prompt="hallo", env=None, cwd="/nonexistent", now=None):
    assert getattr(prompt_attribution, "attribute", None) is not None
    return prompt_attribution.attribute(prompt, {"VNX_PROJECT_ID": PID} if env is None else env, cwd, now)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    state = tmp_path / ".vnx-data" / PID / "state"
    state.mkdir(parents=True)
    return tmp_path, state


def test_session_with_two_deliveries_is_unknown():
    a = _attribute(env={"VNX_PROJECT_ID": PID})
    assert a.status == "ambiguous_session"
    assert a.delivery_id == "unknown"
    assert a.reason == "ambiguous_session"


def test_worker_marker_attributes_to_base_delivery():
    a = _attribute(env={"VNX_PROJECT_ID": PID, "VNX_DISPATCH_ID": "dlv-abc-r2"})
    assert (a.status, a.actor_class, a.delivery_id, a.project_id) == ("attributed", "worker", "dlv-abc", PID)
    b = _attribute(env={"VNX_PROJECT_ID": PID, "VNX_CURRENT_DISPATCH_ID": "dlv-abc"})
    assert (b.actor_class, b.delivery_id) == ("worker", "dlv-abc")


def test_no_marker_is_operator_and_ambiguous():
    a = _attribute()
    assert (a.actor_class, a.status) == ("operator", "ambiguous_session")


def test_t0_environment_does_not_make_a_typed_prompt_automation():
    a = _attribute(env={"VNX_PROJECT_ID": PID, "VNX_ACTOR_CLASS": "t0"})
    assert a.actor_class == "operator"


def test_project_id_is_never_vnx_dev_by_default(tmp_path):
    a = _attribute(env={}, cwd=str(tmp_path))
    assert a.project_id == "unknown"
    assert a.project_id != "vnx-dev"


def test_project_marker_file_resolves_project(tmp_path):
    (tmp_path / ".vnx-project-id").write_text("from-marker\n")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert _attribute(env={}, cwd=str(sub)).project_id == "from-marker"


def _rotation_run(sdir: Path, expected=None, goal=None, age=0):
    run = sdir / "rotation-20261108-100000-1"
    run.mkdir(parents=True)
    if expected is not None:
        (run / "delivery.json").write_text(json.dumps({"expected": expected}))
    (run / "prompt.txt").write_text("p")
    (run / "continuation.json").write_text("{}")
    if goal is not None:
        (run / "goal.txt").write_text(goal)
    old = time.time() - age
    os.utime(run, (old, old))
    return run


@pytest.mark.parametrize("kind", ["kickoff", "goal"])
def test_rotation_text_is_automation_and_older_run_is_not(home, monkeypatch, kind):
    _, state = home
    sdir = state / "t0_rotation"
    run = _rotation_run(sdir, goal="/goal doe dit tot het af is")
    text = f"/kickoff rotatie {run.name}" if kind == "kickoff" else "/goal doe dit tot het af is"
    assert _attribute(text).actor_class == "automation"
    assert _attribute("iets anders").actor_class == "operator"
    for p in sdir.glob("rotation-*"):
        old = time.time() - 31 * 60
        os.utime(p, (old, old))
    assert _attribute(text).actor_class == "operator"


def test_record_holds_no_prompt_text():
    a = _attribute(SECRET)
    rec = prompt_attribution.build_record(a, session_id="s1", prompt=SECRET, ts_utc="2026-11-08T10:00:00Z")
    assert SECRET not in json.dumps(rec)
    assert rec["prompt_len"] == len(SECRET) and len(rec["prompt_sha256"]) == 64
    assert rec["reason"] == "ambiguous_session"


def test_append_record_does_not_create_missing_state_dir(tmp_path):
    a = _attribute()
    rec = prompt_attribution.build_record(a, session_id="s", prompt="x", ts_utc="t")
    assert prompt_attribution.append_record(tmp_path / "missing", rec) is False
    assert not (tmp_path / "missing").exists()


def _record():
    return prompt_attribution.build_record(_attribute(), session_id="s", prompt="x", ts_utc="t")


def test_uncontended_append_writes_exactly_one_line(home):
    _, state = home
    assert prompt_attribution.append_record(state, _record()) is True
    assert len((state / prompt_attribution.STORE_FILENAME).read_text().splitlines()) == 1


def test_append_never_waits_on_a_held_ledger_lock(home, capsys):
    import fcntl
    import threading

    _, state = home
    store = state / prompt_attribution.STORE_FILENAME
    store.write_text("")
    holder = store.open("a")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    result = []
    worker = threading.Thread(target=lambda: result.append(prompt_attribution.append_record(state, _record())), daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(timeout=2)
    elapsed = time.monotonic() - started
    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    holder.close()
    worker.join(timeout=2)
    assert elapsed < 0.2, f"append blocked for {elapsed:.2f}s on a held lock"
    assert result == [False]
    assert store.read_text() == ""
    assert "ledger busy, line dropped" in capsys.readouterr().err


def _run_hook(stdin: str, home_dir: Path, extra_env=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
    env.update({"HOME": str(home_dir), "VNX_PROJECT_ID": PID, **(extra_env or {})})
    start = time.monotonic()
    done = subprocess.run([sys.executable, str(HOOK)], input=stdin, capture_output=True, text=True,
                          env=env, cwd=str(home_dir), check=False)
    return done, time.monotonic() - start


def test_hook_writes_one_line_prints_nothing_exits_zero(home):
    home_dir, state = home
    payload = json.dumps({"session_id": "s1", "cwd": str(home_dir), "prompt": SECRET})
    done, elapsed = _run_hook(payload, home_dir)
    assert done.returncode == 0 and done.stdout == ""
    lines = (state / "prompt_attribution.ndjson").read_text().splitlines()
    assert len(lines) == 1 and SECRET not in lines[0]
    row = json.loads(lines[0])
    assert row["actor_class"] == "operator" and row["delivery_id"] == "unknown"
    assert row["project_id"] == PID and row["session_id"] == "s1"
    assert elapsed < 2.0  # generous CI bound; the cold-start time is reported in the dispatch report


def test_hook_with_worker_marker_writes_delivery_key(home):
    home_dir, state = home
    done, _ = _run_hook(json.dumps({"session_id": "s", "prompt": "p"}), home_dir, {"VNX_DISPATCH_ID": "dlv-abc-r2"})
    row = json.loads((state / "prompt_attribution.ndjson").read_text().splitlines()[0])
    assert done.returncode == 0
    assert (row["actor_class"], row["delivery_id"], "reason" in row) == ("worker", "dlv-abc", False)


@pytest.mark.parametrize("stdin", ["{not json", "", "[1,2]", "null", '{"prompt": 5}'])
def test_hook_exits_zero_and_silent_on_bad_input(home, stdin):
    home_dir, _ = home
    done, _ = _run_hook(stdin, home_dir)
    assert done.returncode == 0 and done.stdout == ""


def test_hook_exits_zero_when_store_is_missing(tmp_path):
    done, _ = _run_hook(json.dumps({"prompt": "x"}), tmp_path)
    assert done.returncode == 0 and done.stdout == ""
    assert not (tmp_path / ".vnx-data").exists()


def test_coverage_reader_reads_the_line_as_prompt_source(home):
    home_dir, state = home
    _run_hook(json.dumps({"session_id": "s", "prompt": "p"}), home_dir, {"VNX_DISPATCH_ID": "dlv-abc"})
    cov = lifecycle_count.measure_coverage(state, project_id=PID)
    assert cov.sources["prompt"] == {"state": "covered", "rows": 1, "with_identity": 1}


def test_kickoff_is_automation_without_delivery_json(home):
    _, state = home
    run = _rotation_run(state / "t0_rotation", goal="/goal x")
    assert not (run / "delivery.json").exists()
    for typed in (f"/kickoff rotatie {run.name}", f"kickoff rotatie {run.name}\n", f"  /kickoff rotatie {run.name}  "):
        assert _attribute(typed).actor_class == "automation"
    assert _attribute("/kickoff rotatie rotation-other").actor_class == "operator"
    assert _attribute(f"/kickoff rotatie {run.name} extra").actor_class == "operator"
    old = time.time() - 31 * 60
    os.utime(run, (old, old))
    assert _attribute(f"/kickoff rotatie {run.name}").actor_class == "operator"
    assert _attribute("/goal x").actor_class == "operator"


@pytest.mark.parametrize("bad", ["../../evil", "ABSOLUTE", "a/b"])
def test_unsafe_project_id_is_unknown_and_writes_nothing_outside(tmp_path, bad):
    home_dir = tmp_path / "home"
    (home_dir / ".vnx-data").mkdir(parents=True)
    (tmp_path / "evil" / "state").mkdir(parents=True)  # a dir the traversal would reach
    if bad == "ABSOLUTE":
        bad = str(tmp_path / "abs-evil")
        (tmp_path / "abs-evil" / "state").mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    (work / ".vnx-project-id").write_text(bad + "\n")
    done, _ = _run_hook(json.dumps({"session_id": "s", "cwd": str(work), "prompt": "x"}), home_dir,
                        {"VNX_PROJECT_ID": ""})
    assert done.returncode == 0 and done.stdout == ""
    assert not list(tmp_path.rglob("prompt_attribution.ndjson"))
    assert _attribute(env={}, cwd=str(work)).project_id == "unknown"


def test_symlinked_state_dir_pointing_outside_writes_nothing(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    home_dir = tmp_path / "home"
    root = home_dir / ".vnx-data" / PID
    root.mkdir(parents=True)
    (root / "state").symlink_to(outside)
    done, _ = _run_hook(json.dumps({"session_id": "s", "prompt": "x"}), home_dir)
    assert done.returncode == 0 and done.stdout == ""
    assert "outside" in done.stderr
    assert not list(outside.iterdir())


def test_symlinked_rotation_dir_outside_root_is_not_read(home, tmp_path):
    _, state = home
    elsewhere = tmp_path / "elsewhere"
    run = _rotation_run(elsewhere, goal="/goal x")
    (state / "t0_rotation").symlink_to(elsewhere)
    assert _attribute(f"/kickoff rotatie {run.name}").actor_class == "operator"


def test_symlinked_run_dir_outside_root_is_ignored(home, tmp_path):
    _, state = home
    sdir = state / "t0_rotation"
    sdir.mkdir()
    other = tmp_path / "other-project"
    other.mkdir()
    (other / "goal.txt").write_text("/goal vreemde opdracht")
    (sdir / "rotation-x").symlink_to(other)
    assert _attribute("/goal vreemde opdracht").actor_class == "operator"
    assert _attribute("/kickoff rotatie rotation-x").actor_class == "operator"


def test_goal_txt_symlink_outside_run_dir_is_ignored(home, tmp_path):
    _, state = home
    run = _rotation_run(state / "t0_rotation")
    secret = tmp_path / "elders.txt"
    secret.write_text("/goal uit een ander bestand")
    (run / "goal.txt").symlink_to(secret)
    assert _attribute("/goal uit een ander bestand").actor_class == "operator"
    assert _attribute(f"/kickoff rotatie {run.name}").actor_class == "automation"


def test_override_dir_does_not_widen_the_root(home, tmp_path, monkeypatch):
    _, state = home
    from t0_rotation_state import STATE_DIR_ENV
    override = tmp_path / "override"
    override.mkdir()
    other = tmp_path / "andere"
    other.mkdir()
    (other / "goal.txt").write_text("/goal via override")
    (override / "rotation-y").symlink_to(other)
    env = {"VNX_PROJECT_ID": PID, STATE_DIR_ENV: str(override)}
    assert _attribute("/goal via override", env=env).actor_class == "operator"
