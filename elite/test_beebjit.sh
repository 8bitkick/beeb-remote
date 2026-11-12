#!/bin/sh
# elite/test_beebjit.sh: run_probe end to end, on beebjit's Model B with the Pi1MHz probe's own
# firmware C (tests/beebjit), the real disc Elite in its drive, all over HTTP as on a real BBC.
# From BASIC's prompt: install the probe, boot Elite, and play 8 game minutes. It must trade at
# Lave, save the commander to the disc (E.CLAUDE in the catalogue), fly to Leesti and dock there,
# with no probe errors and the game hardly stopped. ~12 minutes: the game runs in real time.
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
OUT=${TMPDIR:-/tmp}/probe-beebjit-$$; mkdir -p "$OUT"
cp "$DISC" "$OUT/elite.ssd"
"$HERE/build.sh" > "$OUT/build.log" 2>&1 || { cat "$OUT/build.log"; exit 1; }
BEEBJIT_ARGS="-0 $OUT/elite.ssd -writeable -mutable" "$TOP/tests/beebjit/run_beebjit.sh" "$PORT" > "$OUT/beebjit.log" 2>&1 &
SERVER=$!
trap 'curl -s -X POST "localhost:$PORT/sim/quit" > /dev/null 2>&1; kill $SERVER 2> /dev/null' EXIT
i=0
until curl -s -m 2 "localhost:$PORT/sim/screen" | grep -q '^>'; do
    i=$((i + 1)); [ $i -lt 120 ] || { echo "elite test: FAIL, no BASIC prompt"; tail "$OUT/beebjit.log"; exit 1; }
    sleep 1
done
curl -s -X POST "localhost:$PORT/sim/type?text=X%25%3D23%3ACALL%20%26FC88%0D" > /dev/null
BOOT=1 "$HERE/run_probe" "127.0.0.1:$PORT" 8 "$OUT/journal.jsonl" > "$OUT/run.log" 2>&1
python3 -I - "$OUT" <<'E'
import json, re, sys
out = sys.argv[1]
log = open(f"{out}/run.log").read()
d = open(f"{out}/elite.ssd", "rb").read()
files = [chr(d[15 + i * 8] & 0x7F) + "." + d[8 + i * 8:15 + i * 8].decode("latin-1").rstrip() for i in range(d[0x105] // 8)]
done = re.search(r"done: .*?([\d.]+) wall minutes; (\d+) stops \(([\d.]+) a second\), stopped ([\d.]+)%", log)
checks = [
    ("Elite booted, patched and found", "patched the copy routine and DKS4 in the docked code" in log and "Docked at Lave" in log),
    ("bought at Lave", "Bought " in log),
    ("saved: E.CLAUDE on the disc", "E.CLAUDE" in files),
    ("launched", "Launched from Lave" in log),
    ("docked at Leesti", "Docked at Leesti" in log),
    ("patched the flight code", "patched the copy routine and DKS4 in the flight code" in log),
    ("no probe errors", bool(done) and not re.search(r"^(check|patch|typing|keys|continue): ", log, re.M)),
    ("hardly stopped: under 0.5 stops a second, under 2% of the time", bool(done) and float(done.group(3)) < 0.5 and float(done.group(4)) < 2),
]
for what, ok in checks:
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
failed = sum(not ok for _, ok in checks)
print(f"elite test: {'PASS' if not failed else 'FAIL'}, {len(checks) - failed} of {len(checks)} (log in {out})")
sys.exit(1 if failed else 0)
E
