#!/usr/bin/env python3
"""Video on a BBC Micro's MODE 7 screen, through the Pi1MHz probe, encoded with Teletext Express.

    python3 ttx_stream.py --webcam                 # the webcam, live
    python3 ttx_stream.py ~/Videos/rick.mp4        # a video file

The encoder is a port of Teletext Express (github.com/8bitkick/teletext-express, GPLv3, by
8bitkick): each 6x10 character cell is cut into six tiles, each tile's grey level becomes 0-7,
and a table of every 2^18 combination gives the teletext character that matches best.

The display keeps an even beat because the network isn't in the frame timing.  A copier in the
BBC's RAM, on the vsync event, shows the next frame every 50/fps vsyncs from a ring of 23 1 KB
slots (&2000-&7BFF, free while BASIC sits at its prompt: 0.9 s at 25 fps, to ride out WiFi
stalls); this program
fills the ring ahead with pokes at rung 1 (never an NMI), and starts the copier once 12 frames
are in.  A slot's last byte is its ready flag, written last, in the same poke as the frame.
"""
import argparse
import os
import re
import subprocess
import sys
import time

import numpy as np

from asm6502 import asm
from bbc import BBC, ProbeError

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = os.path.join(HERE, "ttx_lookup.npy")
CHARS_JS = os.environ.get("TTX_CHARS_JS", os.path.join(HERE, "characters.js"))

NSLOTS = 23
SLOT0 = 0x20                               # high byte of slot 0; slot k at &2000 + k*1 KB
PREFILL = 12
COPIER = 0x0900
EVNTV = 0x0220
SCREEN = 0x7C00
CONTROL = 151                              # graphics white, column 0 of every row

# Teletext Express's tiles: x0, y0, x1, y1 within the 6x10 cell, tile b at bits 3b of the index.
TILES = [(0, 0, 2, 2), (3, 0, 5, 2), (0, 3, 2, 6), (3, 3, 5, 6), (0, 7, 2, 9), (3, 7, 5, 9)]


# ---- the encoder -----------------------------------------------------------------------
def glyphs():
    """192 glyphs of 6x10: 96 text (from characters.js), 96 graphics (built as addGraphics);
    NaN for graphics codes with bit 5 set, which Teletext Express leaves out."""
    text = open(CHARS_JS).read()
    body = text[text.index("var teletext_chars = ["):text.index("];")]
    vals = [int(v) for v in re.findall(r"(?<![x0-9a-fA-F])[01](?=\s*[,\n\]])", re.sub(r"//[^\n]*", "", body))]
    assert len(vals) == 96 * 60, len(vals)
    g = np.full((192, 60), np.nan)
    g[:96] = np.array(vals, dtype=float).reshape(96, 60)
    for c in range(96):
        if c & 32:
            continue
        cell = np.zeros((10, 6))
        for bit, (x, y, w, h) in zip((1, 2, 4, 8, 16, 64), ((0, 0, 3, 3), (3, 0, 3, 3), (0, 3, 3, 4),
                                                           (3, 3, 3, 4), (0, 7, 3, 3), (3, 7, 3, 3))):
            cell[y:y + h, x:x + w] = 1 if c & bit else 0
        g[96 + c] = cell.reshape(60)
    return g


def allowed(c, encoding):
    if encoding == 0:                      # Graphics (hi)
        return c > 96 or c == 0
    return not (0 < c < 0x20) and not (0x3F < c < 96)   # Graphics + text (hi)


def lookup(encoding=1):
    path = TABLE.replace(".npy", f"{encoding}.npy")
    if os.path.exists(path):
        return np.load(path)
    g = glyphs().reshape(192, 10, 6)
    charvals = np.zeros((192, 6))
    for b, (x0, y0, x1, y1) in enumerate(TILES):
        area = (x1 - x0 + 1) * (y1 - y0 + 1)
        charvals[:, b] = np.ceil(g[:, y0:y1 + 1, x0:x1 + 1].sum(axis=(1, 2)) / area * 7)
    ok = np.array([allowed(c, encoding) and not np.isnan(charvals[c]).any() for c in range(192)])
    cv = np.where(ok[:, None], charvals, 1e9)
    table = np.zeros(1 << 18, dtype=np.uint8)
    idx = np.arange(1 << 18)
    tv = np.stack([(idx >> (3 * b)) & 7 for b in range(6)], axis=1).astype(float)
    for at in range(0, 1 << 18, 8192):
        d = ((tv[at:at + 8192, None, :] - cv[None, :, :]) ** 2).sum(axis=2)
        table[at:at + 8192] = d.argmin(axis=1)
    np.save(path, table)
    return table


