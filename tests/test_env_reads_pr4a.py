"""PR-4a: deleted knobs and registered readers must leave the environment boundary."""
import ast
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DELETED = set('''VNX_ANALYZER_AUTO_MAX_SESSIONS VNX_ANALYZER_CLAUDE_MODEL
VNX_ANALYZER_DEEPSEEK_MIN_BALANCE VNX_ANALYZER_DEEPSEEK_MIN_BALANCE_CNY
VNX_ANALYZER_DEEPSEEK_MODEL VNX_SMTP_PORT VNX_SMTP_USER VNX_OLLAMA_TIMEOUT'''.split())
REGISTERED = set('''VNX_ANALYZER_LLM VNX_ANALYZER_RESTRICTED_CLAUDE_CAP
VNX_DIGEST_EMAIL VNX_SMTP_HOST VNX_RECEIPT_CLASSIFIER_ENABLED
VNX_OLLAMA_HOST VNX_OLLAMA_MODEL'''.split())
READERS = ['conversation_analyzer/models.py', 'lib/append_receipt_internals/payload.py',
           'send_digest_email.py', 'lib/llm_decision_router.py',
           'lib/adapters/ollama_adapter.py', 'lib/classifier_providers/ollama_provider.py']


def test_no_deleted_reads():
    paths = subprocess.check_output(['git', 'ls-files', 'scripts', 'vnx_cli', 'dashboard'],
                                    cwd=ROOT, text=True).splitlines()
    hits = []
    for name in paths:
        path = ROOT / name
        if path.suffix == '.py':
            hits.extend(f'{name}:{n.lineno}:{n.value}' for n in ast.walk(ast.parse(path.read_text()))
                        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in DELETED)
        elif path.suffix == '.sh':
            hits.extend(f'{name}:{i}' for i, line in enumerate(path.read_text().splitlines(), 1)
                        if re.search(r'\$\{?(' + '|'.join(DELETED) + r')\b', line))
    assert not hits, f'{len(hits)} deleted reads: {hits}'


def test_no_registered_env_reads():
    hits = []
    for name in READERS:
        for n in ast.walk(ast.parse((ROOT / 'scripts' / name).read_text())):
            if ((isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr in {'get', 'pop', 'getenv', 'setdefault'})
                    or isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load)):
                text = ast.unparse(n)
                if any(text.startswith(prefix) for prefix in ('os.environ.get(', 'os.environ.pop(',
                                                               'os.environ[', 'os.getenv(')) and any(repr(k) in text for k in REGISTERED):
                    hits.append(f'{name}:{n.lineno}')
    assert not hits, f'{len(hits)} registered env reads: {hits}'
