#!/usr/bin/env python3
"""demo50 end to end on an emulated Model B: a short clip encoded, put in the Pi1MHz's shared
memory and played by the player in the BBC's RAM, on beebjit running the real Pi1MHz probe
firmware.  It passes if every frame is drawn, none starts late, the probe makes no stop while the
player runs (beebjit's own count, GET /sim/stops), and the RAM it used is put back after.

The clip is ffmpeg's test pattern (moving colour bars and a counter), so no video is needed.
Real time: the BBC plays at 50 frames a second, as it would on the real machine.

    PI1MHZ=~/Pi1MHz python3 tests/test_demo50.py
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import demo50  # noqa: E402
from bbc import BBC  # noqa: E402

PI1MHZ = os.path.expanduser(os.environ.get("PI1MHZ", "~/Pi1MHz"))
RUN_BEEBJIT = os.path.join(HERE, "beebjit", "run_beebjit.sh")
PORT = int(os.environ.get("PORT", "18592"))
HOST = f"127.0.0.1:{PORT}"
PLAYS = [("7:1,0:1,1:1,2:1", []), ("1:2", ["--lines"])]      # every mode, then MODE 1's line palettes
fails = checks = 0


def ok(cond, what):
    global fails, checks
    checks += 1
    print(f"  {'ok' if cond else 'FAIL'}: {what}")
    if not cond:
        fails += 1


def sim_screen():
    return urllib.request.urlopen(f"http://{HOST}/sim/screen", timeout=10).read().decode()


def sim_stops():
    return json.loads(urllib.request.urlopen(f"http://{HOST}/sim/stops", timeout=10).read())["stops"]


def sim(path, **params):
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"http://{HOST}{path}?{q}", method="POST", data=b"")
    return urllib.request.urlopen(req, timeout=60).read().decode()


def missing():
    """What the test needs and doesn't have, or None."""
    bj = os.path.expanduser(os.environ.get("BEEBJIT", "~/presto/beebjit-upstream"))
    if not os.path.exists(os.path.join(bj, "bbc.c")) or not os.path.exists(os.path.join(bj, "roms", "os12.rom")):
        return f"no beebjit at {bj} (BEEBJIT)"
    if not os.path.exists(os.path.join(PI1MHZ, "src", "tests", "ramprobe", "board.h")):
        return f"no Pi1MHz checkout with the probe's test board at {PI1MHZ} (PI1MHZ)"
    with open(os.path.join(PI1MHZ, "src", "ramprobe_api.h")) as f:
        if "ramprobe_api_shared" not in f.read():
            return f"the Pi1MHz checkout at {PI1MHZ} has no shared memory (PI1MHZ)"
    if not shutil.which("ffmpeg"):
        return "no ffmpeg"
    return None


def play(bbc, clip, plan, extra):
    """demo50 as from the command line, with the probe's stops counted while the player runs:
    from when it starts reading shared memory to when it has read the whole stream."""
    out = io.StringIO()
    result = {}

    def run():
        sys.argv = ["demo50.py", clip, "--host", HOST, "--plan", plan, "--no-cache"] + extra
        with contextlib.redirect_stdout(out):
            result["rc"] = demo50.main()
    t = threading.Thread(target=run)
    t.start()
    start = bbc.shared()["read"]
    while t.is_alive() and "uploaded in" not in out.getvalue():      # (the encoding takes a while)
        time.sleep(0.2)
        start = bbc.shared()["read"]
    while t.is_alive() and bbc.shared()["read"] == start:             # until the player has started
        time.sleep(0.05)
    before = sim_stops()
    playing = time.monotonic()
    while t.is_alive() and bbc.shared()["read"] < len_stream(out.getvalue()):
        time.sleep(0.1)
    after = sim_stops()
    took = time.monotonic() - playing
    t.join()
    return result.get("rc"), out.getvalue(), after - before, took


def len_stream(text):
    """The stream's length, from demo50's own report: its "N KB in all", in KB, rounded down."""
    for line in text.splitlines():
        if line.endswith(" KB in all"):
            return int(line.split()[0]) * 1024
    return 1 << 30


if missing():
    print(f"== demo50: SKIPPED, {missing()} ==")
    sys.exit(0)
tmp = tempfile.mkdtemp()
clip = os.path.join(tmp, "bars.mp4")
subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x256:rate=50", "-t", "6",
                "-pix_fmt", "yuv420p", clip], check=True)
server = subprocess.Popen(["sh", RUN_BEEBJIT, str(PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    deadline = time.monotonic() + 300
    while True:
        try:
            if ">" in sim_screen():
                break
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise SystemExit("beebjit didn't come up")
        time.sleep(0.5)
    bbc = BBC(HOST)
    sim("/sim/type", text=BBC.install_line() + "\r")
    ok(bbc.ready(), "the probe is in")
    keys = bbc.peek(0x0B00, 0x100)
    work = bbc.peek(demo50.BLOCKS, len(demo50.BLOCKS_CODE))
    bbc.soft_install()                         # bbc.py's resident routine: demo50 takes it out first
    ok(bbc.soft_installed(), "the resident routine for typing is in")
    for plan, extra in PLAYS:
        frames = int(sum(float(s) for _, s in (p.split(":") for p in plan.split(","))) * 50)
        print(f"== --plan {plan} {' '.join(extra)}")
        rc, text, stops, took = play(bbc, clip, plan, extra)
        counted = [l for l in text.splitlines() if l.startswith("the player counted")]
        ok(rc == 0, f"demo50 finished: {rc}")
        if plan == PLAYS[0][0]:
            ok("took out bbc.py's resident routine" in text and not bbc.soft_installed(),
               "demo50 took the resident routine out before playing")
        ok(counted == [f"the player counted {frames} frames, 0 started late"],
           f"every frame, none late: {counted or text.splitlines()[-3:]}")
        ok(stops == 0, f"no stops while it played ({took:.1f} s): {stops}")
        ok(bbc.peek(0x0B00, 0x100) == keys, "&0B00 put back: the *KEY strings, the probe's *KEY10 among them")
        ok(bbc.peek(demo50.BLOCKS, len(demo50.BLOCKS_CODE)) == work,
           f"&0E00-&{demo50.BLOCKS + len(demo50.BLOCKS_CODE) - 1:04X} put back: a filing system's workspace, BASIC's program")
    sim("/sim/break")
    ok(bbc.ready() and "Pi1MHz probe on" in sim_screen(), "a soft Break brings the probe back")
finally:
    try:
        sim("/sim/quit")
    except OSError:
        pass
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{checks} checks, {fails} failures")
if fails:
    sys.exit(1)
print("demo50: TESTS PASSED")
