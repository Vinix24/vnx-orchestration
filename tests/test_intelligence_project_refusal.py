"""AG-1d1: unresolved intelligence never borrows the vnx-dev tenant."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_intelligence_selector import _setup_quality_db, _seed_proven_pattern
from test_adr_injection import _create_adrs_db
from intelligence_selector import IntelligenceSelector
from intelligence_injection import build_intelligence_section, fetch_adr_context_section
from project_scope import TenantUnresolved
from project_id_refusals import project_id_refusal_section

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path):
    db = tmp_path / 'quality_intelligence.db'
    conn = _setup_quality_db(db)
    conn.execute("ALTER TABLE success_patterns ADD project_id TEXT DEFAULT 'vnx-dev'")
    _seed_proven_pattern(conn, title='other tenant', category='architect')
    conn.execute("UPDATE success_patterns SET project_id = 'other'")
    conn.commit()
    _seed_proven_pattern(conn, title='vnx tenant', category='architect')
    conn.close()
    _create_adrs_db(db)
    return tmp_path


@pytest.mark.parametrize('source', [None, '', 'INVALID', '../other', '/other'])
def test_unresolved_selector_refuses_and_injection_preserves_prompt(store, monkeypatch, caplog, source):
    if source is not None:
        monkeypatch.setenv('VNX_PROJECT_ID', source)
    selector = IntelligenceSelector(store / 'quality_intelligence.db')
    try:
        with pytest.raises(TenantUnresolved):
            selector.select('dlv-refusal', 'dispatch_create', skill_name='architect')
    finally:
        selector.close()
    assert build_intelligence_section('original prompt', 'dlv-refusal', 'architect', store) == 'original prompt'
    assert caplog.messages.count('intelligence: skipped (project_id_unresolved)') == 1
    records = [json.loads(line) for line in (store / 'skip_metrics.ndjson').read_text().splitlines()]
    assert len(records) == 1 and records[0]['context'] == 'intelligence_injection'
    assert not (store / 'dispatch_register.ndjson').exists()


def test_explicit_environment_reads_only_other_tenant(store, monkeypatch):
    monkeypatch.setenv('VNX_PROJECT_ID', 'other')
    selector = IntelligenceSelector(store / 'quality_intelligence.db')
    try:
        items = selector._query_candidates('coding_interactive', ['architect'])['proven_pattern']
        assert [item.title for item in items] == ['other tenant']
    finally:
        selector.close()
    assert fetch_adr_context_section('dlv-other', 'architect', [], store) == ''
    assert 'ADR-007' in fetch_adr_context_section('dlv-explicit', 'architect', [], store, project_id='vnx-dev')


@pytest.mark.parametrize("marker", [False, True])
def test_worker_hook_unresolved_is_silent_and_counts_once(tmp_path, marker):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    hook = scripts / 'userpromptsubmit_worker_intelligence_inject.sh'
    shutil.copy2(ROOT / 'scripts' / hook.name, hook)
    for name in ('lib', 'gather_intelligence.py'):
        (scripts / name).symlink_to(ROOT / 'scripts' / name)
    state = tmp_path / 'state'
    state.mkdir()
    if marker:
        (state / '.vnx-project-id').write_text('other\n')
    (state / 'terminal_state.json').write_text(json.dumps({'terminals': {'T1': {'claimed_by': 'dlv-refusal'}}}))
    dispatches = tmp_path / '.vnx-data' / 'dispatches' / 'active'
    dispatches.mkdir(parents=True)
    (dispatches / 'dlv-refusal.md').write_text('Role: architect\nInstruction:\nbuild a backend\n')
    env = dict(os.environ, VNX_STATE_DIR=str(state), VNX_TERMINAL='T1')
    proc = subprocess.run(['bash', str(hook)], input='{"prompt":"original prompt"}', text=True,
                          capture_output=True, env=env, cwd=tmp_path, timeout=30)
    assert proc.returncode == 0 and proc.stdout == ''
    assert (state / 'intelligence_hook.log').read_text().splitlines().count('intelligence: skipped (project_id_unresolved)') == 1
    records = [json.loads(line) for line in (state / 'skip_metrics.ndjson').read_text().splitlines()]
    assert len(records) == 1 and records[0]['context'] == 'intelligence_injection'
    assert records[0]['hook'] == hook.name
    assert project_id_refusal_section(tmp_path, home=tmp_path)['contexts'] == {'intelligence_injection': 1}


def test_gather_outcome_refuses_unresolved_store(store):
    from test_pattern_injection_outcome import _make_gatherer
    import sqlite3
    with sqlite3.connect(store / 'quality_intelligence.db') as db:
        gatherer = _make_gatherer(store, db)
        with pytest.raises(TenantUnresolved):
            gatherer._record_injection_why(dispatch_id='dlv-refusal', offered_file_paths={'p1': 'x.py'},
                offered_titles={}, offered_content={}, offered_arms={}, report_files=set(), report_text='')


def test_marker_or_explicit_scope_never_queries_legacy_default(store, monkeypatch, caplog):
    (store / '.vnx-project-id').write_text('other\n')
    assert IntelligenceSelector(coord_db_state_dir=store).project_id == 'other'
    assert fetch_adr_context_section('dlv-other', 'architect', [], store) == ''
    assert build_intelligence_section('prompt', 'dlv-other', 'architect', store) == 'prompt'
    assert caplog.messages.count('intelligence: skipped (project_id_unresolved)') == 1
    monkeypatch.setenv('VNX_PROJECT_ID', 'vnx-dev')
    with pytest.raises(TenantUnresolved):
        IntelligenceSelector(store / 'quality_intelligence.db', project_id='other')._query_candidates('coding_interactive', [])


def test_health_probe_cannot_turn_tenant_refusal_into_success(store):
    from check_intelligence_health import check_pipeline_connected
    result = check_pipeline_connected(store / 'quality_intelligence.db')
    assert result['connected'] is False and 'No project_id source' in result['error']


def test_standalone_adr_refusal_is_recorded(store, caplog):
    assert fetch_adr_context_section('dlv-refusal', 'architect', [], store) == ''
    assert caplog.messages.count('intelligence: skipped (project_id_unresolved)') == 1
    assert project_id_refusal_section(store, home=store)['contexts'] == {'intelligence_injection': 1}


def test_late_selector_refusal_preserves_prompt(store, monkeypatch, caplog):
    monkeypatch.setenv('VNX_PROJECT_ID', 'other')
    def refuse(*args, **kwargs):
        raise TenantUnresolved('source read refused')
    monkeypatch.setattr(IntelligenceSelector, 'select', refuse)
    assert build_intelligence_section('prompt', 'dlv-refusal', 'architect', store) == 'prompt'
    assert caplog.messages.count('intelligence: skipped (project_id_unresolved)') == 1
    assert project_id_refusal_section(store, home=store)['contexts'] == {'intelligence_injection': 1}
