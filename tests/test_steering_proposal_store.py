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


@pytest.mark.parametrize("junk", ["{niet json\n", "[1, 2]\n", '{"decision": "approved"}\n', '{"proposal_id": "torn', None])
def test_corrupt_record_file_is_refused_and_left_byte_equal(tmp_path, junk):  # torn: a half last line without newline
    seed(tmp_path)
    path = tmp_path / store.DECISIONS_FILENAME
    path.write_text(path.read_text() * 2 if junk is None else path.read_text() + junk)  # None: proposed twice
    before = path.read_bytes()
    with pytest.raises(store.StoreUnreadable):
        store.proposals(tmp_path, "vnx-dev")
    with pytest.raises(store.StoreUnreadable):
        store.add_revision(tmp_path, "vnx-dev", make("anders"), now=T1)
    assert path.read_bytes() == before


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


@pytest.mark.parametrize("write, revisions, status", [
    (lambda d, pid: store.add_revision(d, "vnx-dev", make("Tweede tekst"), now=T1), [1, 2], "pending"),
    (lambda d, pid: store.decide(d, "vnx-dev", pid, "rejected", reason="r", decided_by="op", now=T1), [1], "rejected")],
    ids=["add_revision", "decide"])
def test_an_append_after_a_last_line_without_newline_starts_on_a_new_line(tmp_path, write, revisions, status):
    pid = seed(tmp_path)
    path = tmp_path / store.DECISIONS_FILENAME
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    record = write(tmp_path, pid)
    entry = store.get(tmp_path, "vnx-dev", pid)
    assert [r["revision"] for r in entry["revisions"]] == revisions and entry["status"] == status
    assert [store.json.loads(line) for line in path.read_text().splitlines()][1:] == [record]


def test_a_hand_written_line_approving_a_missing_revision_is_refused_on_read(tmp_path):
    pid = seed(tmp_path)
    path = tmp_path / store.DECISIONS_FILENAME
    line = store._record("vnx-dev", pid, 3, "approved", T1, reason="r", decided_by="op")
    path.write_text(path.read_text() + store.json.dumps(line) + "\n")
    with pytest.raises(store.StoreUnreadable):
        store.proposals(tmp_path, "vnx-dev")


T2, T3 = "2026-11-11T10:00:00Z", "2026-11-11T11:00:00Z"


def batch_item(voorstel="Voeg een statusoverzicht toe", ts=T0, prompt_id="p1", doelpad="skills/status"):
    return {**make(voorstel, doelpad=doelpad), "evidence": [{"prompt_id": prompt_id, "ts": ts}]}


def reject(tmp_path, pid):
    store.decide(tmp_path, "vnx-dev", pid, "rejected", reason="nee", decided_by="op", now=T1)


def counts(**kw):
    return {**{name: 0 for name in store.OUTCOMES}, **kw}


def test_a_paraphrase_of_a_rejected_proposal_without_new_evidence_is_suppressed(tmp_path):
    pid = seed(tmp_path)
    reject(tmp_path, pid)
    result = store.reconcile(tmp_path, "vnx-dev", [batch_item("Een ander woord", ts=T0, prompt_id="p9")], now=T3)
    entry = store.get(tmp_path, "vnx-dev", pid)
    assert result == counts(suppressed_rejected=1) and entry["status"] == "rejected"
    assert [r["revision"] for r in entry["revisions"]] == [1, 2]  # ADR-072 decision 14: the revision is kept
    again = store.reconcile(tmp_path, "vnx-dev", [batch_item("Een ander woord", ts=T0, prompt_id="p9")], now=T3)
    assert again == counts(suppressed_rejected=1) and len(store.get(tmp_path, "vnx-dev", pid)["revisions"]) == 2


def test_evidence_after_the_rejection_reopens_with_its_own_record(tmp_path):
    pid = seed(tmp_path)
    reject(tmp_path, pid)
    result = store.reconcile(tmp_path, "vnx-dev", [batch_item("Nieuwe tekst", ts=T2, prompt_id="p2")], now=T3)
    entry = store.get(tmp_path, "vnx-dev", pid)
    assert result == counts(reopened=1) and entry["status"] == "pending" and entry["decided_at"] is None
    kinds = [(r["decision"], r["revision"]) for r in store.decisions(tmp_path, "vnx-dev")]
    assert kinds == [("proposed", 1), ("rejected", 1), ("reopened", 2)]
    assert store.decisions(tmp_path, "vnx-dev")[-1]["evidence_prompt_ids"] == ["p2"]


@pytest.mark.parametrize("ts", ["geen datum", None, 5, "2026-11-11T10:00:00", T1, T0])
def test_a_malformed_or_not_later_ts_never_reopens(tmp_path, ts):
    pid = seed(tmp_path)
    reject(tmp_path, pid)
    result = store.reconcile(tmp_path, "vnx-dev", [batch_item("Nieuwe tekst", ts=ts, prompt_id="p2")], now=T3)
    assert result == counts(suppressed_rejected=1) and store.get(tmp_path, "vnx-dev", pid)["status"] == "rejected"


