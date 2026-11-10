"""Capture PR-3, split 1: the window, the pairs, the disposition and the anchor rule.

Fixtures live under tmp_path with HOME pointing there (never ~/.claude or ~/.vnx-data).
The bins are read with the real capture_sources readers; the TZ cases run in a subprocess.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import capture_audit as ca
import capture_sources as cs

SINCE, UNTIL = "2026-11-09T12:00:00Z", "2026-11-10T12:00:00Z"
NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
WINDOW = ca.parse_window(SINCE, UNTIL, chain_until=None, now=NOW)
NEW, OLD = "2026-11-10T08:00:00+00:00", "2026-10-01T08:00:00+00:00"
OI_TITLE = "merge lock blijft achter na afgebroken lus"
NOTE_DESC = "afgebroken merge lus laat lock achter"
_TZ_PROBE = """import sys
sys.path[:0] = [sys.argv[1] + '/scripts/lib', sys.argv[1] + '/scripts']
from datetime import datetime, timezone
import capture_audit as ca, capture_sources as cs
items, state, _ = cs.read_open_items(sys.argv[2])
w = ca.parse_window(sys.argv[3], sys.argv[4], chain_until=None, now=datetime.now(timezone.utc))
raw = {ca.OI: (items, cs.state_of(state), {})}
print(ca.audit_model(raw, w, project_id='p', dismissed_ids=frozenset())['bins']['open_items']['new'])
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    d = {"home": home, "state": home / ".vnx-data" / "proj-a" / "state",
         "memory": home / ".claude" / "projects" / "-proj-a" / "memory", "bucket": home / "bucket"}
    for key in ("state", "memory", "bucket"):
        d[key].mkdir(parents=True)
    (d["state"] / cs.BINDING_FILENAME).write_text(json.dumps({
        "schema": cs.BINDING_SCHEMA, "project_id": "proj-a", "memory_dir": str(d["memory"]),
        "bucket_dir": str(d["bucket"]), "bucket_slug": "mine", "bound_at": NEW}))
    _items(d)
    _nugget(d, "2026-10-01", "koffie-zetten-ochtend")
    return d


def _items(d, *items):
    rows = [{"id": i, "status": "open", "title": t, "created_at": c} for i, t, c in items]
    (d["state"] / "open_items.json").write_text(json.dumps({"items": rows}))


def _note(d, key, desc, body="", modified=NEW, name=None):
    front = f"name: {name or key}\ndescription: {desc}\n" + (f"modified: {modified}\n" if modified else "")
    (d["memory"] / f"{key}.md").write_text(f"---\n{front}---\n{body}\n")


def _nugget(d, day, title, body="- x"):
    with (d["bucket"] / f"{day[:7]}.md").open("a") as fh:
        fh.write(f"### {day} · mine · {title}\n{body}\n\n")


def _raw(d):
    """The three bins as the capture_sources readers hand them over."""
    binding, status = cs.load_binding(d["state"])
    if binding is None:
        return {b: ([], cs.state_of(status), {}) for b in ca.BINS}
    items, oi_state, _ = cs.read_open_items(d["state"])
    notes, mem_state = cs.read_memory_notes(binding)
    entries, nug_state, _ = cs.read_bucket_entries(binding, ["2026-10", "2026-11"])
    return {ca.OI: (items, cs.state_of(oi_state), {}), ca.MEM: (notes, cs.state_of(mem_state), {}),
            ca.NUG: (entries, cs.state_of(nug_state), {})}


def _model(d, dismissed=frozenset(), project="proj-a", window=WINDOW):
    return ca.audit_model(_raw(d), window, project_id=project, dismissed_ids=dismissed)


def test_new_note_with_older_unreferenced_oi_is_one_open_window_pair(env):
    _items(env, ("OI-7", OI_TITLE, OLD))
    _note(env, "merge-lock-note", NOTE_DESC)
    model = _model(env)
    assert (model["anchors_total"], model["open_pairs"], model["backlog_open_pairs"]) == (1, 1, 0)
    [pair] = model["pairs"]
    assert (pair["kind"], pair["oi_id"], pair["anchor_bin"], pair["disposition"], pair["in_window"]) \
        == ("oi_mem", "OI-7", "memory", "open", True)


@pytest.mark.parametrize("created,since,until,new", [
    ("2026-11-10T00:30:00", "2026-11-09T23:00:00Z", "2026-11-10T00:00:00Z", 1),  # CET: 23:30Z, as UTC outside
    ("2026-11-09T23:30:00", "2026-11-09T23:00:00Z", "2026-11-10T00:00:00Z", 0),  # CET: 22:30Z, as UTC inside
    ("2026-07-01T01:30:00", "2026-06-30T23:00:00Z", "2026-07-01T00:00:00Z", 1),  # CEST: 23:30Z, +01:00 outside
    ("2026-06-30T23:30:00", "2026-06-30T23:00:00Z", "2026-07-01T00:00:00Z", 0)])  # CEST: 21:30Z, as UTC inside
