"""Bounded marker read in the content classifier (OI-2021).

``classify_path`` walks up from a path taken from data and reads ``.vnx-project-id`` at each
level. On 2026-10-08 that read blocked the nightly analyzer in ``open``. Every test here runs
``classify_path`` in a child process with its own guard of 30 seconds: on code that blocks,
the guard kills the child and the test fails on that, never by hanging the suite. CI runs on
shared runners, so no test asserts a tight wall-clock bound; a deterministic count of reads
started proves "did not wait again".

A blocking read is made by wrapping the classifier's open-and-read in the child: for a listed
directory it waits on an event that only the test releases. Every root, HOME, the boundary
file and the registry live in tmp_path. The child reads new symbols through ``getattr``, so on
the old code a test fails on behaviour (blocked past the guard, wrong class, no WARNING), not
on a missing name.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib"

GUARD_SECONDS = 30
MARKER = ".vnx-project-id"
MARKER_UNREADABLE = "marker_unreadable"

_CHILD = r"""
import json, logging, sys, threading, time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import content_class as cc

spec = json.loads(sys.argv[2])
warnings = []


class Collect(logging.Handler):
    def emit(self, record):
        warnings.append(record.getMessage())


logging.getLogger().addHandler(Collect(level=logging.WARNING))
logging.getLogger().setLevel(logging.WARNING)

reads = {}
release = threading.Event()
blocked = set(spec.get("block", []))
original = getattr(cc, "_open_and_read", None)


def blocking_read(marker):
    directory = str(Path(marker).parent)
    reads[directory] = reads.get(directory, 0) + 1
    if directory in blocked:
        release.wait(25)
    return original(marker)


if blocked:
    cc._open_and_read = blocking_read
for name, value in spec.get("set", {}).items():
    setattr(cc, name, value)

boundary = cc.load_boundary(spec["boundary"])
results = []
for step in spec["steps"]:
    if step == "release":
        release.set()
        deadline = time.monotonic() + 20
        while getattr(cc, "_abandoned_readers", 0) and time.monotonic() < deadline:
            time.sleep(0.05)
        results.append({"still_blocked": getattr(cc, "_abandoned_readers", 0)})
        continue
    before = len(warnings)
    start = time.monotonic()
    origin = cc.classify_path(step, boundary, spec["registry"])
    results.append({
        "cls": origin.cls, "project_id": origin.project_id, "source": origin.source,
        "elapsed": time.monotonic() - start, "warnings": warnings[before:],
        "reads": dict(reads),
    })
print(json.dumps({"results": results,
                  "exported": getattr(cc, "SRC_MARKER_UNREADABLE", None)}))
"""


def _real(path) -> str:
    return os.path.realpath(str(path))


@pytest.fixture
def world(tmp_path):
    home = tmp_path / "home"
    client_root = home / "client-root"
    personal_root = home / "personal-root"
    for d in (client_root / "group" / "client", personal_root / "area" / "topic",
              home / "dev", home / "notes"):
        d.mkdir(parents=True)
    boundary_file = tmp_path / "content_boundary.json"
    boundary_file.write_text(json.dumps({
        "version": 1,
        "client_roots": [str(client_root)],
        "personal_roots": [str(personal_root)],
        "client_project_ids": ["client-proj"],
    }))
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"projects": []}))
    return {"home": home, "client": client_root, "personal": personal_root,
            "boundary_file": boundary_file, "registry": registry, "tmp": tmp_path}


def _register(world, entries):
    world["registry"].write_text(json.dumps({"projects": [
        {"project_id": pid, "path": str(path)} for pid, path in entries]}))


def _classify(world, steps, *, block=(), timeout=None, boundary=None, set_attrs=None):
    """Run ``classify_path`` on each step in one child process under the 30 s guard."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("VNX_MARKER_READ_TIMEOUT_SECONDS", "VNX_CONTENT_BOUNDARY_FILE",
                        "VNX_PROJECT_ID")}
    env["HOME"] = str(world["home"])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if timeout is not None:
        env["VNX_MARKER_READ_TIMEOUT_SECONDS"] = str(timeout)
    spec = {
        "boundary": str(boundary or world["boundary_file"]),
        "registry": str(world["registry"]),
        "block": [_real(d) for d in block],
        "set": set_attrs or {},
        "steps": [s if s == "release" else str(s) for s in steps],
    }
    try:
        proc = subprocess.run([sys.executable, "-c", _CHILD, str(LIB), json.dumps(spec)],
                              capture_output=True, text=True, timeout=GUARD_SECONDS, env=env,
                              cwd=str(world["tmp"]))
    except subprocess.TimeoutExpired:
        pytest.fail(f"classify_path did not return within the {GUARD_SECONDS} s guard: "
                    f"blocked in a marker read")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _triple(result):
    return result["cls"], result["project_id"], result["source"]


