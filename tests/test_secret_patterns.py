"""secret_patterns: one fixture per ADR-075 decision-3 pattern (all values invented)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import secret_patterns as sp

PEM = "-----BEGIN RSA" + " PRIV" + "ATE KEY-----\nMIIfake\n-----END RSA" + " PRIV" + "ATE KEY-----"
FIXTURES = {  # pattern name -> (text, invented value that must vanish); secret_assignment runs first
    "secret_assignment": ("MY_API_KEY=fakevalue77 run", "fakevalue77"),
    "sk_key": ("curl -u sk-test0123456789abcdefXYZ x", "sk-test0123456789abcdefXYZ"),
    "github_token": ("git push https://ghp_test0123456789abcdefghij0@h", "ghp_test0123456789abcdefghij0"),
    "aws_key_id": ("id AKIATESTFAKE0123456X ok", "AKIATESTFAKE0123456X"),
    "slack_token": ("post xoxb-test-0123456789-fake", "xoxb-test-0123456789-fake"),
    "bearer": ("curl -H x Bearer faketoken0123456789", "faketoken0123456789"),
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

SHA = "0123456789abcdef0123456789abcdef01234567"
Q40 = "q" * 40
# (text, exact masked text) for every shell form of a value (rounds 1-3 on #2863): the
# value goes as a whole, the name and the text after it stay.
EXACT_CASES = {
    "oi_2835_escaped_dq": ('run PASSWORD="prefix\\"private suffix"', "run PASSWORD=<secret>"),
    "escaped_dq_then_more": ('run PASSWORD="a1\\"b2\\"c3 d4" next', "run PASSWORD=<secret> next"),
    "after_closed_dq": ('a TOKEN="x\\"y" b', "a TOKEN=<secret> b"),
    "single_quote_spaces": ("run API_TOKEN='zq9 wv8 xu7' next", "run API_TOKEN=<secret> next"),
    "ansi_c_escaped_quote": ("run MY_SECRET=$'zq9\\'wv8 xu7' next", "run MY_SECRET=<secret> next"),
    "unclosed_dq": ('run PASSWORD="zq9 wv8 xu7', "run PASSWORD=<secret>"),
    "unclosed_sq": ("run PASSWORD='zq9 wv8 xu7", "run PASSWORD=<secret>"),
    "trailing_backslash_dq": ('run PASSWORD="zq9wv8\\', "run PASSWORD=<secret>"),
    "unquoted_name_kept": ("export DB_PASSWORD=hunter2x go", "export DB_PASSWORD=<secret> go"),
    "glued_segments": ('run PASSWORD=zq9"wv8 xu7"tt6 next', "run PASSWORD=<secret> next"),
    "long_value_quoted_escape": ('run --ref="' + Q40 + '\\"tail9 z8" next', "run --ref=<secret> next"),
    "authorization_with_quote": ('Authorization: Basic zq9"wv8xu7', "<secret>"),
    "token_inside_unquoted_value": ("PASSWORD=sk-test0123456789abcdef/private-suffix run", "PASSWORD=<secret> run"),
    "subst_with_spaces": ("PASSWORD=$(printf '%s' 'private-suffix') run", "PASSWORD=<secret> run"),
    "two_token_kinds": ("API_KEY=sk-test0123456789abcdef,ghp_test0123456789abcdefghij0/tail77 go", "API_KEY=<secret> go"),
    "quoted_value_with_token": ('MY_TOKEN="pre sk-test0123456789abcdef post99" go', "MY_TOKEN=<secret> go"),
    "nested_subst": ("PASSWORD=$(echo $(cat sec ret1) tail88) run", "PASSWORD=<secret> run"),
    "backtick_subst": ("PASSWORD=`printf x1 y2 z3` run", "PASSWORD=<secret> run"),
    "subst_with_paren_in_quotes": ("PASSWORD=$(echo ')' zz9) run", "PASSWORD=<secret> run"),
    "dq_holds_subst_with_quotes": ('PASSWORD="$(printf "%s" "private suffix")" run', "PASSWORD=<secret> run"),
    "long_value_before_fragment": ("--ref=sk-test0123456789abcdefghijklmnopqrstuvwxyz/private-suffix run", "--ref=<secret> run"),
    "unbalanced_subst": ("PASSWORD=$(printf zz9 yy8\nnext line", "PASSWORD=<secret>\nnext line"),
    "unbalanced_backtick": ("PASSWORD=`printf zz9 yy8\nnext line", "PASSWORD=<secret>\nnext line"),
    "subst_dq_spans_lines": ('PASSWORD=$(echo "a\nzz9\nnext', "PASSWORD=<secret>"),
    "backtick_line_continuation": ("PASSWORD=`x \\\nzz9` run", "PASSWORD=<secret> run"),
    "token_in_bare_arg": ("curl -u sk-test0123456789abcdef/private-suffix run", "curl -u <secret> run"),
    "loose_sha_stays": ("git show " + SHA, "git show " + SHA),
    "sha_after_equals": ("--sha=" + SHA, "--sha=<secret>"),
    "sha_as_header": ("X-Thing: " + SHA, "X-Thing: <secret>"),
}


@pytest.mark.parametrize("case", list(EXACT_CASES))
def test_exact_mask(case):
    text, expected = EXACT_CASES[case]
    assert sp.mask(text) == expected and sp.mask(expected) == expected


def test_shell_value_end_is_the_one_reader():
    assert sp.shell_value_end('"a\\"b" c') == 6
    assert sp.shell_value_end("'a b' c") == 5
    assert sp.shell_value_end("$'a\\'b' c") == 7
    assert sp.shell_value_end('"never closed') == len('"never closed')
    assert sp.shell_value_end("abc def") == 3
    assert sp.shell_value_end("abc def", 3) is None
    with pytest.raises(TypeError):
        sp.shell_value_end(None)


@pytest.mark.parametrize("bad", [None, b"sk-x"])
def test_non_str_raises(bad):
    for fn in (sp.find, sp.mask):
        with pytest.raises(TypeError):
            fn(bad)


# Every fragment token in every value context: no tail of the value may survive.
TOKENS = {
    "sk": "sk-test0123456789abcdef", "github": "ghp_test0123456789abcdefghij0", "aws": "AKIATESTFAKE0123456X",
    "slack": "xoxb-test-0123456789-fake", "jwt": "eyJfakehdr0.fakebody1.fakesig22",
}
CONTEXTS = {
    "bare_name": "REF={v} run", "flag": "--flag={v} run", "sq": "--flag='{v}' run", "dq": '--flag="{v}" run',
    "dq_subst": 'REF="$(printf "%s" "{v}")" run', "dq_backtick": 'REF="`printf "%s" {v}`" run',
    "unbalanced_subst": "REF=$(printf {v}\n run", "bare_arg": "curl -u {v} run", "flag_space": "--ref {v} run",
}


@pytest.mark.parametrize("ctx", list(CONTEXTS))
@pytest.mark.parametrize("tok", list(TOKENS))
def test_every_token_in_every_value_context(tok, ctx):
    token = TOKENS[tok]
    out = sp.mask(CONTEXTS[ctx].format(v=token + "/private-suffix"))
    assert "private-suffix" not in out and out.endswith(" run"), out
    assert all(token[i:i + 8] not in out for i in range(len(token) - 7)), out
