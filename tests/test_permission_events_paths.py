"""OI-2870: a path is masked as one whole shell word, quotes and escapes included."""
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