def levels(grey, low=2, high=98):
    """Teletext Express's normalize: the frame's darkest low% black and brightest high% white,
    so a dim picture uses the whole 0-7 scale."""
    g = grey.astype(np.float64)
    lo, hi = np.percentile(g, (low, high))
    if hi - lo < 16:
        return g
    return np.clip((g - lo) * 255.0 / (hi - lo), 0, 255)


def encode(grey, table, normalize=True):
    """grey: 250x240.  1000 bytes of MODE 7."""
    g = levels(grey) if normalize else grey.astype(np.float64)
    cells = g.reshape(25, 10, 40, 6)
    index = np.zeros((25, 40), dtype=np.int64)
    for b, (x0, y0, x1, y1) in enumerate(TILES):
        area = (x1 - x0 + 1) * (y1 - y0 + 1)
        s = cells[:, y0:y1 + 1, :, x0:x1 + 1].sum(axis=(1, 3))
        index |= np.clip(np.floor(7 * s / (area * 255)), 0, 7).astype(np.int64) << (3 * b)
    c = table[index].astype(np.int64)
    codes = np.where(c > 96, c + 64, c + 32).astype(np.uint8)
    codes[:, 0] = CONTROL
    return codes.tobytes()


def frames(source, fps):
    """Grey 240x250 frames from ffmpeg.  MODE 7's 40x25 cells fill a 4:3 picture on the BBC, so
    the source's middle 4:3 (all of a 4:3 webcam; the middle of a 16:9 video) is squeezed to
    the 240x250 grid of tiles."""
    vf = "crop='min(iw,ih*4/3)':'min(ih,iw*3/4)',scale=240:250,format=gray"
    if source == "webcam":
        cmd = ["ffmpeg", "-loglevel", "error", "-f", "v4l2", "-input_format", "mjpeg", "-framerate", "30",
               "-video_size", "640x480", "-i", "/dev/video0", "-vf", vf, "-f", "rawvideo", "-"]
    else:
        cmd = ["ffmpeg", "-loglevel", "error", "-i", source, "-vf", f"fps={fps},{vf}", "-f", "rawvideo", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    try:
        while True:
            buf = p.stdout.read(240 * 250)
            if len(buf) < 240 * 250:
                return
            yield np.frombuffer(buf, dtype=np.uint8).reshape(250, 240)
    finally:
        p.kill()


# ---- the BBC's side ----------------------------------------------------------------------
SOURCE = """
ev:     PHP                     ; EVNTV: A the event
        CMP #4
        BNE out
        PHA
        TXA
        PHA
        LDA go                  ; started?
        BEQ done
        DEC wait                ; a frame every ivl vsyncs
        BNE done
        LDA ivl
        STA wait
        LDA slot                ; this slot's pages: &20 + 4*slot
        ASL A
        ASL A
        CLC
        ADC #&20
        STA s0+2
        ADC #1
        STA s1+2
        ADC #1
        STA s2+2
        ADC #1
        STA s3+2
        STA rdy+2
        STA clr+2
rdy:    LDA &20FF               ; its last byte: ready?
        BNE show
        LDA #1                  ; not yet: look again next vsync
        STA wait
        BNE done
show:   LDX #0
copy:
s0:     LDA &2000,X
        STA &7C00,X
s1:     LDA &2100,X
        STA &7D00,X
s2:     LDA &2200,X
        STA &7E00,X
s3:     LDA &2300,X
        STA &7F00,X
        INX
        BNE copy
        LDA #0
clr:    STA &20FF               ; free again
        LDX slot                ; the next, round the ring of 23
        INX
        CPX #23
        BCC nxt
        LDX #0
nxt:    STX slot
done:   PLA
        TAX
        PLA
out:    PLP
        JMP (old)
go:     BYTES 00
ivl:    BYTES 02
wait:   BYTES 01
slot:   BYTES 00
old:    BYTES 00 00
"""
CODE, LABELS = asm(SOURCE, COPIER)
OLD, GO, IVL, SLOT = (LABELS[k] - COPIER for k in ("old", "go", "ivl", "slot"))


def install(bbc, ivl):
    """MODE 7 with no cursor, the slots empty, the copier (stopped) in front of whatever EVNTV
    held."""
    bbc.type("MODE 7\r")
    time.sleep(0.5)
    with bbc.stopped():
        bbc._act("/bbc/run", hex="a90a8d00fea9208d01fe60")       # 6845 R10 = &20: no cursor
        v = bbc.peek(EVNTV, 2)
        old = v[0] | v[1] << 8
        if old == COPIER:
            old = int.from_bytes(bbc.peek(COPIER + OLD, 2), "little")
        for k in range(NSLOTS):
            bbc.poke(((SLOT0 + 4 * k) << 8) + 0x3FF, b"\0")
        code = bytearray(CODE)
        code[OLD:OLD + 2] = old.to_bytes(2, "little")
        code[IVL] = ivl
        bbc.poke(COPIER, bytes(code))
        bbc.poke(EVNTV, COPIER.to_bytes(2, "little"))
    return old


def uninstall(bbc, old):
    with bbc.stopped():
        bbc.poke(EVNTV, old.to_bytes(2, "little"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", nargs="?", help="a video file (or --webcam)")
    ap.add_argument("--webcam", action="store_true")
    ap.add_argument("--host", default="Pi1MHz.local")
    ap.add_argument("--fps", type=float, default=25.0, help="a file's frame rate on the BBC (50 at most)")
    ap.add_argument("--seconds", type=float, default=0, help="stop after this long")
    ap.add_argument("--encoding", type=int, default=1, choices=(0, 1),
                    help="0 graphics only, 1 graphics and capital letters (default)")
    ap.add_argument("--no-normalize", action="store_true", help="leave the levels as they come")
    o = ap.parse_args()
    if not o.video and not o.webcam:
        ap.error("a video file, or --webcam")
    table = lookup(o.encoding)
    bbc = BBC(o.host, retry_after=0.25)     # a lost WiFi packet: send again, don't wait a second
    if not bbc.ready():
        print(f"The probe isn't in: type {BBC.install_line()} on the BBC")
        return 1
    ivl = max(1, round(50 / (30 if o.webcam else o.fps)))
    old = install(bbc, ivl)
    pad = bytes(23)
    sent, shown, last, waits, started = 0, 0, 0, 0, False
    t0 = time.monotonic()

    def consumed():
        """Frames the copier has shown so far, from its slot number (it can't be a ring ahead)."""
        nonlocal shown, last
        cur = bbc.peek(COPIER + SLOT, 1)[0]
        shown += (cur - last) % NSLOTS
        last = cur
        return shown

    try:
        for grey in frames("webcam" if o.webcam else o.video, o.fps):
            while sent - shown >= NSLOTS - 1 and sent - consumed() >= NSLOTS - 1:
                waits += 1
                time.sleep(0.01)
            slot = (SLOT0 + 4 * (sent % NSLOTS)) << 8
            bbc.poke(slot, encode(grey, table, not o.no_normalize) + pad + b"\1")
            sent += 1
            if not started and sent == PREFILL:
                bbc.poke(COPIER + GO, b"\1")
                started, t0 = True, time.monotonic()
            if o.seconds and started and time.monotonic() - t0 > o.seconds:
                break
    except (KeyboardInterrupt, ProbeError) as e:
        if isinstance(e, ProbeError):
            print(e)
    finally:
        el, done = time.monotonic() - t0, shown
        try:
            done = consumed()
        except ProbeError as e:
            print(e)
        for _ in range(3):                     # put EVNTV back, whatever went wrong
            try:
                uninstall(bbc, old)
                break
            except ProbeError as e:
                print(e)
    print(f"{done} frames shown in {el:.1f} s: {done / el:.1f} a second (the beat: every {ivl} vsyncs); "
          f"{sent} sent; the ring was full {waits} times; {getattr(bbc, 'retries', 0)} requests sent again")
    return 0


if __name__ == "__main__":
    sys.exit(main())
