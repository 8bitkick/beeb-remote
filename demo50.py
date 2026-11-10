#!/usr/bin/env python3
"""The Rick roll on a BBC Micro at 50 frames a second: MODE 7, then MODE 0, 1 and 2, with the
palette chosen for each scene.  Played from the Pi1MHz's own memory, with no network traffic.

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

The stream, read through the services port from &400000:
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
colours neither has: dithering down the screen as well as across.

A screen byte means nothing without its line's colours, so with --lines a frame's bytes and its
colours change together: a row's new bytes are copied after the raster has drawn it in one frame
and before it comes back in the next, when its new colours come too (a frame's ops start with
the rest of its own lower rows, then the next frame's rows behind the raster).  A row that
doesn't fit is held, old bytes and old colours, until a frame has room: a cut wipes in top down
rather than showing old bytes through new colours.  The lines above a held row close in on its
colours, one a line.  Checked in beebjit frame by frame (all 750 of the Big Buck Bunny test)
against what the encoder meant; not yet on a real BBC.
"""
import argparse
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
STREAM_SIZE = 0x7E0000
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
        STA &FCA6               ; the services port at the stream: &400000
        STA &FCA7
        LDA #&40
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
MAIN_CODE, L = asm(MAIN, PLAYER, {"TAB": TAB})
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


# --lines: a gentler curve (eight colours to dither with need less help), and a pale or dark
# scene not stretched to the full range (a pale sky would go saturated blue)
LINES_LEVELS = dict(black=0.06, contrast=0.9, least=0.8)
LINES_GAMMA = 1.8          # the colours are chosen nearer linear light: a dither mixes light, so half
                           # white and half black looks a light grey, not a middle one


class Levels:
    """The frame's levels stretched to the full range, smoothed over time so the brightness
    doesn't pump from frame to frame; a cut starts afresh."""
    def __init__(self, mode):
        self.lo = self.hi = None
        self.black, self.contrast, self.pivot = BLACK[mode], CONTRAST[mode], PIVOT[mode]
        self.least = 0.0                           # the least range stretched to 0-1

    def __call__(self, rgb, cut):
        lo, hi = np.percentile(rgb, (1, 99))
        if self.lo is None or cut:
            self.lo, self.hi = lo, hi
        else:
            self.lo += 0.1 * (lo - self.lo)
            self.hi += 0.1 * (hi - self.hi)
        lo, hi = self.lo, self.hi
        if hi - lo < self.least:                   # widened towards 0 and 1 in proportion to the room
            a = (self.least - (hi - lo)) / max(1 - (hi - lo), 1e-3)   # each side: a pale scene stays pale
            lo, hi = lo * (1 - a), hi + (1 - hi) * a
        x = np.clip(0.5 + self.contrast * ((rgb - lo) / max(hi - lo, 1e-3) - self.pivot), 0, 1)
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
    smooth, levels, palette = Smooth(), Levels(mode), Palette(2 if mode == 0 else 4) if mode != 2 else None
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
LINE_WT = np.array([1.0, 1.0, 1.0])   # how much an error in red, green, blue counts
LINE_STAY = 0.02           # what keeping a line's colours from the last frame is worth: no flicker
LINE_CARRY = 0.4           # how much of a line's error the next line down takes: lines then mix
                           # into colours neither has, a dither down the screen as well as across


