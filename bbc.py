"""A BBC Micro driven over the network through the Pi1MHz RAM probe (Pi1MHz docs/dev/ramprobe.md).

The probe gives two ways in.  Stops: stop the machine, read and write its memory, call code, let
it go on, each stop by a rung of its ladder, the gentlest first (see BBC.max_rung).  And shared
memory: 8 MB of the Pi1MHz that a remote application and its own code on the BBC both use while
the BBC runs, with no stop (shared_write(), shared_read()).  Everything here is built from those,
with no ROM and nothing on the BBC but the probe's install:

    bbc = BBC("Pi1MHz.local")
    bbc.type("PRINT 6*7\\r")
    print(bbc.screen())

Typing writes into the OS's keyboard buffer during a stop, which is safe wherever the stop lands
(the OS only takes from the buffer; this is the only thing putting in).  The layout is MOS 1.20's,
checked on a Model B: a Master's MOS keeps that buffer elsewhere, so typing refuses there.
"""
import http.client
import json
import time
import urllib.parse

MOS120_RESET = 0xD9CD          # the reset vector at &FFFC on a Model B's MOS 1.20
KEYBUF_START, KEYBUF_END = 0x02D8, 0x02E1   # buffer 0's pointers, offsets into page 3
KEYBUF_FIRST = 0xE0            # the buffer is &03E0-&03FF
ESCAPE_FLAG = 0xFF             # the OS's escape flag; bit 7 set means Escape was pressed
INSTALL = "X%=23:CALL &FC88"     # the PR #51 probe


class ProbeError(Exception):
    """The probe refused, or the machine didn't answer."""


