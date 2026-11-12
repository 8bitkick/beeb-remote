#!/usr/bin/env python3
"""The Rick roll on a BBC Micro at 50 frames a second: MODE 7, then MODE 0, 1 and 2, with the
palette chosen for each scene.  Played from the Pi1MHz's shared memory, with no network traffic
and no stops.

    python3 demo50.py ~/Videos/rick50.mp4                      # encode (cached), upload, play
    python3 demo50.py ~/Videos/rick50.mp4@40 ~/Videos/rick50.mp4@100 --plan 7:8,0:8,1:8,2:8
    python3 demo50.py ~/Videos/rick50.mp4 --plan 7:20,0:20,1:20 --encode-only
    python3 demo50.py ~/Videos/bunny.mov@8 --plan 1:15 --lines         # a palette for every line

Every frame of the 50 fps source is drawn: nothing is skipped and nothing is faked.  The BBC is
taken over completely (interrupts off, the vsync read from the System VIA), and the encoder
knows the player's cost in cycles for every byte and every code it emits.  A frame that has
more changes than fit in the 20 ms is drawn as far as the budget allows and the rest, the
longest-waiting first, goes into the next frame.  The player counts the frames it started late,
so the result can be checked rather than claimed.  Break stops it (and the probe reinstalls).

The copy is 10 cycles a byte: for each of the 80 pages of screen memory there is an unrolled
chain of LDA &FCA9 / STA page+k,Y at &0E00, entered part way in for the length wanted (the
bytes of a chain come last first).  A read of the 1 MHz bus costs 4 cycles plus 1 if it starts
on an even cycle, 2 if odd, and leaves the clock even; the chain's 8-cycle gap keeps every read
on the cheap side.  The player changes mode itself from the OS's own CRTC tables, at a vsync,
and doesn't clear the screen: the first MODE 0 frame is drawn into the 19 KB that MODE 7
doesn't show while MODE 7 is still playing, so the change is instant.  A mode change gets
40 ms.

The stream, in shared memory from its start, read through the services port (GET /bbc gives
shared memory's services offset):
  header  0 the end; &7F a new mode: [the mode's table offset + 1, bit 6: the CRTC too, after
          which the player starts again from the vsync after that], set at the next vsync; else
          bits 0-5 the vsyncs to wait for before this frame is drawn (so the previous frame's
          drawing time), bit 7 set: 16 bytes for the video ULA's palette register come first
  ops     [entry low][Y] (+ data last byte first): jump to entry in the current code page with
          Y: a chain copies 1-15 bytes to its page + Y; a page stub makes Y the code page; the
          end stub (in the player's page) ends the frame; the special stub (in every code page
          but the player's) with Y < &80 sets one MODE 1 colour, with Y >= &80 is a pad

With --lines, MODE 1 has a palette for every line (Kieran Connell's idea): each line may change
one of its four colours from the line above's, written in the horizontal blank before it.  The
encoder knows every op's cost to the cycle (a 1 MHz read ends on an even cycle, so each op's cost
is fixed), so it places each palette op on its exact cycle from the frame's first op and fills
the time between with copies and pads.  The palette op costs 60 cycles of copying time, and
fitting the copies around the ops costs some more.  The lines' colours are
chosen top down with a little of each line's error carried to the next, so lines mix into
colours neither has: dithering down the screen as well as across.  Checked in beebjit, frame by
frame against what the encoder meant, and played on a real Model B: 750 frames at 50.0 a second,
none late.
"""
import argparse
import copy
import itertools
import os
import pickle
import subprocess
import sys
import time

import numpy as np

from asm6502 import asm
from bbc import BBC, ProbeError
import bitmap_play as bp
import ttx_stream as ttx

PLAYER, BLOCKS = 0x0900, 0x0E00
SCREEN, SCREEN_LEN = 0x3000, 0x5000
PAGES = SCREEN_LEN >> 8
CHAIN = 15                                 # the longest copy in one op
STREAM_SIZE = 0x3FFF00                     # the 4 MB of shared memory, less its last page (*CLAUDE's doorbell)
NEWMODE = 0x7F
MODE_TABLE = {7: 0, 0: 17, 1: 34, 2: 51}   # the mode's 17-byte table in the player
CRTC_TOO = 0x40                            # the new-mode operand: table + 1, and this if the CRTC changes
PHYS = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0], [0, 0, 1], [1, 0, 1], [0, 1, 1], [1, 1, 1]], dtype=float)
POP = np.array([bin(i).count("1") for i in range(256)], dtype=np.int64)

# ---- the player's cost, in 2 MHz cycles (see the note on the 1 MHz bus above) ----
PIECE = 18                 # LDA &FCA9 (5) STA &72 (3) LDY &FCA9 (5) JMP (&0072) (5)
COPY = 10                  # LDA &FCA9 (5) STA abs,Y (5)
SETPAGE = PIECE + 6        # ... STY &73, JMP disp
END = PIECE + 3            # the end stub: JMP frame
HEADER = 110               # the header, the vsync wait, the first dispatch
PALETTE = 220              # writing the 16 palette bytes after the vsync (reading them is before it)
MODESET = 420              # setting the mode
CYCLES_PER_VSYNC = 39936   # 312 lines of 64 us at 2 MHz: 50.08 Hz

