"""OI-2870: a path is masked as one whole shell word, quotes and escapes included."""
import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
import permission_events as pe

CASES = [
    'cat "/Users/alice/My Projects/AcmeCorp/notes.txt"',
    "cat '/Users/alice/My Projects/AcmeCorp/notes.txt'",
    r"cat /Users/alice/My\ Projects/AcmeCorp/notes.txt",
    "cat $'/Users/alice/My Projects/AcmeCorp/notes.txt'",
    'ls "~/Client Files/AcmeCorp"',
    'cp "/tmp/My Dir/acme.txt" .',
    r"cp /tmp/My\ Dir/acme.txt .",
    'cat "/private/tmp/claude-1/scratchpad/My Dir/acme.txt"',
    'cat "/Users/alice/.vnx-data/My Dir/acme.txt"',
    'echo $(cat "/Users/alice/My Projects/AcmeCorp/n.txt")',
    'cd "/Users/alice/My Projects/AcmeCorp" && ls',
    'cat "/Users/alice/My Projects/AcmeCorp/notes.txt',  # unbalanced: mask to the end
    'cat "/Users/alice/My Projects"/AcmeCorp/x.txt',
]
LEAKS = ("AcmeCorp", "acme", "Projects", "Dir", "Files")


@pytest.mark.parametrize("raw", CASES)
def test_path_with_space_is_masked_whole(raw):
    out = pe.normalise(raw)
    assert not [leak for leak in LEAKS if leak in out], out
    assert pe.normalise(out) == out


def test_masked_form_is_unquoted_and_keeps_the_rest():
    assert pe.normalise('cat "/Users/alice/My Projects/x" | wc') == "cat ~/... | wc"
    assert pe.normalise('cp "/tmp/My Dir/a.txt" .') == "cp <tmp> ."
    assert pe.normalise("cat '/Users/a/x y'") == "cat ~/..."


def test_unbalanced_quote_leaves_no_tail():
    assert pe.normalise('cat "/Users/alice/My Projects/AcmeCorp') == "cat ~/..."


# OI-2870 round 2: a replacement never covers text that was already emitted.
OVERLAP_CASES = [
    ('echo "~ /Users/alice/report.txt" && ls /etc', "&& ls /etc"),
    ("echo '~ /Users/alice/AcmeCorp/x' && ls /etc", "&& ls /etc"),
    ('echo "/Users/alice/x ~" && ls /etc', "&& ls /etc"),
    ('cp "/Users/alice/a /tmp/b" && ls /etc', "&& ls /etc"),
    ("cp $'/Users/alice/a /tmp/b' && ls /etc", "&& ls /etc"),
    ("echo $'~ /Users/alice/AcmeCorp/x' && ls /etc", "&& ls /etc"),
    ('echo "a ~ b /tmp/acme dir/x" && rm -rf /x', "&& rm -rf /x"),
    ('cat "/Users/alice/My Projects/AcmeCorp/notes.txt" && ls /etc', "&& ls /etc"),
]
OVERLAP_LEAKS = ("alice", "AcmeCorp", "report", "acme")


def _balanced(text):
    """Even count of unescaped ``"`` and, outside double quotes, of ``'``."""
    dq = sq = 0
    in_dq = False
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            dq += 1
            in_dq = not in_dq
        elif c == "'" and not in_dq:
            sq += 1
        i += 1
    return dq % 2 == 0 and sq % 2 == 0


@pytest.mark.parametrize("raw,tail", OVERLAP_CASES)
def test_replacement_never_overlaps_emitted_text(raw, tail):
    once = pe.normalise(raw)
    twice = pe.normalise(once)
    assert twice == once
    assert tail in once and tail in twice
    assert _balanced(once), once
    assert not [leak for leak in OVERLAP_LEAKS if leak in once], once


@pytest.mark.parametrize("raw", CASES)
def test_existing_cases_are_idempotent_after_two_passes(raw):
    once = pe.normalise(raw)
    assert pe.normalise(once) == once


# OI-2870 round 3: a bare "~" inside a quote normalises once, not twice.
BARE_TILDE_CASES = [
    "echo '~' && ls",
    'echo "~" && ls',
    "echo $'~' && ls",
    'echo "~" "/Users/alice/AcmeCorp/x" && ls /etc',
    "echo '~' '~' && ls",
]
_WORDS = ["~", '"~"', "'~'", "/Users/alice/AcmeCorp/x", '"/Users/alice/My AcmeCorp/x"',
          "'/tmp/acme dir/y'", '"~ /Users/alice/AcmeCorp/z"']
COMBOS = [" ".join(c) for n in (1, 2, 3) for c in itertools.product(_WORDS, repeat=n)]


@pytest.mark.parametrize("raw", BARE_TILDE_CASES)
def test_bare_tilde_in_quote_normalises_once(raw):
    once = pe.normalise(raw)
    assert pe.normalise(once) == once
    assert "ls" in once


def test_generated_word_combinations_are_stable_and_leak_nothing():
    for combo in COMBOS:
        raw = f"echo {combo} && ls /etc"
        once = pe.normalise(raw)
        twice = pe.normalise(once)
        assert twice == once, (raw, once, twice)
        for text in (once, twice):
            assert "&& ls /etc" in text, (raw, text)
            assert not [x for x in ("alice", "AcmeCorp", "acme") if x in text], (raw, text)
            assert _balanced(text), (raw, text)
