"""A BBC Micro driven over the network through the Pi1MHz RAM probe (Pi1MHz docs/dev/ramprobe.md).

The probe gives two ways in.  Stops: stop the machine, read and write its memory, call code, let
it go on, each stop by a rung of its ladder, the gentlest first (see BBC.max_rung).  And shared
memory: 4 MB of the Pi1MHz that a remote application and its own code on the BBC both use while
the BBC runs, with no stop (shared_write(), shared_read()).  Everything here is built from those,
with no ROM and nothing on the BBC but the probe's install (and, if asked for, soft_install()'s
routine at &0A00, which types and reads the screen through shared memory with no stops):

    bbc = BBC("Pi1MHz.local")
    bbc.type("PRINT 6*7\\r")
    print(bbc.screen())

Typing writes into the OS's keyboard buffer during a stop, which is safe wherever the stop lands
(the OS only takes from the buffer; this is the only thing putting in).  The layout is MOS 1.20's,
checked on a Model B: a Master's MOS keeps that buffer elsewhere, so typing refuses there.
"""
import hashlib
import http.client
import json
import os
import re
import time
import urllib.parse

from asm6502 import asm

MOS120_RESET = 0xD9CD          # the reset vector at &FFFC on a Model B's MOS 1.20
KEYBUF_START, KEYBUF_END = 0x02D8, 0x02E1   # buffer 0's pointers, offsets into page 3
KEYBUF_FIRST = 0xE0            # the buffer is &03E0-&03FF
# MOS 1.20 stores three characters differently in MODE 7's screen, for the teletext chip's
# glyphs: # as &5F, _ as &60 and ` as &23.  The screen, read back as what was typed:
MODE7_TYPED = {0x5F: "#", 0x60: "_", 0x23: "`"}
ESCAPE_FLAG = 0xFF             # the OS's escape flag; bit 7 set means Escape was pressed
INSTALL = "X%=23:CALL &FC88"     # the PR #51 probe