def _one_warning_naming(result, directory):
    assert len(result["warnings"]) == 1, result["warnings"]
    assert _real(directory) in result["warnings"][0]


def _returned_inside_the_guard(result):
    assert result["elapsed"] < GUARD_SECONDS / 2


# --- 7a --------------------------------------------------------------------------------------

def test_7a_fifo_marker_outside_the_roots_is_unknown_and_stops_the_walk(world):
    proj = world["home"] / "dev" / "proj"
    work = proj / "sub"
    work.mkdir(parents=True)
    (world["home"] / "dev" / MARKER).write_text("outer-proj\n")
    os.mkfifo(proj / MARKER)

    out = _classify(world, [work])
    [result] = out["results"]

    assert _triple(result) == ("unknown", None, MARKER_UNREADABLE)
    assert out["exported"] == MARKER_UNREADABLE
    _one_warning_naming(result, proj)
    _returned_inside_the_guard(result)


# --- 7b --------------------------------------------------------------------------------------

@pytest.mark.parametrize("which, path_parts, expected", [
    ("client", ("group", "client"), ("client", None, "client_root")),
    ("personal", ("area", "topic"), ("personal", None, "personal_root")),
])
def test_7b_fifo_marker_under_a_root_keeps_the_root_class_without_project_id(
        world, which, path_parts, expected):
    root = world[which]
    os.mkfifo(root / MARKER)

    [result] = _classify(world, [root.joinpath(*path_parts)])["results"]

    assert _triple(result) == expected
    _one_warning_naming(result, root)
    _returned_inside_the_guard(result)


# --- 7c --------------------------------------------------------------------------------------

def test_7c_blocked_read_returns_after_the_timeout_and_the_directory_is_not_read_again(world):
    slow = world["home"] / "dev" / "slow"
    (slow / "a").mkdir(parents=True)
    (slow / "b").mkdir()
    (slow / MARKER).write_text("slow-proj\n")

    first, second = _classify(world, [slow / "a", slow / "b"], block=[slow],
                              timeout=1)["results"]

    assert _triple(first) == ("unknown", None, MARKER_UNREADABLE)
    assert first["elapsed"] >= 0.9
    _returned_inside_the_guard(first)
    _one_warning_naming(first, slow)
    assert first["reads"][_real(slow)] == 1

    assert _triple(second) == ("unknown", None, MARKER_UNREADABLE)
    _one_warning_naming(second, slow)
    assert second["reads"][_real(slow)] == 1, "the remembered directory was read again"


# --- 7d --------------------------------------------------------------------------------------

def test_7d_regular_markers_classify_exactly_as_before(world):
    home, dev = world["home"], world["home"] / "dev"
    for name, marker in (("proj-a", "proj-a\n"), ("pa-engine", "client-proj\n"),
                         ("big", "big-proj\n" + "x" * 10_000)):
        (dev / name / "sub").mkdir(parents=True)
        (dev / name / MARKER).write_text(marker)
    (dev / "registered").mkdir()
    _register(world, [("reg-proj", dev / "registered")])
    (world["client"] / MARKER).write_text("clients-root-id\n")
    (world["personal"] / MARKER).write_text("personal-root-id\n")

    steps = [dev / "proj-a" / "sub", dev / "pa-engine", dev / "big" / "sub",
             dev / "registered", home / "notes",
             world["client"] / "group" / "client", world["personal"] / "area" / "topic"]
    results = _classify(world, steps)["results"]

    assert [_triple(r) for r in results] == [
        ("fabric", "proj-a", "project_marker"),
        ("client", "client-proj", "client_project_id"),
        ("fabric", "big-proj", "project_marker"),
        ("fabric", "reg-proj", "registry"),
        ("own", None, "home"),
        ("client", "clients-root-id", "client_root"),
        ("personal", "personal-root-id", "personal_root"),
    ]
    assert all(r["warnings"] == [] for r in results)


