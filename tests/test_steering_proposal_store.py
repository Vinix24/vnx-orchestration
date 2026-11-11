"""steering_proposal_store: identity, revisions, decision records (ADR-072)."""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import steering_proposal_store as store  # noqa: E402

T0, T1 = "2026-11-11T08:00:00Z", "2026-11-11T09:00:00Z"


def make(voorstel="Voeg een statusoverzicht toe", doelpad="skills/status", oorzaak="ontbrekend_overzicht"):
    return {"oorzaak": oorzaak, "artefact_soort": "feature", "doelpad": doelpad, "voorstel": voorstel,
            "evidence": [{"prompt_id": "p1", "ts": T0}]}


def seed(tmp_path, project="vnx-dev", **kw):
    return store.add_revision(tmp_path, project, make(**kw), now=T0)["proposal_id"]


def test_identity_follows_oorzaak_soort_doelpad_and_project_not_the_text(tmp_path):
    pid = seed(tmp_path)
    assert seed(tmp_path, voorstel="Een ander woord voor hetzelfde") == pid
    assert seed(tmp_path, doelpad="skills/andere") != pid
    assert seed(tmp_path, oorzaak="poort_te_streng") != pid
    assert store.proposal_id("a", "x", "y", "z") != store.proposal_id("b", "x", "y", "z")
    assert len(store.proposals(tmp_path, "vnx-dev")) == 3


def test_a_changed_text_is_revision_two_and_revision_one_stays_readable(tmp_path):
    pid = seed(tmp_path)
    seed(tmp_path, voorstel="Een overzicht van de status")
    entry = store.get(tmp_path, "vnx-dev", pid)
    assert [r["revision"] for r in entry["revisions"]] == [1, 2]
    assert entry["revisions"][0]["voorstel"] == "Voeg een statusoverzicht toe"
    assert entry["status"] == "pending"


def test_decisions_bind_to_a_revision_and_the_state_machine_holds(tmp_path):
    pid = seed(tmp_path)
    for kwargs in ({"decision": "applied", "applied_ref": "#1"}, {"decision": "deferred"},
                   {"decision": "approved", "revision": 2}, {"decision": "approved", "reason": ""}):
        args = {"reason": "r", "decided_by": "op", **kwargs}
        with pytest.raises(store.DecisionRefused):
            store.decide(tmp_path, "vnx-dev", pid, args.pop("decision"), **args)
    record = store.decide(tmp_path, "vnx-dev", pid, "approved", reason="goed", decided_by="op", now=T1)
    assert record["revision"] == 1 and store.get(tmp_path, "vnx-dev", pid)["decided_revision"] == 1
    with pytest.raises(store.DecisionRefused):
        store.decide(tmp_path, "vnx-dev", pid, "applied", reason="r", decided_by="op", applied_ref=" ")
    store.decide(tmp_path, "vnx-dev", pid, "applied", reason="gemerged", decided_by="op", applied_ref="PR #2690")
    entry = store.get(tmp_path, "vnx-dev", pid)
    assert entry["status"] == "applied" and entry["applied_ref"] == "PR #2690"


def test_projects_do_not_see_each_other(tmp_path):
    pid_a, pid_b = seed(tmp_path, "proj-a"), seed(tmp_path, "proj-b")
    assert pid_a != pid_b
    for call in (lambda: store.get(tmp_path, "proj-b", pid_a),
                 lambda: store.decide(tmp_path, "proj-b", pid_a, "approved", reason="x", decided_by="op")):
        with pytest.raises(store.ProposalNotFound):
            call()
    store.decide(tmp_path, "proj-a", pid_a, "rejected", reason="x", decided_by="op", now=T1)
    assert store.get(tmp_path, "proj-b", pid_b)["status"] == "pending"
    assert {r["project_id"] for r in store.decisions(tmp_path, "proj-a")} == {"proj-a"}


