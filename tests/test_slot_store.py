"""RS-2: slot_store rights checks, policy read and doctor (all on tmp_path, platform-free)."""
import copy
import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from slot.slot_store import SlotError, SlotStore

ACL = " 0: group:vnxslot allow list,add_file,search,delete_child,file_inherit,directory_inherit"
POLICY = {"total": 10, "reserved": 2, "runner_reserve": 1, "max_bypass": 3, "max_transition": 600, "max_queue": 20,
          "mode": "observe", "enforce_activation": {},
          "memory": {"swap_growth_mb": 256, "swap_level_mb": 2048, "compressor_gb": 8},
          "profiles": {"runner": {"slots": 1, "max_slots": 8, "max_wait_s": 1800, "heavy": False, "children": ["gate"]},
                       "gate": {"slots": 2, "max_slots": 4, "max_wait_s": 900, "heavy": True, "children": []}}}
STATE = {"holders": {}, "queue": [], "samples": [], "mode": "observe", "fencing_counter": 0, "transition": None,
         "shadow": {"holders": {}, "queue": []}, "memory_recovery": {"blocked": False, "clean_buckets": []}}


def rights(uid=501, group="vnxslot", mode=0o2770, acl=ACL):
    return {"uid": uid, "group": group, "mode": mode, "acl": acl}


@pytest.fixture
def env(tmp_path):
    store, pdir = tmp_path / "store", tmp_path / "policy"
    store.mkdir(), pdir.mkdir()
    (pdir / "host.json").write_text(json.dumps(POLICY))
    table = {store: rights(), pdir: rights(mode=0o755, acl=""), pdir / "host.json": rights(mode=0o644, acl="")}
    st = SlotStore(store, pdir / "host.json", stat_fn=lambda p: dict(table[p]))
    return st, table, store, pdir


def test_valid_setup_and_initialized_state_pass_doctor(env):
    st, *_ = env
    st.initialize_state()
    assert st.doctor() == (0, [])


@pytest.mark.parametrize("key,patch", [
    ("store", {"acl": ""}), ("store", {"mode": 0o2771}), ("store", {"group": "staff"})])
def test_doctor_78_for_bad_store(env, key, patch):
    st, table, store, _ = env
    table[store].update(patch)
    code, reasons = st.doctor()
    assert code == 78 and any(r.startswith("store_invalid") for r in reasons)


@pytest.mark.parametrize("target,patch", [
    ("file", {"mode": 0o664}), ("dir", {"mode": 0o775}), ("file", {"acl": " 0: user:x allow write"}),
    ("dir", {"acl": " 0: group:vnxslot allow list"}), ("file", {"uid": 777}), ("dir", {"uid": 777})])
def test_doctor_78_for_bad_policy_rights(env, target, patch):
    st, table, _, pdir = env
    table[pdir if target == "dir" else pdir / "host.json"].update(patch)
    code, reasons = st.doctor()
    assert code == 78 and any(r.startswith("policy_invalid") for r in reasons)


EXTRA = " 1: {} {} list,add_file,search,delete_child,file_inherit,directory_inherit"


@pytest.mark.parametrize("who", ["everyone", "user:other", "group:staff", "group:vnxslot2"])
def test_foreign_allow_acl_is_refused(env, who):
    st, table, store, _ = env
    table[store]["acl"] = ACL + "\n" + EXTRA.format(who, "allow")
    assert any(r == f"store_invalid: ACL grant for {who}" for r in st.check_store())


def test_deny_acl_is_accepted_and_unparsable_line_is_refused(env):
    st, table, store, _ = env
    table[store]["acl"] = ACL + "\n" + EXTRA.format("everyone", "deny")
    assert st.check_store() == []
    table[store]["acl"] = ACL + "\n garbage line"
    assert "store_invalid: unparsable ACL line" in st.check_store()


@pytest.mark.parametrize("children", [[{}], [1], [None]])
def test_non_string_child_is_policy_invalid_and_doctor_78(env, children):
    st, _, store, pdir = env
    bad = copy.deepcopy(POLICY)
    bad["profiles"]["gate"]["children"] = children
    (pdir / "host.json").write_text(json.dumps(bad))
    with pytest.raises(SlotError) as exc:
        st.read_policy()
    assert str(exc.value) == "policy_invalid: profile 'gate' has a non-string child"
    assert st.doctor()[0] == 78


def test_doctor_78_for_unreadable_host_json(env):
    st, _, _, pdir = env
    (pdir / "host.json").chmod(0)
    if os.access(pdir / "host.json", os.R_OK):
        (pdir / "host.json").write_text("{not json")
    assert any(r.startswith("policy_invalid") for r in st.doctor()[1])
    (pdir / "host.json").unlink()
    assert st.doctor()[0] == 78


