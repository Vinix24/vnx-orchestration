"""Shared content-boundary classifier (scripts/lib/content_class.py).

Every root, HOME, the boundary file and the project registry point at tmp_path. No test reads
the real ~/.vnx, ~/.claude or ~/Personal.
"""

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))

import content_class as cc  # noqa: E402


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    client_root = home / "BUSINESS" / "clients"
    personal_root = home / "Personal"
    for d in (client_root / "acme", personal_root / "health", home / "dev" / "proj-a",
              home / "dev" / "pa-engine", home / "dev" / "plain", home / "notes"):
        d.mkdir(parents=True)
    (home / "dev" / "proj-a" / ".vnx-project-id").write_text("proj-a\n")
    (home / "dev" / "pa-engine" / ".vnx-project-id").write_text("pacompany-engine\n")
    (client_root / ".vnx-project-id").write_text("vincent-clients\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    token = tmp_path / "canary.token"
    token.write_text("CANARYtoken0123456789ab\n")
    boundary_file = tmp_path / "content_boundary.json"
    boundary_file.write_text(json.dumps({
        "version": 1,
        "client_roots": [str(client_root)],
        "personal_roots": [str(personal_root), "~/Desktop/Lifestyle"],
        "client_project_ids": ["pacompany-engine"],
        "provider_exceptions": {"pacompany-engine": ["DeepSeek"]},
        "canary_token_file": str(token),
        "canary_armed_in": [str(personal_root / "canary.md")],
    }))
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"projects": [
        {"project_id": "reg-proj", "path": str(home / "dev" / "plain")},
        {"project_id": "pacompany-engine", "path": str(home / "dev" / "pa-registered")},
    ]}))
    (home / "dev" / "pa-registered").mkdir()
    return {
        "home": home, "client": client_root, "personal": personal_root,
        "boundary": cc.load_boundary(boundary_file), "boundary_file": boundary_file,
        "registry": registry, "token": "CANARYtoken0123456789ab",
    }


def _classify(world, path):
    return cc.classify_path(path, world["boundary"], world["registry"])


# --- load_boundary ---------------------------------------------------------------------------

def test_missing_file_is_an_explicit_unconfigured_state(tmp_path):
    boundary = cc.load_boundary(tmp_path / "nope.json")
    assert boundary.configured is False
    assert boundary.client_roots == ()


