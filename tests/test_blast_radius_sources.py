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


# --- live work ---------------------------------------------------------------------------------
def make_store(tmp_path, name):
    state = tmp_path / name / "state"
    state.mkdir(parents=True)
    return state


def stage(state, did, paths, bucket="pending"):
    d = state.parent / "dispatches" / bucket / did
    d.mkdir(parents=True)
    spec = paths if isinstance(paths, str) else json.dumps({"dispatch_paths": paths})
    (d / "dispatch-spec.json").write_text(spec, encoding="utf-8")


def live_work(**buckets):
    base = {"available": True, "live": [], "starting": [], "stale": [], "unmeasured": []}
    base.update(buckets)
    return base


def item(did, **kw):
    return {"dispatch_id": did, **kw}


@pytest.fixture
def fake_live(monkeypatch):
    def install(value):
        monkeypatch.setattr(bts, "_build_live_work", lambda state, pid, pr: value)
    return install


def test_live_and_starting_counted_stale_left_out(tmp_path, fake_live):
    a, b = make_store(tmp_path, "a"), make_store(tmp_path, "b")
    stage(a, "d1", [{"path": "scripts/x.py"}])
    stage(a, "d2", [{"path": "scripts/y.py", "access": "create"}, {"path": "docs/", "access": "read"}])
    stage(a, "d3", [{"path": "scripts/z.py"}])
    stage(b, "d1", [{"path": "other/only-in-b.py"}])  # same id, other store: never seen from a
    fake_live(live_work(live=[item("d1")], starting=[item("d2")], stale=[item("d3")]))
    out = src.live_subjects(a, "proj-a")
    assert out.unavailable is None and out.reasons == {}
    assert [(s.deliverable_id, s.branch, tuple(s.paths)) for s in out.subjects] == [
        ("d1", "dispatch/d1", ("scripts/x.py",)),
        ("d2", "dispatch/d2", ("scripts/y.py",)),  # read-only path is not a change
    ]


def test_unmeasured_is_unknown_not_clear(tmp_path, fake_live):
    a = make_store(tmp_path, "a")
    fake_live(live_work(unmeasured=[item("d9", lock="unmeasured: boom")]))
    out = src.live_subjects(a, "proj-a")
    assert [(s.deliverable_id, s.paths) for s in out.subjects] == [("d9", None)]
    assert "unmeasured" in out.reasons["d9"]


@pytest.mark.parametrize("value", [
    {"available": False, "reason": "premigration: no table"},
    {"available": None}, {}, None, "garbage", {"available": "yes"},
])
def test_live_work_unavailable_is_unknown_not_empty(tmp_path, fake_live, value):
    fake_live(value)
    out = src.live_subjects(make_store(tmp_path, "a"), "proj-a")
    assert out.unavailable and out.subjects == ()