class BBC:
    """The probe's ladder (docs/dev/ramprobe.md in the Pi1MHz fork): 0 the vsync event, 1 the Pi's
    nIRQ, 2 the NMI.  A stop tries rung 1 first: it comes in at once (6 ms a stop and go on a
    Model B, against 18 ms for rung 0, which waits for the next frame), and IRQ2V is the probe's
    own hook unless a program has taken it (then rung 1 costs a 40 ms wait, and rung 0 is
    next).  vsync=True puts rung 0 first, for screen writes that should land just after the
    frame.  max_rung caps the ladder: 1 (the default) never sends an NMI, which is the last
    resort, for a BBC that won't answer otherwise (and on some BBCs it clicks the cassette
    relay); 2 allows it; 0 only the vsync event."""

    def __init__(self, host, timeout=10.0, max_rung=1, retry_after=None):
        self.base = f"http://{host}"
        self.host = host
        self._conn = None               # one connection, kept open between requests
        self._api = None                # the probe's api: 2 takes wait=1 and poke bodies
        # retry_after: a request with no answer in this many seconds is sent again on a fresh
        # connection, rather than waiting out TCP's resend of a lost WiFi packet (about a
        # second).  Only for requests that do no harm twice, such as streaming pokes.
        self.retry_after = retry_after
        self.timeout = timeout
        self.max_rung = max_rung
        self.rung = None                # the rung the last stop came in by
        self._resets = None
        self._model_b = None
        self._depth = 0                 # with-blocks of stopped() open

    # ---- the probe's routes ----------------------------------------------------------
    def _request(self, path, post=False, body=None, **params):
        """One request on the kept-open connection (HTTP/1.1 keep-alive: a new TCP connection
        for every request cost more than the request).  A connection the Pi has since closed
        (it drops idle ones after ~30 s) is opened again, once."""
        query = urllib.parse.urlencode(params)
        url = path + (f"?{query}" if query else "")
        tries = 6 if self.retry_after else 2
        for attempt in range(1, tries + 1):
            if self._conn is None:
                self._conn = http.client.HTTPConnection(self.host, timeout=self.retry_after or self.timeout)
            try:
                headers = {"Content-Type": "application/octet-stream"} if body else {}
                self._conn.request("POST" if post else "GET", url, body=body if body else (b"" if post else None),
                                   headers=headers)
                r = self._conn.getresponse()
                data = r.read().decode()
                if r.will_close:
                    self._conn.close()
                    self._conn = None
                break
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError, TimeoutError) as e:
                self._conn.close()
                self._conn = None
                if isinstance(e, TimeoutError):
                    self.retries = getattr(self, "retries", 0) + 1
                if attempt == tries:
                    raise ProbeError(f"{path}: the Pi1MHz at {self.base} dropped the connection: {e}") from None
            except (OSError, http.client.HTTPException) as e:
                self._conn.close()
                self._conn = None
                raise ProbeError(f"{path}: can't reach the Pi1MHz at {self.base}: {e}") from None
        if r.status >= 400:
            try:
                why = json.loads(data).get("error") or f"HTTP {r.status}"
            except json.JSONDecodeError:
                why = f"HTTP {r.status}"
            raise ProbeError(f"{path}: {why}")
        return data

    def status(self):
        s = json.loads(self._request("/bbc"))
        self._api = s.get("api", 1)
        return s

    def shared(self):
        """Shared memory, from GET /bbc: its services offset ("at", for the BBC's services port),
        its "size", its JIM address ("jim", for the &FD00 window's page registers), and "read":
        where the services port points in it, or -1 if elsewhere."""
        return self.status()["shared"]

    def shared_write(self, offset, data):
        """Write bytes into shared memory at offset, in one request, with no stop."""
        if data:
            self._request("/bbc/shared", post=True, body=bytes(data), offset=offset)

    def shared_read(self, offset, length):
        """Read bytes of shared memory from offset, with no stop."""
        out = bytearray()
        while len(out) < length:
            n = min(4096, length - len(out))
            out += bytes.fromhex(self._request("/bbc/shared", offset=offset + len(out), len=n).strip())
        return bytes(out)

    def _act(self, path, body=None, **params):
        """Start something, and wait until the Pi says it's over.  With api 2 the Pi answers when
        it is over (wait=1), so there's nothing to poll; otherwise, or if it answered at once
        (it holds one request at a time), poll GET /bbc."""
        if self._api is None:
            self.status()
        if self._api >= 2:
            s = json.loads(self._request(path, post=True, body=body, wait=1, **params))
            if not s.get("busy"):
                if s.get("error"):
                    raise ProbeError(f"{path}: {s['error']}")
                return s
        else:
            if body is not None:
                raise ProbeError(f"{path}: this firmware takes no request body")
            self._request(path, post=True, **params)
        deadline = time.monotonic() + self.timeout
        while True:
            s = self.status()
            if not s["busy"]:
                if s["error"]:
                    raise ProbeError(f"{path}: {s['error']}")
                return s
            if time.monotonic() > deadline:
                raise ProbeError(f"{path}: still {s['state']} after {self.timeout} s")
            time.sleep(0.005)

    def _order(self, vsync):
        rungs = [0, 1, 2] if vsync else [1, 0, 2]
        return [r for r in rungs if r <= self.max_rung]

    def _ladder(self, path, body=None, vsync=False, regs=False, **params):
        """A way in, a rung at a time, the next only when one doesn't answer (the Pi then leaves
        nothing asked of the BBC).  Firmware without the ladder ignores rung= and stops it by
        NMI, as it always did."""
        order = [r for r in self._order(vsync) if not (regs and r == 0)]
        if not order:
            raise ProbeError("registers need rung 1 or 2, and max_rung is 0")
        for rung in order:
            try:
                extra = {"nmi": 1} if rung == 2 else {}       # the firmware since the PR #51 review wants it
                s = self._act(path, body=body, rung=rung, **params, **extra)
                self.rung = s.get("rung", 2)
                return s
            except ProbeError as e:
                if f"rung {rung}" not in str(e) or rung == order[-1]:
                    raise

    def halt(self, vsync=False, regs=False):
        """Stop it, and return the program's registers, or None after a rung 0 stop: that one is
        inside the OS's vsync event, so the registers there are the OS's, and the firmware refuses
        to set any.  regs=True skips rung 0, for a caller that wants to read or change them."""
        return self._ladder("/bbc/halt", vsync=vsync, regs=regs)["stopped"]

    def go(self, **regs):
        """Let it go on, with any registers given (not after a rung 0 stop: halt(regs=True))."""
        self._act("/bbc/continue", **regs)

    def peek(self, addr, length, bank=None):
        params = {"addr": addr, "len": length}
        if bank is not None:
            params["bank"] = bank
        if self._depth:
            self._act("/bbc/peek", **params)
        else:                                          # in, read, out: one request
            self._ladder("/bbc/peek", **params)
        out = bytearray()
        while len(out) < length:
            text = self._request("/bbc/data", offset=len(out), len=4096).strip()
            if not text:
                raise ProbeError("peek: short read")
            out += bytes.fromhex(text)
        return bytes(out)

    def poke(self, addr, data, vsync=False):
        if addr < 0 or addr + len(data) > 0x8000:
            raise ProbeError(f"poke &{addr:04X}+{len(data)}: only main RAM, &0000-&7FFF")
        if self._api is None:
            self.status()
        if self._api >= 2:                             # the bytes as the body, 32 KB a request
            for at in range(0, len(data), 32768):
                part = bytes(data[at:at + 32768])
                if self._depth:
                    self._act("/bbc/poke", body=part, addr=addr + at)
                else:                                  # in, write, out: one request
                    self._ladder("/bbc/poke", body=part, vsync=vsync, addr=addr + at)
            return
        # 192 bytes a request: they travel in the URL as hex, and the Pi1MHz's web server turns
        # away a URL much over 500 characters (MEASURED: 200 bytes go, 256 don't).
        for at in range(0, len(data), 192):
            if self._depth:
                self._act("/bbc/poke", addr=addr + at, hex=data[at:at + 192].hex())
            else:                                      # in, write, out: one request
                self._ladder("/bbc/poke", addr=addr + at, hex=data[at:at + 192].hex())

    def stopped(self):
        """Stop the machine for a with-block, and let it go on after, whatever happens."""
        return _Stopped(self)

    # ---- the machine -----------------------------------------------------------------
    def ready(self):
        """True if the probe is installed.  Notices a Break: the install went with it, and the line
        set *KEY10 to bring it back by itself (see install_line())."""
        s = self.status()
        self._resets = s["resets"]
        return s["installed"]

    def is_model_b(self):
        with self.stopped():
            v = self.peek(0xFFFC, 2)
        return v[0] | v[1] << 8 == MOS120_RESET

    @staticmethod
    def install_line():
        """What to type once after switching on.  It sets *KEY10 too, so every soft Break after
        reinstalls it by itself; Ctrl-Break and power-on clear it."""
        return INSTALL

    # ---- typing ----------------------------------------------------------------------
    def type(self, text, timeout=60.0, wait=True):
        """Type text at the keyboard (\\r for Return), 30 characters at a time, waiting for the OS to
        take each lot.  Only on MOS 1.20 (a Model B).  wait=False puts the last lot in and leaves
        at once: for a command that takes the machine over (interrupts off), which the probe
        can't stop to look at the buffer afterwards."""
        if self._model_b is None:
            self._model_b = self.is_model_b()
        if not self._model_b:
            raise ProbeError("typing goes through MOS 1.20's keyboard buffer, and this isn't a Model B")
        data = text.encode("latin-1")
        deadline = time.monotonic() + timeout
        while data:
            with self.stopped():
                start, end = self.peek(KEYBUF_START, 1)[0], self.peek(KEYBUF_END, 1)[0]
                if start == end:                      # empty: room for 31, use 30
                    chunk, data = data[:30], data[30:]
                    self._put(end, chunk)
            if data or wait:
                self._wait_empty(deadline)

    def _put(self, end, chunk):
        """Store at the end pointer and then advance it, as the OS does; wraps &FF to &E0."""
        slots, y = [], end
        for _ in chunk:
            slots.append(y)
            y = KEYBUF_FIRST if y == 0xFF else y + 1
        runs, at = [], 0
        while at < len(slots):                        # contiguous runs: one poke each
            first = at
            while at + 1 < len(slots) and slots[at + 1] == slots[at] + 1:
                at += 1
            runs.append((slots[first], chunk[first:at + 1]))
            at += 1
        for slot, part in runs:
            self.poke(0x0300 + slot, part)
        self.poke(KEYBUF_END, bytes([y]))              # last: the OS sees all of it at once

    def _wait_empty(self, deadline):
        while True:
            with self.stopped():
                start, end = self.peek(KEYBUF_START, 1)[0], self.peek(KEYBUF_END, 1)[0]
            if start == end:
                return
            if time.monotonic() > deadline:
                raise ProbeError("the BBC isn't taking keys (a program not reading the keyboard?)")
            time.sleep(0.05)

    def escape(self):
        """Press Escape: set the OS's escape flag, which BASIC checks between statements."""
        with self.stopped():
            flag = self.peek(ESCAPE_FLAG, 1)[0]
            self.poke(ESCAPE_FLAG, bytes([flag | 0x80]))

    # ---- the screen ------------------------------------------------------------------
    def mode(self):
        with self.stopped():
            return self.peek(0x0355, 1)[0]

    def screen(self):
        """The screen as text: MODE 7's 25 lines of 40.  Other modes are pictures, not text, so
        this says which mode instead."""
        with self.stopped():
            mode = self.peek(0x0355, 1)[0]
            if mode != 7:
                return f"(MODE {mode}: a graphics mode, which can't be read as text)"
            vars_ = self.peek(0x0350, 2)
            ram = self.peek(0x7C00, 0x400)
        top = vars_[0] | vars_[1] << 8
        rows = []
        for r in range(25):
            row = ""
            for c in range(40):
                v = ram[(top - 0x7C00 + r * 40 + c) & 0x3FF] & 0x7F
                row += chr(v) if 32 <= v < 127 else " "
            rows.append(row.rstrip())
        return "\n".join(rows).rstrip("\n")

    def at_prompt(self):
        """BASIC is waiting at an empty > prompt (in MODE 7)."""
        lines = [l for l in self.screen().splitlines() if l.strip()]
        return bool(lines) and lines[-1].strip() == ">"

    # ---- BASIC -----------------------------------------------------------------------
    def run_basic(self, program, seconds=5.0):
        """Type a BBC BASIC program in (NEW, its lines, RUN), let it run for up to `seconds`, stop it
        with Escape if it's still going, and say what happened."""
        lines = [l.strip() for l in program.replace("\r", "\n").splitlines() if l.strip()]
        bad = [l for l in lines if not l.split(" ", 1)[0].isdigit()]
        if bad:
            return {"error": f"every line needs a line number: {bad[0]!r}"}
        if self.mode() == 7 and not self.at_prompt():
            self.escape()
            time.sleep(0.3)
        self.type("\rNEW\r")
        self.type("".join(l + "\r" for l in lines))
        self.type("RUN\r")
        started = time.monotonic()
        ended = False
        while time.monotonic() - started < seconds:
            time.sleep(0.5)
            if self.mode() == 7 and self.at_prompt():
                ended = True
                break
        ran = round(time.monotonic() - started, 1)
        if not ended:
            self.escape()
            time.sleep(0.5)
        text = self.screen()
        result = {"ended_by_itself": ended, "seconds": ran, "screen": text}
        lines = text.splitlines()
        runs = [i for i, l in enumerate(lines) if l.strip() == ">RUN"]
        after = lines[runs[-1] + 1:] if runs else lines          # only what this run printed
        for line in after:
            if " at line " in line or line.strip().startswith(("Mistake", "Syntax error", "No such")):
                if not (line.strip().startswith("Escape") and not ended):   # our own Escape isn't the program's error
                    result["error"] = line.strip()
        return result


class _Stopped:
    """Stopped for a with-block.  Nested blocks share the outer one's stop; the client counts them
    itself rather than asking the Pi, so a stop left over from an earlier failure is taken over
    and ended, not trusted to someone else."""
    def __init__(self, bbc):
        self.bbc = bbc

    def __enter__(self):
        if self.bbc._depth == 0 and self.bbc.status()["state"] != "halted":
            self.bbc.halt()
        self.bbc._depth += 1
        return self.bbc

    def __exit__(self, *exc):
        self.bbc._depth -= 1
        if self.bbc._depth == 0:
            self.bbc.go()
        return False
