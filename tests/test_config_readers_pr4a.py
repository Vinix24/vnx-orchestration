"""Real config layers and process boundaries for PR-4a."""
import importlib.util
import json
import os
import shutil
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'scripts/lib')]
import config_runtime
import config_store_db
from conversation_analyzer import models
from lib.append_receipt_internals import payload
import llm_decision_router as router


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('VNX_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('VNX_PROJECT_ID', 'pr4a')
    monkeypatch.setenv('VNX_GLOBAL_CONFIG_FILE', str(tmp_path / 'config.json'))
    monkeypatch.setattr(config_runtime, '_wired_for', {})
    monkeypatch.setattr(config_runtime.config_registry, '_db_resolver', None)
    monkeypatch.setattr(config_runtime.config_registry, '_default_project_id', None)
    return tmp_path


@pytest.mark.parametrize('key,env,row', [('VNX_ANALYZER_LLM', 'ollama-only', 'deepseek-harness'),
                                        ('VNX_ANALYZER_LLM', 'deepseek-harness', 'ollama-only'),
                                        ('VNX_RECEIPT_CLASSIFIER_ENABLED', '0', '1'),
                                        ('VNX_RECEIPT_CLASSIFIER_ENABLED', '1', '0')])
@pytest.mark.parametrize('layer', ['stored', 'override', 'env'])
def test_precedence(store, monkeypatch, key, env, row, layer):
    monkeypatch.setenv(key, env)
    monkeypatch.delenv('VNX_OVERRIDE_' + key[4:], raising=False)
    if layer != 'env':
        if key == 'VNX_ANALYZER_LLM':
            Path(os.environ['VNX_GLOBAL_CONFIG_FILE']).write_text(json.dumps(
                {'version': 1, 'values': {key: {'value': row}}}))
        else:
            with sqlite3.connect(store / 'runtime_coordination.db') as conn:
                config_store_db.set_config(conn, 'pr4a', key, row, actor='test')
    if layer == 'override':
        monkeypatch.setenv('VNX_OVERRIDE_' + key[4:], env)
    importlib.reload(models)
    expected = row if layer == 'stored' else env
    if key == 'VNX_ANALYZER_LLM':
        assert models.llm_strategy() == expected
    else:
        from receipt_classifier import is_enabled
        assert is_enabled() == (expected == '1')
        with patch('receipt_classifier.trigger_receipt_classifier_async', return_value=None) as trigger:
            payload._trigger_receipt_classifier({'dispatch_id': 'pr4a'})
        assert trigger.called == (expected == '1')


def test_models_import_does_not_import_runtime(store):
    code = f'''import importlib.util, sys
spec = importlib.util.spec_from_file_location("models", {str(ROOT / 'scripts/conversation_analyzer/models.py')!r})
m = importlib.util.module_from_spec(spec)
sys.modules["models"] = m
spec.loader.exec_module(m)
assert "config_runtime" not in sys.modules
'''
    subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, text=True)


def test_router_passes_arguments_without_env_mutation(monkeypatch):
    monkeypatch.setenv('VNX_OLLAMA_HOST', 'http://env:11434')
    monkeypatch.setenv('VNX_OLLAMA_MODEL', 'env-model')
    before = dict(os.environ)
    def construct(terminal, **kwargs):
        assert not [k for k in os.environ.keys() | before.keys() if os.environ.get(k) != before.get(k)]
        assert kwargs == {'host': 'http://argument:11434', 'model': 'argument-model'}
        raise RuntimeError('stop before network')
    with patch('adapters.ollama_adapter.OllamaAdapter', side_effect=construct), \
         patch('provider_switch.require'):
        with pytest.raises(RuntimeError, match='stop before network'):
            router._decide_ollama({}, 'next?', 'argument-model', 'http://argument:11434', 5)
    assert dict(os.environ) == before


@pytest.mark.parametrize('script,output,variable,expected', [
    ('conversation_analyzer_nightly.sh', 'deepseek-harness\nops@example.com', 'ANALYZER_LLM', 'deepseek-harness'),
    ('nightly_intelligence_pipeline.sh', 'ops@example.com', 'DIGEST_EMAIL', 'ops@example.com'),
    ('launchd/reload_receipt_classifier.sh', '1', 'CLASSIFIER_ENABLED', '1')])
