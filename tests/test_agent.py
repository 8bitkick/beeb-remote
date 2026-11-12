#!/usr/bin/env python3
"""The remote client end to end on an emulated Model B, with a scripted model in place of Claude.

The real bbc.py and agent.py drive beebjit running the real Pi1MHz probe firmware (built by the
tests/beebjit/run_beebjit.sh, from a Pi1MHz checkout), over HTTP.  Only the model is scripted:
each turn checks what the client sent, then answers as Claude would.  No API key, no cost.

    PI1MHZ=~/Pi1MHz python3 tests/test_agent.py
"""
import os
import subprocess
import sys
import time
import types
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import agent  # noqa: E402
from bbc import BBC, ProbeError  # noqa: E402

PI1MHZ = os.path.expanduser(os.environ.get("PI1MHZ", "~/Pi1MHz"))
RUN_BEEBJIT = os.path.join(HERE, "beebjit", "run_beebjit.sh")
PORT = int(os.environ.get("PORT", "18590"))
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


# ---- a scripted model -----------------------------------------------------------------
def tool_use(i, name, **inp):
    return types.SimpleNamespace(type="tool_use", id=f"t{i}", name=name, input=inp)


def text(s):
    return types.SimpleNamespace(type="text", text=s)


def reply(stop, *blocks):
    return types.SimpleNamespace(stop_reason=stop, content=list(blocks), stop_details=None)


class ScriptedModel:
    """beta.messages.create(**request) answers from a list of functions, each given the request."""
    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append(request)
        return self.turns.pop(0)(request)


def last_results(request):
    content = request["messages"][-1]["content"]
    return {r["tool_use_id"]: r for r in content}


BUGGY = "10 FOR I%=1 TO 3\n20 PRINT \"SQUARE \";I%*I%+X\n30 NEXT\n"
FIXED = "10 FOR I%=1 TO 3\n20 PRINT \"SQUARE \";I%*I%\n30 NEXT\n"
FINAL = "10 MODE 7\n20 FOR I%=1 TO 5\n30 PRINT \"SQUARE \";I%*I%\n40 NEXT\n"
seen = {}


def turn1(req):
    seen["first"] = req
    return reply("tool_use", text("I'll try a first version."), tool_use(1, "run_basic", program=BUGGY, seconds=3))


def turn2(req):
    seen["run1"] = last_results(req)["t1"]
    return reply("tool_use", tool_use(2, "run_basic", program=FIXED, seconds=3),
                 tool_use(3, "peek_memory", address=0x0D00, length=1),
                 tool_use(4, "poke_memory", address=0x0D00, hex="00"),
                 tool_use(5, "poke_memory", address=0x3000, hex="a5"))


def turn3(req):
    seen["run2"] = last_results(req)
    return reply("tool_use", tool_use(6, "read_screen"), tool_use(7, "peek_memory", address=0x3000, length=1))


def turn4(req):
    seen["run3"] = last_results(req)
    return reply("end_turn", text("Here are the first five squares.\n\n```basic\n" + FINAL + "```\n"))


def declined(req):
    return types.SimpleNamespace(stop_reason="refusal", content=[],
                                 stop_details=types.SimpleNamespace(category="cyber"))


# ---- the run --------------------------------------------------------------------------
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
    print(f"== remote client: SKIPPED, {missing()} ==")
    sys.exit(0)
