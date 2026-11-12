"""OB-1: binding and containment per file (ADR-072)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import operator_steering_source as src

SLUG = "-proj-vnx"


def _read(state, projects):
    seen = []
    return src.read_sources(state, projects, lambda handle, name: seen.append((name, handle.read()))), seen


@pytest.fixture
def env(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / ".vnx-project-id").write_text("vnx-dev\n")
    state = tmp_path / "proj" / "state"
    projects = tmp_path / "projects"
    (projects / SLUG / "memory").mkdir(parents=True)
    return state, projects, projects / SLUG


def _bind(state, projects):
    src.bind(state, projects, [SLUG], projects / SLUG / "memory")


def test_symlink_pointing_outside_is_refused_and_never_opened(env, tmp_path):
    state, projects, tdir = env
    _bind(state, projects)
    (tdir / "ok.jsonl").write_text("prompt\n")
    outside = tmp_path / "klant.jsonl"
    outside.write_text("klantdata\n")
    outside.chmod(0)  # opening the target would fail the read; a refusal never gets there
    (tdir / "link.jsonl").symlink_to(outside)
    result, seen = _read(state, projects)
    outside.chmod(0o600)
    assert (result.state, result.refused, result.refused_files) == ("error", {"outside_bound_dir": 1}, ["link.jsonl"])
    assert all("klantdata" not in text for _, text in seen)


def test_dotdot_path_and_swap_after_listing_are_refused(env, tmp_path):
    state, projects, tdir = env
    _bind(state, projects)
    outside = tmp_path / "secret.jsonl"
    outside.write_text("geheim\n")
    with pytest.raises(src.Refused, match="outside_bound_dir"):
        src.open_contained(tdir / ".." / ".." / "secret.jsonl", tdir)
    victim = tdir / "s1.jsonl"
    victim.write_text("prompt\n")
    listed = sorted(tdir.glob("*.jsonl"))
    victim.unlink()
    victim.symlink_to(outside)  # swapped for a symlink after listing
    with pytest.raises(src.Refused, match="outside_bound_dir"):
        src.open_contained(listed[0], tdir)


def test_directory_swapped_for_symlink_during_read_is_refused(env, tmp_path):
    state, projects, tdir = env
    _bind(state, projects)
    (tdir / "a.jsonl").write_text("prompt a\n")
    (tdir / "b.jsonl").write_text("prompt b\n")
    outside = tmp_path / "buiten"
    outside.mkdir()
    (outside / "b.jsonl").write_text("MARKER-BUITEN\n")
    consumed = []

    def consume(handle, name):
        consumed.append((name, handle.read()))
        if len(consumed) == 1:
            tdir.rename(projects / "weggezet")
            tdir.symlink_to(outside, target_is_directory=True)

    result = src.read_sources(state, projects, consume)
    assert result.state == "error"
    assert result.refused == {"outside_bound_dir": 1}
    assert all("MARKER-BUITEN" not in text for _, text in consumed)


def test_open_contained_refuses_a_root_that_is_now_a_symlink(tmp_path):
    outside = tmp_path / "buiten"
    outside.mkdir()
    (outside / "x.jsonl").write_text("MARKER-BUITEN\n")
    root = tmp_path / "root"
    canonical = Path(root.parent.resolve() / "root")
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(src.Refused, match="outside_bound_dir"):
        src.open_contained(canonical / "x.jsonl", canonical)


def test_nothing_is_read_before_a_binding_exists(env):
    state, projects, tdir = env
    (tdir / "s1.jsonl").write_text("prompt\n")
    result, seen = _read(state, projects)
    assert result.state == "unbound" and seen == []
    _bind(state, projects)
    result, seen = _read(state, projects)
    assert result.state == "ok" and seen == [("s1.jsonl", "prompt\n")]


@pytest.mark.parametrize("slug", ["clients-x", "My-Business", "-salesminds-app", "a/b", ".."])
def test_bind_refuses_client_and_business_slugs(env, slug):
    state, projects, _ = env
    with pytest.raises(src.BindingError):
        src.bind(state, projects, [slug], projects / SLUG / "memory")
    assert not (state / src.BINDING_FILE).exists()


def test_binding_with_wrong_project_or_schema_is_unbound(env):
    state, projects, _ = env
    _bind(state, projects)
    path = state / src.BINDING_FILE
    record = json.loads(path.read_text())
    for bad in ({"project_id": "other"}, {"schema": "x/9"}, {"transcript_dirs": []}, {"memory_dir": "/etc"}):
        path.write_text(json.dumps({**record, **bad}))
        assert _read(state, projects)[0].state == "unbound"


# --- OB-1b: ledger, filter of decision 3, CLI -------------------------------------------------

GOAL = "/goal KLAAR als de PR gemerged is, hervat na context-rotatie; resterend: zie ## Actief /goal in x.md"


def _line(uuid, content, **extra):
    return json.dumps({"type": "user", "uuid": uuid, "sessionId": "s1", "message": {"role": "user", "content": content}, **extra})


def _fixture(tdir):
    lines = [
        _line("u1", "pas de tests aan, dit klopt niet"),
        _line("u2", "<task-notification>klaar</task-notification>"),
        _line("u3", [{"type": "tool_result", "content": "uitvoer"}]),
        _line("u4", "<system-reminder>x</system-reminder>"),
        _line("u5", "<pasted_content id=ab12>PLAKTEKST</pasted_content id=ab12>"),
        _line("u6", "/kickoff rotatie 20261111-abc"),
        _line("u7", GOAL),
        _line("u8", "mijn woorden <pasted_content id=cd34>GEPLAKT GEHEIM</pasted_content id=cd34>"),
        _line("u9", "<pasted_content id=ef56>nooit gesloten GEPLAKT"),
    ]
    (tdir / "s1.jsonl").write_text("\n".join(lines) + "\n")


def test_filter_passes_operator_prompts_and_counts_each_reason(env):
    state, projects, tdir = env
    _bind(state, projects)
    _fixture(tdir)
    result = src.read_prompts(state, projects)
    assert result.state == "ok"
    assert [p.text for p in result.prompts] == ["pas de tests aan, dit klopt niet", "mijn woorden"]
    assert result.excluded == {"task_notification": 1, "tool_result": 1, "system_reminder": 1,
                               "pasted_content": 2, "automatic": 2}
    assert all("GEPLAKT" not in p.text and "PLAKTEKST" not in p.text for p in result.prompts)


def test_unknown_or_missing_identity_is_never_a_prompt(env):
    state, projects, tdir = env
    _bind(state, projects)
    (tdir / "s1.jsonl").write_text("niet json\n" + json.dumps({"type": "user", "message": {"content": "zonder uuid"}}) + "\n"
                                   + json.dumps({"type": "user", "uuid": "u", "isMeta": None, "message": {"content": 5}}) + "\n")
    result = src.read_prompts(state, projects)
    assert result.prompts == [] and result.excluded == {"malformed": 1, "no_uuid": 1, "no_text": 1}


def test_bind_twice_gives_two_ledger_lines_and_a_hand_edited_snapshot_is_unbound(env):
    state, projects, tdir = env
    _bind(state, projects)
    _bind(state, projects)
    ledger = [json.loads(x) for x in (state / src.LEDGER_FILE).read_text().splitlines()]
    assert len(ledger) == 2 and ledger[1]["previous_transcript_dirs"] == [SLUG] and ledger[0]["previous_transcript_dirs"] is None
    snapshot = json.loads((state / src.BINDING_FILE).read_text())
    assert all(snapshot[k] == ledger[1][k] for k in ("transcript_dirs", "memory_dir", "project_id"))
    (tdir / "other").mkdir()
    (state / src.BINDING_FILE).write_text(json.dumps({**snapshot, "transcript_dirs": ["other"]}))
    result = src.read_prompts(state, projects)
    assert result.state == "unbound" and "ledger" in result.reason
    (state / src.LEDGER_FILE).unlink()
    _bind_only_snapshot = json.dumps(snapshot)
    (state / src.BINDING_FILE).write_text(_bind_only_snapshot)
    assert src.read_prompts(state, projects).state == "unbound"


def _cli(env, *args):
    from operator_steering import main
    state, projects, _ = env
    return main(["--state-dir", str(state), "--projects-root", str(projects), *args])


def test_cli_bind_and_show_never_prints_an_excluded_line(env, capsys):
    state, projects, tdir = env
    assert _cli(env, "bind", f"--transcript-dir={SLUG}", "--memory-dir", str(tdir / "memory")) == 0
    assert _cli(env, "bind", "--transcript-dir=clients-x", "--memory-dir", str(tdir / "memory")) == 2
    _fixture(tdir)
    passed = src.read_prompts(state, projects).prompts[0]
    capsys.readouterr()
    assert _cli(env, "show", "--prompt", passed.prompt_id) == 0
    assert capsys.readouterr().out == f"s1.jsonl:1\n{passed.text}\n"
    excluded_id = src._prompt_id("s1", "u2")
    assert _cli(env, "show", "--prompt", excluded_id) == 1
    out = capsys.readouterr()
    assert "klaar" not in out.out + out.err and out.out == ""