MAIN = f"""
start:  SEI
        LDA #0
        STA frames
        STA frames+1
        STA late
        STA &FCA6               ; the services port at the stream: shared memory's start,
        STA &FCA7               ; on a 64 KB boundary
        LDA #SHARED_HI
        STA &FCA8
        LDA #0
        STA pending
        LDA #{PLAYER >> 8}
        STA &73                 ; the code page: this one
frame:  LDA &FCA9               ; the header
        BNE fr
        JMP fin
fr:     CMP #{NEWMODE}
        BNE fr0
        LDA &FCA9               ; a new mode, at the next vsync
        STA pending
        JMP frame
fr0:    TAX
        AND #&3F
        STA cnt
        INC frames
        BNE fr1
        INC frames+1
fr1:    TXA
        BPL wait                ; X stays positive: no palette
        LDX #15
pal:    LDA &FCA9               ; the palette: 16 bytes, written after the vsync
        STA palbuf,X
        DEX
        BPL pal                 ; X ends at &FF
wait:   STX haspal
        LDA #2                  ; the System VIA's vsync flag (CA1)
        LDX cnt
        DEX
        BNE wn                  ; more than one vsync to wait for: no telling if we're late
        BIT &FE4D
        BEQ wn
        INC late                ; it had already gone: this frame starts late
wn:     BIT &FE4D
        BEQ wn
        STA &FE4D               ; clear it
        DEC cnt
        BNE wn
        BIT haspal
        BPL nopal
        LDX #15
p2:     LDA palbuf,X
        STA &FE21
        DEX
        BPL p2
nopal:  LDA pending
        BEQ disp
        JSR setmode
        LDA #0
        STA pending
disp:   LDA &FCA9               ; every op: the entry's low byte, then Y
        STA &72
        LDY &FCA9
        JMP (&0072)
setpg:  STY &73                 ; the page stub: the code page for the ops that follow
        JMP disp
endf:   JMP frame
setmode:                        ; A: bits 0-5 the mode's table + 1 (17 bytes: the latch pair, the
        PHA                     ; ULA, R0-R13), bit 6: the CRTC too
        AND #&3F
        TAX
        DEX
        LDA TAB,X
        STA &FE40
        LDA TAB+1,X
        STA &FE40
        LDA TAB+2,X
        STA &FE20
        PLA
        AND #&40
        BEQ smdone
        LDY #0
sm:     LDA TAB+3,X
        STY &FE00
        STA &FE01
        INX
        INY
        CPY #14
        BNE sm
        LDA #2                  ; the field after a CRTC change is cut short: start from the next vsync
        STA &FE4D
rs:     BIT &FE4D
        BEQ rs
        STA &FE4D
smdone: RTS
fin:    CLI
        LDA #22                 ; leave the machine in MODE 7 for the OS
        JSR &FFEE
        LDA #7
        JSR &FFEE
        RTS
pending: BYTES 00
cnt:    BYTES 01
frames: BYTES 00 00
late:   BYTES 00
haspal: BYTES 00
palbuf: BYTES 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
"""
BLOCK = 6 * CHAIN + 11 + 5
# R8 is 0, not interlaced as the OS has it: every field is then 312 lines, so the raster is in the
# same place after every vsync, which the line palettes need.
TABLES = bytes.fromhex("0C 0D 4B 3F 28 33 24 1E 02 19 1B 93 12 20 13 28 00"    # MODE 7 (OS 1.20's, cursor off)
                       "04 0D 9C 7F 50 62 28 26 00 20 22 00 07 20 08 06 00"    # MODE 0
                       "04 0D D8 7F 50 62 28 26 00 20 22 00 07 20 08 06 00"    # MODE 1
                       "04 0D F4 7F 50 62 28 26 00 20 22 00 07 20 08 06 00")   # MODE 2
SPECIAL = 0x0B00                           # the line palette and the pads (see LINES below)
# A block for each page of screen memory: the chain (its last unit writes offset 0, so entering
# at unit CHAIN-n copies n bytes to page+Y+n-1 down to page+Y), its own dispatch, its page stub.
# Every code page also has a JMP SPECIAL, so a line palette or a pad can come from any page.
BLOCK_SRC = "\n".join(f"        LDA &FCA9\n        STA &{{page:02X}}{CHAIN - 1 - i:02X},Y" for i in range(CHAIN)) + """
disp:   LDA &FCA9
        STA &72
        LDY &FCA9
        JMP (&0072)
setpg:  STY &73
        JMP disp
"""
STUB = bytes((0x4C, SPECIAL & 255, SPECIAL >> 8))
BLOCKS_CODE, BLOCK_AT, SPECIAL_LO, SETPG_LO = bytearray(), [], {}, {}
for i in range(PAGES):
    at = BLOCKS + len(BLOCKS_CODE)
    if at >> 8 not in SPECIAL_LO:
        SPECIAL_LO[at >> 8] = at & 255
        BLOCKS_CODE += STUB
        at += 3
    code, bl = asm(BLOCK_SRC.format(page=(SCREEN >> 8) + i), at)
    assert len(code) == BLOCK and bl["setpg"] == at + 6 * CHAIN + 11
    SETPG_LO.setdefault(bl["setpg"] >> 8, bl["setpg"] & 255)
    BLOCK_AT.append(at)
    BLOCKS_CODE += code
TAB = BLOCKS + len(BLOCKS_CODE)            # the mode tables, after the blocks
BLOCKS_CODE += TABLES
end = BLOCKS + len(BLOCKS_CODE)
if (end - 1) >> 8 not in SPECIAL_LO:
    SPECIAL_LO[(end - 1) >> 8] = end & 255
    BLOCKS_CODE += STUB
BLOCKS_CODE = bytes(BLOCKS_CODE)
assert BLOCKS + len(BLOCKS_CODE) <= SCREEN, hex(BLOCKS + len(BLOCKS_CODE))
MAIN_CODE, L = asm(MAIN, PLAYER, {"TAB": TAB, "SHARED_HI": 0})      # (the labels; see main_code)
assert len(MAIN_CODE) <= 0x100, len(MAIN_CODE)
SETPG_LO[PLAYER >> 8] = L["setpg"] & 255
assert all(p in SETPG_LO and p in SPECIAL_LO for p in range(BLOCKS >> 8, (TAB - 1) >> 8)), (SETPG_LO, SPECIAL_LO)


def entry(addr, n):
    """The address of the chain copying n bytes to the screen address addr and up."""
    return BLOCK_AT[(addr >> 8) - (SCREEN >> 8)] + 6 * (CHAIN - n)