def test_two_writers_under_the_lock_lose_no_revision(tmp_path):
    errors = []

    def writer(n):
        try:
            [store.add_revision(tmp_path, "vnx-dev", make(doelpad=f"pad/{n}/{i}"), now=T0) for i in range(5)]
        except BaseException as exc:  # reported on the main thread
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(store.proposals(tmp_path, "vnx-dev")) == 10 == len(store.decisions(tmp_path, "vnx-dev"))
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("junk", ["{niet json\n", "[1, 2]\n", '{"decision": "approved"}\n', None])
def test_corrupt_record_file_is_refused_and_left_byte_equal(tmp_path, junk):
    seed(tmp_path)
    path = tmp_path / store.DECISIONS_FILENAME
    path.write_text(path.read_text() * 2 if junk is None else path.read_text() + junk)  # None: proposed twice
    before = path.read_bytes()
    with pytest.raises(store.StoreUnreadable):
        store.proposals(tmp_path, "vnx-dev")
    with pytest.raises(store.StoreUnreadable):
        store.add_revision(tmp_path, "vnx-dev", make("anders"), now=T1)
    assert path.read_bytes() == before


def test_projection_is_rebuilt_from_the_records_and_never_read(tmp_path):
    pid = seed(tmp_path)
    store.decide(tmp_path, "vnx-dev", pid, "rejected", reason="x", decided_by="op", now=T1)
    projection = tmp_path / store.PROJECTION_FILENAME
    good = projection.read_text()
    projection.write_text("{torn")
    store.proposals(tmp_path, "vnx-dev")
    assert projection.read_text() == good


@pytest.mark.parametrize("bad", [{}, make(voorstel=""), make(doelpad="  "), make(oorzaak="insufficient_evidence"),
                                 {**make(), "evidence": "p1"}, "tekst"])
def test_invalid_proposal_is_refused_and_nothing_is_written(tmp_path, bad):
    with pytest.raises(store.ProposalInvalid):
        store.add_revision(tmp_path, "vnx-dev", bad, now=T0)
    assert not (tmp_path / store.DECISIONS_FILENAME).exists()


@pytest.mark.parametrize("project", [None, "", "  ", 3])
def test_project_id_is_required_everywhere(tmp_path, project):
    for call in (lambda: store.add_revision(tmp_path, project, make()), lambda: store.proposals(tmp_path, project)):
        with pytest.raises(store.ProposalInvalid):
            call()


@pytest.mark.parametrize("revision", [0, -1, True, "1"])
@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_a_decision_the_read_path_would_refuse_is_never_appended(tmp_path, revision, decision):
    pid = seed(tmp_path)
    path = tmp_path / store.DECISIONS_FILENAME
    before = path.read_bytes()
    with pytest.raises(store.DecisionRefused):
        store.decide(tmp_path, "vnx-dev", pid, decision, reason="r", decided_by="op", revision=revision, now=T1)
    assert path.read_bytes() == before and store.get(tmp_path, "vnx-dev", pid)["status"] == "pending"


def test_applied_binds_to_the_approved_revision(tmp_path):
    pid = seed(tmp_path)
    store.decide(tmp_path, "vnx-dev", pid, "approved", reason="goed", decided_by="op", revision=1, now=T1)
    seed(tmp_path, voorstel="Tweede revisie")
    path = tmp_path / store.DECISIONS_FILENAME
    before = path.read_bytes()
    for revision in (999, 2):
        with pytest.raises(store.DecisionRefused):
            store.decide(tmp_path, "vnx-dev", pid, "applied", reason="r", decided_by="op", revision=revision, applied_ref="#1")
    assert path.read_bytes() == before and store.get(tmp_path, "vnx-dev", pid)["status"] == "approved"


def test_a_hand_written_line_approving_a_missing_revision_is_refused_on_read(tmp_path):
    pid = seed(tmp_path)
    path = tmp_path / store.DECISIONS_FILENAME
    line = store._record("vnx-dev", pid, 3, "approved", T1, reason="r", decided_by="op")
    path.write_text(path.read_text() + store.json.dumps(line) + "\n")
    with pytest.raises(store.StoreUnreadable):
        store.proposals(tmp_path, "vnx-dev")