def test_unreadable_and_unsupported_files_are_unconfigured_not_exceptions(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert cc.load_boundary(bad).configured is False
    wrong = tmp_path / "v2.json"
    wrong.write_text(json.dumps({"version": 2, "client_roots": ["/x"]}))
    assert cc.load_boundary(wrong).configured is False


def test_env_var_points_the_default_path_at_a_test_file(world, monkeypatch):
    monkeypatch.setenv(cc.BOUNDARY_ENV, str(world["boundary_file"]))
    assert cc.load_boundary().configured is True


def test_short_canary_token_is_ignored(world, tmp_path):
    short = tmp_path / "short.token"
    short.write_text("abc123\n")
    data = json.loads(world["boundary_file"].read_text())
    data["canary_token_file"] = str(short)
    f = tmp_path / "b2.json"
    f.write_text(json.dumps(data))
    assert cc.load_boundary(f).canary_token is None


# --- classify_path ---------------------------------------------------------------------------

def test_personal_and_client_roots_win(world):
    assert _classify(world, world["personal"] / "health").cls == cc.PERSONAL
    assert _classify(world, world["client"] / "acme").cls == cc.CLIENT
    assert _classify(world, world["client"] / "acme").source == cc.SRC_CLIENT_ROOT


def test_legacy_personal_root_is_expanded_from_home(world):
    legacy = world["home"] / "Desktop" / "Lifestyle" / "x"
    assert _classify(world, legacy).cls == cc.PERSONAL


def test_roots_compare_components_not_string_prefixes(world):
    sibling = world["home"] / "BUSINESS" / "clients-old" / "x"
    sibling.mkdir(parents=True)
    assert _classify(world, sibling).cls == cc.OWN


def test_deleted_directory_still_classifies_through_its_deepest_ancestor(world):
    gone = world["client"] / "removed-client" / "deep" / "dir"
    assert not gone.exists()
    assert _classify(world, gone).cls == cc.CLIENT


def test_symlink_into_a_restricted_root_is_client(world):
    link = world["home"] / "dev" / "link-to-client"
    link.symlink_to(world["client"] / "acme")
    assert _classify(world, link).cls == cc.CLIENT


def test_marker_makes_a_fabric_project_and_carries_its_id(world):
    origin = _classify(world, world["home"] / "dev" / "proj-a" / "src" / "deeper")
    assert origin == cc.Origin(cc.FABRIC, "proj-a", cc.SRC_MARKER)


def test_marker_is_read_from_the_file_never_from_the_env(world, monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
    assert _classify(world, world["home"] / "dev" / "proj-a").project_id == "proj-a"


def test_client_project_id_marker_is_client_through_the_project_id(world):
    origin = _classify(world, world["home"] / "dev" / "pa-engine")
    assert origin == cc.Origin(cc.CLIENT, "pacompany-engine", cc.SRC_CLIENT_PROJECT_ID)


def test_registry_path_is_a_fabric_project(world):
    origin = _classify(world, world["home"] / "dev" / "plain" / "sub")
    assert origin == cc.Origin(cc.FABRIC, "reg-proj", cc.SRC_REGISTRY)


def test_registry_entry_for_a_client_project_id_is_client(world):
    origin = _classify(world, world["home"] / "dev" / "pa-registered")
    assert origin.cls == cc.CLIENT and origin.source == cc.SRC_CLIENT_PROJECT_ID


def test_registry_is_read_tolerantly(world, tmp_path):
    for content in ("not json", json.dumps(["list"]), json.dumps({"projects": ["x", {"path": 3}]})):
        reg = tmp_path / "r.json"
        reg.write_text(content)
        origin = cc.classify_path(world["home"] / "dev" / "plain", world["boundary"], reg)
        assert origin.cls == cc.OWN


def test_below_home_is_own_and_home_itself_is_unknown(world):
    assert _classify(world, world["home"] / "notes").cls == cc.OWN
    assert _classify(world, world["home"]).cls == cc.UNKNOWN


def test_none_root_and_tmp_are_unknown(world, tmp_path):
    assert _classify(world, None).cls == cc.UNKNOWN
    assert _classify(world, "").cls == cc.UNKNOWN
    assert _classify(world, "/").cls == cc.UNKNOWN
    assert _classify(world, tmp_path / "scratch").cls == cc.UNKNOWN


def test_root_hit_also_reports_the_marker_id_but_not_as_the_deciding_source(world):
    nested = world["client"] / "acme" / "build" / "pa-engine"
    nested.mkdir(parents=True)
    (nested / ".vnx-project-id").write_text("pacompany-engine\n")
    origin = _classify(world, nested)
    assert origin.cls == cc.CLIENT
    assert origin.project_id == "pacompany-engine"
    assert origin.source == cc.SRC_CLIENT_ROOT
    assert not cc.exception_applies(origin, "deepseek", world["boundary"])


# --- classify_text ---------------------------------------------------------------------------

def test_text_with_absolute_client_path_is_client(world):
    text = f"please read {world['client']}/acme/notes.md and summarise"
    hit = cc.classify_text(text, world["boundary"])
    assert hit == cc.Origin(cc.CLIENT, None, cc.SRC_TEXT_PATH)


def test_text_with_tilde_form_path_is_restricted(world):
    assert cc.classify_text("cat ~/Personal/health/log.md", world["boundary"]).cls == cc.PERSONAL
    assert cc.classify_text("see ~/BUSINESS/clients/acme/a.md.", world["boundary"]).cls == cc.CLIENT


def test_root_alone_or_glob_is_not_a_concrete_path(world):
    b = world["boundary"]
    assert cc.classify_text(f"scan {world['client']}", b) is None
    assert cc.classify_text(f"scan {world['client']}/", b) is None
    assert cc.classify_text(f"scan {world['client']}/*/notes", b) is None
    assert cc.classify_text("rg foo ~/Personal/**", b) is None


def test_sibling_directory_name_is_not_a_hit(world):
    assert cc.classify_text(f"{world['client']}-old/x/notes.md", world["boundary"]) is None


def test_personal_outranks_client_in_one_text(world):
    text = f"{world['client']}/a/b and {world['personal']}/health/c"
    assert cc.classify_text(text, world["boundary"]).cls == cc.PERSONAL


def test_canary_token_in_text_is_restricted(world):
    hit = cc.classify_text(f"leak {world['token']} here", world["boundary"])
    assert hit is not None and hit.source == cc.SRC_CANARY


def test_plain_text_and_unconfigured_boundary_give_no_hit(world, tmp_path):
    assert cc.classify_text("just a dev message", world["boundary"]) is None
    assert cc.classify_text(None, world["boundary"]) is None
    unconfigured = cc.load_boundary(tmp_path / "missing.json")
    assert cc.classify_text(f"{world['client']}/acme/x", unconfigured) is None


# --- ordering and exceptions -----------------------------------------------------------------

def test_most_restrictive_ordering():
    assert cc.most_restrictive([cc.FABRIC, cc.OWN]) == cc.OWN
    assert cc.most_restrictive([cc.OWN, cc.UNKNOWN]) == cc.UNKNOWN
    assert cc.most_restrictive([cc.UNKNOWN, cc.CLIENT, cc.FABRIC]) == cc.CLIENT
    assert cc.most_restrictive([cc.CLIENT, cc.PERSONAL]) == cc.PERSONAL
    assert cc.most_restrictive([]) == cc.UNKNOWN
    assert cc.most_restrictive([None, cc.OWN]) == cc.OWN


def test_restricted_sets():
    assert cc.DISPATCH_RESTRICTED == {cc.CLIENT, cc.PERSONAL}
    assert cc.ANALYZER_RESTRICTED == {cc.CLIENT, cc.PERSONAL, cc.UNKNOWN}


def test_allowed_exception_providers(world):
    b = world["boundary"]
    assert cc.allowed_exception_providers("pacompany-engine", b) == frozenset({"deepseek"})
    assert cc.allowed_exception_providers("seocrawler-v2", b) == frozenset()
    assert cc.allowed_exception_providers(None, b) == frozenset()
    assert cc.allowed_exception_providers("pacompany-engine", cc.load_boundary(Path("/nope"))) == frozenset()


def test_allowed_exception_providers_reads_the_env_boundary_by_default(world, monkeypatch):
    monkeypatch.setenv(cc.BOUNDARY_ENV, str(world["boundary_file"]))
    assert cc.allowed_exception_providers("pacompany-engine") == frozenset({"deepseek"})


def test_exception_never_covers_glm_or_kimi(world):
    origin = cc.Origin(cc.CLIENT, "pacompany-engine", cc.SRC_CLIENT_PROJECT_ID)
    b = world["boundary"]
    assert cc.exception_applies(origin, "deepseek", b)
    assert not cc.exception_applies(origin, "glm", b)
    assert not cc.exception_applies(origin, "kimi", b)
    assert not cc.exception_applies(cc.Origin(cc.CLIENT, "pacompany-engine", cc.SRC_CLIENT_ROOT),
                                    "deepseek", b)


# --- registry fallback: the most specific entry decides (fix-forward round 5 on PR #2032) ------

def _registry(world, entries, client_ids=("client-x-id",)):
    """Write a registry of (project_id, path) pairs and a boundary that knows the client ids."""
    world["registry"].write_text(json.dumps({"projects": [
        {"project_id": pid, "path": str(path)} for pid, path in entries]}))
    world["boundary_file"].write_text(json.dumps({
        "version": 1,
        "client_roots": [str(world["client"])],
        "personal_roots": [str(world["personal"])],
        "client_project_ids": list(client_ids),
    }))
    world["boundary"] = cc.load_boundary(world["boundary_file"])


def _nested(world):
    broad = world["home"] / "dev" / "broad"
    nested = broad / "client-x"
    (nested / "src").mkdir(parents=True)
    (broad / "other").mkdir()
    return broad, nested


def test_g1_nested_client_checkout_wins_over_a_broad_entry_listed_first(world):
    broad, nested = _nested(world)
    _registry(world, [("proj-broad", broad), ("client-x-id", nested)])
    origin = _classify(world, nested / "src")
    assert (origin.cls, origin.project_id, origin.source) == (
        cc.CLIENT, "client-x-id", cc.SRC_CLIENT_PROJECT_ID)


def test_g2_file_order_does_not_matter(world):
    broad, nested = _nested(world)
    _registry(world, [("client-x-id", nested), ("proj-broad", broad)])
    origin = _classify(world, nested / "src")
    assert (origin.cls, origin.project_id, origin.source) == (
        cc.CLIENT, "client-x-id", cc.SRC_CLIENT_PROJECT_ID)


def test_g3_path_below_the_broad_entry_only_stays_fabric(world):
    broad, nested = _nested(world)
    _registry(world, [("proj-broad", broad), ("client-x-id", nested)])
    origin = _classify(world, broad / "other")
    assert (origin.cls, origin.project_id, origin.source) == (
        cc.FABRIC, "proj-broad", cc.SRC_REGISTRY)


def test_g4_same_path_with_a_client_id_listed_second_is_client(world):
    broad, _ = _nested(world)
    _registry(world, [("proj-broad", broad), ("client-x-id", broad)])
    origin = _classify(world, broad / "other")
    assert (origin.cls, origin.project_id, origin.source) == (
        cc.CLIENT, "client-x-id", cc.SRC_CLIENT_PROJECT_ID)


def test_g4b_same_path_with_two_fabric_ids_gives_the_first(world):
    broad, _ = _nested(world)
    _registry(world, [("proj-first", broad), ("proj-second", broad)])
    origin = _classify(world, broad / "other")
    assert (origin.cls, origin.project_id, origin.source) == (
        cc.FABRIC, "proj-first", cc.SRC_REGISTRY)


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 1, 0), (1, 2, 0), (1, 0, 2)])
def test_g5_three_levels_the_deepest_containing_entry_decides(world, order):
    dev = world["home"] / "dev"
    broad = dev / "broad"
    deep = broad / "client-x"
    (deep / "src").mkdir(parents=True)
    levels = [("proj-dev", dev), ("proj-broad", broad), ("client-x-id", deep)]
    _registry(world, [levels[i] for i in order])
    assert _classify(world, dev / "plain").project_id == "proj-dev"
    assert _classify(world, broad).project_id == "proj-broad"
    assert _classify(world, deep / "src").project_id == "client-x-id"

