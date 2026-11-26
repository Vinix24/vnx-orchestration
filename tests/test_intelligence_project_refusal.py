"""AG-1d1: unresolved intelligence never borrows the vnx-dev tenant."""
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from test_intelligence_selector import (_setup_quality_db, _seed_proven_pattern,
    _seed_antipattern, _seed_prevention_rule, _seed_recent_dispatch)
from test_adr_injection import _create_adrs_db
from intelligence_selector import IntelligenceSelector, InjectionResult
from intelligence_sources import proven_pattern, failure_prevention, recent_comparable, _recording
from intelligence_injection import build_intelligence_section, fetch_adr_context_section, fetch_intelligence_section
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
    conn.execute("UPDATE success_patterns SET last_used = datetime('now')")
    for table, seed in [('antipatterns', _seed_antipattern),
                        ('prevention_rules', _seed_prevention_rule),
                        ('dispatch_metadata', _seed_recent_dispatch)]:
        conn.execute(f'ALTER TABLE {table} ADD project_id TEXT')
        for project in ('other', 'vnx-dev'):
            kwargs = {'dispatch_id': project} if table == 'dispatch_metadata' else {}
            row_id = seed(conn, **kwargs)
            conn.execute(f'UPDATE {table} SET project_id = ? WHERE id = ?', (project, row_id))
    conn.commit()
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
def test_worker_hook_refuses_only_unresolved_store(tmp_path, marker):
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
    if marker:
        assert not (state / 'skip_metrics.ndjson').exists()
        return
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


@pytest.mark.parametrize('mode', ['', '1', 'shadow'])
@pytest.mark.parametrize('explicit', [False, True])
def test_marker_or_explicit_scope_reads_selected_tenant(store, monkeypatch, mode, explicit):
    (store / '.vnx-project-id').write_text('other\n')
    monkeypatch.setenv('VNX_USE_CENTRAL_DB', mode)
    if explicit:
        monkeypatch.setenv('VNX_PROJECT_ID', 'foreign')
    selector = IntelligenceSelector(store / 'quality_intelligence.db', project_id='other' if explicit else None)
    monkeypatch.setattr(selector, '_get_central_qi_conn', lambda: _setup_quality_db(store / 'quality_intelligence.db'))
    try:
        candidates = selector._query_candidates('coding_interactive', [])
        assert [i.title for i in candidates['proven_pattern']] == ['other tenant']
        assert [i.item_id for i in candidates['failure_prevention']] == (
            ['intel_ap_1'] if mode == '1' else ['intel_ap_1', 'intel_pr_1'])
        assert [i.source_refs for i in candidates['recent_comparable']] == [['other']]
        result = InjectionResult(dispatch_id='dlv-other', injection_point='dispatch_create',
                                 injected_at='2026-11-26T00:00:00Z', suppressed=[], task_class='coding_interactive',
                                 items=candidates['proven_pattern'] + candidates['failure_prevention'])
        selector.record_injection(result)
        assert selector.stamp_source_dispatch_ids(result) == 2
        db = selector._get_quality_db()
        assert {r[0] for r in db.execute('SELECT project_id FROM dispatch_pattern_offered')} == {'other'}
        for table in ('success_patterns', 'antipatterns'):
            rows = db.execute(f'SELECT project_id, source_dispatch_ids FROM {table} ORDER BY id').fetchall()
            assert json.loads(rows[0][1]) == ['dlv-other'] and not rows[1][1]
        if not mode:
            section = fetch_intelligence_section('dlv-other', 'architect', store, project_id='other' if explicit else None)
            assert 'other tenant' in section and 'vnx tenant' not in section
    finally:
        selector.close()


@pytest.mark.parametrize('reader', [proven_pattern, failure_prevention, recent_comparable])
@pytest.mark.parametrize('mode', ['', '1', 'shadow'])
@pytest.mark.parametrize('source', [None, '', 'INVALID', '../other', '/other', 'marker'])
def test_source_resolves_store_or_refuses(store, monkeypatch, reader, mode, source):
    monkeypatch.setenv('VNX_USE_CENTRAL_DB', mode)
    if source == 'marker':
        (store / '.vnx-project-id').write_text('other\n')
    elif source is not None:
        monkeypatch.setenv('VNX_PROJECT_ID', source)
    selector = IntelligenceSelector(store / 'quality_intelligence.db')
    query = getattr(reader, 'query_' + reader.__name__.rsplit('.', 1)[1] + ('s' if reader is proven_pattern else ''))
    kwargs = {'reconcile_fn': selector._maybe_reconcile_confidence} if reader is proven_pattern else {}
    try:
        if source == 'marker':
            items = query(selector._get_quality_db(), 'coding_interactive', [], has_column_fn=selector._has_column,
                          central_conn_fn=lambda: _setup_quality_db(store / 'quality_intelligence.db'), **kwargs)
            assert [i.item_id for i in items] == ({proven_pattern: ['intel_sp_1'],
                failure_prevention: ['intel_ap_1'] if mode == '1' else ['intel_ap_1', 'intel_pr_1'],
                recent_comparable: ['intel_dm_other']}[reader])
            return
        with pytest.raises(TenantUnresolved):
            query(selector._get_quality_db(), 'coding_interactive', [], has_column_fn=selector._has_column,
                  central_conn_fn=lambda: _setup_quality_db(store / 'quality_intelligence.db'), **kwargs)
    finally:
        selector.close()


def test_source_identity_read_failure_is_visible():
    from intelligence_sources._common import _source_project_id
    db = sqlite3.connect(':memory:')
    db.close()
    with pytest.raises(TenantUnresolved, match='cannot read intelligence store identity'):
        _source_project_id(db)


def test_recorders_refuse_unresolved_project(store):
    selector = IntelligenceSelector(store / 'quality_intelligence.db', project_id='other')
    try:
        items = selector._query_candidates('coding_interactive', [])['proven_pattern']
        result = InjectionResult('dispatch_create', '2026-11-26T00:00:00Z', items, [], 'coding_interactive', 'dlv-other')
        for record, args in [(_recording.record_pattern_usage, (selector._has_column,)),
                             (_recording.stamp_source_dispatch_ids, ())]:
            with pytest.raises(TenantUnresolved):
                record(result, selector._get_quality_db(), *args)
    finally:
        selector.close()


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
