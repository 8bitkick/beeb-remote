"""Claude's MODE 7 status page, as the *CLAUDE ROM showed it (pi/teletext.js, ported): a white
bar with Claude's status and progress, the prompt, the plan as a checklist, the code in green.
page() gives the 1000 bytes; show() puts them on the BBC's screen during one stop."""
import re

COLS, ROWS = 40, 25
RED, GREEN, YELLOW, BLUE, WHITE = 129, 130, 131, 132, 135
G_GREEN, G_YELLOW, G_WHITE = 146, 147, 151
SOLID, NEW_BACKGROUND = 127, 157
PLAN_ROWS, TAIL_FIRST_ROW, FOOTER_ROW = 7, 12, 24
UNSAFE = re.compile(r"[#\[\]\\^_`{|}~]")       # the SAA5050 shows these as other symbols


def clean(text):
    return UNSAFE.sub(" ", re.sub(r"[^\x20-\x7e]", "", str(text)))


def wrap(text, width):
    lines, line = [], ""
    for w in clean(text).split():
        if (len(line) + 1 if line else 0) + len(w) > width:
            if line:
                lines.append(line)
            line = w[:width]
        else:
            line = f"{line} {w}" if line else w
    if line:
        lines.append(line)
    return lines


def codes(s):
    return [ord(c) for c in clean(s)]


def bar(status, right=""):
    """Row 0: white background, a red asterisk, CLAUDE and the status in blue, right at the end."""
    lead = [WHITE, NEW_BACKGROUND, RED, *codes("*"), BLUE]
    right = clean(right)
    room = COLS - len(lead) - len(right) - 1
    left = f"CLAUDE {clean(status)}"[:room].ljust(room)
    return (lead + codes(left) + codes(f" {right}"))[:COLS]


def page(status="", right="", prompt="", plan=(), step=-1, tail="", footer=""):
    rows = [[] for _ in range(ROWS)]
    rows[0] = bar(status, right)
    r = 1
    for line in wrap(f"> {prompt}", COLS - 1)[:2]:
        rows[r] = [YELLOW, *codes(line)]
        r += 1
    r += 1
    for i, s in enumerate(list(plan)[:PLAN_ROWS]):
        g, a = (G_GREEN, GREEN) if i < step else (G_YELLOW, YELLOW) if i == step else (G_WHITE, WHITE)
        rows[r] = [g, SOLID, a, *codes((wrap(s, COLS - 3) or [""])[0])]
        r += 1
    if tail:
        lines = []
        for raw in re.sub(r"```\w*", "", tail).split("\n"):
            raw = clean(raw)
            for i in range(0, max(1, len(raw)), COLS - 1):
                lines.append(raw[i:i + COLS - 1])
        shown = [l for i, l in enumerate(lines) if l.strip() or i < len(lines) - 1][-(FOOTER_ROW - TAIL_FIRST_ROW):]
        for i, line in enumerate(shown):
            rows[TAIL_FIRST_ROW + i] = [GREEN, *codes(line)]
    if footer:
        rows[FOOTER_ROW] = [BLUE, NEW_BACKGROUND, WHITE, *codes(footer)]
    out = bytearray(b" " * (COLS * ROWS))
    for i, row in enumerate(rows):
        row = row[:COLS]
        out[i * COLS:i * COLS + len(row)] = bytes(row)
    return bytes(out)


def show(bbc, data, first_row=0):
    """Write rows of a page to MODE 7's screen, wherever BASIC has scrolled its start to.  The caller
    makes sure the BBC is in MODE 7."""
    with bbc.stopped():
        v = bbc.peek(0x0350, 2)
        top = v[0] | v[1] << 8
        start = (top - 0x7C00 + first_row * COLS) & 0x3FF
        first = min(len(data), 0x400 - start)
        bbc.poke(0x7C00 + start, data[:first])
        if first < len(data):
            bbc.poke(0x7C00, data[first:])