def test_rights_seam_failure_is_a_refusal(env):
    st, *_ = env
    st._stat = lambda p: (_ for _ in ()).throw(OSError("no ls"))
    assert st.doctor()[0] == 78


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(mode="auto"), lambda p: p.update(total=True), lambda p: p["profiles"]["gate"].update(children=["ghost"]),
    lambda p: p.update(reserved=9, runner_reserve=1), lambda p: p["profiles"]["gate"].update(slots=9),
    lambda p: p.pop("memory"), lambda p: p["profiles"]["gate"].update(heavy="yes")])
def test_invalid_policy_fields_are_policy_invalid(env, mutate):
    st, _, _, pdir = env
    bad = copy.deepcopy(POLICY)
    mutate(bad)
    (pdir / "host.json").write_text(json.dumps(bad))
    with pytest.raises(SlotError) as exc:
        st.read_policy()
    assert exc.value.code == "policy_invalid" and exc.value.exit_code == 78


@pytest.mark.parametrize("raw", ["1e999", "-1e999", "Infinity", "NaN"])
def test_non_finite_memory_threshold_is_policy_invalid(env, raw):
    st, _, _, pdir = env
    text = json.dumps(POLICY).replace('"swap_level_mb": 2048', '"swap_level_mb": ' + raw)
    assert raw in text
    (pdir / "host.json").write_text(text)
    with pytest.raises(SlotError) as exc:
        st.read_policy()
    assert exc.value.code == "policy_invalid" and exc.value.exit_code == 78
    assert st.doctor()[0] == 78


def test_env_budget_never_changes_the_policy_budget(env, monkeypatch):
    st, *_ = env
    monkeypatch.setenv("VNX_SLOT_TOTAL", "99")
    monkeypatch.setenv("VNX_SLOT_MODE", "off")
    policy = st.read_policy()
    assert policy["total"] == 10 and policy["mode"] == "observe"


@pytest.mark.parametrize("content", ["{broken", "[]", json.dumps({"holders": {}})])
def test_corrupt_state_is_state_corrupt_never_empty(env, content):
    st, _, store, _ = env
    (store / "state.json").write_text(content)
    with pytest.raises(SlotError) as exc:
        st.read_state()
    assert exc.value.code == "state_corrupt"
    with pytest.raises(SlotError):
        with st.transaction():
            pass
    assert (store / "state.json").read_text() == content
    assert st.doctor()[0] == 78


def test_missing_state_is_not_an_empty_store(env):
    st, *_ = env
    with pytest.raises(SlotError) as exc:
        st.read_state()
    assert exc.value.code == "state_corrupt"


def test_state_fixture_is_a_valid_state(env):
    st, _, store, _ = env
    (store / "state.json").write_text(json.dumps(STATE))
    assert st.read_state() == STATE and st.doctor() == (0, [])


def test_initialize_refuses_over_existing_state(env):
    st, _, store, _ = env
    st.initialize_state()
    before = (store / "state.json").read_bytes()
    with pytest.raises(SlotError):
        st.initialize_state()
    assert (store / "state.json").read_bytes() == before


def test_exception_in_transaction_leaves_state_byte_identical(env):
    st, _, store, _ = env
    st.initialize_state()
    before = (store / "state.json").read_bytes()
    with pytest.raises(RuntimeError):
        with st.transaction() as state:
            state["fencing_counter"] = 5
            raise RuntimeError("boom")
    assert (store / "state.json").read_bytes() == before
    assert [p.name for p in store.iterdir() if p.name.endswith(".tmp")] == []


def test_clean_transaction_persists_and_files_are_0660_under_umask_022(env):
    st, _, store, _ = env
    old = os.umask(0o022)
    try:
        st.initialize_state()
        with st.transaction() as state:
            state["fencing_counter"] = 7
    finally:
        os.umask(old)
    assert st.read_state()["fencing_counter"] == 7
    for name in ("state.json", "ledger.lock"):
        assert stat.S_IMODE((store / name).stat().st_mode) == 0o660


def test_transaction_refuses_on_wrong_rights(env):
    st, table, store, _ = env
    st.initialize_state()
    before = (store / "state.json").read_bytes()
    table[store]["mode"] = 0o2777
    with pytest.raises(SlotError) as exc:
        with st.transaction():
            pass
    assert exc.value.code == "store_invalid"
    assert (store / "state.json").read_bytes() == before


def test_state_rewrite_with_invalid_shape_is_refused(env):
    st, _, store, _ = env
    st.initialize_state()
    before = (store / "state.json").read_bytes()
    with pytest.raises(SlotError):
        with st.transaction() as state:
            state["holders"] = None
    assert (store / "state.json").read_bytes() == before


def test_initial_state_follows_policy_mode(env):
    st, _, store, pdir = env
    off = dict(POLICY, mode="off")
    (pdir / "host.json").write_text(json.dumps(off))
    st.initialize_state()
    assert st.read_state()["mode"] == "off"
