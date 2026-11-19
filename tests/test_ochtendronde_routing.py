"""ochtendronde D2b: classify analyzer points project or fleet, route fleet points to the vnx-dev ledger."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import ochtendronde_agenda as ag
import ochtendronde_routing as rt
import operator_queue as oq
import operator_queue_dream_analyzer as qa

NOW, DAY = "2026-11-19T05:00:00Z", "2026-11-19"
FLEET = rt.FLEET_PROJECT


class World:
    """Temp project stores, a temp register and one ledger dir per project; never the live ones."""

    def __init__(self, tmp_path, *names):
        self.tmp = tmp_path
        self.data = {n: tmp_path / "data" / n for n in names}
        for n in names:
            (self.data[n] / "state").mkdir(parents=True)

    def ledger(self, name):
        return self.tmp / "ledger" / name

    def edits(self, name, *rows):
        (self.data[name] / "state" / "pending_edits.json").write_text(json.dumps({"edits": list(rows)}))

    def read(self, project, register=None):
        routing = rt.Routing(project, register=register if register is not None else self.data,
                             fleet_state_dir=self.ledger(FLEET))
        return qa.read_analyzer(project, data_root=self.data[project], now=NOW, state_dir=self.ledger(project),
                                routing=routing)

    def points(self, name):
        return list(oq.read_state(name, state_dir=self.ledger(name))["points"].values())


def edit(i, target="docs/x.md", status="pending", **extra):
    return {"id": i, "category": "c", "target": target, "status": status, **extra}


def test_fabric_target_opens_one_fleet_point_and_none_in_source(tmp_path):
    w = World(tmp_path, "A", FLEET)
    w.edits("A", edit(3, target="skills/x/SKILL.md"))
    out = w.read("A")
    assert [p["ref"] for p in w.points(FLEET)] == ["project:A/edit:3"]
    assert w.points("A") == [] and len(out["routed"]["opened"]) == 1 and out["opened"] == []
    assert w.points(FLEET)[0]["point_id"] == oq.point_id(FLEET, 4, "project:A/edit:3", 1)
    w.read("A")
    assert len(w.points(FLEET)) == 1


def test_same_fingerprint_in_two_projects_is_one_fleet_point_and_one_stays_alone(tmp_path):
    w = World(tmp_path, "A", "B", FLEET)
    w.edits("A", edit(1, proposal_key="k-shared"), edit(2, proposal_key="k-only-a"))
    w.edits("B", edit(7, proposal_key="k-shared"))
    w.read("A")
    out_b = w.read("B")
    assert [p["ref"] for p in w.points(FLEET)] == ["project:A/edit:1"]
    assert [p["ref"] for p in w.points("A")] == ["edit:2"] and w.points("B") == []
    assert out_b["routed_elsewhere"] == [{"ref": "edit:7", "owner": "A"}]


def test_deciding_the_source_closes_the_routed_point_and_writes_nothing_in_the_source_store(tmp_path):
    w = World(tmp_path, "A", FLEET)
    w.edits("A", edit(3, target="agents/x/CLAUDE.md"))
    w.read("A")
    w.edits("A", edit(3, target="agents/x/CLAUDE.md", status="applied"))
    state = w.data["A"] / "state"
    before = {p.name: p.read_bytes() for p in state.iterdir()}
    out = w.read("A")
    point = w.points(FLEET)[0]
    assert point["state"] == "beantwoord" and out["routed"]["closed"] == [point["point_id"]]
    assert {p.name: p.read_bytes() for p in state.iterdir()} == before
    assert w.points("A") == []


def test_line_gone_closes_through_history_or_stays_open_as_unknown(tmp_path):
    w = World(tmp_path, "A", FLEET)
    w.edits("A", edit(3, target="skills/s/SKILL.md"))
    w.read("A")
    w.edits("A")
    w.read("A")
    point = w.points(FLEET)[0]
    assert point["state"] == "open" and point["unknown_reden"]
    (w.data["A"] / "state" / "edit_history.json").write_text(json.dumps([{"id": 3, "status": "applied", "applied_at": "x"}]))
    w.read("A")
    assert w.points(FLEET)[0]["state"] == "beantwoord"


def test_pause_count_follows_the_ledger_the_point_stands_in(tmp_path):
    w = World(tmp_path, "A", FLEET)
    w.edits("A", edit(3, target="skills/x/SKILL.md"), edit(4))
    w.read("A")
    for name in ("A", FLEET):
        ag.write_snapshot(name, DAY, state_dir=w.ledger(name), now=NOW)
    a = ag.decision_status("A", DAY, state_dir=w.ledger("A"))
    v = ag.decision_status(FLEET, DAY, state_dir=w.ledger(FLEET))
    assert list(a.points.values()) == ["open"]
    assert list(v.points.values()) == ["open"] and a.points.keys().isdisjoint(v.points.keys())


def test_unreadable_store_while_counting_is_reported_and_does_not_make_a_fleet_point(tmp_path):
    w = World(tmp_path, "A", "B", FLEET)
    w.edits("A", edit(1, proposal_key="k"))
    (w.data["B"] / "state" / "pending_edits.json").write_text("{not json")
    out = w.read("A")
    assert w.points(FLEET) == [] and w.points("A") == []
    assert out["classification"] == {"edit:1": "unknown"} and out["dekking"] == "unknown"
    assert "B" in out["unclassified"][0]["reden"] and "B" in out["reden"]


def test_missing_data_dir_of_a_register_project_is_unreadable_not_empty(tmp_path):
    w = World(tmp_path, "A", FLEET)
    w.edits("A", edit(1, proposal_key="k"))
    out = w.read("A", register={**w.data, "ghost": tmp_path / "nope"})
    assert out["unclassified"] and w.points("A") == [] and w.points(FLEET) == []


def test_unreadable_register_keeps_fabric_target_and_holds_back_fingerprinted_points(tmp_path):
    w = World(tmp_path, "A", FLEET)
    w.edits("A", edit(1, target="skills/a/SKILL.md"), edit(2, proposal_key="k"))
    bad = tmp_path / "projects.json"
    bad.write_text("garbage")
    with pytest.raises(rt.RoutingRefused):
        rt.load_register(bad)
    routing = rt.Routing("A", register_path=bad, fleet_state_dir=w.ledger(FLEET))
    out = qa.read_analyzer("A", data_root=w.data["A"], now=NOW, state_dir=w.ledger("A"), routing=routing)
    assert [p["ref"] for p in w.points(FLEET)] == ["project:A/edit:1"]
    assert [u["ref"] for u in out["unclassified"]] == ["edit:2"] and out["dekking"] == "unknown"


def test_no_fingerprint_and_no_fabric_target_is_a_project_point(tmp_path):
    w = World(tmp_path, "A", "B", FLEET)
    w.edits("A", edit(1))
    w.edits("B", edit(1))
    w.read("A")
    assert [p["ref"] for p in w.points("A")] == ["edit:1"] and w.points(FLEET) == []


def test_register_file_is_read_with_project_ids_and_data_dirs(tmp_path):
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"project_id": "A", "data_dir": "/d/a"}, {"project_id": "../x", "data_dir": "/d"},
                                            {"name": "no-id"}]}))
    assert rt.load_register(reg) == {"A": Path("/d/a")}
    reg.write_text(json.dumps({"projects": "x"}))
    with pytest.raises(rt.RoutingRefused):
        rt.load_register(reg)


@pytest.mark.parametrize("target,expected", [
    ("skills/a/SKILL.md", True), ("agents/t/CLAUDE.md", True), ("scripts/lib/prompts/p.md", True),
    ("scripts/lib/dispatch_cli.py", True), ("scripts/lib/stop_conditions.py", True), ("scripts/pr_merge.py", True),
    ("scripts/review_gate_manager.py", True), ("scripts/lib/review_x.py", True), ("./skills/a", True),
    ("docs/skills/a", False), ("skills/../docs/a", False), ("/skills/a", False), ("../skills/a", False),
    ("scripts/lib/dispatch_cli.pyc", False), ("", False), (None, False), (5, False)])
def test_in_fabric(target, expected):
    assert rt.in_fabric(target) is expected


@pytest.mark.parametrize("owner,ref", [("", "e:1"), ("a/b", "e:1"), ("A", ""), ("A", "/abs"), ("A", "a/../b"),
                                       ("A", "x" * 300), ("A", None)])
def test_routed_ref_refuses(owner, ref):
    with pytest.raises(rt.RoutingRefused):
        rt.routed_ref(owner, ref)


def test_fleet_project_source_keeps_its_own_ledger(tmp_path):
    w = World(tmp_path, FLEET)
    w.edits(FLEET, edit(1, target="skills/a/SKILL.md"))
    w.read(FLEET)
    assert [p["ref"] for p in w.points(FLEET)] == ["edit:1"]


def test_reader_without_routing_is_unchanged(tmp_path):
    w = World(tmp_path, "A")
    w.edits("A", edit(1, target="skills/a/SKILL.md"))
    qa.read_analyzer("A", data_root=w.data["A"], now=NOW, state_dir=w.ledger("A"))
    assert [p["ref"] for p in w.points("A")] == ["edit:1"]