class LinePalettes:
    """For each line in turn, top down: the error carried from the line above added, the best 4
    colours of those at most one colour from the line above's (one palette op), scored on the
    whole line, then the line ordered-dithered across.  A colour that stays keeps its logical
    colour, so only the changed one needs a palette op and its pixels redrawing.

    The lines of a held row keep last frame's colours and pixels exactly (see encode_all: its new
    bytes didn't fit, so the row shows the old picture rather than old bytes through new
    colours).  The lines above a held row close in on its colours, one a line."""
    def __init__(self):
        self.pals = np.zeros((256, 4), int)          # what the screen shows: at first, all black
        self.pix = np.zeros((256, 320), np.uint8)
        self.t = bayer(256, 320)

    @staticmethod
    def _next(cur):
        """The colour sets one palette op from cur: a colour other than black (or a second
        black) changed for one not in it, keeping the others' logical colours."""
        out = [list(cur)]
        for s in range(4):
            if cur[s] == 0 and cur.count(0) == 1:        # (one black stays; a second may go)
                continue
            for c in range(1, 8):
                if c not in cur:
                    v = list(cur)
                    v[s] = c
                    out.append(v)
        return out

    def plan(self, rgb, cut, held=None):
        """(pals, pix) for the frame, the held lines (bool, 256) as they were."""
        h, w, _ = rgb.shape
        held = np.zeros(h, bool) if held is None else held
        nxt = np.full(h + 1, h)                        # the first held line below each line
        for y in range(h - 1, -1, -1):
            nxt[y] = y if held[y] else nxt[y + 1]
        pals, pix = np.zeros((h, 4), int), np.zeros((h, w), np.uint8)
        carry, cur = np.zeros((w, 3)), None
        for y in range(h):
            t = rgb[y] + carry
            if held[y]:
                cur, p = list(self.pals[y]), self.pix[y].astype(int)
            else:
                y0 = nxt[y + 1] if y + 1 < h else h    # (held[y] is False, so nxt[y] would be y)
                target = list(self.pals[y0]) if y0 < h else None
                if cur is None:                        # line 0: any colours (the header's palette)
                    old = None if cut else list(self.pals[0])
                    cands = []
                    for c in LINE_COMBOS:
                        if old is None:
                            cands.append(list(c))
                        else:
                            keep, seen = [], set()
                            for x in old:              # (each colour kept once: the screen starts all black)
                                keep.append(x if x in c and x not in seen else None)
                                seen.add(x)
                            rest = iter(x for x in c if x not in keep)
                            cands.append([x if x is not None else next(rest) for x in keep])
                else:
                    cands = self._next(cur)
                if target is not None:
                    room = y0 - y                      # palette ops left before the held line
                    ok = [v for v in cands if sum(a != b for a, b in zip(v, target)) <= room]
                    if not ok:                         # closing in: one of target's colours, in its slot
                        base = list(cur) if cur is not None else list(target)
                        diff = [s for s in range(4) if base[s] != target[s]]
                        if diff:
                            base[diff[0]] = target[diff[0]]
                        ok = [base]
                    cands = ok
                C = np.array(cands)
                d8 = (((t[:, None, :] - PHYS[None]) ** 2) * LINE_WT).sum(2)           # w x 8
                err = d8[:, C].min(2).mean(0)
                if not cut:
                    same = np.array([sorted(v) == sorted(self.pals[y]) for v in cands])
                    err = err + LINE_STAY * ~same
                cur = cands[int(err.argmin())]
                x = t + (self.t[y][:, None] - 0.5) * AMPLITUDE
                dist = (((x[:, None, :] - PHYS[cur][None]) ** 2) * LINE_WT).sum(2)    # w x 4
                p = dist.argmin(1)
                if not cut and (self.pals[y] == cur).all():
                    q = self.pix[y].astype(int)
                    p = np.where(dist[np.arange(w), q] - dist.min(1) < EPS[1], q, p)
            pals[y], pix[y] = cur, p
            carry = np.clip(LINE_CARRY * (t - PHYS[np.array(cur)[p]]), -0.5, 0.5)
        return pals, pix

    def commit(self, pals, pix):
        self.pals, self.pix = pals.copy(), pix.copy()


def line_ops(pals):
    """The palette ops [(line, Y)] for lines 1-255: at most one colour changes a line."""
    ops = []
    for y in range(1, 256):
        ch = [k for k in range(4) if pals[y][k] != pals[y - 1][k]]
        assert len(ch) <= 1, (y, pals[y - 1], pals[y])
        if ch:
            ops.append((y, ch[0] * 8 + int(pals[y][ch[0]])))
    return ops


def frames_lines(path, start, seconds, fps):
    """The MODE 1 frames, levelled and smoothed: (cut, rgb)."""
    m = MODES[1]
    smooth, levels, last_small = Smooth(), Levels(1), None
    levels.black, levels.contrast, levels.least = LINES_LEVELS["black"], LINES_LEVELS["contrast"], LINES_LEVELS["least"]
    for f in source(path, start, seconds, m["w"], m["h"], fps):
        small = f[::8, ::8]
        cut = last_small is not None and np.abs(small - last_small).mean() > 0.12
        last_small = small
        yield cut, levels(smooth(f, cut), cut) ** LINES_GAMMA


LINE_A = 4550              # line y's palette op starts LINE_A + 128 * y cycles from the frame's first op;
                           # its writes then land in the middle of the blank (measured in beebjit:
                           # 4535-4565 is clean)