def test_reopen_only_applies_to_a_rejected_proposal(tmp_path):
    pid = seed(tmp_path)
    path = tmp_path / store.DECISIONS_FILENAME
    before = path.read_bytes()
    with pytest.raises(store.DecisionRefused):
        store.add_revision(tmp_path, "vnx-dev", make("Nieuwe tekst"), reopen=True, now=T2)
    assert path.read_bytes() == before
    reject(tmp_path, pid)
    store.add_revision(tmp_path, "vnx-dev", make("Nieuwe tekst"), reopen=True, now=T2)
    assert store.get(tmp_path, "vnx-dev", pid)["status"] == "pending"


def test_same_content_twice_is_unchanged_and_changed_content_is_a_revision(tmp_path):
    first = store.reconcile(tmp_path, "vnx-dev", [batch_item(), batch_item()], now=T0)
    assert first == counts(added=1, unchanged=1)
    assert store.reconcile(tmp_path, "vnx-dev", [batch_item()], now=T1) == counts(unchanged=1)
    assert store.reconcile(tmp_path, "vnx-dev", [batch_item("Gewijzigd")], now=T2) == counts(revised=1)
    entry = store.proposals(tmp_path, "vnx-dev")[0]
    assert [r["voorstel"] for r in entry["revisions"]] == ["Voeg een statusoverzicht toe", "Gewijzigd"]
    assert len(store.decisions(tmp_path, "vnx-dev")) == 2


@pytest.mark.parametrize("bad", [{}, make(voorstel=""), {**make(), "evidence": "p1"}, "tekst"])
def test_one_invalid_item_writes_nothing_of_the_batch(tmp_path, bad):
    with pytest.raises(store.ProposalInvalid):
        store.reconcile(tmp_path, "vnx-dev", [batch_item(), bad], now=T0)
    assert not (tmp_path / store.DECISIONS_FILENAME).exists() and not (tmp_path / store.PROJECTION_FILENAME).exists()


def test_the_projection_is_rebuilt_from_the_records_and_never_read(tmp_path):
    pid = seed(tmp_path)
    projection = tmp_path / store.PROJECTION_FILENAME
    good = projection.read_text()
    assert store.json.loads(good)["proposals"][0]["proposal_id"] == pid
    for torn in ('{"schema": "steering_proposals/1", "proposals": [', "", '{"proposals": []}'):
        projection.write_text(torn)
        assert store.reconcile(tmp_path, "vnx-dev") == counts()
        assert projection.read_text() == good
    projection.write_text("kapot")
    assert store.get(tmp_path, "vnx-dev", pid)["status"] == "pending" and projection.read_text() == good
    projection.unlink()
    store.decide(tmp_path, "vnx-dev", pid, "rejected", reason="nee", decided_by="op", now=T1)
    assert store.json.loads(projection.read_text())["proposals"][0]["status"] == "rejected"
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("decision", ["approved", "applied"])
def test_a_new_revision_of_an_approved_or_applied_proposal_keeps_status_and_binding(tmp_path, decision):
    pid = seed(tmp_path)
    store.decide(tmp_path, "vnx-dev", pid, "approved", reason="ok", decided_by="op", now=T1)
    if decision == "applied":
        store.decide(tmp_path, "vnx-dev", pid, "applied", reason="ok", decided_by="op", now=T2, applied_ref="PR #1")
    assert store.reconcile(tmp_path, "vnx-dev", [batch_item("Tweede tekst", ts=T3)], now=T3) == counts(revised=1)
    entry = store.get(tmp_path, "vnx-dev", pid)
    assert entry["status"] == decision and entry["decided_revision"] == 1
    assert [r["revision"] for r in entry["revisions"]] == [1, 2]
    assert entry["revisions"][0]["voorstel"] == "Voeg een statusoverzicht toe"


@pytest.mark.parametrize("ids", ["p1", ["p1", 3], ["p1", ""], ["p1", None], 7, ("p1", 2)])
def test_evidence_prompt_ids_is_a_list_of_non_empty_strings(tmp_path, ids):
    with pytest.raises(store.ProposalInvalid):
        store.add_revision(tmp_path, "vnx-dev", make(), evidence_prompt_ids=ids, now=T0)
    assert not (tmp_path / store.DECISIONS_FILENAME).exists()
    seed(tmp_path)
    path = tmp_path / store.DECISIONS_FILENAME
    path.write_text(path.read_text().replace('"evidence_prompt_ids": ["p1"]', '"evidence_prompt_ids": "p1"'))
    with pytest.raises(store.StoreUnreadable):
        store.proposals(tmp_path, "vnx-dev")