# ---- line palettes (MODE 1): a colour changed in the horizontal blank before any line ----------
# An op [SPECIAL_LO[page]][Y]: Y < &80 writes logical colour Y >> 3 as physical colour Y & 7 (its
# four entries of the video ULA's palette, from the tables at PALTAB); Y >= &80 is a pad, entering
# the run of NOPs at &0B00 + Y.  The encoder knows every op's cost to the cycle, so it starts each
# line's palette op where its writes land in that line's blank.
PALTAB = 0x0C00
SPECIAL_SRC = f"""
special: CPY #&80
        BCS pad
        LDA &{PALTAB:04X},Y
        LDX &{PALTAB + 32:04X},Y
        STA &FE21
        STX &FE21
        LDA &{PALTAB + 64:04X},Y
        STA &FE21
        LDA &{PALTAB + 96:04X},Y
        STA &FE21
        JMP &{L["disp"]:04X}
pad:    STY &76
        JMP (&0076)
"""
SLED_AT, SLED_NOPS = 0x80, 120
SPECIAL_CODE = asm(SPECIAL_SRC, SPECIAL)[0]


def main_code(shared_at):
    """The player for shared memory at services offset shared_at (GET /bbc's shared.at)."""
    if shared_at & 0xFFFF:
        raise ProbeError(f"shared memory at &{shared_at:06X}: the player wants it on a 64 KB boundary")
    return asm(MAIN, PLAYER, {"TAB": TAB, "SHARED_HI": shared_at >> 16})[0]
assert len(SPECIAL_CODE) <= SLED_AT
SPECIAL_CODE = (SPECIAL_CODE.ljust(SLED_AT, b"\xEA") + b"\xEA" * SLED_NOPS + bytes((0x24, 0x70))
                + bytes((0x4C, L["disp"] & 255, L["disp"] >> 8)))
assert len(SPECIAL_CODE) <= 0x100
M1_ENTRIES = [[e for e in range(16) if ((e >> 3) & 1) * 2 + ((e >> 1) & 1) == c] for c in range(4)]
PALTAB_CODE = bytes((M1_ENTRIES[i >> 3][k] << 4) | ((i & 7) ^ 7) for k in range(4) for i in range(32))
PAL_OP = 60                # its writes at 35, 39, 47 and 55 cycles from its start
PAD_MIN, PAD_MAX = 40, 40 + 2 * SLED_NOPS


def pad_y(cycles):
    """The Y of the pad op costing cycles (even, PAD_MIN to PAD_MAX)."""
    assert cycles % 2 == 0 and PAD_MIN <= cycles <= PAD_MAX, cycles
    return SLED_AT + SLED_NOPS - (cycles - PAD_MIN) // 2


MODES = {7: dict(w=240, h=250, at=0x7C00 - SCREEN, size=1000),
         0: dict(w=640, h=256, at=0, size=SCREEN_LEN),
         1: dict(w=320, h=256, at=0, size=SCREEN_LEN),
         2: dict(w=160, h=256, at=0, size=SCREEN_LEN)}


def source(path, start, seconds, w, h, fps, grey=False):
    vf = f"fps={fps},crop='min(iw,ih*4/3)':'min(ih,iw*3/4)',scale={w}:{h},format={'gray' if grey else 'rgb24'}"
    ch = 1 if grey else 3
    p = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-ss", str(start), "-t", str(seconds), "-i", path,
                          "-vf", vf, "-f", "rawvideo", "-"], stdout=subprocess.PIPE)
    try:
        while True:
            buf = p.stdout.read(w * h * ch)
            if len(buf) < w * h * ch:
                return
            a = np.frombuffer(buf, dtype=np.uint8)
            yield a.reshape(h, w) if grey else a.reshape(h, w, 3).astype(np.float64) / 255
    finally:
        p.kill()


class Smooth:
    """Noise out, motion in: a pixel that barely moved is averaged with its past, one that
    moved is taken as it comes (so no motion blur).  A cut starts afresh."""
    def __init__(self):
        self.sm = None

    def __call__(self, f, cut):
        if self.sm is None or cut:
            self.sm = f.copy()
        else:
            k = np.clip(np.abs(f - self.sm).mean(axis=2, keepdims=True) / 0.12, 0.15, 1.0)
            self.sm += k * (f - self.sm)
        return self.sm


BLACK = {7: 0, 0: 0.0, 1: 0.03, 2: 0.03}     # the black level: how much dither the shadows keep
CONTRAST = {7: 1, 0: 1.7, 1: 1.2, 2: 1.2}    # the slope about the pivot: steeper means more solid black and white
PIVOT = {7: 0.5, 0: 0.58, 1: 0.5, 2: 0.5}    # the grey that stays put: above the middle, more of the picture goes black


class Levels:
    """The frame's levels stretched to the full range, smoothed over time so the brightness
    doesn't pump from frame to frame; a cut starts afresh."""
    def __init__(self, mode):
        self.lo = self.hi = None
        self.black, self.contrast, self.pivot = BLACK[mode], CONTRAST[mode], PIVOT[mode]

    def __call__(self, rgb, cut):
        lo, hi = np.percentile(rgb, (1, 99))
        if self.lo is None or cut:
            self.lo, self.hi = lo, hi
        else:
            self.lo += 0.1 * (lo - self.lo)
            self.hi += 0.1 * (hi - self.hi)
        x = np.clip(0.5 + self.contrast * ((rgb - self.lo) / max(self.hi - self.lo, 1e-3) - self.pivot), 0, 1)
        return self.black + (1 - self.black) * x


