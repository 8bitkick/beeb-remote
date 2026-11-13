#!/usr/bin/env python3
"""beeb.py: drive a BBC Micro through the Pi1MHz probe, one short command at a time, each result
checked.  Built on bbc.py.  The host is --host or PI1MHZ_HOST (default Pi1MHz.local).

    beeb.py status                      the probe's state, as one line
    beeb.py cmd "*CAT" [--expect 2]     type one line at BASIC's prompt and print what it printed;
                                        exit 1 if the BBC answered with an error
    beeb.py screen                      the MODE 7 screen as text
    beeb.py basic prog.bas [--seconds 5]  NEW, type the program, RUN; print the screen
    beeb.py peek ADDR LEN               hex
    beeb.py poke ADDR HEX
    beeb.py stops                       the probe's stop counter (each stop can click the relay)
    beeb.py soft-install                put in the resident routine (one stop), for typing and
                                        reading the screen with none
    beeb.py soft-uninstall              take it out (no stop, unless it doesn't answer)

Every probe operation on BBC RAM is a stop: the BBC freezes for milliseconds and the cassette
relay may click.  Without the resident routine `cmd` takes two (one to type, one to read the
screen) plus one per extra wait, and never polls.  With it (soft-install, once after the probe's
install line), `cmd`, `basic` and `screen` take none: they type and read through shared memory,
which takes no stop (bbc.py shared_read/shared_write).
"""
import argparse
import os
import re
import sys
import time

from bbc import BBC, ProbeError

# what MOS, BASIC and the filing systems say when something went wrong
ERRORS = re.compile(r"\b(Mistake|Syntax error|No such variable|Bad (?:command|name|program|Sum|drive|"
                    r"address|string|key|MODE)|Not found|File not found|Cat full|Disc (?:fault|error|full)|"
                    r"Locked|Read only|Too big|No room|Escape|not formatted|Can't|Missing|Type mismatch|"
                    r"Out of range|Bad call)\b")


def command(bbc, line, expect=0.5, timeout=30.0):
    """Type line, wait about as long as it should take, read the screen, and return the lines it
    printed.  If the prompt isn't back, wait twice as long and read again.  Raises ProbeError
    with the message if the BBC printed an error.  With the resident routine in, no stops and any
    length; without it, 29 characters at most (one stop to type) and one stop per read."""
    if len(line) > 29 and not bbc.soft_installed():
        raise ProbeError(f"{line!r}: 29 characters at most, so it goes in with one stop (or soft-install)")
    bbc.type(line + "\r", wait=False)
    wait = 0.05 * (len(line) + 1) + expect
    deadline = time.monotonic() + timeout
    while True:
        time.sleep(wait)
        rows = [r.rstrip() for r in bbc.screen().splitlines() if r.strip()]
        n = (len(line) + 40) // 40                 # the rows the echo takes: 40 characters a row
        echo = (">" + line).replace(" ", "")
        ends = [i + n - 1 for i in range(len(rows) - n + 1) if "".join(rows[i:i + n]).replace(" ", "") == echo]
        mine = max(ends, default=None)
        if mine is not None and rows[-1] == ">" and len(rows) - 1 > mine:
            out = rows[mine + 1:-1]
            bad = [r for r in out if ERRORS.search(r)]
            if bad:
                raise ProbeError(f"{line}: {' / '.join(bad)}")
            return out
        if time.monotonic() > deadline:
            raise ProbeError(f"{line}: no prompt after {timeout:.0f} s; the screen ends {rows[-3:]}")
        wait = min(wait * 2, 4.0)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default=os.environ.get("PI1MHZ_HOST", "Pi1MHz.local"))
    sub = ap.add_subparsers(dest="what", required=True)
    sub.add_parser("status")
    c = sub.add_parser("cmd")
    c.add_argument("line")
    c.add_argument("--expect", type=float, default=0.5, help="about how long it takes to run, in seconds")
    c.add_argument("--timeout", type=float, default=30.0)
    sub.add_parser("screen")
    b = sub.add_parser("basic")
    b.add_argument("file")
    b.add_argument("--seconds", type=float, default=5.0)
    p = sub.add_parser("peek")
    p.add_argument("addr")
    p.add_argument("len")
    q = sub.add_parser("poke")
    q.add_argument("addr")
    q.add_argument("hex")
    sub.add_parser("stops")
    sub.add_parser("soft-install")
    sub.add_parser("soft-uninstall")
    o = ap.parse_args()
    bbc = BBC(o.host)
    try:
        if o.what == "status":
            s = bbc.status()
            print(f"{s['state']}, installed {s['installed']}, build {s['build']}, stops {s.get('stops')}, "
                  f"shared &{s['shared']['at']:X}+{s['shared']['size'] >> 20} MB" if s.get("shared") else "",
                  f"error {s['error']!r}" if s.get("error") else "",
                  "soft typing in" if s.get("shared") and bbc.soft_installed() else "")
        elif o.what == "cmd":
            print("\n".join(command(bbc, o.line, o.expect, o.timeout)))
        elif o.what == "screen":
            print(bbc.screen())
        elif o.what == "basic":
            src = sys.stdin.read() if o.file == "-" else open(o.file).read()
            print(bbc.run_basic(src, o.seconds)["screen"])
        elif o.what == "peek":
            print(bbc.peek(int(o.addr, 0), int(o.len, 0)).hex())
        elif o.what == "poke":
            bbc.poke(int(o.addr, 0), bytes.fromhex(o.hex))
        elif o.what == "stops":
            print(bbc.status().get("stops"))
        elif o.what == "soft-install":
            before = bbc.status().get("stops")
            bbc.soft_install()
            print(f"the resident routine is in at &0A00 ({bbc.status().get('stops') - before} stops)")
        elif o.what == "soft-uninstall":
            print("taken out" if bbc.soft_uninstall() else "it wasn't in")
    except ProbeError as e:
        print(f"beeb.py: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
