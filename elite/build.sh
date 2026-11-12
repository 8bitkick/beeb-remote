#!/bin/sh -e
# elite/build.sh: elite/run_probe, elite-agent's player on a real BBC Micro Model B through the
# Pi1MHz RAM probe (elite/README.md).  No emulator: the BBC is the machine.  The player itself,
# its tuning and its careers are elite-agent's: this builds against its cplayer.
#   ELITE_AGENT  an elite-agent checkout (default ~/elite-agent)
HERE=$(cd "$(dirname "$0")" && pwd)
C=${ELITE_AGENT:-$HOME/elite-agent}/cplayer
if [ ! -f "$C/player.h" ]; then echo "elite/build.sh: no elite-agent player at $C (ELITE_AGENT)"; exit 1; fi
B=$(mktemp -d)
trap 'rm -rf "$B"' EXIT
for f in keyq agent cfly combat docked drv elite_ver fair flight galaxy pilot ships show wsp_pilot wsp_dock; do
    gcc -O2 -fwrapv -w -c "$C/$f.c" -o "$B/$f.o"
done
gcc -O2 -Wall -fwrapv -I"$C" -o "$HERE/run_probe" "$HERE/run_probe.c" "$B"/*.o -lm
echo "built $HERE/run_probe"