def test_naive_created_at_is_host_local_time(env, created, since, until, new):
    _items(env, ("OI-1", "los", created))
    proc_env = {**os.environ, "HOME": str(env["home"]), "TZ": "Europe/Amsterdam"}
    proc = subprocess.run([sys.executable, "-c", _TZ_PROBE, str(_REPO_ROOT), str(env["state"]), since, until],
                          capture_output=True, text=True, env=proc_env, cwd=str(env["home"]))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(new)


def test_parse_window_sources():
    assert ca.parse_window(SINCE, UNTIL, chain_until=None, now=NOW) == ca.Window(NOW - timedelta(hours=24), NOW, "explicit")
    prev = NOW - timedelta(hours=3)
    assert ca.parse_window(None, None, chain_until=prev, now=NOW) == ca.Window(prev, NOW, "chain")
    assert ca.parse_window(None, None, chain_until=None, now=NOW) == ca.Window(NOW - timedelta(hours=24), NOW, "first24h")


@pytest.mark.parametrize("since,until,chain,now", [
    (SINCE, None, None, NOW), ("2026-11-09T12:00:00", UNTIL, None, NOW), (UNTIL, SINCE, None, NOW),
    (SINCE, SINCE, None, NOW), ("gisteren", UNTIL, None, NOW), (5, UNTIL, None, NOW),
    (None, None, NOW + timedelta(seconds=1), NOW), (None, None, datetime(2026, 11, 9), NOW),
    (None, None, None, datetime(2026, 11, 10))])
def test_parse_window_refuses(since, until, chain, now):
    with pytest.raises(ValueError):
        ca.parse_window(since, until, chain_until=chain, now=now)


def test_tokens_jaccard_and_thresholds():
    assert (ca.TOKEN_MIN_LEN, ca.JACCARD_MIN, ca.SHARED_MIN) == (4, 0.34, 2)
    assert ca.tokens("Merge-lus OI-12: Lock2x réview") == {"merge", "lock", "réview"}
    assert ca.jaccard_pair(frozenset("abc"), frozenset("abd")) == (0.5, 2)
    assert ca.jaccard_pair(frozenset(), frozenset()) == (0.0, 0)
    oi = [{"id": i, "title_tokens_input": "alpha beta gamma delta"} for i in ("OI-1", "OI-2")]
    oi.append({"id": "OI-3", "title_tokens_input": "alpha"})
    mem = [{"key": "third", "name": "alpha beta epsilon zeta"},  # shared 2, jaccard 2/6 < 0.34
           {"key": "one", "name": "alpha"},  # shared 1 with OI-3, jaccard 1.0
           {"key": "pair", "name": "alpha beta", "description": "epsilon"}]  # shared 2, jaccard 2/5
    found = {(p.kind, p.home["id"], p.other["key"]) for p in ca.find_pairs(oi, mem, [], project_id="p")}
    assert found == {("oi_mem", "OI-1", "pair"), ("oi_mem", "OI-2", "pair")}


def test_note_without_modified_uses_mtime_and_says_so(env):
    _items(env, ("OI-7", OI_TITLE, OLD))
    _note(env, "merge-lock-note", NOTE_DESC, modified=None)
    stamp = datetime(2026, 11, 10, 8, tzinfo=timezone.utc).timestamp()
    os.utime(env["memory"] / "merge-lock-note.md", (stamp, stamp))
    [pair] = _model(env)["pairs"]
    assert (pair["anchor_bin"], pair["modified_source"], pair["in_window"]) == ("memory", "mtime", True)


def test_pair_with_only_old_records_is_backlog_not_window(env):
    _items(env, ("OI-7", OI_TITLE, OLD), ("OI-8", "nieuw los item", NEW))
    _note(env, "merge-lock-note", NOTE_DESC, modified=OLD)  # the frontmatter wins over a fresh mtime
    model = _model(env)
    assert (model["anchors_total"], model["open_pairs"], model["backlog_open_pairs"]) == (1, 0, 1)
    assert (model["pairs"][0]["in_window"], model["pairs"][0]["anchor_bin"]) == (False, None)


def test_no_new_record_is_an_empty_measurement_not_zero(env):
    _items(env, ("OI-7", OI_TITLE, OLD))
    _note(env, "merge-lock-note", NOTE_DESC, modified=OLD)
    model = _model(env)
    assert (model["anchors_total"], model["open_pairs"], model["backlog_open_pairs"]) == (0, None, 1)


@pytest.mark.parametrize("oi_at,day,new", [  # chain [SINCE, UNTIL) [UNTIL, UNTIL+1d); (oi, nugget) new per window
    (SINCE, "2026-11-09", ((1, 1), (0, 0))),  # the first window's since day
    (UNTIL, "2026-11-10", ((0, 0), (1, 1))),  # the boundary falls mid-day on D: one window, never both
    ("2026-11-11T12:00:00Z", "2026-11-11", ((0, 0), (0, 0))),  # the last window's until day: a later window
    (OLD, "2026-11-08", ((0, 0), (0, 0)))])