# ---- the resident routine: typing and the screen with no stops ---------------------------
# It lives in the cassette and RS423 input buffer, &0A00-&0AFF, idle unless a tape is read or
# RS423 input is on: either would write over it, so uninstall first.  (The printer buffer,
# &0880-&08BF, is 64 bytes: not enough for typing, the screen copy and saving the services port.)
# It is chained into the vsync event behind the probe's own EVNTV hook in FRED, through that
# hook's JMP operand (&FC90-&FC91, which the Pi lets the BBC write), so EVNTV itself stays the
# probe's: rung 0 still works, typing the install line again leaves it be, and any Break takes it
# out (the Pi rewrites the hook, and the probe's install points it at the OS's default again).
# Each vsync it saves the services port pointer, reads four bytes of its mailbox in shared memory,
# and puts the pointer back; only with work asked for does it do more.  The port holds the byte at
# its address ready to read, so a network write to that very byte reads stale once: the routine
# sets the address afresh each vsync before reading the mailbox, and before each character from
# the ring (which the host writes before the count), and the program's own pointer, put back by
# writing it, is fetched afresh too.  It doesn't use the &FD00 window.
SOFT_AT = 0x0A00
SOFT_HOOK = 0xFC90              # the probe's EVNTV hook's JMP operand, in FRED
SOFT_PROBE_EVNTV = 0xFC8F       # the hook itself, which EVNTV must point at
SOFT_SLICE = 128                # screen bytes copied in one vsync: 8 vsyncs for the 1 KB
# Its mailbox, from the end of shared memory (the last page is *CLAUDE's doorbell):
SOFT_CTL = 0x200                # control page: size - SOFT_CTL
SOFT_RING = 0x300               # the typing ring, 256 bytes
SOFT_SCR = 0x700                # the screen's copy, &7C00-&7FFF, 1 KB
# In the control page: the host writes UREQ (uninstall, when not 0), EREQ (Escapes asked), W
# (characters written, mod 256) and SREQ (screen copies asked); the BBC writes GONE (it has
# unhooked itself), R (characters taken), the OS's &350-&355 (the screen's start, its mode at
# &355) and then SDONE (the screen copies done).  MARK is the host's own note of an install.
C_UREQ, C_EREQ, C_W, C_SREQ, C_GONE, C_R, C_VARS, C_SDONE, C_MARK = 0, 1, 2, 3, 4, 5, 6, 12, 16
SOFT_MAGIC = b"SOFT"
SOFT_SOURCE = """
entry:  PHP                     ; EVNTV, behind the probe's hook: A the event
        CMP #4
        BEQ vsync
        PLP
chain:  JMP &0000               ; on to what the hook held (the install puts it here)
vsync:  PHA
        TXA
        PHA
        TYA
        PHA
        LDX #2                  ; the services port pointer, as the program left it
save:   LDA &FCA6,X
        PHA
        DEX
        BPL save
        LDA #CTLHI              ; the port at the mailbox
        STA &FCA8
        LDA #0
        JSR ctlp
        LDA &FCA9               ; UREQ
        BNE unhook
        LDA &FCA9               ; EREQ
        CMP edone
        BEQ noesc
        STA edone
        SEC                     ; Escape: bit 7 of the OS's flag, set as OSBYTE 125 does
        ROR &FF
noesc:  LDA &FCA9               ; W
        STA w
        CMP r
        BNE typing
        LDA &FCA9               ; SREQ
        STA sreq
        CMP sdone
        BNE screen
rest:   LDX #&FD                ; the pointer back (&FBA9+&FD is &FCA6), then the registers
back:   PLA
        STA &FBA9,X
        INX
        BNE back
        PLA
        TAY
        PLA
        TAX
        PLA
        PLP
        JMP chain
unhook: LDA chain+1             ; out of the chain: the hook to what it held before
        STA SOFTHOOK
        LDA chain+2
        STA SOFTHOOK+1
        LDA #C_GONE             ; and say so
        JSR ctlp
        STA &FCA9
        BNE rest
typing: LDA r                   ; the next character, from the ring
        LDX #RINGMID
        JSR setp
        LDA &FCA9
        LDX #0                  ; into keyboard buffer 0
        JSR insv
        BCS tdone               ; full: again next vsync
        INC r
        LDA r
        CMP w
        BNE typing
tdone:  LDA #C_R                ; R, for the host
        JSR ctlp
        LDA r
        STA &FCA9
        BCS rest                ; always: C is set on both ways here
screen: LDA slice               ; a slice of &7C00-&7FFF a vsync
        LSR A
        TAY
        LDA #0
        ROR A
        TAX
        TYA
        ADC #&7C
        STA src+2
        TYA
        ADC #SCRMID
        STA &FCA7
        STX &FCA6
        LDY #SLICE
src:    LDA &7C00,X
        STA &FCA9
        INX
        DEY
        BNE src
        INC slice
        LDA slice
        CMP #NSLICES
        BEQ sfin
        JMP rest
sfin:   STY slice
        LDA #C_VARS             ; the OS's &350-&355, then SDONE
        JSR ctlp
vars:   LDA &0350,Y
        STA &FCA9
        INY
        CPY #6
        BNE vars
        LDA sreq
        STA sdone
        STA &FCA9
        JMP rest
insv:   JMP (&022A)
ctlp:   LDX #CTLMID             ; the port at A in the control page
setp:   STA &FCA6               ; the port at A in page X
        STX &FCA7
        RTS
w:      BYTES 00
sreq:   BYTES 00
edone:  BYTES 00
r:      BYTES 00
sdone:  BYTES 00
slice:  BYTES 00
"""


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

    def __init__(self, host, timeout=10.0, max_rung=1, retry_after=None, user=None, password=None):
        self.base = f"http://{host}"
        self.host = host
        # The Pi1MHz's web interface can have a login (HTTP digest), which then guards the probe's
        # routes too.  None (the default) takes PI1MHZ_USER and PI1MHZ_PASSWORD from the
        # environment, so every application here can use it; with neither, no login is sent.
        self.user = user if user is not None else os.environ.get("PI1MHZ_USER")
        self.password = password if password is not None else os.environ.get("PI1MHZ_PASSWORD")
        self._digest = None             # the server's last challenge: realm, nonce, qop, opaque
        self._nc = 0
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
        # type(), screen(), escape() and mode() go through the resident routine, putting it in
        # (one stop) whenever it isn't and BASIC is at its prompt, rather than stopping the BBC for
        # every 30 characters.  soft_uninstall() turns this off, for programs that want its memory.
        self.auto_soft = True

    # ---- the probe's routes ----------------------------------------------------------
    def _authorization(self, method, uri):
        """The Authorization header for a request, from the server's last challenge (RFC 2617:
        MD5, qop=auth), or None before any."""
        d = self._digest
        if not d or self.user is None:
            return None
        md5 = lambda t: hashlib.md5(t.encode()).hexdigest()
        ha1 = md5(f"{self.user}:{d['realm']}:{self.password or ''}")
        ha2 = md5(f"{method}:{uri}")
        self._nc += 1
        nc, cnonce = f"{self._nc:08x}", os.urandom(8).hex()
        fields = {"username": self.user, "realm": d["realm"], "nonce": d["nonce"], "uri": uri}
        if "auth" in d.get("qop", "").split(","):
            fields.update(qop="auth", nc=nc, cnonce=cnonce,
                          response=md5(f"{ha1}:{d['nonce']}:{nc}:{cnonce}:auth:{ha2}"))
        else:
            fields["response"] = md5(f"{ha1}:{d['nonce']}:{ha2}")
        if "opaque" in d:
            fields["opaque"] = d["opaque"]
        return "Digest " + ", ".join(f'{k}={v}' if k in ("qop", "nc") else f'{k}="{v}"'
                                     for k, v in fields.items())

    def _request(self, path, post=False, body=None, **params):
        """One request, logging in first if the server asks and a user was given."""
        for _ in range(2):
            status, headers, data = self._send(path, post, body, **params)
            challenge = headers.get("WWW-Authenticate", "")
            if status != 401 or self.user is None or not challenge.lower().startswith("digest"):
                break
            stale = self._digest is not None and "stale=true" in challenge.lower()
            if self._digest is not None and not stale:
                break                   # turned down with a challenge it had answered: wrong login
            self._digest = dict(re.findall(r'(\w+)="?([^",]*)"?', challenge[6:]))
            self._nc = 0
        if status == 401:
            raise ProbeError(f"{path}: the Pi1MHz wants a login" + (": user and password wrong?" if self.user else
                                                                    ": give BBC(user=, password=)"))
        if status >= 400:
            try:
                why = json.loads(data).get("error") or f"HTTP {status}"
            except json.JSONDecodeError:
                why = f"HTTP {status}"
            raise ProbeError(f"{path}: {why}")
        return data

    def _send(self, path, post=False, body=None, **params):
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
                auth = self._authorization("POST" if post else "GET", url)
                if auth:
                    headers["Authorization"] = auth
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
        return r.status, r.headers, data

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

    def run(self, code, a=0, x=0, y=0):
        """Run up to 80 bytes of 6502 ending in RTS, in a stop (inside stopped()); its A, X, Y and P."""
        return self._act("/bbc/run", hex=bytes(code).hex(), a=a, x=x, y=y)["result"]

    # ---- the resident routine: typing and the screen with no stops ---------------------
    def _soft_place(self):
        """Shared memory's services offset and the mailbox's offsets in it: control, ring, screen."""
        sh = self.shared()
        at, size = sh["at"], sh["size"]
        ctl, ring, scr = size - SOFT_CTL, size - SOFT_RING, size - SOFT_SCR
        if (at + ctl) >> 16 != (at + scr) >> 16:
            raise ProbeError("the resident routine's mailbox would cross a 64 KB line in shared memory")
        return at, ctl, ring, scr

    def soft_code(self, at, ctl, ring, scr):
        """The resident routine's bytes, for shared memory at services offset `at`."""
        code, labels = asm(SOFT_SOURCE, SOFT_AT, {
            "CTLHI": (at + ctl) >> 16, "CTLMID": (at + ctl) >> 8 & 0xFF, "RINGMID": (at + ring) >> 8 & 0xFF,
            "SCRMID": (at + scr) >> 8 & 0xFF, "SOFTHOOK": SOFT_HOOK,
            "SLICE": SOFT_SLICE, "NSLICES": 0x400 // SOFT_SLICE,
            "C_GONE": C_GONE, "C_R": C_R, "C_VARS": C_VARS})
        assert len(code) <= 0x100 and SOFT_SLICE == 128, len(code)
        return code, labels

    def soft_installed(self):
        """True if the resident routine is in: its mark in the mailbox, from an install since the
        last reset, and it hasn't unhooked itself.  No stop."""
        s = self.status()
        if not s.get("shared"):
            return False
        _, ctl, _, _ = self._soft_place_from(s)
        c = self.shared_read(ctl, C_MARK + 8)
        return (c[C_MARK:C_MARK + 4] == SOFT_MAGIC and c[C_GONE] == 0
                and int.from_bytes(c[C_MARK + 4:C_MARK + 8], "little") == s["resets"])

    def soft(self):
        """True if typing and reading go through the resident routine: it's in, or auto_soft is on
        and it can be put in now (one stop; after the probe's install, on a Model B, with BASIC at
        its prompt).  Raises ProbeError if a program is running instead: nothing is typed into it."""
        if self.soft_installed():
            return True
        if not self.auto_soft:
            return False
        s = self.status()
        if not (s.get("shared") and s.get("installed")):
            return False
        self.soft_install(at_prompt=True)
        return True

    def _soft_place_from(self, s):
        at, size = s["shared"]["at"], s["shared"]["size"]
        return at, size - SOFT_CTL, size - SOFT_RING, size - SOFT_SCR

    def soft_install(self, at_prompt=False):
        """Put the resident routine in, with one stop.  From then on type(), screen() and escape()
        take none.  Model B only (MOS 1.20's buffers and screen variables).  at_prompt: only if
        BASIC is waiting at its prompt in MODE 7, checked in the same stop, so that a program
        running (a game, which may use &0A00 itself) is left alone."""
        if self.soft_installed():
            return
        at, ctl, ring, scr = self._soft_place()
        code, labels = self.soft_code(at, ctl, ring, scr)
        self.shared_write(ctl, bytes(C_MARK + 8))          # the mailbox idle, the mark gone
        chain = labels["chain"] + 1
        hook = asm(f"""
                LDA {SOFT_HOOK}         ; the hook's old target, for the routine to go on to
                STA {chain}
                LDA {SOFT_HOOK + 1}
                STA {chain + 1}
                LDA #{SOFT_AT & 0xFF}     ; then the hook to the routine
                STA {SOFT_HOOK}
                LDA #{SOFT_AT >> 8}
                STA {SOFT_HOOK + 1}
                LDA &02C3               ; the vsync event's flag (the probe's install turns it on)
                RTS""", 0xFDA0)[0]
        look = asm(f"""
                LDX {SOFT_HOOK}
                LDY {SOFT_HOOK + 1}
                RTS""", 0xFDA0)[0]
        with self.stopped():
            v = self.peek(0xFFFC, 2)
            if v[0] | v[1] << 8 != MOS120_RESET:
                raise ProbeError("the resident routine is for MOS 1.20 (a Model B)")
            e = self.peek(0x0220, 2)
            if e[0] | e[1] << 8 != SOFT_PROBE_EVNTV:
                raise ProbeError(f"EVNTV is &{e[1]:02X}{e[0]:02X}, not the probe's hook: type the install line again")
            if at_prompt:
                mode = self.peek(0x0355, 1)[0]
                v = self.peek(0x0350, 2)
                lines = [l for l in self._mode7_text(self.peek(0x7C00, 0x400), v[0] | v[1] << 8).splitlines()
                         if l.strip()] if mode == 7 else []
                if not lines or lines[-1].strip() != ">":
                    raise ProbeError(f"the resident routine is out and the BBC isn't at BASIC's prompt (MODE {mode}): "
                                     "a program is running, so nothing typed or read; soft_install() if you mean it")
            f = self.peek(0x021E, 2)
            if f[0] | f[1] << 8 == SOFT_AT:
                raise ProbeError("FSCV leads to &0A00: *CLAUDE's hook is there (star_claude.py); a Break takes it out")
            r = self.run(look)
            if r["x"] | r["y"] << 8 == SOFT_AT:        # in already, its mark lost: keep its chain
                code = bytearray(code)
                code[chain - SOFT_AT:chain - SOFT_AT + 2] = self.peek(chain, 2)
                self.poke(SOFT_AT, code)
                flag = self.peek(0x02C3, 1)[0]
            else:
                self.poke(SOFT_AT, code)
                flag = self.run(hook)["a"]
        s = self.status()
        self.shared_write(ctl + C_MARK, SOFT_MAGIC + s["resets"].to_bytes(4, "little"))
        self._model_b = True
        if not flag:
            raise ProbeError("the vsync event is off (*FX13,4?): the routine won't run until *FX14,4")

    def soft_uninstall(self, timeout=2.0):
        """Take the resident routine out: by asking it (no stop), or, if it doesn't answer, with one
        stop.  True if it was in.  type() and the rest then stop the BBC instead of putting it back."""
        self.auto_soft = False
        s = self.status()
        if not s.get("shared"):
            return False
        _, ctl, _, _ = self._soft_place_from(s)
        c = self.shared_read(ctl, C_MARK + 8)
        if c[C_MARK:C_MARK + 4] != SOFT_MAGIC:
            return False
        if int.from_bytes(c[C_MARK + 4:C_MARK + 8], "little") == s["resets"] and c[C_GONE] == 0:
            self.shared_write(ctl + C_UREQ, b"\1")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and self.shared_read(ctl + C_GONE, 1)[0] == 0:
                time.sleep(0.02)
            if self.shared_read(ctl + C_GONE, 1)[0] == 0:
                self.soft_unhook()
        self.shared_write(ctl, bytes(C_MARK + 8))
        return True

    def soft_unhook(self):
        """The fallback, with one stop: point the probe's EVNTV hook back where the routine went on
        to, if it leads to the routine.  True if it did."""
        chain = self.soft_code(*self._soft_place())[1]["chain"] + 1
        code = asm(f"""
                LDA {SOFT_HOOK}
                CMP #{SOFT_AT & 0xFF}
                BNE no
                LDA {SOFT_HOOK + 1}
                CMP #{SOFT_AT >> 8}
                BNE no
                LDA {chain}
                STA {SOFT_HOOK}
                LDA {chain + 1}
                STA {SOFT_HOOK + 1}
                LDA #1
                RTS
        no:     LDA #0
                RTS""", 0xFDA0)[0]
        with self.stopped():
            return bool(self.run(code)["a"])

    def _soft_wait(self, ctl, at, want, timeout, what):
        deadline = time.monotonic() + timeout
        while True:
            got = self.shared_read(ctl + at, 1)[0]
            if got == want:
                return
            if time.monotonic() > deadline:
                raise ProbeError(f"the resident routine isn't answering ({what}): a program took the vsync "
                                 "event or EVNTV?  soft_uninstall(), then soft_install() again")
            time.sleep(0.02)

    def soft_type(self, text, timeout=60.0, wait=True):
        """Type text through the resident routine, with no stop: into the ring in shared memory,
        from which it puts characters into the keyboard buffer each vsync, while there's
        room.  Any length.  wait=True returns once it has put them all in."""
        _, ctl, ring, _ = self._soft_place_from(self.status())
        data = text.encode("latin-1")
        c = self.shared_read(ctl, C_R + 1)
        w, r = c[C_W], c[C_R]
        deadline = time.monotonic() + timeout
        while data:
            room = 255 - ((w - r) & 0xFF)
            if room:
                part, data = data[:room], data[room:]
                first = part[:256 - w]                 # up to the ring's end, then from its start
                self.shared_write(ring + w, first)
                self.shared_write(ring, part[len(first):])
                w = (w + len(part)) & 0xFF
                self.shared_write(ctl + C_W, bytes([w]))
                deadline = time.monotonic() + timeout
            if data:
                time.sleep(0.05)
                new = self.shared_read(ctl + C_R, 1)[0]
                if new != r:
                    r, deadline = new, time.monotonic() + timeout
                elif time.monotonic() > deadline:
                    raise ProbeError("the BBC isn't taking keys (a program not reading the keyboard?)")
        if wait:
            self._soft_wait(ctl, C_R, w, timeout, "typing")

    def soft_screen(self, timeout=3.0):
        """The resident routine's copy of &7C00-&7FFF and the OS's screen variables: (1 KB, the
        screen's start, the mode).  No stop."""
        _, ctl, _, scr = self._soft_place_from(self.status())
        want = (self.shared_read(ctl + C_SREQ, 1)[0] + 1) & 0xFF
        self.shared_write(ctl + C_SREQ, bytes([want]))
        self._soft_wait(ctl, C_SDONE, want, timeout, "the screen")
        v = self.shared_read(ctl + C_VARS, 6)
        return self.shared_read(scr, 0x400), v[0] | v[1] << 8, v[5]

    def soft_escape(self):
        """Press Escape through the resident routine, with no stop."""
        _, ctl, _, _ = self._soft_place_from(self.status())
        e = self.shared_read(ctl + C_EREQ, 1)[0]
        self.shared_write(ctl + C_EREQ, bytes([(e + 1) & 0xFF]))

    # ---- typing ----------------------------------------------------------------------
    def type(self, text, timeout=60.0, wait=True):
        """Type text at the keyboard (\\r for Return), 30 characters at a time, waiting for the OS to
        take each lot.  Only on MOS 1.20 (a Model B).  wait=False puts the last lot in and leaves
        at once: for a command that takes the machine over (interrupts off), which the probe
        can't stop to look at the buffer afterwards.  With the resident routine in
        (soft_install()), through it, with no stop."""
        if self.soft():
            return self.soft_type(text, timeout, wait)
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
        if self.soft():
            return self.soft_escape()
        with self.stopped():
            flag = self.peek(ESCAPE_FLAG, 1)[0]
            self.poke(ESCAPE_FLAG, bytes([flag | 0x80]))

    # ---- the screen ------------------------------------------------------------------
    def mode(self):
        if self.soft():
            return self.soft_screen()[2]
        with self.stopped():
            return self.peek(0x0355, 1)[0]

    def screen(self):
        """The screen as text: MODE 7's 25 lines of 40.  Other modes are pictures, not text, so
        this says which mode instead.  With the resident routine in, from its copy, with no stop."""
        if self.soft():
            ram, top, mode = self.soft_screen()
        else:
            with self.stopped():
                mode = self.peek(0x0355, 1)[0]
                if mode == 7:
                    vars_ = self.peek(0x0350, 2)
                    ram = self.peek(0x7C00, 0x400)
            if mode == 7:
                top = vars_[0] | vars_[1] << 8
        if mode != 7:
            return f"(MODE {mode}: a graphics mode, which can't be read as text)"
        return self._mode7_text(ram, top)

    @staticmethod
    def _mode7_text(ram, top):
        """MODE 7's screen memory (&7C00-&7FFF, the screen starting at top) as 25 lines of text."""
        rows = []
        for r in range(25):
            row = ""
            for c in range(40):
                v = ram[(top - 0x7C00 + r * 40 + c) & 0x3FF] & 0x7F
                row += MODE7_TYPED.get(v, chr(v)) if 32 <= v < 127 else " "
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
        if re.search(r"\*\s*(TAPE|FX\s*2\b)|OSBYTE\s*2\b", program, re.I) and self.soft_installed():
            return {"error": "the program may read a tape or RS423, whose buffer at &0A00 holds the resident "
                             "routine: soft_uninstall() first"}
        if not self.at_prompt():
            self.escape()
            time.sleep(0.3)
        self.type("\rNEW\r")
        self.type("".join(l + "\r" for l in lines))
        self.type("RUN\r")
        started = time.monotonic()
        ended = False
        while time.monotonic() - started < seconds:
            time.sleep(0.5)
            if self.at_prompt():
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
