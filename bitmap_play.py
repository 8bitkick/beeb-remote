#!/usr/bin/env python3
"""Video in MODE 2 (160x256, 8 colours) or MODE 1 (320x256, 4 colours) on a BBC Micro, from the
Pi1MHz's shared memory: no network and no stops while it plays.

    python3 bitmap_play.py ~/Videos/rick.mp4 [--mode 2] [--fps 25] [--seconds 30]

The Pi 5 dithers each frame to the mode's colours, packs it in the BBC's screen layout, and
sends only what changed since the last: skip n bytes, copy n bytes, fill n bytes with one.  A
frame that would take longer to draw than its share of time (a cut) draws its biggest changes
first and leaves the rest for the next frames, so the beat holds.  The whole video goes into
shared memory (POST /bbc/shared) in one request; a player in the BBC's RAM (&0900, CALL &900)
reads it through the services port, timed by a vsync counter on EVNTV.  Escape stops it.

The stream: for each frame, the vsyncs it lasts, then codes, then 0; a last 0 ends it.
Codes: 1-127 skip that many bytes; &80+n copy n+1 bytes that follow; &C0+n fill n+1 bytes
with the byte that follows.
"""
import argparse
import subprocess
import sys
import time

import numpy as np

from asm6502 import asm
from bbc import BBC, ProbeError

PLAYER = 0x0900
SCREEN = 0x3000
SCREEN_LEN = 0x5000
STREAM_SIZE = 0x7E0000             # of the 8 MB of shared memory: the last 128 KB stays free
BAYER = (np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) + 0.5) / 16
CYCLES_PER_VSYNC = 40000           # 2 MHz, 20 ms
COPY, FILL, OP = 19, 13, 30         # the player's cycles a copied byte, a filled byte, a code

SOURCE = """
        SEI                     ; a vsync counter, in front of whatever EVNTV held
        LDA &0220
        STA old
        LDA &0221
        STA old+1
        LDA #hook
        STA &0220
        LDA #&09
        STA &0221
        CLI
        LDA #0                  ; the services port at the stream: shared memory's start,
        STA &FCA6               ; on a 64 KB boundary
        STA &FCA7
        LDA #SHARED_HI
        STA &FCA8
        LDA vc
        STA tgt
frame:  LDA &FCA9               ; the vsyncs this frame lasts; 0 is the end
        BEQ fin
        CLC
        ADC tgt
        STA tgt
wt:     LDA vc                  ; until its time (no wait if a frame ran over)
        SEC
        SBC tgt
        BMI wt
        LDA #0
        STA &70
        LDA #&30
        STA &71
op:     LDA &FCA9
        BEQ fdone
        BMI run
        CLC                     ; skip n
        ADC &70
        STA &70
        BCC op
        INC &71
        BNE op
run:    CMP #&C0
        BCS fill
        AND #&3F                ; copy n+1
        TAX
        INX
        LDY #0
lit:    LDA &FCA9
        STA (&70),Y
        INY
        DEX
        BNE lit
        BEQ adv
fill:   AND #&3F                ; fill n+1
        TAX
        INX
        LDA &FCA9
        LDY #0
fl:     STA (&70),Y
        INY
        DEX
        BNE fl
adv:    TYA
        CLC
        ADC &70
        STA &70
        BCC op
        INC &71
        BNE op
fdone:  BIT &FF                 ; Escape?
        BPL frame
fin:    SEI
        LDA old
        STA &0220
        LDA old+1
        STA &0221
        CLI
        RTS
hook:   PHP                     ; EVNTV: count the vsyncs
        CMP #4
        BNE ho
        INC vc
ho:     PLP
        JMP (old)
vc:     BYTES 00
tgt:    BYTES 00
old:    BYTES 00 00
"""


def player(shared_at):
    """The player for shared memory at services offset shared_at (GET /bbc's shared.at)."""
    if shared_at & 0xFFFF:
        raise ProbeError(f"shared memory at &{shared_at:06X}: the player wants it on a 64 KB boundary")
    code, labels = asm(SOURCE, PLAYER, {"SHARED_HI": shared_at >> 16})
    assert labels["hook"] >> 8 == PLAYER >> 8 and len(code) < 0x100
    return code


