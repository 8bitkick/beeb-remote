#!/bin/sh
# elite/test_beebjit.sh: run_probe end to end, on beebjit's Model B with the Pi1MHz probe's own
# firmware C (tests/beebjit), the real disc Elite in its drive, all over HTTP as on a real BBC.
# From BASIC's prompt: install the probe, boot Elite, and play 8 game minutes. It must trade at
# Lave, save the commander to the disc (E.CLAUDE in the catalogue), fly to Leesti and dock there,
# with no probe errors and the game hardly stopped, by run_probe's count and by the emulator's own
# (GET /sim/stops), with two minutes of play with no stop at all.  ~12 minutes: the
# game runs in real time.
#   ELITE_AGENT  an elite-agent checkout (default ~/elite-agent), for the player
#   PI1MHZ       a Pi1MHz checkout with the probe (default ~/Pi1MHz)
#   BEEBJIT      a beebjit checkout (default ~/presto/beebjit-upstream)
#   DISC         the flicker-free disc Elite (default: elite-agent's rp2350-bbc-micro/discs/elite-disc-flicker-free-sth.ssd)
#   PORT         the port to serve on (default 8093)
HERE=$(cd "$(dirname "$0")" && pwd)
TOP=$(dirname "$HERE")
EA=${ELITE_AGENT:-$HOME/elite-agent}
DISC=${DISC:-$EA/rp2350-bbc-micro/discs/elite-disc-flicker-free-sth.ssd}
PORT=${PORT:-8093}
for need in "$EA/cplayer/player.h" "$DISC" "${PI1MHZ:-$HOME/Pi1MHz}/src/ramprobe.c" "${BEEBJIT:-$HOME/presto/beebjit-upstream}/bbc.c"; do
    if [ ! -f "$need" ]; then echo "elite test: SKIPPED, no $need"; exit 0; fi
done
if ! grep -q ramprobe_api_shared "${PI1MHZ:-$HOME/Pi1MHz}/src/ramprobe_api.h"; then echo "elite test: SKIPPED, the Pi1MHz checkout has no shared memory"; exit 0; fi
OUT=${TMPDIR:-/tmp}/probe-beebjit-$$; mkdir -p "$OUT"
cp "$DISC" "$OUT/elite.ssd"
"$HERE/build.sh" > "$OUT/build.log" 2>&1 || { cat "$OUT/build.log"; exit 1; }
BEEBJIT_ARGS="-0 $OUT/elite.ssd -writeable -mutable" "$TOP/tests/beebjit/run_beebjit.sh" "$PORT" > "$OUT/beebjit.log" 2>&1 &
SERVER=$!
trap 'kill $COUNTER 2> /dev/null; curl -s -X POST "localhost:$PORT/sim/quit" > /dev/null 2>&1; kill $SERVER 2> /dev/null' EXIT
i=0
until curl -s -m 2 "localhost:$PORT/sim/screen" | grep -q '^>'; do
    i=$((i + 1)); [ $i -lt 120 ] || { echo "elite test: FAIL, no BASIC prompt"; tail "$OUT/beebjit.log"; exit 1; }
    sleep 1
done
curl -s -X POST "localhost:$PORT/sim/type?text=X%25%3D23%3ACALL%20%26FC88%0D" > /dev/null
# the emulator's count of the probe's stops, every 5 s while it plays
(while :; do echo "$(date +%s) $(curl -s -m 2 "localhost:$PORT/sim/stops")"; sleep 5; done) > "$OUT/stops.log" &
COUNTER=$!
BOOT=1 "$HERE/run_probe" "127.0.0.1:$PORT" 8 "$OUT/journal.jsonl" > "$OUT/run.log" 2>&1
kill $COUNTER 2> /dev/null
python3 -I - "$OUT" <<'E'
import json, re, sys
out = sys.argv[1]
log = open(f"{out}/run.log").read()
d = open(f"{out}/elite.ssd", "rb").read()
files = [chr(d[15 + i * 8] & 0x7F) + "." + d[8 + i * 8:15 + i * 8].decode("latin-1").rstrip() for i in range(d[0x105] // 8)]
done = re.search(r"done: .*?([\d.]+) wall minutes; (\d+) stops \(([\d.]+) a second\), stopped ([\d.]+)%", log)
samples = [(int(t), int(m.group(1))) for t, m in
           ((l.split(" ", 1)[0], re.search(r'"stops":(\d+)', l)) for l in open(f"{out}/stops.log")) if m]
quiet = longest = 0      # the longest stretch with no stop, in seconds
for (t0, n0), (t1, n1) in zip(samples, samples[1:]):
    quiet = quiet + t1 - t0 if n1 == n0 else 0
    longest = max(longest, quiet)
sim_stops = samples[-1][1] - samples[0][1] if len(samples) > 1 else -1
sim_rate = sim_stops / max(1, samples[-1][0] - samples[0][0]) if len(samples) > 1 else 9
checks = [
    ("Elite booted, patched and found", "patched the copy routine and DKS4 in the docked code" in log and "Docked at Lave" in log),
    ("bought at Lave", "Bought " in log),
    ("saved: E.CLAUDE on the disc", "E.CLAUDE" in files),
    ("launched", "Launched from Lave" in log),
    ("docked at Leesti", "Docked at Leesti" in log),
    ("patched the flight code", "patched the copy routine and DKS4 in the flight code" in log),
    ("no probe errors", bool(done) and not re.search(r"^(check|patch|typing|keys|continue): ", log, re.M)),
    ("hardly stopped: under 0.5 stops a second, under 2% of the time", bool(done) and float(done.group(3)) < 0.5 and float(done.group(4)) < 2),
    (f"the emulator counted {sim_stops} stops, boot and all ({sim_rate:.3f} a second): under 0.5 a second", 0 <= sim_stops and sim_rate < 0.5),
    (f"a stretch with no stop: {longest} s, at least 120", longest >= 120),
]
for what, ok in checks:
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
failed = sum(not ok for _, ok in checks)
print(f"elite test: {'PASS' if not failed else 'FAIL'}, {len(checks) - failed} of {len(checks)} (log in {out})")
sys.exit(1 if failed else 0)
E
