"""secret_patterns: one fixture per ADR-075 decision-3 pattern (all values invented)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import secret_patterns as sp

PEM = "-----BEGIN RSA" + " PRIV" + "ATE KEY-----\nMIIfake\n-----END RSA" + " PRIV" + "ATE KEY-----"
FIXTURES = {  # pattern name -> (text, invented value that must vanish)
    "sk_key": ("curl -u sk-test0123456789abcdefXYZ x", "sk-test0123456789abcdefXYZ"),
    "github_token": ("git push https://ghp_test0123456789abcdefghij0@h", "ghp_test0123456789abcdefghij0"),
    "aws_key_id": ("id AKIATESTFAKE0123456X ok", "AKIATESTFAKE0123456X"),
    "slack_token": ("post xoxb-test-0123456789-fake", "xoxb-test-0123456789-fake"),
    "bearer": ("curl -H x Bearer faketoken0123456789", "faketoken0123456789"),
    "secret_assignment": ("MY_API_KEY=fakevalue77 run", "fakevalue77"),
    "authorization_header": ("curl -H 'Authorization: Basic fakecred99'", "fakecred99"),
    "pem_block": (PEM, "MIIfake"),
    "jwt": ("t eyJfakehdr0.fakebody1.fakesig22 x", "fakebody1"),
    "long_value": ("run --ref=" + "q" * 20 + "Z" * 20 + " x", "q" * 20 + "Z" * 20),
}

def test_ten_patterns_one_fixture_each():
    assert [p.name for p in sp.PATTERNS] == list(FIXTURES) and len(FIXTURES) == 10

@pytest.mark.parametrize("name", list(FIXTURES))
def test_find_names_and_mask_removes_value(name):
    text, value = FIXTURES[name]
    names = sp.find(text)
    assert name in names and all(value not in n for n in names)
    out = sp.mask(text)
    assert value not in out and "<secret>" in out and sp.mask(out) == out

def test_assignment_keeps_name_masks_value():
    assert sp.mask("export DB_PASSWORD=hunter2x go") == "export DB_PASSWORD=<secret> go"
    assert sp.mask('x token="a b c"') == "x token=<secret>"

def test_long_value_only_in_equals_or_header_position():
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert sp.mask(f"git show {sha}") == f"git show {sha}"
    assert sha not in sp.mask(f"--sha={sha}")
    assert sha not in sp.mask(f"X-Thing: {sha}")

@pytest.mark.parametrize("bad", [None, b"sk-x"])
def test_non_str_raises(bad):
    for fn in (sp.find, sp.mask):
        with pytest.raises(TypeError):
            fn(bad)