@pytest.mark.parametrize('status', [0, 3, None])
def test_shell_config_snapshot(tmp_path, script, output, variable, expected, status):
    text = (ROOT / 'scripts' / script).read_text()
    assert 'vnx_resolve_project_root "${BASH_SOURCE[0]:-$0}"' in text
    block = text[text.index('# Config snapshot'):text.index('# End config snapshot.')]
    binary = tmp_path / 'bin/vnx'
    binary.parent.mkdir()
    if status is not None:
        binary.write_text(f'#!/bin/bash\n[ "$1 $2 $3" = "config get --raw" ] || exit 9\nprintf "%s\\n" "{output}"\nexit {status}\n')
        binary.chmod(0o755)
    pipeline = script == 'nightly_intelligence_pipeline.sh'
    probe = '\nprintf "STATUS:%s" "$DIGEST_SNAPSHOT_STATUS"' if pipeline else ''
    result = subprocess.run(['bash', '-eu', '-c', block + f'\nprintf "%s" "${variable}"' + probe],
                            env={**os.environ, 'VNX_ROOT': str(tmp_path)}, text=True, capture_output=True)
    assert result.returncode == (0 if status == 0 or pipeline else 1), result.stderr
    value = expected if status == 0 or (pipeline and status is not None) else ""
    assert result.stdout == value + (f'STATUS:{int(status != 0)}' if pipeline else '')
    assert ('WARNING:' in result.stderr) == (status != 0)


@pytest.mark.parametrize('configured', [None, 'qwen2.5-coder:14b', 'custom-model'])
def test_ollama_defaults_and_config(store, monkeypatch, configured):
    from adapters.ollama_adapter import OllamaAdapter
    from classifier_providers.ollama_provider import OllamaProvider, _api_host
    monkeypatch.delenv('VNX_OLLAMA_MODEL', raising=False)
    monkeypatch.setenv('VNX_OLLAMA_HOST', 'http://env:11434')
    values = {'VNX_OLLAMA_HOST': {'value': 'http://stored:11434/'}}
    if configured is not None:
        values['VNX_OLLAMA_MODEL'] = {'value': configured}
    (store / 'config.json').write_text(json.dumps({'version': 1, 'values': values}))
    assert OllamaAdapter('T1')._model == (configured or 'gemma3:27b')
    assert router.DecisionRouter(data_dir=store).ollama_model == (configured or 'gemma3:27b')
    assert OllamaProvider().model == (configured or 'llama3.1:8b')
    assert models.ollama_model() == (configured or 'qwen2.5-coder:14b')
    assert OllamaAdapter('T1')._host == _api_host() == 'http://stored:11434'


def test_classifier_read_failure_is_logged(store, monkeypatch):
    with patch.object(config_runtime, 'get_bool', side_effect=RuntimeError('unreadable config')), \
         patch.object(payload, '_emit') as emit:
        payload._trigger_receipt_classifier({})
    emit.assert_called_once_with('WARN', 'receipt_classifier_trigger_failed', error='unreadable config')


def test_unknown_key_retains_unset_source(store):
    assert config_runtime.get_with_source('VNX_PR4A_UNKNOWN') == (None, 'unset')


def test_digest_uses_stored_settings_and_fixed_transport(store, monkeypatch, capsys):
    import send_digest_email
    monkeypatch.setenv('VNX_DIGEST_EMAIL', 'env@example.com')
    monkeypatch.setenv('VNX_SMTP_HOST', 'smtp.env.example.com')
    monkeypatch.setenv('VNX_SMTP_PASS', 'test-only')
    values = {'VNX_DIGEST_EMAIL': {'value': 'stored@example.com'},
              'VNX_SMTP_HOST': {'value': 'smtp.stored.example.com'}}
    (store / 'config.json').write_text(json.dumps({'version': 1, 'values': values}))
    assert send_digest_email.send_email('test', 'body', dry_run=True)
    output = capsys.readouterr().out
    assert 'To: stored@example.com' in output and 'From: stored@example.com' in output
    assert 'Host: smtp.stored.example.com:587' in output


@pytest.mark.parametrize('layout', ['.claude/vnx-system', '.vnx', '.'])
@pytest.mark.parametrize('script,expected', [
    ('conversation_analyzer_nightly.sh', 'deepseek-harness|ops@example.com|'),
    ('nightly_intelligence_pipeline.sh', '|ops@example.com|'),
    ('launchd/reload_receipt_classifier.sh', '||1')])