@pytest.mark.parametrize("content", [
    b"p" * 5000 + b"\n",
    b"\xff\xfe not utf-8\n",
], ids=["first-line-past-the-byte-bound", "not-utf-8"])
def test_7d_a_marker_outside_the_size_and_encoding_bounds_is_unreadable(world, content):
    proj = world["home"] / "dev" / "proj"
    proj.mkdir()
    (proj / MARKER).write_bytes(content)

    [result] = _classify(world, [proj])["results"]

    assert _triple(result) == ("unknown", None, MARKER_UNREADABLE)
    _one_warning_naming(result, proj)


# --- 7e --------------------------------------------------------------------------------------

def test_7e_a_timeout_never_yields_fabric_or_own_and_never_lowers_a_root(world):
    home, dev = world["home"], world["home"] / "dev"
    outer = dev / "outer"
    inner = outer / "inner"
    (inner / "work").mkdir(parents=True)
    (outer / MARKER).write_text("outer-proj\n")
    (inner / MARKER).write_text("inner-proj\n")
    _register(world, [("reg-proj", inner)])
    (world["client"] / MARKER).write_text("clients-root-id\n")
    (world["personal"] / MARKER).write_text("personal-root-id\n")

    steps = [inner / "work", home / "notes" / "today",
             world["client"] / "group" / "client", world["personal"] / "area" / "topic"]
    blocked = [inner, home / "notes", world["client"], world["personal"]]
    results = _classify(world, steps, block=blocked, timeout=1)["results"]

    for result, directory in zip(results, blocked):
        _one_warning_naming(result, directory)
        _returned_inside_the_guard(result)
    fabric_marker, own_home, client, personal = results
    for result in (fabric_marker, own_home):
        assert result["cls"] not in ("fabric", "own")
        assert _triple(result) == ("unknown", None, MARKER_UNREADABLE)
    assert _triple(client) == ("client", None, "client_root")
    assert _triple(personal) == ("personal", None, "personal_root")


# --- 7f --------------------------------------------------------------------------------------

def test_7f_unconfigured_boundary_fifo_is_unknown_marker_unreadable(world):
    proj = world["home"] / "dev" / "proj"
    proj.mkdir()
    os.mkfifo(proj / MARKER)
    os.mkfifo(world["client"] / MARKER)
    missing = world["tmp"] / "no_boundary.json"

    results = _classify(world, [proj, world["client"] / "group" / "client"],
                        boundary=missing)["results"]

    for result, directory in zip(results, (proj, world["client"])):
        assert _triple(result) == ("unknown", None, MARKER_UNREADABLE)
        _one_warning_naming(result, directory)
        _returned_inside_the_guard(result)


# --- step 4: the bound on abandoned readers --------------------------------------------------

def test_at_the_abandoned_reader_bound_no_read_starts_until_a_blocked_one_returns(world):
    dev = world["home"] / "dev"
    for name in ("d1", "d2", "d3", "d4"):
        (dev / name / "x").mkdir(parents=True)
        (dev / name / MARKER).write_text(f"{name}-proj\n")
    d1, d2, d3, d4 = (dev / n for n in ("d1", "d2", "d3", "d4"))

    results = _classify(
        world, [d1 / "x", d2 / "x", d3 / "x", "release", d4 / "x", d1 / "x"],
        block=[d1, d2, d3], timeout=0.5, set_attrs={"MARKER_MAX_ABANDONED": 2})["results"]
    first, second, at_bound, released, resumed, remembered = results

    for result in (first, second):
        assert _triple(result) == ("unknown", None, MARKER_UNREADABLE)
    assert first["reads"][_real(d1)] == 1 and second["reads"][_real(d2)] == 1

    # Two readers are blocked: the third call starts no read at all, not even one level down.
    assert _triple(at_bound) == ("unknown", None, MARKER_UNREADABLE)
    _one_warning_naming(at_bound, d3 / "x")
    assert at_bound["reads"] == second["reads"]

    assert released == {"still_blocked": 0}
    assert _triple(resumed) == ("fabric", "d4-proj", "project_marker")
    assert resumed["warnings"] == []

    # A directory that timed out stays refused after its reader returned.
    assert _triple(remembered) == ("unknown", None, MARKER_UNREADABLE)
    _one_warning_naming(remembered, d1)
    assert remembered["reads"][_real(d1)] == 1
