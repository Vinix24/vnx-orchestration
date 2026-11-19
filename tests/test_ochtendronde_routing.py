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
        self.tmp, self.data = tmp_path, {n: tmp_path / "data" / n for n in names}
        for n in names:
            (self.data[n] / "state").mkdir(parents=True)

    def ledger(self, name):
        return self.tmp / "ledger" / name

    def edits(self, name, *rows):
        (self.data[name] / "state" / "pending_edits.json").write_text(json.dumps({"edits": list(rows)}))

    def read(self, project, register=None):
        routing = rt.Routing(project, register=register or self.data, fleet_state_dir=self.ledger(FLEET))
        return qa.read_analyzer(project, data_root=self.data[project], now=NOW, state_dir=self.ledger(project), routing=routing)

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
    out_b = [w.read(p) for p in "AB"][1]
    assert [p["ref"] for p in w.points(FLEET)] == ["project:A/edit:1"]
    assert [p["ref"] for p in w.points("A")] == ["edit:2"] and w.points("B") == []
    assert out_b["routed_elsewhere"] == [{"ref": "edit:7", "owner": "A"}]


def test_deciding_the_source_closes_the_routed_point_and_writes_nothing_in_the_source_store(tmp_path):
    w = World(tmp_path, "A", FLEET)
    w.edits("A", edit(3, target="agents/x/CLAUDE.md"))
    w.read("A")
    w.edits("A", edit(3, target="agents/x/CLAUDE.md", status="applied"))
    snap = lambda: {p.name: p.read_bytes() for p in (w.data["A"] / "state").iterdir()}
    before, out = snap(), w.read("A")
    point = w.points(FLEET)[0]
    assert point["state"] == "beantwoord" and out["routed"]["closed"] == [point["point_id"]] and w.points("A") == []
    assert snap() == before


def test_line_gone_closes_through_history_or_stays_open_as_unknown(tmp_path):
    w = World(tmp_path, "A", FLEET)
    w.edits("A", edit(3, target="skills/s/SKILL.md"))
    w.read("A"), w.edits("A"), w.read("A")
    point = w.points(FLEET)[0]
    assert point["state"] == "open" and point["unknown_reden"]
    (w.data["A"] / "state" / "edit_history.json").write_text(json.dumps([{"id": 3, "status": "applied", "applied_at": "x"}]))
    w.read("A")
    assert w.points(FLEET)[0]["state"] == "beantwoord"


def test_pause_count_follows_the_ledger_the_point_stands_in(tmp_path):
    w = World(tmp_path, "A", FLEET)
    w.edits("A", edit(3, target="skills/x/SKILL.md"), edit(4))
    w.read("A")
    [ag.write_snapshot(n, DAY, state_dir=w.ledger(n), now=NOW) for n in ("A", FLEET)]
    a, v = (ag.decision_status(n, DAY, state_dir=w.ledger(n)) for n in ("A", FLEET))
    assert list(a.points.values()) == ["open"] == list(v.points.values()) and a.points.keys().isdisjoint(v.points.keys())


@pytest.mark.parametrize("why", ["garbage", "ghost"])
def test_unreadable_or_missing_other_store_while_counting_is_reported_and_makes_no_fleet_point(tmp_path, why):
    w = World(tmp_path, "A", "B", FLEET)
    w.edits("A", edit(1, proposal_key="k"))
    if why == "garbage":
        (w.data["B"] / "state" / "pending_edits.json").write_text("{not json")
    out = w.read("A", register={**w.data, "ghost": tmp_path / "nope"} if why == "ghost" else None)
    assert w.points(FLEET) == [] and w.points("A") == [] and out["classification"] == {"edit:1": "unknown"}
    assert out["dekking"] == "unknown" and out["unclassified"][0]["reden"] and out["reden"]


def test_unreadable_register_keeps_fabric_target_and_holds_back_fingerprinted_points(tmp_path):
    w = World(tmp_path, "A", FLEET)
    w.edits("A", edit(1, target="skills/a/SKILL.md"), edit(2, proposal_key="k"))
    bad = tmp_path / "projects.json"
    bad.write_text("garbage")
    pytest.raises(rt.RoutingRefused, rt.load_register, bad)
    routing = rt.Routing("A", register_path=bad, fleet_state_dir=w.ledger(FLEET))
    out = qa.read_analyzer("A", data_root=w.data["A"], now=NOW, state_dir=w.ledger("A"), routing=routing)
    assert [p["ref"] for p in w.points(FLEET)] == ["project:A/edit:1"]
    assert [u["ref"] for u in out["unclassified"]] == ["edit:2"] and out["dekking"] == "unknown"


