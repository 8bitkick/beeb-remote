#!/usr/bin/env python3
"""Remote debugging, a few lines at a time: stop the BBC mid-program, read its registers and a
BASIC variable, change the variable, write onto the screen, and let it carry on.

    python3 debug_example.py --host Pi1MHz.local

Needs the probe installed on the BBC (X%=24:CALL &FC88), in BASIC at the > prompt.
"""
import argparse
import time

from bbc import BBC

ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("--host", default="Pi1MHz.local")
ap.add_argument("--token", help="the probe's probe_token, if set")
o = ap.parse_args()

bbc = BBC(o.host, token=o.token, max_rung=1)

# A program to look into: it counts in I% forever.
bbc.type('NEW\r10 MODE 7\r20 REPEAT:I%=I%+1:PRINT TAB(4,4)"Counting: ";I%:UNTIL FALSE\rI%=0:RUN\r')
time.sleep(3)

banner = (bytes([132, 157, 135]) + b"  I% changed from the network  " + bytes([156])).ljust(40, b" ")
with bbc.stopped():                                  # the BBC waits while this block runs
    regs = bbc.status()["stopped"]                   # where it was stopped
    i = int.from_bytes(bbc.peek(0x0424, 4), "little")    # BASIC keeps I% at &0424
    bbc.poke(0x0424, (999990).to_bytes(4, "little"))
    bbc.poke(0x7C00 + 40 * 9, banner)                # straight into MODE 7 screen memory

print("stopped at", {k: hex(v) for k, v in regs.items()})
print("I% was", i, "- now set to 999990")
time.sleep(1)
print("screen row 4:", bbc.screen().splitlines()[4].strip())
bbc.escape()