def bayer(h, w):
    return np.tile(bp.BAYER, (h // 4 + 1, w // 4 + 1))[:h, :w]


EPS = {0: 0.08, 1: 0.35, 2: 0.35}   # MODE 0 in brightness; MODE 1 and 2 in squared colour distance: about 0.05 of brightness
AMPLITUDE = 0.5               # how far the ordered dither pushes a MODE 1 pixel about in colour


def dither(rgb, pal, prev=None, eps=0.0):
    """Logical colours.  Two colours: by brightness.  Four: the nearest of the four, the ordered
    dither pushing each pixel about in colour.  With prev (last frame's pixels), a pixel keeps
    its colour unless the new choice is clearly better by eps: the dither then doesn't shimmer
    on noise, which looks better and costs the BBC far less."""
    h, w, _ = rgb.shape
    t = bayer(h, w)
    if len(pal) == 2:
        lum = rgb @ np.array([0.299, 0.587, 0.114])
        pix = (lum > t).astype(np.uint8)
        if prev is not None:
            pix = np.where(np.abs(lum - t) < eps, prev, pix)
        return pix
    x = rgb + (t[:, :, None] - 0.5) * AMPLITUDE
    dist = ((x[:, :, None, :] - PHYS[list(pal)][None, None]) ** 2).sum(3)
    pix = dist.argmin(2).astype(np.uint8)
    if prev is not None:
        dprev = np.take_along_axis(dist, prev[:, :, None].astype(np.int64), 2)[:, :, 0]
        pix = np.where(dprev - dist.min(2) < eps, prev, pix)
    return pix


def pack0(pix):
    b = np.packbits(pix.astype(bool), axis=1)
    return b.reshape(32, 8, 80).transpose(0, 2, 1).reshape(SCREEN_LEN).tobytes()


def palette_error(rgb, pal):
    small = rgb[::8, ::8].reshape(-1, 3)
    return ((small[:, None, :] - PHYS[list(pal)][None]) ** 2).sum(2).min(1).mean()


COMBOS = {2: [(0, c) for c in range(1, 8)],
          4: [c for c in itertools.combinations(range(8), 4) if 0 in c]}


class Palette:
    """The best n of the BBC's 8 colours for the scene (black always one), kept until another
    is clearly better and a second has passed, or the scene cuts."""
    def __init__(self, n):
        self.n, self.pal, self.since = n, None, 0

    def __call__(self, rgb, cut):
        self.since += 1
        if self.n == 2:
            bright = rgb[rgb @ np.array([0.299, 0.587, 0.114]) > 0.5]
            mean = bright.mean(axis=0) if len(bright) else np.ones(3)
            best = (0, 1 + int(((PHYS[1:] - mean) ** 2).sum(1).argmin()))
            err = {best: 0.0, self.pal: 1.0} if self.pal else {best: 0.0}
        else:
            err = {c: palette_error(rgb, c) for c in COMBOS[4]}
            best = min(err, key=err.get)
        if self.pal is None or cut or (self.since >= 50 and err[best] < 0.75 * err.get(self.pal, 1e9)):
            if best != self.pal:
                self.pal, self.since = best, 0
                return self.pal, True
        return self.pal, False


def ula_palette(mode, pal):
    """The 16 bytes for the video ULA's palette register: entry << 4, colour EOR 7.  An entry's
    bits are the pixel's bits 7,5,3,1: in MODE 0 bit 3 is the pixel; in MODE 1 bits 3 and 1
    are its two bits; in MODE 2 all four are, 8-15 the same colours as 0-7 (no flashing)."""
    out = bytearray()
    for e in range(16):
        c = (e >> 3) & 1 if mode == 0 else ((e >> 3) & 1) * 2 + ((e >> 1) & 1) if mode == 1 else e & 7
        out.append((e << 4) | (pal[c] ^ 7))
    return bytes(out)


# ---- the frames a mode wants on the screen ----------------------------------------------------
def targets(path, mode, start, seconds, fps, table):
    """For each frame: (cut, the palette's 16 bytes or None, the bytes for the mode's part of
    screen memory)."""
    m = MODES[mode]
    smooth, levels, palette = Smooth(), Levels(mode), Palette(4) if mode == 1 else None
    if mode == 0:                                  # MODE 0: black and white, always
        first = [True]
        def palette(rgb, cut):
            was, first[0] = first[0], False
            return (0, 7), was
    colour, colour_since = 7, 0                    # MODE 7: the graphics colour code follows the scene
    last_small, prev_pix, n = None, None, 0
    lum = np.array([0.299, 0.587, 0.114])
    for f in source(path, start, seconds, m["w"], m["h"], fps):
        small = f[::8, ::8]
        cut = last_small is not None and np.abs(small - last_small).mean() > 0.12
        last_small = small
        if mode == 7:
            grey = np.clip(f @ lum * 255, 0, 255).astype(np.uint8)
            colour_since += 1
            if cut or colour_since > 100 or n == 0:
                bright = f[f @ lum > 0.5]
                mean = bright.mean(axis=0) if len(bright) else np.ones(3)
                want = 1 + int(((PHYS[1:] - mean) ** 2).sum(1).argmin())
                if want != colour:
                    colour, colour_since = want, 0
            cur = bytearray(ttx.encode(grey, table))
            cur[::40] = bytes([0x90 + colour]) * 25
            yield cut, None, bytes(cur)
        else:
            rgb = levels(smooth(f, cut), cut)
            pal, changed_pal = palette(rgb, cut) if palette else (tuple(range(8)), n == 0)   # MODE 2: all eight
            if changed_pal:
                prev_pix = None                        # new colours: every pixel chosen afresh
            pix = dither(rgb, pal, prev_pix, EPS[mode])
            prev_pix = pix
            yield cut, ula_palette(mode, pal) if changed_pal else None, pack0(pix) if mode == 0 else bp.pack(pix, mode)
        n += 1


# ---- MODE 1 with a palette for every line ---------------------------------------------------
LINE_COMBOS = [c for c in itertools.combinations(range(8), 4) if 0 in c]       # black always one
LINE_SETS = [set(c) for c in LINE_COMBOS]
LINE_CHANGES = np.array([[4 - len(a & b) for b in LINE_SETS] for a in LINE_SETS])
LINE_MASK = np.zeros((8, len(LINE_COMBOS)), dtype=bool)
for j, c in enumerate(LINE_COMBOS):
    LINE_MASK[list(c), j] = True
LINE_STAY = 0.02           # what keeping a line's colours from the last frame is worth: no flicker
LINE_CARRY = 0.4           # how much of a line's error the next line down takes: lines then mix
                           # into colours neither has, a dither down the screen as well as across


LINE_TUPLES = [t for t in itertools.permutations(range(8), 4) if 0 in t]   # colours in slot order
LINE_TUPLE_AT = {t: i for i, t in enumerate(LINE_TUPLES)}
LINE_TUPLE_SET = np.array([LINE_COMBOS.index(tuple(sorted(t))) for t in LINE_TUPLES])
LINE_NEXT = np.array([[i] + [LINE_TUPLE_AT[u] for u in LINE_TUPLES
                             if sum(a != b for a, b in zip(t, u)) == 1] for i, t in enumerate(LINE_TUPLES)])
LINE_TUPLE_ARR = np.array(LINE_TUPLES)


def unpack1(mem):
    """MODE 1 screen memory to logical colours, 256 x 320."""
    b = np.frombuffer(bytes(mem), np.uint8).reshape(32, 80, 8).transpose(0, 2, 1).reshape(256, 80)
    pix = np.zeros((256, 320), np.uint8)
    for p in range(4):
        pix[:, p::4] = ((b >> (7 - p)) & 1) * 2 + ((b >> (3 - p)) & 1)
    return pix


ROW_OF_BYTE = np.arange(SCREEN_LEN) // 640


class LineEncoder:
    """MODE 1 with a palette for every line, never showing old pixels in new colours: a character
    row (8 lines) changes any of its lines' colours only in a frame that also writes every byte of
    the row that changes.  Rows that can't this frame keep their colours (their pixels still move
    towards the picture in them) and go first next time; after a cut the new scene wipes in from
    the top.  The lines' colours are chosen together, down the screen (Viterbi over the colours in
    slot order: a line may change one slot from the line above's), each line scored on its own
    error plus LINE_STAY for differing from what it shows now."""
    def __init__(self):
        self.shown = None                          # each line's colours, as a LINE_TUPLES index
        self.wait = np.zeros(32)
        self.wiping = False                        # after a cut, until every row has the new scene
        self.t = bayer(256, 320)

    def line_errors(self, rgb):
        d8 = ((rgb[:, :, None, :] - PHYS[None, None]) ** 2).sum(3)              # 256 x 320 x 8
        return np.stack([d8[:, :, list(c)].min(2).mean(1) for c in LINE_COMBOS], 1)   # 256 x 35

    def choose(self, E, closed):
        n = len(LINE_TUPLES)
        Es = E[:, LINE_TUPLE_SET] + LINE_STAY * (np.arange(n)[None] != self.shown[:, None])
        lines_closed = np.repeat(closed, 8)
        Es[lines_closed] = np.inf
        Es[lines_closed, self.shown[lines_closed]] = E[lines_closed, LINE_TUPLE_SET[self.shown[lines_closed]]]
        cost, back = Es[0].copy(), np.zeros((256, n), dtype=np.int64)
        for y in range(1, 256):
            c = cost[LINE_NEXT]
            k = c.argmin(1)
            back[y] = LINE_NEXT[np.arange(n), k]
            cost = c[np.arange(n), k] + Es[y]
        st = np.zeros(256, dtype=np.int64)
        st[-1] = int(cost.argmin())
        for y in range(255, 0, -1):
            st[y - 1] = back[y, st[y]]
        return st

    def dither(self, rgb, st, mpix):
        pals = LINE_TUPLE_ARR[st]
        shown = LINE_TUPLE_ARR[self.shown]
        pix, carry = np.zeros((256, 320), np.uint8), np.zeros((320, 3))
        for y in range(256):
            t = rgb[y] + carry
            x = t + (self.t[y][:, None] - 0.5) * AMPLITUDE
            dist = ((x[:, None, :] - PHYS[pals[y]][None]) ** 2).sum(2)
            p = dist.argmin(1)
            q = mpix[y].astype(np.int64)
            same = pals[y][q] == shown[y][q]                  # what's on screen still means that colour
            p = np.where(same & (dist[np.arange(320), q] - dist.min(1) < EPS[1]), q, p)
            pix[y] = p
            carry = np.clip(LINE_CARRY * (t - PHYS[pals[y][p]]), -0.5, 0.5)
        return pix

    def frame(self, rgb, cut, screen, first=False, share=0.85):
        """(16 palette bytes for line 0, screen memory wanted, palette ops, the lines' colours, the
        bytes that must be written this frame)."""
        mpix = unpack1(screen.mem)
        if first or self.shown is None:
            pal = Palette(4)(rgb, True)[0]
            self.shown = np.full(256, LINE_TUPLE_AT[tuple(pal)])
            mpix[:] = 0
        E = self.line_errors(rgb)
        closed = np.zeros(32, dtype=bool)
        if first:
            closed[:] = True
        mem = np.frombuffer(bytes(screen.mem), np.uint8)
        while True:
            st = self.choose(E, closed)
            pix = self.dither(rgb, st, mpix)
            cur = np.frombuffer(bp.pack(pix, 1), np.uint8)
            if self.wiping or cut:                 # rows still to come keep the old scene: no copying
                cur = np.where(np.repeat(closed, 640), mem, cur)          # spent on what goes
            moved = (st != self.shown).reshape(32, 8).any(1) & ~closed
            diff = cur != mem
            row_cost = np.array([diff[r * 640:(r + 1) * 640].sum() for r in range(32)]) * moved
            ops = int((st[1:] != st[:-1]).sum())
            budget = share * (LINE_END - ops * PAL_OP) / COPY        # bytes this frame can surely take
            if row_cost.sum() <= budget:
                break
            # close rows until it fits: after a cut the bottom ones (a wipe from the top), else the
            # ones that have waited least, the costliest first
            cand = [r for r in range(32) if moved[r]]
            cand.sort(key=(lambda r: -r) if cut else (lambda r: (self.wait[r], -row_cost[r])))
            total = row_cost.sum()
            for r in cand:
                if total <= budget:
                    break
                closed[r] = True
                total -= row_cost[r]
        self.wait = np.where(closed, self.wait + 1, 0)
        self.wiping = bool(closed.any()) and (self.wiping or cut)
        must = np.repeat(moved, 640) & diff
        if self.wiping or cut:
            pix = np.where(np.repeat(closed, 8)[:, None], mpix, pix)
        self.shown = st
        pals = LINE_TUPLE_ARR[st]
        ops = [(y, k * 8 + int(pals[y][k])) for y in range(1, 256) for k in range(4) if pals[y][k] != pals[y - 1][k]]
        return ula_palette(1, tuple(int(c) for c in pals[0])), cur.tobytes(), ops, pals, must


def targets_lines(path, start, seconds, fps):
    """For each MODE 1 frame: (cut, the frame's levelled colours), for LineEncoder."""
    m = MODES[1]
    smooth, levels, last_small = Smooth(), Levels(1), None
    for f in source(path, start, seconds, m["w"], m["h"], fps):
        small = f[::8, ::8]
        cut = last_small is not None and np.abs(small - last_small).mean() > 0.12
        last_small = small
        yield cut, levels(smooth(f, cut), cut)


LINE_A = 4550              # cycles from a frame's first op to the start of line 1's palette op;
                           # its writes then land in the middle of the blank (measured in beebjit:
                           # 4535-4565 is clean)
LINE_END = 39000           # the frame's ops end by here, leaving the next header time before the vsync


# ---- the changes, costed ------------------------------------------------------------------
class Screen:
    """What the BBC's screen memory holds, and the ops that take it towards each frame within
    the frame's budget."""
    def __init__(self):
        self.mem = bytearray(SCREEN_LEN)
        self.known = np.zeros(SCREEN_LEN, dtype=bool)         # bytes the player has written
        self.age = np.zeros(SCREEN_LEN, dtype=np.int64)

    def pieces(self, cur, care, low):
        """Candidate pieces (start, end, weight, estimated cycles): the runs of bytes that
        differ (gaps of one bridged: a byte costs less than a piece), weighted by how long
        they have waited and how much they change; the low ones (drawn out of sight, for
        later) weigh least."""
        p, c = np.frombuffer(self.mem, dtype=np.uint8), np.frombuffer(cur, dtype=np.uint8)
        bits = POP[p ^ c]
        changed = care & ((bits > 0) | ~self.known)
        self.age = np.where(changed, self.age + 1, 0)
        w = np.where(changed, 2 * (self.age - 1) + np.minimum(bits, 3) + 1, 0)
        if low is not None:
            w[low & changed] = 1
        cw = np.concatenate(([0], np.cumsum(w)))
        d = np.diff(np.concatenate(([0], changed.astype(np.int8), [0])))
        starts, ends = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
        out = []
        for s, e in zip(starts, ends):
            if out and s - out[-1][1] <= 1:
                out[-1][1] = e
            else:
                out.append([s, e])
        res = []
        for s0, e0 in out:
            for s in range(s0, e0, 4 * CHAIN):             # long runs in parts, so a frame can take some
                e = min(s + 4 * CHAIN, e0)
                n = e - s
                est = -(-n // CHAIN) * PIECE + n * COPY + (((e - 1) >> 8) - (s >> 8) + 1) * SETPAGE // 2
                res.append((s, e, int(cw[e] - cw[s]), est))
        return res, changed

    def emit(self, cur, chosen):
        """The ops for the chosen pieces, in order, and their exact cost."""
        out, cost, page = bytearray(), HEADER, PLAYER >> 8

        def op(addr, y, data=b""):
            nonlocal cost, page
            if addr >> 8 != page:
                out.extend((SETPG_LO[page], addr >> 8))
                cost += SETPAGE
                page = addr >> 8
            out.extend((addr & 255, y))
            out.extend(data)

        for s, e in chosen:
            at = s
            while at < e:
                n = min(e - at, CHAIN)
                op(entry(SCREEN + at, n), (SCREEN + at) & 255, cur[at:at + n][::-1])
                cost += PIECE + n * COPY
                at += n
        op(L["endf"], 0)
        cost += END
        return bytes(out), cost

    def encode(self, cur, budget, care=None, low=None, wipe=False):
        """The ops for this frame, their cost, the bytes it changes and the bytes it leaves
        for later."""
        care = np.ones(SCREEN_LEN, dtype=bool) if care is None else care
        cands, changed = self.pieces(cur, care, low)
        if wipe or changed.mean() > 0.5:                 # a cut: top to bottom, a wipe
            order = sorted(cands, key=lambda x: x[0])
        else:
            order = sorted(cands, key=lambda x: (-x[2], x[0]))
        chosen, spent = [], HEADER + END + SETPAGE
        for s, e, w, est in order:
            if spent + est <= budget:
                chosen.append((s, e, w))
                spent += est
        chosen.sort()
        while True:
            codes, cost = self.emit(cur, [(s, e) for s, e, w in chosen])
            if cost <= budget or not chosen:
                break
            drop = min(range(len(chosen)), key=lambda i: chosen[i][2])
            chosen.pop(drop)
        for s, e, w in chosen:
            self.mem[s:e] = cur[s:e]
            self.known[s:e] = True
            self.age[s:e] = 0
        left = int((changed & (np.frombuffer(self.mem, dtype=np.uint8) != np.frombuffer(cur, dtype=np.uint8))).sum())
        return codes, cost, int(changed.sum()), left


    def encode_lines(self, cur, ops, wipe=False, must=None):
        """A MODE 1 frame with line palettes: each palette op starts on its cycle, LINE_A + 128 *
        line from the frame's first op, and the copies fill the time between, the longest-waiting
        bytes first, in address order, padded to the cycle."""
        care = np.ones(SCREEN_LEN, dtype=bool)
        cands, changed = self.pieces(cur, care, None)
        order = sorted(cands, key=lambda x: x[0]) if (wipe or changed.mean() > 0.5) else \
            sorted(cands, key=lambda x: (-x[2], x[0]))
        if must is not None:                               # the rows changing colour: all of them, first
            cm = np.concatenate(([0], np.cumsum(must)))
            order.sort(key=lambda x: cm[x[1]] == cm[x[0]])
        starts = [LINE_A + 128 * y for y, _ in ops]
        capacity = sum(b - a for a, b in zip([24] + [x + PAL_OP for x in starts], starts + [LINE_END]))
        chosen, spent = [], 0
        for s0, e0, w, est in order:
            if spent + est > 0.8 * capacity and not (must is not None and cm[e0] > cm[s0]):
                continue
            chosen.append((s0, e0))
            spent += est
        chunks = []
        for s0, e0 in sorted(chosen):
            for a in range(s0, e0, CHAIN):
                chunks.append([a, min(a + CHAIN, e0)])
        out, page, t = bytearray(), PLAYER >> 8, 0
        done = []

        def emit_pad(r):
            nonlocal t
            while r:
                c = min(r, PAD_MAX)
                if 0 < r - c < PAD_MIN:
                    c = r - PAD_MIN
                out.extend((SPECIAL_LO[page], pad_y(c)))
                t += c
                r -= c

        out.extend((SETPG_LO[page], BLOCK_AT[0] >> 8))              # leave the player's page
        page, t = BLOCK_AT[0] >> 8, 24
        ci = 0
        for g in range(len(ops) + 1):
            until = starts[g] if g < len(ops) else (LINE_END - SETPAGE - END) & ~1
            while ci < len(chunks):
                a, e = chunks[ci]
                addr0 = SCREEN + a
                pg = entry(addr0, 1) >> 8                           # the chain's page, for 1 byte
                n = e - a
                fit = 0
                for k in range(n, 0, -1):
                    ent = entry(SCREEN + e - k, k)
                    c = (SETPAGE if ent >> 8 != page else 0) + PIECE + k * COPY
                    r = until - t - c
                    if r == 0 or r >= PAD_MIN:
                        fit = k
                        break
                if not fit:
                    break
                k = fit
                ent = entry(SCREEN + e - k, k)
                if ent >> 8 != page:
                    out.extend((SETPG_LO[page], ent >> 8))
                    page = ent >> 8
                    t += SETPAGE
                out.extend((ent & 255, (SCREEN + e - k) & 255))
                out.extend(cur[e - k:e][::-1])
                t += PIECE + k * COPY
                done.append((e - k, e))
                if k == n:
                    ci += 1
                else:
                    chunks[ci][1] = e - k                              # the rest, later
            emit_pad(until - t)
            if g < len(ops):
                out.extend((SPECIAL_LO[page], ops[g][1]))
                t += PAL_OP
        if page != PLAYER >> 8:
            out.extend((SETPG_LO[page], PLAYER >> 8))
            t += SETPAGE
        out.extend((L["endf"] & 255, 0))
        t += END
        for a, e in done:
            self.mem[a:e] = cur[a:e]
            self.known[a:e] = True
            self.age[a:e] = 0
        unequal = np.frombuffer(self.mem, dtype=np.uint8) != np.frombuffer(cur, dtype=np.uint8)
        left = int((changed & unequal).sum())
        self.missed = int((must & unequal).sum()) if must is not None else 0
        return bytes(out), t, int(changed.sum()), left


PREDRAW = 12          # MODE 7 frames during which the next mode's first frame is drawn out of sight


def encode_all(path, plan, start, fps, budget, log=print, lines=False, on_frame=None, hold=False):
    """The whole stream for the plan [(mode, seconds), ...], and a report per mode.  A frame's
    header carries the vsyncs to wait for before it: the frame before it has that long to
    draw.  A mode change gets 40 ms: a CRTC change costs a field, after which its frame has one;
    a change of ULA mode alone gives its frame two."""
    table = ttx.lookup(1)
    ivl = max(1, round(50 / fps))
    screen = Screen()
    data, at, report = bytearray(), start, []
    gens = []
    for mode, secs in plan:
        gens.append(targets_lines(path, at, secs, fps) if lines and mode == 1 else targets(path, mode, at, secs, fps, table))
        at += secs
    line_enc = LineEncoder()
    for i, (mode, secs) in enumerate(plan):
        m = MODES[mode]
        care = np.zeros(SCREEN_LEN, dtype=bool)
        care[m["at"]:m["at"] + m["size"]] = True
        frames = list(gens[i]) if mode == 7 else gens[i]
        total = len(frames) if mode == 7 else None
        ahead = None                                   # the next mode's first frame, drawn out of sight
        if mode == 7 and i + 1 < len(plan) and plan[i + 1][0] != 7 and not (lines and plan[i + 1][0] == 1):
            first = next(gens[i + 1], None)
            if first is not None:
                gens[i + 1] = itertools.chain([first], gens[i + 1])
                ahead = first[2]
        crtc = i == 0 or (plan[i - 1][0] == 7) != (mode == 7)    # MODE 7's CRTC differs from the bitmap modes'
        data += bytes((NEWMODE, (MODE_TABLE[mode] + 1) | (CRTC_TOO if crtc else 0)))
        switch = ivl if crtc else 2 * ivl                  # a CRTC change costs a field; else 40 ms to draw in
        n, backlog, size0 = 0, [], len(data)
        for k, fr in enumerate(frames):
            line_ops = line_pals = must = None
            if lines and mode == 1:                        # the first frame has the mode change's time
                if hold and k > 0 and line_enc.wait.any():   # rows still to draw: this field draws the
                    fr = (False, held)                       # same frame again (the source frame is dropped)
                held = fr[1]
                share = 0.85
                while True:                                # the rows changing colour must fit: else fewer
                    enc0, screen0 = copy.deepcopy(line_enc), copy.deepcopy(screen)
                    pal, cur_part, line_ops, line_pals, must = line_enc.frame(fr[1], fr[0], screen, first=k == 0, share=share)
                    if k == 0:
                        break
                    cur = bytearray(screen.mem)
                    cur[m["at"]:m["at"] + m["size"]] = cur_part
                    screen.encode_lines(bytes(cur), line_ops, wipe=fr[0], must=must)
                    ok = screen.missed == 0
                    line_enc, screen = enc0, screen0
                    if ok:
                        pal, cur_part, line_ops, line_pals, must = line_enc.frame(fr[1], fr[0], screen, first=False, share=share)
                        break
                    share *= 0.8
                cut = fr[0]
                if k == 0:
                    line_ops = None
            else:
                cut, pal, cur_part = fr
            cur = bytearray(screen.mem)
            cur[m["at"]:m["at"] + m["size"]] = cur_part
            fcare, low = care, None
            if ahead is not None and k >= total - PREDRAW:
                cur[:m["at"]] = ahead[:m["at"]]
                fcare = np.ones(SCREEN_LEN, dtype=bool)
                low = np.zeros(SCREEN_LEN, dtype=bool)
                low[:m["at"]] = True
            vsyncs = switch if k == 0 else ivl
            frame_budget = vsyncs * CYCLES_PER_VSYNC * budget - (PALETTE if pal else 0) - (MODESET if k == 0 else 0)
            if line_ops is not None:
                codes, cost, changed, left = screen.encode_lines(bytes(cur), line_ops, wipe=cut, must=must)
                assert screen.missed == 0, f"frame {k}: {screen.missed} bytes of rows changing colour not written"
            else:
                codes, cost, changed, left = screen.encode(bytes(cur), frame_budget, fcare, low, wipe=cut)
            data.append((switch if k == 1 else ivl) | (0x80 if pal else 0))
            if pal:
                data += pal
            data += codes
            n += 1
            backlog.append(left)
            if on_frame:
                on_frame(mode, k, bytes(screen.mem), pal, line_pals if line_ops is not None else None)
            if n % 250 == 0:
                log(f"  MODE {mode}: {n} frames, {(len(data) - size0) // 1024} KB")
        report.append((mode, n, len(data) - size0, np.array(backlog)))
    data.append(0)
    return bytes(data), report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", nargs="+", help="path, or path@seconds to start there; played one after another")
    ap.add_argument("--host", default="Pi1MHz.local")
    ap.add_argument("--plan", default="7:15,0:15,1:15,2:15", help="mode:seconds, ... in order")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--fps", type=float, default=50.0)
    ap.add_argument("--budget", type=float, default=0.99, help="the share of a vsync's cycles a frame may take")
    ap.add_argument("--encode-only", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--lines", action="store_true", help="MODE 1 with a palette for every line")
    ap.add_argument("--hold", action="store_true", help="with --lines: when a frame doesn't fit, hold it "
                    "for another field rather than move on with rows behind")
    o = ap.parse_args()
    plan = [(int(m), float(s)) for m, s in (p.split(":") for p in o.plan.split(","))]
    shows = []
    for item in o.video:
        video, _, at = item.rpartition("@") if "@" in item else (item, "", "")
        start = float(at) if at else o.start
        cache = (f"{video}.{o.plan.replace(':', '-').replace(',', '_')}.s{start:g}.f{o.fps:g}.b{o.budget:g}"
                 f".k{BLACK[0]:g}-{BLACK[1]:g}.c{CONTRAST[0]:g}-{CONTRAST[1]:g}.p{PIVOT[0]:g}{'.L4' if o.lines else ''}{'h' if o.hold else ''}.v3.demo")
        if os.path.exists(cache) and not o.no_cache:
            with open(cache, "rb") as f:
                data, report = pickle.load(f)
            print(f"encoded already: {cache}")
        else:
            t = time.monotonic()
            data, report = encode_all(video, plan, start, o.fps, o.budget, lines=o.lines, hold=o.hold)
            print(f"encoded {video} from {start:g} s in {time.monotonic() - t:.0f} s")
            with open(cache, "wb") as f:
                pickle.dump((data, report), f)
        for mode, n, size, backlog in report:
            full = MODES[mode]["size"]
            print(f"MODE {mode}: {n} frames, {size // 1024} KB ({size / max(n, 1):.0f} bytes a frame).  Left for the next "
                  f"frame: none in {100 * (backlog == 0).mean():.0f}% of frames; median {int(np.median(backlog))} bytes, "
                  f"90% {int(np.percentile(backlog, 90))}, worst {int(backlog.max())} ({100 * backlog.max() / full:.0f}% of the screen)")
        print(f"{len(data) // 1024} KB in all")
        if len(data) > STREAM_SIZE:
            print(f"{len(data) // 1024} KB is more than shared memory takes ({STREAM_SIZE // 1024} KB): shorten the plan")
            return 1
        shows.append((data, report))
    if o.encode_only:
        return 0
    bbc = BBC(o.host, timeout=600)
    try:
        if not bbc.ready():
            print(f"The probe isn't in: type {BBC.install_line()} on the BBC")
            return 1
        secs = sum(s for _, s in plan)
        shared = bbc.shared()
        room = min(STREAM_SIZE, shared["size"] - 0x100)
        too_long = [len(d) for d, _ in shows if len(d) > room]
        if too_long:
            print(f"{too_long[0] // 1024} KB is more than this Pi1MHz's shared memory takes ({room // 1024} KB): "
                  "shorten the plan")
            return 1
        player = main_code(shared["at"])
        # The player's line palette and pads go over &0B00, where the OS keeps the *KEY strings
        # (the probe's install puts its *KEY10 there, for a soft Break), its palette table over
        # &0C00 (user-defined characters), and its copy chains over &0E00 on, where a filing
        # system keeps its workspace (MMFS its catalogue, which it then finds "Bad Sum") and BASIC
        # its program.  All are put back once the player has returned.
        kept = {SPECIAL: bbc.peek(SPECIAL, 0x100), PALTAB: bbc.peek(PALTAB, len(PALTAB_CODE)),
                BLOCKS: bbc.peek(BLOCKS, len(BLOCKS_CODE))}
        for i, (data, report) in enumerate(shows):
            t = time.monotonic()
            bbc.shared_write(0, data)
            up = time.monotonic() - t
            print(f"uploaded in {up:.1f} s ({len(data) / max(up, 1e-3) / 1024:.0f} KB/s)", flush=True)
            bbc.poke(PLAYER, player)
            bbc.poke(BLOCKS, BLOCKS_CODE)
            bbc.poke(SPECIAL, SPECIAL_CODE)
            bbc.poke(PALTAB, PALTAB_CODE)
            bbc.poke(0x76, bytes((0, SPECIAL >> 8)))           # the pads' jump: JMP (&0076)
            was = bbc.shared()["read"]
            bbc.type(f"CALL &{PLAYER:X}\r", wait=False)          # the player takes the machine over
            while bbc.shared()["read"] == was:                     # until it has started
                time.sleep(0.2)
            t = time.monotonic()
            time.sleep(max(0.0, secs - 0.5))                       # nothing on the network meanwhile
            while bbc.shared()["read"] < len(data) and time.monotonic() - t < secs + 60:
                time.sleep(0.5)
            el = time.monotonic() - t
            time.sleep(1.0)                                        # the player's exit: MODE 7, back to BASIC
            total = sum(r[1] for r in report)
            print(f"the BBC took {el:.1f} s for {total} frames ({total / el:.1f} a second)")
            counters = bbc.peek(L["frames"], 3)
            print(f"the player counted {counters[0] | counters[1] << 8} frames, {counters[2]} started late", flush=True)
        for at, was in kept.items():
            bbc.poke(at, was)
    except ProbeError as e:
        print(e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