def test_live_work_raising_and_missing_db_are_unavailable(tmp_path, monkeypatch):
    a = make_store(tmp_path, "a")
    out = src.live_subjects(a, "proj-a")  # real _build_live_work, no runtime_coordination.db
    assert out.unavailable and "not found" in out.unavailable
    monkeypatch.setattr(bts, "_build_live_work", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert "RuntimeError" in src.live_subjects(a, "proj-a").unavailable


@pytest.mark.parametrize("name", ["live", "starting", "unmeasured"])
@pytest.mark.parametrize("bucket_value", [None, "x", {"a": 1}])
def test_malformed_bucket_is_unavailable(tmp_path, fake_live, name, bucket_value):
    fake_live(live_work(**{name: bucket_value}))
    assert src.live_subjects(make_store(tmp_path, "s"), "p").unavailable


@pytest.mark.parametrize("bad", [None, {}, {"dispatch_id": None}, {"dispatch_id": ""}, {"dispatch_id": 5}, "str"])
def test_item_without_dispatch_id_is_unavailable(tmp_path, fake_live, bad):
    fake_live(live_work(live=[bad]))
    assert src.live_subjects(make_store(tmp_path, "a"), "p").unavailable
    fake_live(live_work(unmeasured=[bad]))
    assert src.live_subjects(make_store(tmp_path, "b"), "p").unavailable


@pytest.mark.parametrize("spec", [
    None,                                   # no spec staged
    "{not json",                            # malformed
    "[]",                                   # not an object
    json.dumps({}),                         # field missing
    json.dumps({"dispatch_paths": None}),   # None
    json.dumps({"dispatch_paths": []}),     # empty
    json.dumps({"dispatch_paths": "a.py"}),
    json.dumps({"dispatch_paths": [{"path": None}]}),
    json.dumps({"dispatch_paths": [{"nopath": 1}]}),
    json.dumps({"dispatch_paths": ["a.py"]}),
    json.dumps({"dispatch_paths": [{"path": "a.py", "access": "bogus"}]}),  # unknown value
    json.dumps({"dispatch_paths": [{"path": "a.py", "access": "read"}]}),   # nothing writable
])
def test_unreadable_spec_gives_paths_none(tmp_path, fake_live, spec):
    a = make_store(tmp_path, "a")
    if spec is not None:
        stage(a, "d1", spec)
    fake_live(live_work(live=[item("d1")]))
    out = src.live_subjects(a, "p")
    assert out.unavailable is None
    assert out.subjects[0].paths is None and out.reasons["d1"]


def test_two_specs_for_one_id_are_not_guessed(tmp_path, fake_live):
    a = make_store(tmp_path, "a")
    stage(a, "d1", [{"path": "a.py"}], "pending")
    stage(a, "d1", [{"path": "b.py"}], "completed")
    fake_live(live_work(live=[item("d1")]))
    assert src.live_subjects(a, "p").subjects[0].paths is None


# --- open PRs ----------------------------------------------------------------------------------
class FakeGh:
    def __init__(self, prs, files=None, fail_view=()):
        self.prs, self.files, self.fail_view, self.calls = prs, files or {}, set(fail_view), []

    def __call__(self, argv, cwd):
        self.calls.append(list(argv))
        if argv[:2] == ["pr", "list"]:
            if self.prs is None:
                raise RuntimeError("gh: not logged in")
            return json.dumps(self.prs)
        n = int(argv[2])
        if n in self.fail_view:
            raise RuntimeError("HTTP 502")
        pr_ = next(p for p in self.prs if p["number"] == n)
        return json.dumps({"headRefName": pr_["headRefName"], "headRefOid": pr_["headRefOid"],
                           "files": [{"path": p} for p in self.files[n]]})

    def views(self):
        return [c for c in self.calls if c[1] == "view"]


def pr(n, branch, sha):
    return {"number": n, "headRefName": branch, "headRefOid": sha}


def test_pr_files_read_cached_and_served_without_gh(tmp_path):
    cache = tmp_path / "state" / "blast_radius_pr_cache.json"
    gh = FakeGh([pr(1, "dispatch/d1", "aaa"), pr(2, "feature/x", "bbb")], {1: ["a.py"], 2: ["b.py", "c/d.py"]})
    out = src.open_pr_subjects(tmp_path, "p", cache, gh)
    assert [(s.deliverable_id, s.branch, tuple(s.paths)) for s in out.subjects] == [
        ("d1", "dispatch/d1", ("a.py",)), ("", "feature/x", ("b.py", "c/d.py"))]
    assert len(gh.views()) == 2
    stored = json.loads(cache.read_text())
    assert stored["version"] == 1 and set(stored["entries"]) == {"p:1:aaa", "p:2:bbb"}

    again = FakeGh([pr(1, "dispatch/d1", "aaa"), pr(2, "feature/x", "bbb")])  # a view would KeyError
    out2 = src.open_pr_subjects(tmp_path, "p", cache, again)
    assert again.views() == [] and out2.subjects == out.subjects


def test_new_head_is_re_read_and_old_entry_dropped(tmp_path):
    cache = tmp_path / "c.json"
    src.open_pr_subjects(tmp_path, "p", cache, FakeGh([pr(1, "dispatch/d1", "aaa")], {1: ["a.py"]}))
    gh = FakeGh([pr(1, "dispatch/d1", "ccc")], {1: ["a.py", "new.py"]})
    out = src.open_pr_subjects(tmp_path, "p", cache, gh)
    assert len(gh.views()) == 1 and tuple(out.subjects[0].paths) == ("a.py", "new.py")
    assert set(json.loads(cache.read_text())["entries"]) == {"p:1:ccc"}


def test_cache_is_per_project(tmp_path):
    cache = tmp_path / "c.json"
    src.open_pr_subjects(tmp_path, "p1", cache, FakeGh([pr(1, "dispatch/d1", "aaa")], {1: ["a.py"]}))
    gh = FakeGh([pr(1, "dispatch/d1", "aaa")], {1: ["other.py"]})
    out = src.open_pr_subjects(tmp_path, "p2", cache, gh)
    assert len(gh.views()) == 1 and tuple(out.subjects[0].paths) == ("other.py",)
    assert set(json.loads(cache.read_text())["entries"]) == {"p1:1:aaa", "p2:1:aaa"}


def test_gh_view_error_is_unknown_not_cached_no_retry(tmp_path):
    cache = tmp_path / "c.json"
    gh = FakeGh([pr(1, "dispatch/d1", "aaa"), pr(2, "dispatch/d2", "bbb")], {2: ["b.py"]}, fail_view=[1])
    out = src.open_pr_subjects(tmp_path, "p", cache, gh)
    assert out.subjects[0].paths is None and "502" in out.reasons["d1"]
    assert tuple(out.subjects[1].paths) == ("b.py",)
    assert [c[2] for c in gh.views()] == ["1", "2"]  # one attempt each
    assert set(json.loads(cache.read_text())["entries"]) == {"p:2:bbb"}


def test_gh_list_error_is_unavailable(tmp_path):
    out = src.open_pr_subjects(tmp_path, "p", tmp_path / "c.json", FakeGh(None))
    assert out.subjects == () and "not logged in" in out.unavailable
    assert not (tmp_path / "c.json").exists()


@pytest.mark.parametrize("listing", ["{not json", '{"a": 1}', "null"])
def test_gh_list_garbage_is_unavailable(tmp_path, listing):
    out = src.open_pr_subjects(tmp_path, "p", tmp_path / "c.json", lambda argv, cwd: listing)
    assert out.unavailable and out.subjects == ()


@pytest.mark.parametrize("entry", [
    None, {}, {"number": None, "headRefName": "b", "headRefOid": "s"},
    {"number": True, "headRefName": "b", "headRefOid": "s"},
    {"number": "1", "headRefName": "b", "headRefOid": "s"},
    {"number": 1, "headRefName": None, "headRefOid": "s"},
    {"number": 1, "headRefName": "b", "headRefOid": None},
    {"number": 1, "headRefName": "b", "headRefOid": ""},
])
def test_malformed_list_entry_is_unavailable(tmp_path, entry):
    out = src.open_pr_subjects(tmp_path, "p", tmp_path / "c.json", lambda argv, cwd: json.dumps([entry]))
    assert out.unavailable and out.subjects == ()


@pytest.mark.parametrize("view", [
    "{not json", "null", "{}", '{"files": null, "headRefOid": "s"}', '{"files": [], "headRefOid": null}',
    '{"files": [], "headRefOid": ""}', '{"files": ["a.py"], "headRefOid": "s"}',
    '{"files": [{"path": null}], "headRefOid": "s"}', '{"files": [{"name": "a"}], "headRefOid": "s"}',
])
def test_unusable_view_is_unknown_and_not_cached(tmp_path, view):
    def gh(argv, cwd):
        return json.dumps([pr(1, "dispatch/d1", "aaa")]) if argv[1] == "list" else view
    cache = tmp_path / "c.json"
    out = src.open_pr_subjects(tmp_path, "p", cache, gh)
    assert out.subjects[0].paths is None and out.reasons["d1"]
    assert not cache.exists()


@pytest.mark.parametrize("content", [
    "{not json", "[]", json.dumps({"version": 99, "entries": {"p:1:aaa": ["x.py"]}}),
    json.dumps({"version": 1, "entries": None}), json.dumps({"version": 1}),
    json.dumps({"version": 1, "entries": {"p:1:aaa": "x.py"}}),
    json.dumps({"version": 1, "entries": {"p:1:aaa": [1, 2]}}),
    json.dumps({"version": 1, "entries": {"p:1:aaa": None}}),
])
def test_bad_cache_is_ignored_and_re_read(tmp_path, content):
    cache = tmp_path / "c.json"
    cache.write_text(content)
    gh = FakeGh([pr(1, "dispatch/d1", "aaa")], {1: ["real.py"]})
    out = src.open_pr_subjects(tmp_path, "p", cache, gh)
    assert len(gh.views()) == 1 and tuple(out.subjects[0].paths) == ("real.py",)
    assert json.loads(cache.read_text())["entries"] == {"p:1:aaa": ["real.py"]}


@pytest.mark.parametrize("pid", ["", "  ", None])
def test_missing_project_id_is_unavailable(tmp_path, pid):
    gh = FakeGh([])
    assert src.open_pr_subjects(tmp_path, pid, tmp_path / "c.json", gh).unavailable
    assert gh.calls == []


def test_unwritable_cache_still_returns_the_subjects(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    gh = FakeGh([pr(1, "dispatch/d1", "aaa")], {1: ["a.py"]})
    out = src.open_pr_subjects(tmp_path, "p", blocker / "c.json", gh)
    assert tuple(out.subjects[0].paths) == ("a.py",)


def test_default_cache_path(tmp_path):
    assert src.default_cache_path(tmp_path / "state") == tmp_path / "state" / "blast_radius_pr_cache.json"
