#!/usr/bin/env python3
"""*CLAUDE end to end on an emulated Model B: the hook (star_claude.py) on beebjit running the real
Pi1MHz probe firmware, typed at its keyboard, heard by the watcher; every stop by the ladder's
rung 0 (the vsync event), so no NMI.  No Claude, no API key.

    PI1MHZ=~/Pi1MHz python3 tests/test_star_claude.py
"""
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import star_claude as sc  # noqa: E402
from bbc import BBC  # noqa: E402

PI1MHZ = os.path.expanduser(os.environ.get("PI1MHZ", "~/Pi1MHz"))
RUN_BEEBJIT = os.path.join(HERE, "beebjit", "run_beebjit.sh")
PORT = int(os.environ.get("PORT", "18591"))
HOST = f"127.0.0.1:{PORT}"
fails = checks = 0


def ok(cond, what):
    global fails, checks
    checks += 1
    print(f"  {'ok' if cond else 'FAIL'}: {what}")
    if not cond:
        fails += 1


def sim_screen():
    return urllib.request.urlopen(f"http://{HOST}/sim/screen", timeout=10).read().decode()


def sim(path, **params):
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"http://{HOST}{path}?{q}", method="POST", data=b"")
    return urllib.request.urlopen(req, timeout=60).read().decode()


def lines():
    return [l.strip() for l in sim_screen().splitlines() if l.strip()]


print("== the hook, assembled")
ok(len(sc.CODE) <= 0x100, f"{len(sc.CODE)} bytes, in &0A00-&0AFF")
ok(sc.intact(sc.hook_bytes(0x1234, 7)), "intact() ignores the vector and the count")
ok(not sc.intact(sc.hook_bytes(0x1234)[:-1] + b"X"), "and sees a changed byte")

def missing():
    """What run_beebjit.sh needs and doesn't have, or None."""
    bj = os.path.expanduser(os.environ.get("BEEBJIT", "~/presto/beebjit-upstream"))
    if not os.path.exists(os.path.join(bj, "bbc.c")) or not os.path.exists(os.path.join(bj, "roms", "os12.rom")):
        return f"no beebjit at {bj} (BEEBJIT)"
    if not os.path.exists(os.path.join(PI1MHZ, "src", "tests", "ramprobe", "board.h")):
        return f"no Pi1MHz checkout with the probe's test board at {PI1MHZ} (PI1MHZ)"
    with open(os.path.join(PI1MHZ, "src", "ramprobe_api.h")) as f:
        if "ramprobe_api_shared" not in f.read():
            return f"the Pi1MHz checkout at {PI1MHZ} has no shared memory (PI1MHZ)"
    return None


if missing():
    print(f"== *CLAUDE: SKIPPED, {missing()} ==")
    sys.exit(1 if fails else 0)
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
    rungs = []
    real_halt = bbc.halt
    bbc.halt = lambda: (real_halt(), rungs.append(bbc.rung))[0]
    sim("/sim/type", text=BBC.install_line() + "\r")
    ok(bbc.ready(), "the probe is in")

    sc.CHECK_EVERY = 3.0
    heard, logs = [], []
    hold = threading.Event()

    def handle(prompt):
        heard.append(prompt)
        hold.wait(30)
    threading.Thread(target=sc.Watcher(bbc, log=logs.append).watch, args=(handle,), daemon=True).start()
    deadline = time.monotonic() + 15
    while not any("is in" in l for l in logs) and time.monotonic() < deadline:
        time.sleep(0.1)
    ok(any("is in" in l for l in logs), f"the watcher put the hook in: {logs[:1]}")
    ok(rungs and set(rungs) <= {0, 1}, f"its stops by rung 1 or 0, no NMI: {rungs}")
    rungs.clear()

    def ask(text, want):
        n = len(heard)
        sim("/sim/type", text=f"*CLAUDE {text}\r")
        t = time.monotonic()
        while len(heard) == n and time.monotonic() - t < 10:
            time.sleep(0.02)
        ok(heard[n:] == [want], f"*CLAUDE {text}: heard {heard[n:]} in {time.monotonic() - t:.2f} s")

    print("== a prompt")
    ask("  draw a house", "DRAW A HOUSE")          # beebjit types with Caps Lock on, as a BBC starts
    ok(lines()[-2] == "Claude is on it", f"the BBC says so: {lines()[-2]!r}")
    sim("/sim/type", text="*CLAUDE another\r")
    ok(lines()[-2] == "Claude is busy", f"one at a time: {lines()[-2]!r}")
    hold.set()
    time.sleep(1.0)
    hold.clear()
    sim("/sim/type", text="*CLAUDE\r")
    ok(lines()[-2] == "*CLAUDE <prompt>", f"no prompt, the usage: {lines()[-2]!r}")
    sim("/sim/type", text="PRINT 6*7\r")
    ok(lines()[-2] == "42", "BASIC carries on")

    ok(rungs and set(rungs) <= {0, 1}, f"watching and answering, no NMI: {sorted(set(rungs))}")

    print("== the next one, and after a Break")
    ask("two", "TWO")
    hold.set()
    time.sleep(1.0)
    sim("/sim/break")
    deadline = time.monotonic() + 15
    while sum("is in" in l for l in logs) < 2 and time.monotonic() < deadline:
        time.sleep(0.1)
    ok(sum("is in" in l for l in logs) == 2, "the hook back after the Break")
    ok(set(rungs) <= {0, 1}, f"and after the Break, no NMI still: {sorted(set(rungs))}")
    ask("after a break", "AFTER A BREAK")
finally:
    try:
        sim("/sim/quit")
    except OSError:
        pass
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()

print(f"\n{checks} checks, {fails} failures")
if fails:
    sys.exit(1)
print("*CLAUDE: TESTS PASSED")
