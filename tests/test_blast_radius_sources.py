"""blast-radius-en-afhankelijkheden B4a: live-work and open-PR sources (ADR-096 decisions 9 and 11)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))

import blast_radius_sources as src
import build_t0_state as bts


def store(tmp_path, name):
    (tmp_path / name / "state").mkdir(parents=True)
    return tmp_path / name / "state"


def stage(state, did, spec, bucket="pending"):
    d = state.parent / "dispatches" / bucket / did
    d.mkdir(parents=True)
    (d / "dispatch-spec.json").write_text(spec if isinstance(spec, str) else json.dumps({"dispatch_paths": spec}))


def lw(**b):
    return {"available": True, "live": [], "starting": [], "stale": [], "unmeasured": [], **b}


@pytest.fixture
def fake_live(monkeypatch):
    return lambda v: monkeypatch.setattr(bts, "_build_live_work", lambda *a: v)


def test_live_and_starting_counted_stale_left_out_unmeasured_unknown(tmp_path, fake_live):
    a, b = store(tmp_path, "a"), store(tmp_path, "b")
    stage(a, "d1", [{"path": "x.py"}])
    stage(a, "d2", [{"path": "y.py", "access": "create"}, {"path": "docs/", "access": "read"}])
    stage(b, "d1", [{"path": "only-in-b.py"}])  # same id in the other store is never seen
    fake_live(lw(live=[{"dispatch_id": "d1"}], starting=[{"dispatch_id": "d2"}], stale=[{"dispatch_id": "d3"}],
                 unmeasured=[{"dispatch_id": "d9", "lock": "unmeasured: boom"}]))
    out = src.live_subjects(a, "p")
    assert [(s.deliverable_id, s.branch, s.paths) for s in out.subjects] == [
        ("d1", "dispatch/d1", ("x.py",)), ("d2", "dispatch/d2", ("y.py",)), ("d9", "dispatch/d9", None)]
    assert set(out.reasons) == {"d9"} and out.unavailable is None


@pytest.mark.parametrize("value", [{"available": False, "reason": "no db"}, {"available": None}, {}, None, "x"])
def test_live_work_unavailable_is_not_an_empty_list(tmp_path, fake_live, value):
    fake_live(value)
    out = src.live_subjects(store(tmp_path, "a"), "p")
    assert out.unavailable and out.subjects == ()


def test_real_live_work_without_db_and_a_raise_are_unavailable(tmp_path, monkeypatch):
    a = store(tmp_path, "a")
    assert "not found" in src.live_subjects(a, "p").unavailable
    monkeypatch.setattr(bts, "_build_live_work", lambda *x: (_ for _ in ()).throw(RuntimeError("x")))
    assert "RuntimeError" in src.live_subjects(a, "p").unavailable


@pytest.mark.parametrize("name", ["live", "starting", "unmeasured"])
@pytest.mark.parametrize("bad", [None, "x", {"a": 1}, [None], [{}], [{"dispatch_id": None}], [{"dispatch_id": " "}], [{"dispatch_id": 5}]])
def test_malformed_bucket_or_item_is_unavailable(tmp_path, fake_live, name, bad):
    fake_live(lw(**{name: bad}))
    assert src.live_subjects(store(tmp_path, "a"), "p").unavailable


@pytest.mark.parametrize("spec", [
    None, "{nope", "[]", "{}", json.dumps({"dispatch_paths": None}), json.dumps({"dispatch_paths": []}),
    json.dumps({"dispatch_paths": "a.py"}), json.dumps({"dispatch_paths": ["a.py"]}),
    json.dumps({"dispatch_paths": [{"path": None}]}), json.dumps({"dispatch_paths": [{"nopath": 1}]}),
    json.dumps({"dispatch_paths": [{"path": "a.py", "access": "bogus"}]}),
    json.dumps({"dispatch_paths": [{"path": "a.py", "access": "read"}]}),
])
def test_unreadable_spec_gives_paths_none(tmp_path, fake_live, spec):
    a = store(tmp_path, "a")
    if spec is not None:
        stage(a, "d1", spec)
    fake_live(lw(live=[{"dispatch_id": "d1"}]))
    out = src.live_subjects(a, "p")
    assert out.unavailable is None and out.subjects[0].paths is None and out.reasons["d1"]


def test_two_specs_for_one_id_are_not_guessed(tmp_path, fake_live):
    a = store(tmp_path, "a")
    stage(a, "d1", [{"path": "a.py"}])
    stage(a, "d1", [{"path": "b.py"}], "completed")
    fake_live(lw(live=[{"dispatch_id": "d1"}]))
    assert src.live_subjects(a, "p").subjects[0].paths is None


class FakeGh:
    def __init__(self, prs, files=None, fail_view=()):
        self.prs, self.files, self.fail_view, self.calls = prs, files or {}, set(fail_view), []

    def __call__(self, argv, cwd):
        self.calls.append(list(argv))
        if argv[1] == "list":
            if self.prs is None:
                raise RuntimeError("gh: not logged in")
            return json.dumps(self.prs)
        n = int(argv[2])
        if n in self.fail_view:
            raise RuntimeError("HTTP 502")
        p = next(p for p in self.prs if p["number"] == n)
        return json.dumps({"headRefName": p["headRefName"], "headRefOid": p["headRefOid"],
                           "files": [{"path": f} for f in self.files[n]]})

    def views(self):
        return [c[2] for c in self.calls if c[1] == "view"]


def pr(n, branch, sha):
    return {"number": n, "headRefName": branch, "headRefOid": sha}


def entries(cache):
    return json.loads(cache.read_text())["entries"]


def test_files_read_cached_and_served_from_cache_without_gh(tmp_path):
    cache = tmp_path / "state" / "blast_radius_pr_cache.json"
    prs = [pr(1, "dispatch/d1", "aaa"), pr(2, "feature/x", "bbb")]
    out = src.open_pr_subjects(tmp_path, "p", cache, gh := FakeGh(prs, {1: ["a.py"], 2: ["b.py"]}))
    assert [(s.deliverable_id, s.branch, s.paths) for s in out.subjects] == [
        ("d1", "dispatch/d1", ("a.py",)), ("", "feature/x", ("b.py",))]
    assert gh.views() == ["1", "2"] and set(entries(cache)) == {"p:1:aaa", "p:2:bbb"}
    again = FakeGh(prs)  # a view would raise KeyError
    assert src.open_pr_subjects(tmp_path, "p", cache, again).subjects == out.subjects and again.views() == []


def test_new_head_is_re_read_old_entry_dropped_other_project_kept(tmp_path):
    cache = tmp_path / "c.json"
    src.open_pr_subjects(tmp_path, "q", cache, FakeGh([pr(1, "dispatch/d1", "aaa")], {1: ["q.py"]}))
    src.open_pr_subjects(tmp_path, "p", cache, FakeGh([pr(1, "dispatch/d1", "aaa")], {1: ["a.py"]}))
    out = src.open_pr_subjects(tmp_path, "p", cache, gh := FakeGh([pr(1, "dispatch/d1", "ccc")], {1: ["new.py"]}))
    assert gh.views() == ["1"] and out.subjects[0].paths == ("new.py",)
    assert set(entries(cache)) == {"q:1:aaa", "p:1:ccc"}


def test_gh_view_error_is_unknown_not_cached_no_retry(tmp_path):
    cache = tmp_path / "c.json"
    gh = FakeGh([pr(1, "dispatch/d1", "aaa"), pr(2, "dispatch/d2", "bbb")], {2: ["b.py"]}, fail_view=[1])
    out = src.open_pr_subjects(tmp_path, "p", cache, gh)
    assert out.subjects[0].paths is None and "502" in out.reasons["d1"] and out.subjects[1].paths == ("b.py",)
    assert gh.views() == ["1", "2"] and set(entries(cache)) == {"p:2:bbb"}


@pytest.mark.parametrize("listing", ["{nope", '{"a": 1}', "null", "[null]", "[{}]"] + [
    json.dumps([{"number": n, "headRefName": b, "headRefOid": s}]) for n, b, s in
    [(None, "b", "s"), (True, "b", "s"), ("1", "b", "s"), (1, None, "s"), (1, "b", None), (1, "b", "")]])
def test_garbage_list_is_unavailable(tmp_path, listing):
    out = src.open_pr_subjects(tmp_path, "p", tmp_path / "c.json", lambda argv, cwd: listing)
    assert out.unavailable and out.subjects == ()


def test_gh_list_error_and_missing_project_id_are_unavailable(tmp_path):
    assert "not logged in" in src.open_pr_subjects(tmp_path, "p", tmp_path / "c.json", FakeGh(None)).unavailable
    for pid in ("", "  ", None):
        assert src.open_pr_subjects(tmp_path, pid, tmp_path / "c.json", gh := FakeGh([])).unavailable and gh.calls == []


@pytest.mark.parametrize("view", [
    "{nope", "null", "{}", '{"files": null, "headRefOid": "s"}', '{"files": [], "headRefOid": null}',
    '{"files": [], "headRefOid": ""}', '{"files": ["a.py"], "headRefOid": "s"}',
    '{"files": [{"path": null}], "headRefOid": "s"}', '{"files": [{"name": "a"}], "headRefOid": "s"}'])
def test_unusable_view_is_unknown_and_not_cached(tmp_path, view):
    gh = lambda argv, cwd: json.dumps([pr(1, "dispatch/d1", "aaa")]) if argv[1] == "list" else view
    out = src.open_pr_subjects(tmp_path, "p", tmp_path / "c.json", gh)
    assert out.subjects[0].paths is None and out.reasons["d1"] and not (tmp_path / "c.json").exists()


@pytest.mark.parametrize("content", [
    "{nope", "[]", json.dumps({"version": 99, "entries": {"p:1:aaa": ["x.py"]}}), json.dumps({"version": 1}),
    json.dumps({"version": 1, "entries": None}), json.dumps({"version": 1, "entries": {"p:1:aaa": "x.py"}}),
    json.dumps({"version": 1, "entries": {"p:1:aaa": [1]}}), json.dumps({"version": 1, "entries": {"p:1:aaa": None}})])
def test_bad_cache_is_ignored_and_re_read(tmp_path, content):
    cache = tmp_path / "c.json"
    cache.write_text(content)
    out = src.open_pr_subjects(tmp_path, "p", cache, gh := FakeGh([pr(1, "dispatch/d1", "aaa")], {1: ["real.py"]}))
    assert gh.views() == ["1"] and out.subjects[0].paths == ("real.py",) and entries(cache) == {"p:1:aaa": ["real.py"]}


def test_unwritable_cache_still_returns_the_subjects(tmp_path):
    (tmp_path / "file").write_text("x")
    out = src.open_pr_subjects(tmp_path, "p", tmp_path / "file" / "c.json", FakeGh([pr(1, "dispatch/d1", "a")], {1: ["a.py"]}))
    assert out.subjects[0].paths == ("a.py",)
