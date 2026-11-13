#!/usr/bin/env python3
"""The resident routine (bbc.py soft_install) on an emulated Model B: typing and reading the
screen with no stops, on beebjit running the real Pi1MHz probe firmware, over HTTP.

The emulator counts the probe's stops itself (GET /sim/stops), so every check of "no stop" here is
its count, not the client's.  A blank disc is in drive 0, for *CAT.

    PI1MHZ=~/Pi1MHz python3 tests/test_soft.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import beeb  # noqa: E402
import bbc as bbcmod  # noqa: E402
from bbc import BBC, ProbeError  # noqa: E402

PI1MHZ = os.path.expanduser(os.environ.get("PI1MHZ", "~/Pi1MHz"))
RUN_BEEBJIT = os.path.join(HERE, "beebjit", "run_beebjit.sh")
PORT = int(os.environ.get("PORT", "8141"))
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


def sim_stops():
    return json.loads(urllib.request.urlopen(f"http://{HOST}/sim/stops", timeout=10).read())["stops"]


def sim(path, **params):
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"http://{HOST}{path}?{q}", method="POST", data=b"")
    return urllib.request.urlopen(req, timeout=60).read().decode()


def missing():
    bj = os.path.expanduser(os.environ.get("BEEBJIT", "~/presto/beebjit-upstream"))
    if not os.path.exists(os.path.join(bj, "bbc.c")) or not os.path.exists(os.path.join(bj, "roms", "os12.rom")):
        return f"no beebjit at {bj} (BEEBJIT)"
    if not os.path.exists(os.path.join(PI1MHZ, "src", "tests", "ramprobe", "board.h")):
        return f"no Pi1MHz checkout with the probe's test board at {PI1MHZ} (PI1MHZ)"
    with open(os.path.join(PI1MHZ, "src", "ramprobe_api.h")) as f:
        if "ramprobe_api_shared" not in f.read():
            return f"the Pi1MHz checkout at {PI1MHZ} has no shared memory (PI1MHZ)"
    return None


LOOK = bytes((0xAE, 0x90, 0xFC, 0xAC, 0x91, 0xFC, 0x60))       # LDX &FC90: LDY &FC91: RTS


def vectors(bbc):
    """EVNTV and the probe's hook's target, with one stop."""
    with bbc.stopped():
        e = bbc.peek(0x0220, 2)
        r = bbc.run(LOOK)
    return e[0] | e[1] << 8, r["x"] | r["y"] << 8


def checksum_line(text):
    """A BASIC statement that prints a checksum of the string it carries, and that checksum."""
    stmt = f'S%=0:A$="{text}":FOR I%=1 TO LEN(A$):S%=S%+ASC(MID$(A$,I%,1))*I%:NEXT:PRINT "SUM";S%'
    return stmt, sum(ord(c) * (i + 1) for i, c in enumerate(text))


def screen_text():
    return " ".join(sim_screen().split())


if missing():
    print(f"== resident routine: SKIPPED, {missing()} ==")
    sys.exit(0)
tmp = tempfile.mkdtemp()
disc = os.path.join(tmp, "blank.ssd")
with open(disc, "wb") as f:                    # an empty DFS catalogue, 800 sectors
    d = bytearray(200 * 1024)
    d[0:8] = b"SOFTTEST"
    d[0x106], d[0x107] = 0x03, 0x20
    f.write(d)
env = dict(os.environ, BEEBJIT_ARGS=f"-0 {disc} -writeable -mutable")
server = subprocess.Popen(["sh", RUN_BEEBJIT, str(PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
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
    evntv, hooked = vectors(bbc)
    ok(evntv == 0xFC8F, f"EVNTV is the probe's hook: &{evntv:04X}, which goes on to &{hooked:04X}")

    print("== install")
    ok(not bbc.soft_installed(), "not in before the install")
    before = sim_stops()
    bbc.soft_install()
    stops = sim_stops() - before
    ok(stops == 1, f"soft_install: {stops} stop")
    ok(bbc.soft_installed(), "soft_installed() says so")
    e, h = vectors(bbc)
    ok(e == 0xFC8F and h == bbcmod.SOFT_AT, f"EVNTV still the probe's hook (&{e:04X}), the hook to &{h:04X}")
    code = bbc.soft_code(*bbc._soft_place())[0]
    got = bbc.peek(bbcmod.SOFT_AT, len(code))
    ok(len(code) <= 256 and got[:7] + got[9:] == code[:7] + code[9:], f"the routine at &0A00, {len(code)} bytes")
    chain = got[7:9]
    ok(chain[0] | chain[1] << 8 == hooked, f"it goes on to &{chain[1]:02X}{chain[0]:02X}, what the hook held")
    try:
        bbc.soft_install()
        ok(True, "a second soft_install() does nothing")
    except ProbeError as ex:
        ok(False, f"a second soft_install(): {ex}")

    print("== typing and reading the screen, the emulator counting stops")
    before = sim_stops()
    out = beeb.command(bbc, "*CAT")
    ok(any("SOFTTEST" in l for l in out), f"cmd *CAT: {out}")
    out = beeb.command(bbc, "PRINT 6*7")
    ok([l.strip() for l in out] == ["42"], f"cmd PRINT 6*7: {out}")
    try:
        beeb.command(bbc, "PRNT 6*7")
        ok(False, "a mistyped command is caught")
    except ProbeError as ex:
        ok("Mistake" in str(ex), f"a mistyped command is caught: {ex}")
    text = "".join(chr(33 + (i * 7) % 94) for i in range(150)).replace('"', "'")
    stmt, want = checksum_line(text)
    ok(len(stmt) >= 200, f"a {len(stmt)}-character line")
    out = beeb.command(bbc, stmt)
    ok(out and out[-1].replace(" ", "") == f"SUM{want}", f"it arrives exactly: {out[-1:]} (SUM{want})")
    prog = "".join(f"{10 * n} T%=T%+{n}*{n}:REM line {n} !#$%&'()*+,-./:;<=>?@[\\]^_{{|}}~\n" for n in range(1, 30))
    prog += "300 PRINT \"T=\";T%\n"
    ok(len(prog.splitlines()) == 30, "a 30-line program")
    r = bbc.run_basic("5 T%=0\n" + prog, 10)
    ok(r["ended_by_itself"] and "error" not in r and f"T={sum(n * n for n in range(1, 30))}" in r["screen"].replace(" ", ""),
       f"typed and run, its output read back: {r['screen'].splitlines()[-2:]}")
    bbc.type("LIST 290\r")
    time.sleep(0.5)
    ok("290 T%=T%+29*29:REM line 29 !#$%&'()*+,-./:;<=>?@[\\]^_{|}~".replace(" ", "")
       in "".join(bbc.screen().splitlines()).replace(" ", ""),
       "LIST shows a line as typed, every character")
    stops = sim_stops() - before
    ok(stops == 0, f"no stops through all of it: {stops}")

    print("== a full keyboard buffer: nothing lost")
    before = sim_stops()
    bbc.type("NEW\r10 T%=TIME:REPEAT UNTIL TIME>T%+300\rRUN\r")
    text = "".join(chr(65 + (i * 5) % 26) for i in range(120))
    stmt, want = checksum_line(text)
    t = time.monotonic()
    bbc.type(stmt + "\r", wait=False)
    time.sleep(1.5)
    ctl = bbc._soft_place()[1]
    c = bbc.shared_read(ctl, 8)
    behind = (c[bbcmod.C_W] - c[bbcmod.C_R]) & 0xFF
    ok(behind > 0, f"while the program doesn't read the keyboard, the routine waits: {behind} characters still in the ring")
    bbc._soft_wait(ctl, bbcmod.C_R, c[bbcmod.C_W], 30, "typing")
    time.sleep(1.5)
    ok(f"SUM{want}" in screen_text().replace(" ", ""), f"and they all arrive after it: SUM{want}")
    ok(sim_stops() == before, f"no stops: {sim_stops() - before}")

    print("== Escape, with no stop")
    before = sim_stops()
    bbc.type("10 REPEAT:UNTIL FALSE\rRUN\r")
    time.sleep(0.5)
    bbc.escape()
    time.sleep(0.5)
    ok("Escape at line 10" in sim_screen(), "escape() stops a program")
    ok(sim_stops() == before, f"no stops: {sim_stops() - before}")

    print("== the probe's stops still work with it in")
    b0 = BBC(HOST, max_rung=0)
    ok(b0.halt() is None and b0.rung == 0, "rung 0 (the vsync event, through the hook)")
    b0.go()
    r = bbc.halt(regs=True)
    ok(r is not None and bbc.rung == 1, f"rung 1: {r}")
    bbc.go()
    with b0.stopped():
        ok(b0.peek(0x0220, 2) == b"\x8f\xfc" and b0.rung == 0, "a rung 0 peek")
    out = beeb.command(bbc, "PRINT 2+2")
    ok([l.strip() for l in out] == ["4"], f"and typing after them: {out}")

    print("== its mark in shared memory lost (another application wrote there)")
    bbc.shared_write(bbc._soft_place()[1] + bbcmod.C_MARK, bytes(8))
    ok(not bbc.soft_installed(), "soft_installed() is false without the mark")
    before = sim_stops()
    bbc.soft_install()
    ok(sim_stops() - before == 1 and bbc.soft_installed(), f"soft_install() takes it back: {sim_stops() - before} stop")
    ok(vectors(bbc)[1] == bbcmod.SOFT_AT, "the hook still leads to the routine, once")
    out = beeb.command(bbc, "PRINT 8*8")
    ok([l.strip() for l in out] == ["64"], f"and typing: {out}")

    print("== uninstall")
    before = sim_stops()
    ok(bbc.soft_uninstall(), "soft_uninstall()")
    ok(sim_stops() == before, f"by its mailbox: no stop ({sim_stops() - before})")
    ok(not bbc.soft_installed(), "not in after")
    ok(vectors(bbc) == (evntv, hooked), "EVNTV and the hook's target exactly as before")
    ok(not bbc.soft_uninstall(), "a second soft_uninstall() finds nothing")
    out = beeb.command(bbc, "PRINT 3+3")
    ok([l.strip() for l in out] == ["6"], f"typing with stops again: {out}")
    bbc.soft_install()
    bbc.type("*FX13,4\r")                     # the vsync event off: the routine can't answer
    time.sleep(0.5)
    before = sim_stops()
    ok(bbc.soft_uninstall(timeout=0.5), "soft_uninstall() with the routine deaf")
    ok(sim_stops() - before == 1, f"the fallback: {sim_stops() - before} stop")
    ok(vectors(bbc) == (evntv, hooked), "EVNTV and the hook's target exactly as before")
    bbc.type("*FX14,4\r")
    time.sleep(0.5)

    print("== *CLAUDE's hook and the routine both want &0A00")
    bbc.soft_install()
    import star_claude
    w = star_claude.Watcher(bbc, log=lambda m: None)
    ok(w.check() is None and bbc.peek(0x021E, 2) != b"\x00\x0a", "star_claude.py leaves its hook out while the routine is in")
    bbc.soft_uninstall()

    print("== Breaks")
    bbc.soft_install()
    sim("/sim/break")
    ok(bbc.ready() and not bbc.soft_installed(), "a soft Break: the probe back, the routine out")
    ok(vectors(bbc) == (evntv, hooked), "EVNTV the probe's hook, to the OS's default")
    bbc.soft_install()
    out = beeb.command(bbc, "PRINT 5*5")
    ok([l.strip() for l in out] == ["25"], f"in again, and typing: {out}")
    sim("/sim/break", ctrl=1)
    sim("/sim/type", text=BBC.install_line() + "\r")
    ok(bbc.ready() and not bbc.soft_installed(), "Ctrl-Break and the install line: the routine out")
    ok(vectors(bbc) == (evntv, hooked), "EVNTV the probe's hook, to the OS's default")
    bbc.auto_soft = True                       # as a new BBC() is; soft_uninstall() above turned it off
    before = sim_stops()
    out = beeb.command(bbc, "PRINT 7*7")
    ok([l.strip() for l in out] == ["49"] and bbc.soft_installed() and sim_stops() - before == 1,
       f"typing puts the routine back by itself, with one stop: {out}, {sim_stops() - before} stops")
    bbc.soft_uninstall()
    before = sim_stops()
    out = beeb.command(bbc, "PRINT 8*8")
    ok([l.strip() for l in out] == ["64"] and not bbc.soft_installed(),
       f"after soft_uninstall(), typing with stops and the routine left out: {out}, {sim_stops() - before} stops")
    bbc.auto_soft = True
    bbc.soft_install()
    bbc.type("MODE 2:REPEAT UNTIL FALSE\r")         # a program running, in a graphics mode, as a game is
    time.sleep(1)
    bbc.soft_uninstall()
    fresh = BBC(bbc.host)
    before = sim_stops()
    try:
        fresh.type("PRINT 9*9\r")
        ok(False, "typing into a running program put the routine in")
    except ProbeError as ex:
        ok("isn't at BASIC's prompt" in str(ex) and not fresh.soft_installed() and sim_stops() - before == 1,
           f"a program running: the routine left out, nothing typed, one stop to look: {sim_stops() - before} stops")
    bbc.auto_soft = False
    bbc.escape()
    time.sleep(0.5)
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
print("RESIDENT ROUTINE: TESTS PASSED")