LINE_END = 39000           # the frame's ops end by here, leaving the next header time before the vsync
ROW = 640                  # a character row: 8 lines, 80 bytes each


def row_drawn(r):
    """The cycle (from the frame's first op) by which the raster has finished character row r.
    Line y's palette op starts at LINE_A + 128 * y, its writes are in the blank before line y and
    the line is shown in the 80 cycles after; then a margin."""
    return LINE_A + 128 * (8 * r + 7) + 140 + 32


def row_start(r):
    """The cycle by which row r's bytes must all be in memory: its first line's palette op."""
    return LINE_A + 128 * 8 * r + 32


def row_cost(changed):
    """The cycles to copy a row's changed bytes (bool, 640): runs, bridged over gaps of one."""
    d = np.diff(np.concatenate(([0], changed.astype(np.int8), [0])))
    s, e = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
    cost, last = 0, -9
    for a, b in zip(s, e):
        n = b - a
        if a - last <= 1:
            cost += COPY * (a - last)                  # bridged
        else:
            cost += PIECE
        cost += n * COPY + (n // CHAIN) * PIECE
        last = b
    return cost + 2 * SETPAGE


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


    @staticmethod
    def schedule(ops, queue, until_end=LINE_END):
        """One frame's ops: its palette ops (this frame's colours), each started on its cycle,
        LINE_A + 128 * line from the frame's first op (or the cycle given), and the copies in the queue, in
        order, filling the time between.  A queue item [start, end, row, src, early, late]
        copies src[start:end]: not before cycle early (the raster has drawn the row) and
        finishing by late (the raster comes to it), if late isn't None.  Returns (the ops, the
        cycles, the pieces written [(start, end)], what's left of the queue, the rows that
        missed their late)."""
        queue = [list(q) for q in queue]
        starts = [o[2] if len(o) > 2 else LINE_A + 128 * o[0] for o in ops]     # (line, Y[, cycle])
        assert all(b - a >= PAL_OP for a, b in zip(starts, starts[1:])), "palette ops overlap"
        out, page, t = bytearray(), PLAYER >> 8, 0
        pieces, failed = [], set()

        def emit_pad(n):
            nonlocal t
            while n:
                c = min(n, PAD_MAX)
                if 0 < n - c < PAD_MIN:
                    c = n - PAD_MIN
                out.extend((SPECIAL_LO[page], pad_y(c)))
                t += c
                n -= c

        out.extend((SETPG_LO[page], BLOCK_AT[0] >> 8))  # leave the player's page
        page, t = BLOCK_AT[0] >> 8, 24
        qi = 0
        for g in range(len(ops) + 1):
            until = starts[g] if g < len(ops) else (until_end - SETPAGE - END) & ~1
            while qi < len(queue):
                a, e, r, src, early, late = queue[qi]
                if r in failed:
                    qi += 1
                    continue
                wait = max(0, ((early + 1) & ~1) - t)
                if 0 < wait < PAD_MIN:
                    wait = PAD_MIN
                fit = 0
                for k in range(e - a, 0, -1):
                    ent = entry(SCREEN + e - k, k)
                    c = wait + (SETPAGE if ent >> 8 != page else 0) + PIECE + k * COPY
                    rest = until - t - c
                    if rest == 0 or rest >= PAD_MIN:
                        fit = k
                        break
                if not fit:
                    break
                if late is not None and t + c > late:
                    failed.add(r)
                    qi += 1
                    continue
                emit_pad(wait)
                k = fit
                ent = entry(SCREEN + e - k, k)
                if ent >> 8 != page:
                    out.extend((SETPG_LO[page], ent >> 8))
                    page = ent >> 8
                    t += SETPAGE
                out.extend((ent & 255, (SCREEN + e - k) & 255))
                out.extend(src[e - k:e][::-1])
                t += PIECE + k * COPY
                pieces.append((e - k, e))
                if k == e - a:
                    qi += 1
                else:
                    queue[qi][1] = e - k                # the rest, later
            emit_pad(until - t)
            if g < len(ops):
                out.extend((SPECIAL_LO[page], ops[g][1]))
                t += PAL_OP
        if page != PLAYER >> 8:
            out.extend((SETPG_LO[page], PLAYER >> 8))
            t += SETPAGE
        out.extend((L["endf"] & 255, 0))
        t += END
        left = [q for q in queue[qi:] if q[2] not in failed]
        failed |= {q[2] for q in left if q[5] is not None}
        return bytes(out), t, pieces, [q for q in left if q[2] not in failed], failed


PREDRAW = 12          # MODE 7 frames during which the next mode's first frame is drawn out of sight


def row_chunks(changed, rows, src, early):
    """Queue items for the changed bytes of rows (top down): runs, bridged over gaps of one, in
    pieces of a chain's length at most."""
    out = []
    for r in rows:
        m = changed[r * ROW:(r + 1) * ROW]
        d = np.diff(np.concatenate(([0], m.astype(np.int8), [0])))
        s, e = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
        runs = []
        for a, b in zip(s, e):
            if runs and a - runs[-1][1] <= 1:
                runs[-1][1] = b
            else:
                runs.append([a, b])
        for a, b in runs:
            for x in range(a, b, CHAIN):
                out.append([r * ROW + x, r * ROW + min(x + CHAIN, b), r, src, early(r), None])
    return out


def encode_lines_mode(frames, data, ivl, switch, on_frame=None, log=print):
    """MODE 1 with line palettes, into data; returns (frames, rows held per frame).

    Frame k shows S_k: its colours come with it (the header's for line 0, palette ops for the
    rest), and its bytes are all in screen memory before the raster gets to them.  A row's
    bytes for S_{k+1} are written after the raster has drawn the row in frame k and before it
    comes back to it in frame k+1, so a frame's ops start with the rest of its own bytes (rows
    lower down, before the raster) and go on to the next frame's (rows above it, behind the
    raster).  A row whose bytes don't fit in that window is held: it shows exactly what it
    showed (old bytes, old colours) until a frame has room for it, never old bytes through new
    colours.  S_0 is black (the first frame, with the mode change, has no line ops), and the
    rows the picture hasn't reached yet stay black."""
    lp = LinePalettes()
    nrow = SCREEN_LEN // ROW
    age = np.zeros(nrow, int)
    state, known = bytes(SCREEN_LEN), np.zeros(SCREEN_LEN, bool)       # S_k: what frame k shows
    pals_k, k, held_log, tail = lp.pals.copy(), 0, [], []

    def frame(codes, pals):
        data.append((switch if k == 1 else ivl) | 0x80)
        data.extend(ula_palette(1, tuple(int(c) for c in pals[0])))
        data.extend(codes)

    def changes(cur):
        return (np.frombuffer(state, np.uint8) != np.frombuffer(cur, np.uint8)) | ~known

    for cut, rgb in frames:
        ops = [] if k == 0 else line_ops(pals_k)
        end = LINE_END - (MODESET if k == 0 else 0)
        early = (lambda r: 0) if k == 0 else row_drawn
        pals, pix = lp.plan(rgb, cut)
        ch = changes(bp.pack(pix, 1))
        rows = [r for r in range(nrow) if ch[r * ROW:(r + 1) * ROW].any()]
        cost = {r: row_cost(ch[r * ROW:(r + 1) * ROW]) for r in rows}
        tail_cost = sum(PIECE + (q[1] - q[0]) * COPY for q in tail)
        cap = 0.9 * (end - 24 - PAL_OP * len(ops) - tail_cost)
        if cut or sum(cost.values()) > 2 * cap:
            order = sorted(rows)                       # a cut: a wipe, top down
        else:
            order = sorted(rows, key=lambda r: -(age[r] + 1) * cost[r])
        take, spent = set(), 0
        for r in order:
            if spent + cost[r] <= cap:
                take.add(r)
                spent += cost[r]
        hold = set(rows) - take
        while True:
            held = np.zeros(256, bool)
            for r in hold:
                held[8 * r:8 * r + 8] = True
            pals, pix = lp.plan(rgb, cut, held)
            cur = bp.pack(pix, 1)
            ch = changes(cur)
            need = [r for r in range(nrow) if r not in hold and ch[r * ROW:(r + 1) * ROW].any()]
            codes, t, pieces, rest, failed = Screen.schedule(ops, tail + row_chunks(ch, need, cur, early))
            assert not failed, ("a promised row missed the raster", k, failed)
            nxt = [q[:4] + [0, row_start(q[2])] for q in rest]
            _, _, _, rest2, failed2 = Screen.schedule(line_ops(pals), nxt)
            failed2 |= {q[2] for q in rest2}
            if not failed2:
                break
            hold |= failed2
        frame(codes, pals_k)
        if on_frame:
            on_frame(1, k, state, None, pals_k)
        state = cur
        for r in need:
            known[r * ROW:(r + 1) * ROW] = True
        lp.commit(pals, pix)
        tail = nxt
        for r in range(nrow):
            age[r] = age[r] + 1 if r in hold else 0
        held_log.append(len(hold))
        pals_k, k = pals, k + 1
        if k % 250 == 0:
            log(f"  MODE 1: {k} frames")
    codes, t, pieces, rest, failed = Screen.schedule(line_ops(pals_k), tail)    # the last: its own bytes
    assert not failed and not rest
    frame(codes, pals_k)
    if on_frame:
        on_frame(1, k, state, None, pals_k)
    return k + 1, np.array(held_log + [0])


def encode_all(path, plan, start, fps, budget, log=print, lines=False, on_frame=None):
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
        gens.append(frames_lines(path, at, secs, fps) if lines and mode == 1 else targets(path, mode, at, secs, fps, table))
        at += secs
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
        if lines and mode == 1:
            n, held = encode_lines_mode(frames, data, ivl, switch, on_frame, log)
            report.append((mode, n, len(data) - size0, held * ROW))      # the held rows' bytes
            continue
        for k, fr in enumerate(frames):
            cut, pal, cur_part = fr[:3]
            line_ops = fr[3] if len(fr) > 3 and k > 0 else None     # the first frame has the mode change's time
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
                codes, cost, changed, left = screen.encode_lines(bytes(cur), line_ops, wipe=cut)
            else:
                codes, cost, changed, left = screen.encode(bytes(cur), frame_budget, fcare, low, wipe=cut)
            data.append((switch if k == 1 else ivl) | (0x80 if pal else 0))
            if pal:
                data += pal
            data += codes
            n += 1
            backlog.append(left)
            if on_frame:
                on_frame(mode, k, bytes(screen.mem), pal, fr[4] if line_ops is not None else None)
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
    o = ap.parse_args()
    plan = [(int(m), float(s)) for m, s in (p.split(":") for p in o.plan.split(","))]
    shows = []
    for item in o.video:
        video, _, at = item.rpartition("@") if "@" in item else (item, "", "")
        start = float(at) if at else o.start
        cache = (f"{video}.{o.plan.replace(':', '-').replace(',', '_')}.s{start:g}.f{o.fps:g}.b{o.budget:g}"
                 f".k{BLACK[0]:g}-{BLACK[1]:g}.c{CONTRAST[0]:g}-{CONTRAST[1]:g}.p{PIVOT[0]:g}{'.L6' if o.lines else ''}.v2.demo")
        if os.path.exists(cache) and not o.no_cache:
            with open(cache, "rb") as f:
                data, report = pickle.load(f)
            print(f"encoded already: {cache}")
        else:
            t = time.monotonic()
            data, report = encode_all(video, plan, start, o.fps, o.budget, lines=o.lines)
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
            print("more than the stream buffer holds: shorten the plan")
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
        for i, (data, report) in enumerate(shows):
            t = time.monotonic()
            bbc._request("/bbc/buffer", post=True, body=data, offset=0)
            up = time.monotonic() - t
            print(f"uploaded in {up:.1f} s ({len(data) / max(up, 1e-3) / 1024:.0f} KB/s)", flush=True)
            bbc.poke(PLAYER, MAIN_CODE)
            bbc.poke(BLOCKS, BLOCKS_CODE)
            bbc.poke(SPECIAL, SPECIAL_CODE)
            bbc.poke(PALTAB, PALTAB_CODE)
            bbc.poke(0x76, bytes((0, SPECIAL >> 8)))           # the pads' jump: JMP (&0076)
            was = bbc.status()["read"]
            bbc.type(f"CALL &{PLAYER:X}\r", wait=False)          # the player takes the machine over
            while bbc.status()["read"] == was:                     # until it has started
                time.sleep(0.2)
            t = time.monotonic()
            time.sleep(max(0.0, secs - 0.5))                       # nothing on the network meanwhile
            while bbc.status()["read"] < len(data) and time.monotonic() - t < secs + 60:
                time.sleep(0.5)
            el = time.monotonic() - t
            time.sleep(1.0)                                        # the player's exit: MODE 7, back to BASIC
            total = sum(r[1] for r in report)
            print(f"the BBC took {el:.1f} s for {total} frames ({total / el:.1f} a second)")
            counters = bbc.peek(L["frames"], 3)
            print(f"the player counted {counters[0] | counters[1] << 8} frames, {counters[2]} started late", flush=True)
    except ProbeError as e:
        print(e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
