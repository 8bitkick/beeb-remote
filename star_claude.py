#!/usr/bin/env python3
"""*CLAUDE <prompt> on the BBC, with no ROM: a hook on the filing system vector catches the command,
and this program, watching from another computer, runs Claude on the prompt (agent.py).

    ANTHROPIC_API_KEY=... python3 star_claude.py --host Pi1MHz.local [--run]

Leave it running.  It puts the hook in (and back after a Break) by itself, once the probe is
installed (X%=23:CALL &FC88).  On the BBC, at the BASIC prompt:

    *CLAUDE a bouncing ball

How the BBC tells it without being stopped: the hook keeps the prompt in the BBC's RAM, and
writes "CLDE" and a count into the last page of the Pi1MHz's shared memory through the services
port; this program reads that with GET /bbc/shared twice a second.  Every 20 s, and after every
reset, one stop checks the hook (a Break or a program may have taken it) and reads the prompt.
Stops come in by the probe's ladder, never by NMI unless --max-rung 2 allows it.

The BBC's RAM it takes: &0900-&09C8 (the prompt) and &0A00-&0AFF (the hook), the cassette and
RS423 buffers, so not with either in use.  &A8-&A9 while the command runs, which the OS keeps for
* commands.
"""
import argparse
import sys
import time

from asm6502 import asm
from bbc import BBC, ProbeError

HOOK, PROMPT, FSCV = 0x0A00, 0x0900, 0x021E
PROMPT_MAX = 200
DOORBELL = b"CLDE"             # then the count, in shared memory's last page
CHECK_EVERY = 20.0
RING_EVERY = 0.5

SOURCE = """
hook:   CMP #3                  ; FSCV 3: a * command nothing else took, at (X,Y)
        BEQ ours
        JMP (oldv)
ours:   PHA
        STX &A8
        STY &A9
        LDY #0
skip:   LDA (&A8),Y             ; past spaces and stars
        CMP #32
        BEQ next
        CMP #42
        BNE word
next:   INY
        BNE skip
word:   LDX #0
match:  LDA (&A8),Y
        AND #&DF                ; either case
        CMP name,X
        BEQ same
        JMP no
same:   INY
        INX
        CPX #6
        BNE match
        LDA (&A8),Y             ; the word ends there
        CMP #33
        BCC text
        JMP no
text:   LDX #m_use-msgs         ; (leading spaces go with the prompt; the watcher strips them)
        CMP #13
        BEQ say
        LDX #m_busy-msgs
        LDA busy
        BNE say
        LDX #0
copy:   LDA (&A8),Y             ; the prompt, to PROMPT, ending in CR
        STA PROMPT,X
        CMP #13
        BEQ copied
        INY
        INX
        CPX #PROMPT_MAX
        BNE copy
        LDA #13
        STA PROMPT,X
copied: LDA #1
        STA busy                ; until the watcher has done it
        INC seq
        LDA &FCA6               ; the program's services port pointer, for after
        PHA
        LDA &FCA7
        PHA
        LDA &FCA8
        PHA
        LDA #BELLLO             ; the port at the doorbell, in shared memory
        STA &FCA6
        LDA #BELLMID
        STA &FCA7
        LDA #BELLHI
        STA &FCA8
        LDX #0                  ; C L D E, then the count
bell:   LDA ring,X
        STA &FCA9
        INX
        CPX #5
        BNE bell
        PLA
        STA &FCA8
        PLA
        STA &FCA7
        PLA
        STA &FCA6
        LDX #m_ok-msgs
say:    LDA msgs,X
        BEQ said
        JSR &FFE3
        INX
        BNE say
said:   PLA
        LDA #0
        RTS
no:     LDX &A8
        LDY &A9
        PLA
        JMP (oldv)
oldv:   BYTES 00 00
busy:   BYTES 00
ring:   TEXT CLDE
seq:    BYTES 00
name:   TEXT CLAUDE
msgs:
m_ok:   TEXT Claude is on it\\r
        BYTES 00
m_busy: TEXT Claude is busy\\r
        BYTES 00
m_use:  TEXT *CLAUDE <prompt>\\r
        BYTES 00
"""
def assemble(bell=0):
    """The hook, ringing the doorbell at services offset `bell`."""
    global CODE, LABELS, OLDV, SEQ, BUSY
    CODE, LABELS = asm(SOURCE, HOOK, {"PROMPT": PROMPT, "PROMPT_MAX": PROMPT_MAX, "BELLLO": bell & 0xFF,
                                      "BELLMID": (bell >> 8) & 0xFF, "BELLHI": bell >> 16})
    OLDV, SEQ, BUSY = (LABELS[k] - HOOK for k in ("oldv", "seq", "busy"))
    assert len(CODE) <= 0x100, len(CODE)


