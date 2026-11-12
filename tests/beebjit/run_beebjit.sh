#!/bin/sh -e
# The RAM probe on beebjit: a Model B with MOS 1.20, BASIC II and DFS 0.90 on the 8271, where
# kc-beeb is a Master 128.  The real firmware C on board.h, patched into a copy of beebjit, and
# served over HTTP for the tests here.
#   BEEBJIT    a beebjit checkout (default: ~/presto/beebjit-upstream)
#   PI1MHZ     a Pi1MHz checkout on ramprobe-fixes, or stream-fixes for the stream buffer
#              (default: ~/Pi1MHz).  Its test board, stubs and 6502code.bin are used.

#   run_beebjit.sh PORT      just serve it on 127.0.0.1:PORT
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=${SRC_DIR:-${PI1MHZ:-$HOME/Pi1MHz}/src}
BEEBJIT=${BEEBJIT:-$HOME/presto/beebjit-upstream}
if [ ! -f "$BEEBJIT/bbc.c" ] || [ ! -f "$BEEBJIT/roms/os12.rom" ]; then
   echo "== ramprobe on beebjit: SKIPPED, no beebjit at $BEEBJIT =="
   exit 0
fi
B=$(mktemp -d)
trap 'rm -rf "$B"' EXIT
mkdir "$B/bj" "$B/pi"
(cd "$BEEBJIT" && git archive HEAD 2>/dev/null || tar -c --exclude=.git .) | tar -x -C "$B/bj"
python3 -I - "$B/bj/bbc.c" <<'E'
import sys
p = sys.argv[1]
s = open(p).read()
def rep(a, b):
    global s
    assert s.count(a) == 1, a
    s = s.replace(a, b)
# FRED and JIM, which beebjit leaves unconnected, go to the board.
rep("""https://stardot.org.uk/forums/viewtopic.php?f=4&t=17509
       */
      ret = 0xFF;""", """https://stardot.org.uk/forums/viewtopic.php?f=4&t=17509
       */
      ret = pi1mhz_bus_read(addr);""")
rep("""      assert(0);
    }
    break;
  }

  return ret;
}

static void
bbc_jit_encoding_handle_timing(""", """      assert(0);
    } else {
      pi1mhz_bus_write(addr, val);
    }
    break;
  }

  return ret;
}

static void
bbc_jit_encoding_handle_timing(""")
# nRST: the board hears a Break and the power coming on.
rep("""  state_6502_reset(p_bbc->p_state_6502);

  if (p_bbc->is_compat_old_1MHz_cycles) {""", """  state_6502_reset(p_bbc->p_state_6502);
  pi1mhz_bbc_reset();

  if (p_bbc->is_compat_old_1MHz_cycles) {""")
rep("""  timing_reset_total_timer_ticks(p_timing);
  bbc_power_on_memory_reset(p_bbc);""", """  timing_reset_total_timer_ticks(p_timing);
  bbc_power_on_memory_reset(p_bbc);
  pi1mhz_bbc_reset();""")
i = s.index("bbc_create(int mode,")
j = s.index("  return p_bbc;", i)
s = s[:j] + "  pi1mhz_attach(p_bbc);\n" + s[j:]
k = s.index("static inline int\nbbc_is_1MHz_address")
s = s[:k] + """uint8_t pi1mhz_bus_read(uint16_t addr);
void pi1mhz_bus_write(uint16_t addr, uint8_t val);
void pi1mhz_bbc_reset(void);
void pi1mhz_attach(struct bbc_struct* p_bbc);

""" + s[k:]
open(p, 'w').write(s)
E
# The firmware's own C on the Pi1MHz checkout's test board (src/tests/ramprobe), as run_tests.sh
# builds it for kc-beeb: the real helpers.c and ram_emulator.c, and the SD card's 6502code.bin.
cp -r "$SRC"/tests/ramprobe/stubs/* "$B/pi/"
for f in services_emulator.c services.h ramprobe.c ramprobe.h ramprobe_api.c ramprobe_api.h \
         helpers.c helpers.h ram_emulator.c ram_emulator.h; do cp "$SRC/$f" "$B/pi/"; done
cp "$SRC"/tests/ramprobe/board.h "$HERE"/beebjit_glue.c "$B/pi/"
export RAMPROBE_6502CODE=${RAMPROBE_6502CODE:-$SRC/../firmware/Pi1MHz/6502code.bin}
# beebjit's own files, as its build_headless_opt.sh lists them, then the board's.
BJ_FILES=$(sed -n '/-o beebjit/,/-lm/p' "$B/bj/build_headless_opt.sh" | tr -d '\\' | tr ' ' '\n' | grep -E '\.(c|S)$')
for f in services_emulator ramprobe ramprobe_api helpers ram_emulator; do
   gcc -std=gnu2x -O2 -g -D_GNU_SOURCE -w -I"$B/pi" -c "$B/pi/$f.c" -o "$B/pi/$f.o"
done
gcc -std=gnu2x -O2 -g -D_GNU_SOURCE -w $(grep -q ramprobe_api_buffer "$SRC/ramprobe_api.h" && echo -DHAVE_STREAM_BUFFER) -I"$B/pi" -I"$B/bj" -c "$B/pi/beebjit_glue.c" -o "$B/pi/beebjit_glue.o"
(cd "$B/bj" && gcc -O2 -g -DNDEBUG -DBEEBJIT_HEADLESS -fno-pie -no-pie -Wa,--noexecstack -w -o "$B/beebjit" \
    $BJ_FILES "$B"/pi/*.o -lm -lpthread) 2> "$B/build.log" || { cat "$B/build.log"; exit 1; }
cd "$B/bj"
run() { PI1MHZ_PORT=$1 exec "$B/beebjit" -headless -mode ${BEEBJIT_MODE:-interp} -max-frames 4000000000 -exit-on-max-frames; }
if [ -n "$1" ]; then run "$1"; fi
echo "usage: run_beebjit.sh PORT"; exit 1
