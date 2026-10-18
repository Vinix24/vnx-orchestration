"""The screen checks of the automatic /clear on every A0 pane capture (A0 rule 6)."""
from __future__ import annotations

import pytest

from conftest import PANE_EXPECTED as EXPECTED, PANES, PROMPT, pane_text, with_input

import context_budget
import pane_screen

SETTINGS = context_budget.DEFAULTS



def test_every_capture_has_an_expectation():
    assert sorted(p.name for p in PANES.iterdir()) == sorted(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_capture_reads_as_measured(name):
    screen = pane_screen.read(pane_text(name), SETTINGS)
    assert pane_screen.idle_problem(screen) == EXPECTED[name]


def test_dim_text_after_the_prompt_is_not_typed_text():
    placeholder = pane_screen.read(pane_text("01-idle-placeholder-auto.esc.txt"), SETTINGS)
    suggestion = pane_screen.read(pane_text("05-idle-suggestion-auto.esc.txt"), SETTINGS)
    assert (placeholder.typed, suggestion.typed) == ("", "")
    assert pane_screen.read(pane_text("06-idle-typed-text-auto.esc.txt"), SETTINGS).typed == "half typed"


def test_what_is_typed_reads_back_exactly():
    screen = pane_screen.read(with_input(pane_text("04-idle-empty-auto.esc.txt"), "/clear"), SETTINGS)
    assert (screen.box, screen.typed, screen.busy) == (True, "/clear", False)


def test_dim_text_after_typed_text_is_left_out():
    text = with_input(pane_text("04-idle-empty-auto.esc.txt"), "/clear\x1b[2m <args>\x1b[0m")
    assert pane_screen.read(text, SETTINGS).typed == "/clear"


@pytest.mark.parametrize("name", ["04-idle-empty-auto.esc.txt", "08-idle-empty-mode2.esc.txt",
                                  "07-after-clear-auto.esc.txt", "01-idle-placeholder-auto.esc.txt"])
def test_the_row_and_the_end_of_the_typed_text_are_where_tmux_puts_the_cursor(name):
    """tmux counts cursor_y from the first line of the capture and cursor_x in screen columns (measured, 3.7)."""
    text = with_input(pane_text(name), "/clear")
    row = next(number for number, line in enumerate(text.split("\n")) if PROMPT in line)
    screen = pane_screen.read(text, SETTINGS)
    assert (screen.typed, screen.row, screen.end) == ("/clear", row, len(PROMPT) + len("/clear"))


def test_an_empty_input_line_ends_right_after_the_prompt():
    assert pane_screen.read(pane_text("04-idle-empty-auto.esc.txt"), SETTINGS).end == len(PROMPT)
    assert pane_screen.read(pane_text("05-idle-suggestion-auto.esc.txt"), SETTINGS).end == len(PROMPT)


def test_the_end_counts_screen_columns_not_characters():
    base = pane_text("04-idle-empty-auto.esc.txt")
    assert pane_screen.read(with_input(base, "/clear\x1b[2m <args>\x1b[0m"), SETTINGS).end == len(PROMPT) + 6
    assert pane_screen.read(with_input(base, "日本"), SETTINGS).end == len(PROMPT) + 4
    assert pane_screen.read(with_input(base, "é"), SETTINGS).end == len(PROMPT) + 1
    assert pane_screen.read(with_input(base, "a\x1b]8;;file://x\x1b\\b"), SETTINGS).end == len(PROMPT) + 2
    assert pane_screen.read(with_input(base, "a b  "), SETTINGS).end == len(PROMPT) + 3


def test_no_input_box_has_no_row_or_end():
    screen = pane_screen.read(pane_text("11-permission-dialog-mode2.esc.txt"), SETTINGS)
    assert (screen.row, screen.end) == (None, None)


def test_a_prompt_in_the_history_is_not_the_input_line():
    """History prompts have a plain space after `❯`; only the input line has the no-break space."""
    text = pane_text("04-idle-empty-auto.txt")
    assert "❯ Say OK and nothing else." in text
    without_box = "\n".join(line for line in text.split("\n") if not pane_screen.is_rule(line))
    assert pane_screen.read(without_box, SETTINGS).box is False


def test_the_dialog_choice_line_is_never_an_input_line():
    text = pane_text("11-permission-dialog-mode2.esc.txt")
    assert "1. " in pane_screen.plain(text) and PROMPT not in text
    assert pane_screen.read(text, SETTINGS) == pane_screen.Screen(False, None, False)


def test_a_second_line_between_the_rules_is_no_input_box():
    """A multi-line prompt, or anything else between the last two rules, is not an empty input line."""
    text = pane_text("04-idle-empty-auto.esc.txt").replace(PROMPT, PROMPT + "\nsecond line", 1)
    assert pane_screen.idle_problem(pane_screen.read(text, SETTINGS)) == "no_input_box"


def test_no_rules_at_all_is_no_input_box():
    assert pane_screen.idle_problem(pane_screen.read("$ sleep 60\n", SETTINGS)) == "no_input_box"
    assert pane_screen.idle_problem(pane_screen.read("", SETTINGS)) == "no_input_box"


def test_a_spinner_anywhere_above_the_box_is_busy_but_not_below_it():
    idle = pane_text("04-idle-empty-auto.txt")
    lines = idle.split("\n")
    above = "\n".join(lines[:20] + ["✶ Pondering… (2s · ↓ 10 tokens)"] + lines[20:])
    below = idle + "\n✶ Pondering… (2s · ↓ 10 tokens)"
    assert pane_screen.read(above, SETTINGS).busy is True
    assert pane_screen.read(below, SETTINGS).busy is False


def test_a_finished_turn_line_is_not_a_spinner():
    assert "✻ Churned for 1s · done 7:00 PM" in pane_text("04-idle-empty-auto.txt")
    assert pane_screen.read(pane_text("04-idle-empty-auto.txt"), SETTINGS).busy is False


def test_the_patterns_are_settings():
    custom = dict(SETTINGS, prompt_pattern="^> ", busy_pattern="^working")
    text = "history\n" + "─" * 40 + "\n> \n" + "─" * 40 + "\n"
    assert pane_screen.idle_problem(pane_screen.read(text, custom)) is None
    assert pane_screen.idle_problem(pane_screen.read("working\n" + text, custom)) == "busy"
    assert pane_screen.idle_problem(pane_screen.read(text, SETTINGS)) == "no_input_box"


@pytest.mark.parametrize("sequence, dim", [
    ("\x1b[2m", True),
    ("\x1b[2;38;5;246m", True),
    ("\x1b[38;5;2m", False),  # colour 2, not dim
    ("\x1b[38;2;2;2;2m", False),  # a true colour of 2s, not dim
    ("\x1b[48;5;2m", False),
    ("\x1b[38:5:2m", False),
    ("\x1b[2m\x1b[22m", False),
    ("\x1b[2m\x1b[0m", False),
    ("\x1b[2m\x1b[m", False),
    ("\x1b[2m\x1b[1m", True),  # bold leaves dim on
])
def test_the_dim_flag_follows_sgr(sequence, dim):
    assert pane_screen.cells(sequence + "x") == [("x", dim)]


def test_other_escape_sequences_show_nothing():
    link = "\x1b]8;id=a;file://x\x1b\\name\x1b]8;;\x1b\\"
    assert pane_screen.plain(link) == "name"
    assert pane_screen.plain("a\x1b]0;title\x07b") == "ab"
    assert pane_screen.plain("a\x1b[2Kb\x1b(Bc") == "abc"
    assert pane_screen.plain("unterminated \x1b]8;;") == "unterminated "
    assert pane_screen.plain("cut \x1b[38;5") == "cut "


def test_a_hyperlink_in_the_input_line_is_not_typed_text():
    text = with_input(pane_text("04-idle-empty-auto.esc.txt"), "\x1b]8;;file://x\x1b\\\x1b]8;;\x1b\\")
    assert pane_screen.read(text, SETTINGS).typed == ""