assemble()


def hook_bytes(oldv, seq=0):
    b = bytearray(CODE)
    b[OLDV:OLDV + 2] = bytes([oldv & 0xFF, oldv >> 8])
    b[SEQ] = seq
    return bytes(b)


def intact(page):
    """The hook's code is there, whatever its vector, count and flag hold."""
    return page[:OLDV] == CODE[:OLDV] and page[BUSY + 1:SEQ] == CODE[BUSY + 1:SEQ] \
        and page[SEQ + 1:] == CODE[SEQ + 1:]


class Watcher:
    def __init__(self, bbc, log=print):
        self.bbc, self.log = bbc, log
        shared = bbc.shared()
        self.bell = shared["size"] - 0x100        # the doorbell: shared memory's last page
        assemble(shared["at"] + self.bell)
        self.seen = None               # the hook's count, as last handled

    def doorbell(self):
        """Has the BBC rung, by the bytes the hook put in shared memory?  No stop."""
        b = self.bbc.shared_read(self.bell, 5)
        return b[:4] == DOORBELL and b[4] != self.seen

    def check(self):
        """One stop: put the hook in if it isn't, and return a prompt waiting, if there is one."""
        bbc = self.bbc
        if bbc.soft_installed():               # bbc.py's resident routine has &0A00
            self.log("[the resident routine for typing is at &0A00: beeb.py soft-uninstall, for *CLAUDE]")
            return None
        with bbc.stopped():
            v = bbc.peek(FSCV, 2)
            fscv = v[0] | v[1] << 8
            page = bbc.peek(HOOK, len(CODE))
            if fscv != HOOK or not intact(page):
                oldv = fscv if fscv != HOOK else page[OLDV] | page[OLDV + 1] << 8
                bbc.poke(HOOK, hook_bytes(oldv, page[SEQ]))
                bbc.poke(FSCV, bytes([HOOK & 0xFF, HOOK >> 8]))
                self.seen = page[SEQ]
                self.log(f"[*CLAUDE is in: the hook at &{HOOK:04X}, then FSCV's &{oldv:04X}]")
                return None
            seq, busy = page[SEQ], page[BUSY]
            if busy and seq != self.seen:
                text = bbc.peek(PROMPT, PROMPT_MAX + 1).split(b"\r")[0].decode("latin-1").strip()
                self.seen = seq
                if text:
                    return text
                bbc.poke(HOOK + BUSY, b"\0")          # *CLAUDE and only spaces
                return None
            if busy:                           # one handled before this watcher started
                bbc.poke(HOOK + BUSY, b"\0")
            self.seen = seq
        return None

    def finished(self):
        """Let the hook take another."""
        self.bbc.poke(HOOK + BUSY, b"\0")

    def watch(self, handle):
        resets, next_check, waiting_said = None, 0.0, False
        while True:
            try:
                s = self.bbc.status()
                if not s["installed"] or s["state"] != "running":
                    if not s["installed"] and not waiting_said:
                        self.log(f"[waiting for the probe: type {BBC.install_line()} on the BBC]")
                        waiting_said = True
                    time.sleep(1.0)
                    continue
                waiting_said = False
                now = time.monotonic()
                if s["resets"] != resets or now >= next_check or self.doorbell():
                    resets, next_check = s["resets"], now + CHECK_EVERY
                    prompt = self.check()
                    if prompt:
                        self.log(f"[*CLAUDE {prompt}]")
                        try:
                            handle(prompt)
                        finally:
                            self.finished()
                        next_check = 0.0
                time.sleep(RING_EVERY)
            except ProbeError as e:
                self.log(f"[{e}]")
                time.sleep(2.0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="Pi1MHz.local")
    ap.add_argument("--run", action="store_true", help="RUN each program once it's typed")
    ap.add_argument("--echo", action="store_true", help="no Claude: show each prompt on the BBC (a test)")
    ap.add_argument("--max-rung", type=int, default=1, choices=(0, 1, 2),
                    help="the probe's ladder: 2 allows an NMI, the last resort (default 1: never)")
    o = ap.parse_args()
    bbc = BBC(o.host, max_rung=o.max_rung)
    if o.echo:
        import teletext

        def handle(prompt):
            teletext.show(bbc, teletext.page("heard you", "", prompt, footer="*CLAUDE, echo test"))
    else:
        import anthropic
        from agent import run_prompt
        client = anthropic.Anthropic()

        def handle(prompt):
            run_prompt(client, bbc, prompt, run=o.run)
    print(f"[watching {o.host} for *CLAUDE]")
    try:
        Watcher(bbc).watch(handle)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
