"""Capture PR-3: the window, the pairs, the disposition, the anchor rule and the read model.

Fixtures live under tmp_path with HOME pointing there (never ~/.claude or ~/.vnx-data).
The bins are read with the real capture_sources readers; the TZ cases and the CLI run
in a subprocess.
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

SCRIPT = _REPO_ROOT / "scripts" / "capture_audit.py"
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
_CLI_FIRST_PROBE = """import json, os, sys
sys.path.insert(0, sys.argv[1] + '/scripts')
import capture_audit as cli
print(json.dumps([os.path.realpath(cli.__file__), os.path.realpath(cli.ca.__file__), callable(cli.ca.audit_model)]))
"""


def _project(home: Path, project: str) -> dict:
    d = {"home": home, "state": home / ".vnx-data" / project / "state",
         "memory": home / ".claude" / "projects" / f"-{project}" / "memory", "bucket": home / f"bucket-{project}"}
    for key in ("state", "memory", "bucket"):
        d[key].mkdir(parents=True)
    (d["state"] / cs.BINDING_FILENAME).write_text(json.dumps({
        "schema": cs.BINDING_SCHEMA, "project_id": project, "memory_dir": str(d["memory"]),
        "bucket_dir": str(d["bucket"]), "bucket_slug": "mine", "bound_at": NEW}))
    _items(d)
    _nugget(d, "2026-10-01", "koffie-zetten-ochtend")
    return d


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return _project(tmp_path / "home", "proj-a")


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


def _audit(d):
    return ca.build_audit(d["state"], ca.parse_window(SINCE, UNTIL, chain_until=None, now=NOW))


def _written(d):
    return json.loads((d["state"] / ca.AUDIT_FILENAME).read_text())


def _run(*args, home):
    drop = ("VNX_PROJECT_ID", "VNX_DISPATCH_PATHS", "VNX_ENFORCE_WORKER_PERMISSIONS")
    proc_env = {**{k: v for k, v in os.environ.items() if k not in drop}, "HOME": str(home)}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          env=proc_env, cwd=str(home))


def test_cli_audit_window(env):
    _items(env, ("OI-7", OI_TITLE, "2026-10-01T10:00:00"))
    _note(env, "merge-lock-note", NOTE_DESC)
    proc = _run("audit", "--state-dir", str(env["state"]), "--since", SINCE, "--until", UNTIL, "--json", home=env["home"])
    assert proc.returncode == 0, f"proc.returncode == 0: {proc.stderr}"
    audit = _written(env)
    assert json.loads(proc.stdout) == audit
    assert (audit["window"]["source"], audit["anchors_total"], audit["open_pairs"], audit["backlog_open_pairs"]) \
        == ("explicit", 1, 1, 0)
    assert audit["bins"]["bucket"]["foreign_slug_skipped"] == 0
    [pair] = audit["pairs"]
    assert (pair["kind"], pair["oi_id"], pair["anchor_bin"], pair["disposition"]) == ("oi_mem", "OI-7", "memory", "open")
    text = _run("audit", "--state-dir", str(env["state"]), "--since", SINCE, "--until", UNTIL, home=env["home"])
    assert text.returncode == 0 and "open_pairs=1" in text.stdout and "merge" not in text.stdout


def test_cli_audit_refuses_and_writes_nothing(env):
    proc = _run("audit", "--state-dir", str(env["state"]), "--since", SINCE, home=env["home"])
    assert proc.returncode == 2 and not (env["state"] / ca.AUDIT_FILENAME).exists()
    missing = env["home"] / "tikfout" / "state"  # a typo in --state-dir never creates a stray state dir
    proc = _run("audit", "--state-dir", str(missing), "--since", SINCE, "--until", UNTIL, home=env["home"])
    assert proc.returncode == 2 and not missing.parent.exists()


def test_build_audit_without_binding_is_unbound_and_null(env):
    (env["state"] / cs.BINDING_FILENAME).unlink()
    audit = _audit(env)
    assert {row["state"] for row in audit["bins"].values()} == {"unbound"}
    assert (audit["open_pairs"], audit["window"]["chain_until"]) == (None, None)


def test_ledger_dismissal_counts_and_broken_lines_do_not(env):
    _items(env, ("OI-7", OI_TITLE, OLD))
    _note(env, "merge-lock-note", NOTE_DESC)
    pid = _audit(env)["pairs"][0]["pair_id"]
    good = {"pair_id": pid, "reason": "geen duplicaat", "at": NEW, "by": "operator"}
    broken = ["{kapot", json.dumps({**good, "reason": " "}), json.dumps({**good, "at": "2026-11-10T08:00:00"}),
              json.dumps({**good, "pair_id": pid[:-1] + "g"}), json.dumps({**good, "pair_id": 7}),
              json.dumps({k: v for k, v in good.items() if k != "by"})]
    ledger = env["state"] / ca.LEDGER_FILENAME
    ledger.write_text("\n".join(broken) + "\n")
    assert _audit(env)["pairs"][0]["disposition"] == "open"
    ledger.write_text("\n".join(broken + [json.dumps(good)]) + "\n")
    assert _audit(env)["pairs"][0]["disposition"] == "dismissed"
    ledger.write_bytes(b"\xff\xfe")
    assert _audit(env)["pairs"][0]["disposition"] == "open"


def test_written_json_has_no_title_heading_body_or_file_name(env):
    _items(env, ("OI-7", OI_TITLE, OLD))
    _note(env, "merge-lock-note", NOTE_DESC, body="geheime notitietekst OI-7", name="merge-lock-naam")
    _nugget(env, "2026-11-10", "merge-lock-blijft-achter", "- geheime nuggettekst")
    assert len(_audit(env)["pairs"]) == 3
    blob = (env["state"] / ca.AUDIT_FILENAME).read_text()
    for secret in (OI_TITLE, NOTE_DESC, "merge-lock-note", "merge-lock-naam", "merge-lock-blijft-achter",
                   "koffie-zetten-ochtend", "geheime", "afgebroken", "blijft"):
        assert secret not in blob


def test_two_projects_with_the_same_oi_ids_do_not_see_each_other_on_disk(env):
    other = _project(env["home"], "proj-b")
    for d in (env, other):
        _items(d, ("OI-7", OI_TITLE, OLD))
        _note(d, "merge-lock-note", NOTE_DESC)
    a = _audit(env)
    (other["state"] / ca.LEDGER_FILENAME).write_text(json.dumps(
        {"pair_id": a["pairs"][0]["pair_id"], "reason": "r", "at": NEW, "by": "operator"}) + "\n")
    b = _audit(other)
    assert (a["project_id"], b["project_id"], len(a["pairs"]), len(b["pairs"])) == ("proj-a", "proj-b", 1, 1)
    assert a["pairs"][0]["pair_id"] != b["pairs"][0]["pair_id"] and b["pairs"][0]["disposition"] == "open"
    assert set(a["pairs"][0]["locators"]).isdisjoint(b["pairs"][0]["locators"])


def test_chain_starts_at_the_last_successful_until(env):
    assert ca.previous_chain_until(env["state"]) is None
    ca.build_audit(env["state"], ca.parse_window(None, None, chain_until=None, now=NOW))
    assert ca.previous_chain_until(env["state"]) == NOW
    later = NOW + timedelta(hours=6)
    explicit = ca.parse_window("2026-11-10T13:00:00Z", "2026-11-10T14:00:00Z", chain_until=None, now=later)
    ca.build_audit(env["state"], explicit)  # an explicit run never moves the chain
    assert ca.previous_chain_until(env["state"]) == NOW
    (env["state"] / "open_items.json").unlink()  # a failed chain run carries the chain on
    failed = ca.build_audit(env["state"], ca.parse_window(None, None, chain_until=NOW, now=later))
    assert (failed["window"]["since"], ca.previous_chain_until(env["state"])) == (NOW.isoformat(), NOW)
    _items(env)
    ca.build_audit(env["state"], ca.parse_window(None, None, chain_until=NOW, now=later))
    assert ca.previous_chain_until(env["state"]) == later
    for corrupt in ("{kapot", "[]", json.dumps({"schema": "capture_audit/9", "window": {"chain_until": None}}),
                    json.dumps({"schema": ca.AUDIT_SCHEMA, "window": {"chain_until": "2026-11-10T12:00:00"}})):
        (env["state"] / ca.AUDIT_FILENAME).write_text(corrupt)
        with pytest.raises(ValueError):
            ca.previous_chain_until(env["state"])
        with pytest.raises(ValueError):  # an explicit run cannot carry an unknown chain_until, so it writes nothing
            ca.build_audit(env["state"], explicit)
        assert (env["state"] / ca.AUDIT_FILENAME).read_text() == corrupt
    for args in ((), ("--since", SINCE, "--until", UNTIL)):
        assert _run("audit", "--state-dir", str(env["state"]), *args, home=env["home"]).returncode == 2


def test_cli_chain_run_starts_at_the_previous_chain_until(env):
    first = ca.build_audit(env["state"], ca.parse_window(
        None, None, chain_until=None, now=datetime.now(timezone.utc) - timedelta(hours=1)))
    chain = json.loads(_run("audit", "--state-dir", str(env["state"]), "--json", home=env["home"]).stdout)
    assert (chain["window"]["source"], chain["window"]["since"]) == ("chain", first["window"]["chain_until"])
    explicit = _run("audit", "--state-dir", str(env["state"]), "--since", SINCE, "--until", UNTIL, home=env["home"])
    assert explicit.returncode == 0 and _written(env)["window"]["chain_until"] == chain["window"]["until"]


@pytest.mark.parametrize("reader,bin_name,reshape", [
    ("read_open_items", ca.OI, lambda r: (*r, 0)), ("read_open_items", ca.OI, list),
    ("read_memory_notes", ca.MEM, lambda r: (*r, 0)), ("read_bucket_entries", ca.NUG, lambda r: (*r[:2], None)),
    ("read_bucket_entries", ca.NUG, lambda r: (*r[:2], True))])
def test_a_reader_of_another_shape_is_error_and_the_measurement_null(env, monkeypatch, reader, bin_name, reshape):
    _items(env, ("OI-7", OI_TITLE, OLD))
    _note(env, "merge-lock-note", NOTE_DESC)
    real = getattr(cs, reader)
    monkeypatch.setattr(cs, reader, lambda *args: reshape(real(*args)))
    audit = _audit(env)
    assert audit["bins"][bin_name] == {"state": "error", "count": None, "new": None}
    assert (audit["anchors_total"], audit["open_pairs"], audit["backlog_open_pairs"]) == (None, None, None)


def test_cli_imported_first_still_loads_the_lib(env):
    proc = subprocess.run([sys.executable, "-c", _CLI_FIRST_PROBE, str(_REPO_ROOT)],
                          capture_output=True, text=True, cwd=str(env["home"]))
    assert proc.returncode == 0, proc.stderr
    lib = _REPO_ROOT / "scripts" / "lib" / "capture_audit.py"
    assert json.loads(proc.stdout) == [str(SCRIPT.resolve()), str(lib.resolve()), True]