def test_window_borders_count_once_in_a_chain(env, oi_at, day, new):
    _items(env, ("OI-1", "los", oi_at))
    _nugget(env, day, "los")
    chain = (WINDOW, ca.parse_window(None, None, chain_until=NOW, now=NOW + timedelta(days=1)))
    assert tuple(tuple(_model(env, window=w)["bins"][b]["new"] for b in (ca.OI, ca.NUG)) for w in chain) == new


@pytest.mark.parametrize("oi_extra,note_body,nug_body,expected", [
    ("", "zie OI-7", "- x", {"oi_mem": "linked", "oi_nug": "open", "mem_nug": "open"}),
    (" [[merge-lock-note]]", "", "- x", {"oi_mem": "open", "oi_nug": "open", "mem_nug": "open"}),  # home side only
    ("", "zie OI-70", "- OI-7 en [[merge-lock-naam]]", {"oi_mem": "open", "oi_nug": "linked", "mem_nug": "linked"})])
def test_linked_only_when_the_non_home_record_carries_the_home_key(env, oi_extra, note_body, nug_body, expected):
    _items(env, ("OI-7", OI_TITLE + oi_extra, OLD))
    _note(env, "merge-lock-note", NOTE_DESC, body=note_body, name="merge-lock-naam")
    _nugget(env, "2026-11-10", "merge-lock-blijft-achter", nug_body)
    assert {p["kind"]: p["disposition"] for p in _model(env)["pairs"]} == expected


@pytest.mark.parametrize("refs", [None, "OI-1", {"oi": "OI-17"}, {"oi": None}, {"oi": 5}, {}])
def test_malformed_refs_never_link(refs):
    pair = ca.Pair("0" * 16, "oi_mem", {"id": "OI-1"}, {"body_refs": refs}, 1.0, 2)
    assert ca.disposition(pair, frozenset()) == "open"
    assert ca.disposition(pair, frozenset({"0" * 16})) == "dismissed"


def test_dismissed_pair_stays_dismissed_but_a_link_wins(env):
    _items(env, ("OI-7", OI_TITLE, OLD))
    _note(env, "merge-lock-note", NOTE_DESC)
    pid = _model(env)["pairs"][0]["pair_id"]
    for _ in range(2):
        model = _model(env, dismissed=frozenset({pid}))
        assert (model["pairs"][0]["disposition"], model["open_pairs"]) == ("dismissed", 0)
    assert _model(env, dismissed=frozenset({pid.upper() + "x"}))["pairs"][0]["disposition"] == "open"
    _note(env, "merge-lock-note", NOTE_DESC, body="zie OI-7")
    assert _model(env, dismissed=frozenset({pid}))["pairs"][0]["disposition"] == "linked"


@pytest.mark.parametrize("breakage", ["no_binding", "items_absent", "bucket_absent", "bad_created_at",
                                      "memory_error", "bin_missing", "unknown_state"])
def test_one_bin_not_ok_makes_the_counts_null(env, breakage):
    _items(env, ("OI-7", OI_TITLE, OLD), *([("OI-8", "x", "gisteren")] if breakage == "bad_created_at" else []))
    _note(env, "merge-lock-note", NOTE_DESC)
    if breakage == "memory_error":
        (env["memory"] / "kapot.md").write_bytes(b"\xff\xfe")
    elif breakage in ("no_binding", "items_absent", "bucket_absent"):
        path = {"no_binding": env["state"] / cs.BINDING_FILENAME, "items_absent": env["state"] / "open_items.json",
                "bucket_absent": env["bucket"] / "2026-10.md"}[breakage]
        path.unlink()
    raw = _raw(env)
    if breakage == "bin_missing":
        del raw[ca.NUG]
    elif breakage == "unknown_state":
        raw[ca.NUG] = (raw[ca.NUG][0], "OK", {})
    model = ca.audit_model(raw, WINDOW, project_id="proj-a", dismissed_ids=frozenset())
    assert (model["anchors_total"], model["open_pairs"], model["backlog_open_pairs"]) == (None, None, None)
    assert any(row["state"] != "ok" and row["new"] is None for row in model["bins"].values())


def test_read_model_has_no_title_heading_body_or_file_name(env):
    _items(env, ("OI-7", OI_TITLE, OLD))
    _note(env, "merge-lock-note", NOTE_DESC, body="geheime notitietekst OI-7", name="merge-lock-naam")
    _nugget(env, "2026-11-10", "merge-lock-blijft-achter", "- geheime nuggettekst")
    model = _model(env)
    assert len(model["pairs"]) == 3
    blob = json.dumps(model)
    for secret in (OI_TITLE, NOTE_DESC, "merge-lock-note", "merge-lock-naam", "merge-lock-blijft-achter",
                   "koffie-zetten-ochtend", "geheime", "afgebroken", "blijft"):
        assert secret not in blob


def test_a_dismissal_of_one_project_never_dismisses_the_same_pair_of_another(env):
    _items(env, ("OI-7", OI_TITLE, OLD))
    _note(env, "merge-lock-note", NOTE_DESC)
    pid_a = _model(env)["pairs"][0]["pair_id"]
    assert _model(env, dismissed=frozenset({pid_a}), project="proj-b")["pairs"][0]["disposition"] == "open"