# ---- frames -------------------------------------------------------------------------------
def frames(path, mode, fps):
    w = 160 if mode == 2 else 320
    vf = f"fps={fps},crop='min(iw,ih*4/3)':'min(ih,iw*3/4)',scale={w}:256,format=rgb24"
    p = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", path, "-vf", vf, "-f", "rawvideo", "-"],
                         stdout=subprocess.PIPE)
    try:
        while True:
            buf = p.stdout.read(w * 256 * 3)
            if len(buf) < w * 256 * 3:
                return
            yield np.frombuffer(buf, dtype=np.uint8).reshape(256, w, 3).astype(np.float64) / 255
    finally:
        p.kill()


def levels(rgb):
    lo, hi = np.percentile(rgb, (1, 99))
    return np.clip((rgb - lo) / max(hi - lo, 1e-3), 0, 1)


def dither(rgb, mode):
    """Logical colours, ordered dither.  MODE 2: each of red, green and blue on or off (the
    BBC's colours 0-7).  MODE 1: four greys, the palette set to black, red, cyan, white."""
    h, w, _ = rgb.shape
    t = np.tile(BAYER, (h // 4 + 1, w // 4 + 1))[:h, :w]
    rgb = levels(rgb)
    if mode == 2:
        bits = rgb > t[:, :, None]
        return (bits[:, :, 0] * 1 + bits[:, :, 1] * 2 + bits[:, :, 2] * 4).astype(np.uint8)
    lum = rgb @ np.array([0.299, 0.587, 0.114])
    ladder = np.array([0.0, 0.299, 0.701, 1.0])           # black, red, cyan, white
    i = np.clip(np.searchsorted(ladder, lum) - 1, 0, 2)
    lo, hi = ladder[i], ladder[i + 1]
    return (i + ((lum - lo) / (hi - lo) > t)).astype(np.uint8)


def pack(pix, mode):
    """Logical colours to the 20 KB of screen memory, in the BBC's layout: character rows of
    640 bytes, each 80 columns of 8 bytes, a byte per pixel row."""
    h, w = pix.shape
    if mode == 2:                                          # 2 pixels a byte: bits 7,5,3,1 and 6,4,2,0
        l, r = pix[:, 0::2].astype(np.uint16), pix[:, 1::2].astype(np.uint16)
        b = np.zeros(l.shape, dtype=np.uint16)
        for k in range(4):
            b |= ((l >> k) & 1) << (2 * k + 1) | ((r >> k) & 1) << (2 * k)
    else:                                                  # 4 pixels a byte: bits 7-p (high), 3-p (low)
        b = np.zeros((h, w // 4), dtype=np.uint16)
        for p in range(4):
            c = pix[:, p::4].astype(np.uint16)
            b |= ((c >> 1) & 1) << (7 - p) | (c & 1) << (3 - p)
    b = b.astype(np.uint8)                                 # 256 x 80
    return b.reshape(32, 8, 80).transpose(0, 2, 1).reshape(SCREEN_LEN).tobytes()


# ---- the changes ----------------------------------------------------------------------------
def runs_of(mask):
    """(start, end) of each run of True."""
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]))


def codes_for(cur, start, end):
    """Copy and fill codes for cur[start:end], and their cost in cycles."""
    out, cost, i = bytearray(), 0, start
    while i < end:
        j = i
        while j < end and cur[j] == cur[i] and j - i < 64:
            j += 1
        if j - i >= 3:                                     # a fill
            out += bytes((0xC0 + j - i - 1, cur[i]))
            cost += OP + FILL * (j - i)
            i = j
            continue
        k = i                                              # a copy, up to the next fill of 3
        while k < end and k - i < 64 and not (k + 2 < end and cur[k] == cur[k + 1] == cur[k + 2]):
            k += 1
        k = max(k, i + 1)
        out += bytes((0x80 + k - i - 1,)) + bytes(cur[i:k])
        cost += OP + COPY * (k - i)
        i = k
    return out, cost


def delta(prev, cur, budget):
    """The codes taking the screen from prev towards cur within budget cycles, biggest runs
    first; and the screen as it then is."""
    p, c = np.frombuffer(prev, dtype=np.uint8), np.frombuffer(cur, dtype=np.uint8)
    changed = p != c
    # bridge gaps of under 3 unchanged bytes: a skip costs about as much as copying them
    rs = runs_of(changed)
    merged = []
    for s, e in rs:
        if merged and s - merged[-1][1] < 3:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    done = []
    spent = 0
    for s, e in sorted(merged, key=lambda r: -(r[1] - r[0])):
        out, cost = codes_for(c, s, e)
        if spent + cost + OP > budget and done:
            continue
        done.append((s, e, out))
        spent += cost + OP
    done.sort()
    stream, at = bytearray(), 0
    after = bytearray(prev)
    for s, e, out in done:
        gap = s - at
        while gap > 0:
            stream.append(min(gap, 127))
            gap -= min(gap, 127)
        stream += out
        after[s:e] = cur[s:e]
        at = e
    stream.append(0)
    return bytes(stream), bytes(after), spent, int(changed.sum()), sum(e - s for s, e, _ in done)


def encode_video(path, mode, fps, seconds):
    ivl = max(1, round(50 / fps))
    budget = int(ivl * CYCLES_PER_VSYNC * 0.85)
    screen = bytes(SCREEN_LEN)                             # MODE clears it
    out, n, carried = bytearray(), 0, 0
    for rgb in frames(path, mode, fps):
        cur = pack(dither(rgb, mode), mode)
        codes, screen, spent, changed, sent = delta(screen, cur, budget)
        if len(out) + 1 + len(codes) + 1 > STREAM_SIZE:
            break
        out.append(ivl)
        out += codes
        carried += changed - sent
        n += 1
        if seconds and n >= seconds * fps:
            break
    out.append(0)
    return bytes(out), n, ivl, screen, carried


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--host", default="Pi1MHz.local")
    ap.add_argument("--mode", type=int, default=2, choices=(1, 2))
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--seconds", type=float, default=0)
    o = ap.parse_args()
    t = time.monotonic()
    data, n, ivl, _, carried = encode_video(o.video, o.mode, o.fps, o.seconds)
    print(f"encoded {n} frames, {len(data) // 1024} KB ({len(data) / max(n, 1) / 1024:.1f} KB a frame), "
          f"in {time.monotonic() - t:.1f} s; changes carried to later frames: {carried} bytes", flush=True)
    bbc = BBC(o.host, timeout=120)
    try:
        if not bbc.ready():
            print(f"The probe isn't in: type {BBC.install_line()} on the BBC")
            return 1
        t = time.monotonic()
        bbc.shared_write(0, data)
        up = time.monotonic() - t
        print(f"uploaded in {up:.1f} s ({len(data) / max(up, 1e-3) / 1024:.0f} KB/s)", flush=True)
        bbc.type(f"MODE {o.mode}\r")
        if o.mode == 1:
            bbc.type("VDU19,1,1;0;19,2,6;0;\r")              # black, red, cyan, white
        time.sleep(0.5)
        bbc.poke(PLAYER, player(bbc.shared()["at"]))
        bbc.type(f"CALL &{PLAYER:X}\r", wait=False)      # the player takes the machine over
        t = time.monotonic()
        time.sleep(max(0.0, n * ivl / 50.08 - 1.0))          # nothing on the network meanwhile
        while bbc.shared()["read"] < len(data) - 1 and time.monotonic() - t < n * ivl / 50 + 60:
            time.sleep(0.5)
        el = time.monotonic() - t
        print(f"played {n} frames in about {el:.1f} s ({n / el:.1f} a second)")
    except ProbeError as e:
        print(e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