server = subprocess.Popen(["sh", RUN_BEEBJIT, str(PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    deadline = time.monotonic() + 300
    while True:
        try:
            if ">" in urllib.request.urlopen(f"http://{HOST}/sim/screen", timeout=1).read().decode():
                break
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise SystemExit("beebjit didn't come up")
        time.sleep(0.5)
    bbc = BBC(HOST)

    print("== before the probe is in")
    ok(not bbc.ready(), "ready() is false")
    try:
        bbc.halt()
        ok(False, "halt refused")
    except ProbeError as e:
        ok("not installed" in str(e), f"halt refused: {e}")

    print("== the install line, once")
    sim("/sim/type", text=BBC.install_line() + "\r")
    ok(bbc.ready() and "Pi1MHz probe on" in sim_screen(), "installed, and the BBC says so")
    sim("/sim/break")
    ok(bbc.ready(), "after a Break it's back by itself")
    ok(bbc.is_model_b(), "and it's a Model B")

    print("== registers: a rung 0 stop has none (it's inside the OS); halt(regs=True) skips it")
    b0 = BBC(HOST, max_rung=0)
    ok(b0.halt() is None and b0.rung == 0, "rung 0: halted, no registers")
    try:
        b0.go(pc=0x2000)
        ok(False, "continue at a new PC after rung 0 refused")
    except ProbeError as e:
        ok("rung 0" in str(e), f"continue at a new PC after rung 0 refused: {e}")
    b0.go()
    try:
        b0.halt(regs=True)
        ok(False, "halt(regs=True) with max_rung 0 refused")
    except ProbeError as e:
        ok("rung 1 or 2" in str(e), f"halt(regs=True) with max_rung 0 refused: {e}")
    r = bbc.halt(regs=True)
    ok(r is not None and bbc.rung == 1 and {"pc", "a", "s"} <= set(r), f"halt(regs=True): rung {bbc.rung}, {r}")
    bbc.go(pc=r["pc"], a=r["a"])
    time.sleep(0.3)
    bbc.type("PRINT 6*7\r")
    time.sleep(0.5)
    ok(bbc.screen().splitlines()[-2].strip() == "42", "a continue that sets them, and BASIC carries on")

    print("== bbc.py on its own")
    bbc.type("PRINT 6*7\r")
    time.sleep(0.5)
    ok(bbc.screen().splitlines()[-2].strip() == "42", "type: PRINT 6*7 gives 42")
    r = bbc.run_basic("10 I%=0\n20 I%=I%+1:GOTO 20\n", 2)
    ok(not r["ended_by_itself"] and "error" not in r, f"an endless loop is stopped, and our Escape isn't its error: {r.get('error')}")
    long = "10 REM " + "X" * 200 + "\n20 PRINT \"LONG\";LEN(\"" + "A" * 100 + "\")\n"
    r = bbc.run_basic(long, 5)
    ok(r["ended_by_itself"] and "LONG100" in r["screen"] and "error" not in r, "a 207-character line goes in whole, and runs")
    ok("every line" in bbc.run_basic("PRINT 1", 1).get("error", ""), "a line with no number is refused before typing")

    print("== the agent, with a scripted model")
    model = ScriptedModel([turn1, turn2, turn3, turn4])
    out = []
    answer = agent.converse(model, bbc, "Print the first five square numbers", log=out.append)
    first = seen["first"]
    ok(first["model"] == "claude-opus-5-5" and first["output_config"] == {"effort": "high"}, "asks claude-opus-5-5 at high effort")
    ok(first["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in first["betas"], "with fallbacks: default")
    ok(all(t["strict"] and not t["input_schema"]["additionalProperties"] for t in first["tools"]), "strict tools")
    r1 = seen["run1"]
    ok("No such variable at line 20" in r1["content"] and not r1["is_error"], "run 1: the error and its line come back")
    r2 = seen["run2"]
    ok("SQUARE 9" in r2["t2"]["content"].replace("  ", " ") or "SQUARE 9" in " ".join(r2["t2"]["content"].split()), "run 2: the fixed program's output")
    ok(r2["t3"]["content"] == "4c" and not r2["t3"]["is_error"], f"peek &0D00, the probe's JMP: {r2['t3']['content']}")
    ok(r2["t4"]["is_error"] and "NMI jump" in r2["t4"]["content"], "poke on the probe's NMI jump refused")
    ok(not r2["t5"]["is_error"], "poke &3000 done")
    r3 = seen["run3"]
    ok("SQUARE" in r3["t6"]["content"], "read_screen shows the screen")
    ok(r3["t7"]["content"] == "a5", "and peek reads back the poke")
    ok(answer and "```basic" in answer, "the final answer has the program")
    ok(agent.type_final(bbc, answer, log=out.append), "the final program is typed in")
    bbc.type("LIST\r")
    time.sleep(0.5)
    shown = bbc.screen()
    ok("40 NEXT" in shown and "30 PRINT \"SQUARE \";I%*I%" in shown, "LIST shows it in the BBC")
    bbc.type("RUN\r")
    time.sleep(1)
    ok("SQUARE 25" in " ".join(bbc.screen().split()), "and RUN runs it: SQUARE 25")
    print("     what the user saw: " + " | ".join(out))

    print("== declined")
    out = []
    ok(agent.converse(ScriptedModel([declined]), bbc, "x", log=out.append) is None and "declined: cyber" in out[0],
       f"a refusal stops the loop: {out}")

    print("== a Break mid-session")
    sim("/sim/break")
    ok(bbc.ready(), "the probe is back after a Break mid-session")
    bbc.type("PRINT 2+2\r")
    time.sleep(0.5)
    ok(bbc.screen().splitlines()[-2].strip() == "4", "and typing works again")
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
print("REMOTE CLIENT: TESTS PASSED")