@pytest.mark.parametrize('cli_status', [0, 3, None])
def test_engine_config_snapshot(tmp_path, layout, script, expected, cli_status):
    """Execute whole scripts, stopping at a debugger breakpoint after the snapshot.

    The resolver and snapshot execute unchanged; no shell block is extracted.
    Only the CLI and launchctl process boundaries have local stand-ins.
    """
    consumer = tmp_path / 'consumer'
    consumer.mkdir()
    subprocess.run(['git', 'init', '-q', str(consumer)], check=True)
    engine = consumer / layout
    scripts = engine / 'scripts'
    (scripts / 'lib').mkdir(parents=True)
    for name in ('vnx_paths.sh', 'vnx_resolve_root.sh', 'vnx_run_bounded.sh'):
        shutil.copy2(ROOT / 'scripts/lib' / name, scripts / 'lib' / name)
    target = scripts / script
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'scripts' / script, target)
    text = target.read_text()
    stop_line = text[:text.index('# End config snapshot.')].count('\n') + 1
    home = tmp_path / 'home'
    (home / 'Library/LaunchAgents').mkdir(parents=True)
    state = tmp_path / 'data/state'
    state.mkdir(parents=True)
    binary = engine / 'bin/vnx'
    binary.parent.mkdir()
    if cli_status is not None:
        binary.write_text(f'''#!/bin/bash
[ "$1 $2 $3" = "config get --raw" ] || exit 9
[ "$VNX_PROJECT_ROOT" = "{consumer}" ] || exit 10
[ "$VNX_PROJECT_ID" = "consumer-test" ] || exit 11
shift 3
for key in "$@"; do
    case "$key" in
        VNX_ANALYZER_LLM) echo deepseek-harness ;;
        VNX_DIGEST_EMAIL) echo ops@example.com ;;
        VNX_RECEIPT_CLASSIFIER_ENABLED) echo 1 ;;
        *) exit 12 ;;
    esac
done
exit {cli_status}
''')
        binary.chmod(0o755)
    if layout != '.':
        assert not (consumer / 'bin/vnx').exists()
    # On old code the classifier reaches launchctl before its snapshot.
    launchctl = binary.parent / 'launchctl'
    launchctl.write_text('#!/bin/sh\necho com.vnx.receipt-classifier-batch\n')
    launchctl.chmod(0o755)
    if script.startswith('launchd/'):
        for relative in ('launchd/com.vnx.receipt-classifier-batch.plist',
                         'lib/receipt_classifier_batch.py'):
            shutil.copy2(ROOT / 'scripts' / relative, scripts / relative)
    debugger = tmp_path / 'breakpoint.sh'
    debugger.write_text('''trap 'if [[ ${BASH_SOURCE[0]} == "$SNAPSHOT_SCRIPT" && $LINENO -gt $SNAPSHOT_END ]]; then
    printf "SNAPSHOT:%s|%s|%s\\n" "${ANALYZER_LLM-}" "${DIGEST_EMAIL-}" "${CLASSIFIER_ENABLED-}"
    exit 0
fi' DEBUG
''')
    env = {'PATH': f"{binary.parent}:{os.environ['PATH']}", 'HOME': str(home),
           'VNX_HOME': str(engine), 'VNX_PROJECT_ID': 'consumer-test',
           'VNX_DATA_DIR': str(state.parent), 'VNX_STATE_DIR': str(state),
           'BASH_ENV': str(debugger), 'SNAPSHOT_SCRIPT': str(target),
           'SNAPSHOT_END': str(stop_line)}
    result = subprocess.run(['bash', str(target)], cwd=consumer, env=env,
                            text=True, capture_output=True, timeout=30)
    if cli_status == 0:
        assert result.returncode == 0, result.stderr
        assert f'SNAPSHOT:{expected}' in result.stdout
        assert 'WARNING:' not in result.stderr
    else:
        pipeline = script == 'nightly_intelligence_pipeline.sh'
        assert result.returncode == (0 if pipeline else 1), result.stdout + result.stderr
        assert 'WARNING:' in result.stderr and 'snapshot unavailable' in result.stderr
        assert ('SNAPSHOT:' in result.stdout) == pipeline
        assert 'using ollama-only' not in result.stderr