def test_register_file_is_read_with_project_ids_and_data_dirs(tmp_path):
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"project_id": "A", "data_dir": "/d/a"}, {"project_id": "../x", "data_dir": "/d"}, {"name": "x"}]}))
    assert rt.load_register(reg) == {"A": Path("/d/a")}
    reg.write_text(json.dumps({"projects": "x"}))
    with pytest.raises(rt.RoutingRefused):
        rt.load_register(reg)


@pytest.mark.parametrize("target,expected", [
    *[(t, True) for t in ("skills/a/SKILL.md", "agents/t/CLAUDE.md", "scripts/lib/prompts/p.md", "./skills/a", "scripts/pr_merge.py",
                          "scripts/lib/dispatch_cli.py", "scripts/lib/stop_conditions.py", "scripts/review_gate_manager.py",
                          "scripts/lib/review_x.py")],
    *[(t, False) for t in ("docs/skills/a", "skills/../docs/a", "/skills/a", "../skills/a", "", None, 5, "scripts/lib/dispatch_cli.pyc")]])
def test_in_fabric(target, expected):
    assert rt.in_fabric(target) is expected


@pytest.mark.parametrize("owner,ref", [("", "e:1"), ("a/b", "e:1"), ("A", ""), ("A", "/abs"), ("A", "a/../b"),
                                       ("A", "x" * 300), ("A", None)])
def test_routed_ref_refuses(owner, ref):
    with pytest.raises(rt.RoutingRefused):
        rt.routed_ref(owner, ref)


@pytest.mark.parametrize("row", [edit(1, target="skills/a/SKILL.md"), edit(1, proposal_key="k-own")])
def test_fleet_project_source_keeps_its_own_ledger(tmp_path, row):
    w = World(tmp_path, FLEET)
    w.edits(FLEET, row), w.read(FLEET)
    assert [p["ref"] for p in w.points(FLEET)] == ["edit:1"]


@pytest.mark.parametrize("routed", [True, False])
def test_reading_the_fleet_project_leaves_another_projects_routed_point_alone(tmp_path, routed):
    w = World(tmp_path, "A", FLEET)
    w.edits("A", edit(3, target="skills/x/SKILL.md"))
    w.edits(FLEET)
    (w.data[FLEET] / "state" / "pending_archival.json").write_text('{"pending_archival": []}')
    w.read("A")
    ledger = w.ledger(FLEET) / oq.LEDGER_FILENAME
    before = ledger.read_bytes()
    w.read(FLEET) if routed else qa.read_analyzer(FLEET, data_root=w.data[FLEET], now=NOW, state_dir=w.ledger(FLEET))
    assert ledger.read_bytes() == before and "unknown" not in before.decode()
    w.edits("A", edit(3, target="skills/x/SKILL.md", status="applied"))
    w.read("A")
    assert w.points(FLEET)[0]["state"] == "beantwoord"


@pytest.mark.parametrize("routed", [True, False])  # a plain point; and a reader called without routing
def test_plain_point_stays_in_the_source_ledger(tmp_path, routed):
    w = World(tmp_path, "A", FLEET)
    w.edits("A", edit(1, target=None if routed else "skills/a/SKILL.md"))
    w.read("A") if routed else qa.read_analyzer("A", data_root=w.data["A"], now=NOW, state_dir=w.ledger("A"))
    assert [p["ref"] for p in w.points("A")] == ["edit:1"] and w.points(FLEET) == []


@pytest.mark.parametrize("other", ["A", "x"])  # "A" sorts below vnx-dev, "x" above it
@pytest.mark.parametrize("flip", [False, True])
def test_fleet_project_holder_honours_the_elected_owner(tmp_path, other, flip):
    w = World(tmp_path, other, FLEET)
    w.edits(other, edit(1, proposal_key="k"))
    w.edits(FLEET, edit(7, proposal_key="k"))
    outs = {p: w.read(p) for p in ((FLEET, other) if flip else (other, FLEET))}
    owner, loser = (FLEET, other) if FLEET < other else (other, FLEET)
    mine, theirs = ("edit:7", "edit:1") if owner == FLEET else (f"project:{other}/edit:1", "edit:7")
    assert [p["ref"] for n in (other, FLEET) for p in w.points(n)] == [mine]
    assert outs[loser]["routed_elsewhere"] == [{"ref": theirs, "owner": owner}]
