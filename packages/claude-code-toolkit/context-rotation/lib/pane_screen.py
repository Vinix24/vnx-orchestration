"""Reading a Claude Code screen from `tmux capture-pane -p -e` (loop step 7, A0 rule 6).

Measured on Claude Code 2.1.300 (tests/fixtures/a0/panes):

- The input box is the one line directly between the last two lines of `─` on the screen.
- That line starts with the prompt, `❯` and a no-break space (the setting
  `prompt_pattern`). Empty input is nothing after the prompt, or only dim text
  (`ESC[2m`): a placeholder or a prompt suggestion, which a capture without `-e`
  cannot tell from typed text.
- Busy is a spinner line, `<glyph> <Word>… (<n>s`, anywhere above the input box (the
  setting `busy_pattern`), while the input line stays empty.
- A permission dialog replaces the input box with a menu (`❯ 1. Yes`, where Enter
  approves): no input box means not idle.

Only SGR sequences (`ESC[...m`) change what is read here, and of them only the dim
flag; other escape sequences (OSC hyperlinks) are skipped. A row is a line of the
capture counted from 0, as tmux counts `cursor_y`, and a column counts what a
character takes on the screen (2 for a wide one, 0 for a combining one), as tmux
counts `cursor_x`. Standard library only.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

ESC = "\x1b"
RULE = "─"
MIN_RULE_LENGTH = 10
DIM, NORMAL_INTENSITY = 2, 22
# 38, 48 and 58 take a colour after them: "5;<n>" or "2;<r>;<g>;<b>".
EXTENDED_COLOURS = (38, 48, 58)


class Screen(NamedTuple):
    """What the job needs to know of a screen: `typed` is None without an input box.

    `row` is the input line's row and `end` the column right after its last typed
    character, where the cursor stands when nothing moved it; both None without an
    input box.
    """

    box: bool
    typed: Optional[str]
    busy: bool
    row: Optional[int] = None
    end: Optional[int] = None


def width(char: str) -> int:
    """The columns a character takes on the screen."""
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def _dim_after(params: str, dim: bool) -> bool:
    """The dim flag after one SGR sequence with these parameters."""
    parts = params.split(";")
    index = 0
    while index < len(parts):
        part = parts[index]
        index += 1
        if ":" in part:  # a colour with its own sub-parameters, "38:5:174"
            continue
        code = 0 if part == "" else int(part) if part.isascii() and part.isdigit() else None
        if code == 0:
            dim = False
        elif code == DIM:
            dim = True
        elif code == NORMAL_INTENSITY:
            dim = False
        elif code in EXTENDED_COLOURS and index < len(parts):
            mode = parts[index]
            index += 1 + (1 if mode == "5" else 3 if mode == "2" else 0)
    return dim


def _osc_end(line: str, start: int) -> int:
    """The index after the end of an OSC sequence (BEL or ESC \\), or the end of the line."""
    ends = []
    bell = line.find("\x07", start)
    if bell >= 0:
        ends.append(bell + 1)
    terminator = line.find(ESC + "\\", start)
    if terminator >= 0:
        ends.append(terminator + 2)
    return min(ends) if ends else len(line)


def _sequence_end(line: str, start: int) -> Tuple[int, Optional[str]]:
    """(index after the escape sequence at `start`, its SGR parameters or None)."""
    kind = line[start + 1:start + 2]
    if kind == "[":
        end = start + 2
        while end < len(line) and not "@" <= line[end] <= "~":
            end += 1
        if end < len(line) and line[end] == "m":
            return end + 1, line[start + 2:end]
        return end + 1, None
    if kind == "]":
        return _osc_end(line, start + 2), None
    end = start + 1  # intermediate bytes, then one final byte: ESC ( B
    while end < len(line) and " " <= line[end] <= "/":
        end += 1
    return end + 1, None


def cells(line: str) -> List[Tuple[str, bool]]:
    """(character, dim) for every character the line shows."""
    found, dim, index = [], False, 0
    while index < len(line):
        if line[index] != ESC:
            found.append((line[index], dim))
            index += 1
            continue
        index, params = _sequence_end(line, index)
        if params is not None:
            dim = _dim_after(params, dim)
    return found


def plain(line: str) -> str:
    """The characters a line shows, without escape sequences."""
    return "".join(char for char, _ in cells(line))


def is_rule(text: str) -> bool:
    shown = text.rstrip(" ")
    return len(shown) >= MIN_RULE_LENGTH and set(shown) == {RULE}


def read(text: str, settings: Dict[str, Any]) -> Screen:
    """The input box, what is typed in it and whether a spinner runs above it."""
    lines = text.split("\n")
    shown = [plain(line) for line in lines]
    rules = [number for number, line in enumerate(shown) if is_rule(line)]
    busy_pattern = re.compile(settings["busy_pattern"])
    if len(rules) < 2 or rules[-1] - rules[-2] != 2:
        return Screen(False, None, any(busy_pattern.search(line) for line in shown))
    top = rules[-2]
    busy = any(busy_pattern.search(line) for line in shown[:top])
    input_cells = cells(lines[top + 1])
    prompt = re.match(settings["prompt_pattern"], "".join(char for char, _ in input_cells))
    if prompt is None:
        return Screen(False, None, busy)
    typed = "".join(char for char, dim in input_cells[prompt.end():] if not dim).rstrip(" ")
    shown = [index for index in range(prompt.end(), len(input_cells))
             if not input_cells[index][1] and input_cells[index][0] != " "]
    last = shown[-1] + 1 if shown else prompt.end()
    return Screen(True, typed, busy, top + 1, sum(width(char) for char, _ in input_cells[:last]))


def idle_problem(screen: Screen) -> Optional[str]:
    """Why /clear must not be typed on this screen (a key of state_fields.CLEAR_ABORTS); None when it may."""
    if not screen.box:
        return "no_input_box"
    if screen.busy:
        return "busy"
    if screen.typed:
        return "input"
    return None
