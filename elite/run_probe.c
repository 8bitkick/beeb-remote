// The C player on a real BBC Micro Model B, through the Pi1MHz RAM probe and its shared memory
// (Pi1MHz PR #51, docs/dev/ramprobe.md there; elite/README.md here).  No ROM, no keyboard rig.
//   run_probe HOST[:PORT] [minutes] [journal.jsonl]
// BOOT=1 loads Elite from the disc first, from BASIC's prompt with the probe installed
// (X%=23:CALL &FC88).  Model B disc Elite (flicker-free) only.
//
// The game isn't stopped while it plays.  A routine patched into each code, where its joystick
// code was, copies the RAM the player reads into the Pi1MHz's shared memory through the services
// port each time Elite asks for a key press (RDKEY: once a main-loop iteration, and in every
// screen that waits for a key), and this program reads it with GET /bbc/shared.  Keys go the
// other way: Elite's key routine, DKS4, is patched to look its key up in a list in the &FD00
// window, which shows a page of shared memory, and this program writes the list with
// POST /bbc/shared.
// The probe stops the BBC only to patch a code just loaded, and to type the commander's name
// into the OS's keyboard buffer.
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>
#include "player.h"
#include "keyq.h"
#include "elite_ram.h"
#include "keys.h"

// ---- HTTP: one request at a time, the connection kept while the server keeps it

static char s_host[128];
static int s_port = 80, s_sock = -1;

static void die(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "run_probe: ");
    vfprintf(stderr, fmt, ap);
    fprintf(stderr, "\n");
    va_end(ap);
    exit(1);
}

static int connect_host(void) {
    struct addrinfo hints = {0}, *res;
    char port[8];
    snprintf(port, sizeof port, "%d", s_port);
    hints.ai_family = AF_INET, hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(s_host, port, &hints, &res)) return -1;
    int fd = socket(res->ai_family, res->ai_socktype, 0);
    if (fd >= 0 && connect(fd, res->ai_addr, res->ai_addrlen)) close(fd), fd = -1;
    freeaddrinfo(res);
    if (fd >= 0) {
        int one = 1;
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
        struct timeval tv = {15, 0};
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    }
    return fd;
}

static int send_all(int fd, const char *p, size_t n) {
    while (n) {
        ssize_t k = send(fd, p, n, MSG_NOSIGNAL);
        if (k <= 0) return -1;
        p += k, n -= (size_t) k;
    }
    return 0;
}

// method, path (with its query) and a body (or none) -> the HTTP status, the answer in out (NUL-terminated)
static int http_body(const char *method, const char *path, const uint8_t *body, size_t blen, char *out, size_t outsz) {
    static char buf[16384];
    for (int attempt = 0; attempt < 20; attempt++) {      // (WiFi drops a request now and then: retry for ~10 s)
        if (attempt) usleep(500000);
        if (s_sock < 0 && (s_sock = connect_host()) < 0) continue;
        char req[4096];
        int n = snprintf(req, sizeof req, "%s %s HTTP/1.1\r\nHost: %s\r\nContent-Length: %zu\r\n\r\n", method, path, s_host, blen);
        if (n >= (int) sizeof req) die("request too long: %s", path);
        if (send_all(s_sock, req, (size_t) n) || (blen && send_all(s_sock, (const char *) body, blen))) { close(s_sock), s_sock = -1; continue; }
        size_t got = 0;
        char *ans = NULL;
        while (!ans) {
            ssize_t k = recv(s_sock, buf + got, sizeof buf - 1 - got, 0);
            if (k <= 0) break;
            got += (size_t) k, buf[got] = 0;
            char *e = strstr(buf, "\r\n\r\n");
            if (e) ans = e + 4;
        }
        if (!ans) { close(s_sock), s_sock = -1; continue; }
        int status = atoi(buf + 9);
        char *cl = strcasestr(buf, "Content-Length:");
        size_t want = cl && cl < ans ? (size_t) atol(cl + 15) : 0;
        int keep = !(strcasestr(buf, "Connection: close") && strcasestr(buf, "Connection: close") < ans);
        size_t head = (size_t) (ans - buf);
        while (got - head < want) {
            if (got >= sizeof buf - 1) die("answer too long for %s", path);
            ssize_t k = recv(s_sock, buf + got, sizeof buf - 1 - got, 0);
            if (k <= 0) break;
            got += (size_t) k, buf[got] = 0;
        }
        size_t len = got - head < want ? got - head : want;
        if (len >= outsz) len = outsz - 1;
        memcpy(out, ans, len), out[len] = 0;
        if (!keep) close(s_sock), s_sock = -1;
        return status;
    }
    die("no answer from %s:%d", s_host, s_port);
    return 0;
}
static int http(const char *method, const char *path, char *out, size_t outsz) { return http_body(method, path, NULL, 0, out, outsz); }

static double now_s(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (double) t.tv_sec + t.tv_nsec / 1e9;
}

// a string field of the probe's status JSON ("" if absent), and a number field (-1 if absent or null)
static void json_str(const char *j, const char *key, char *out, size_t n) {
    char pat[40];
    snprintf(pat, sizeof pat, "\"%s\":\"", key);
    const char *p = strstr(j, pat);
    size_t i = 0;
    if (p) for (p += strlen(pat); *p && *p != '"' && i + 1 < n; p++) out[i++] = *p;
    out[i] = 0;
}
static long json_num(const char *j, const char *key) {
    char pat[40];
    snprintf(pat, sizeof pat, "\"%s\":", key);
    const char *p = strstr(j, pat);
    if (!p || !isdigit((unsigned char) p[strlen(pat)])) return -1;
    return atol(p + strlen(pat));
}

// ---- the probe (Pi1MHz docs/dev/ramprobe.md: HTTP routes)

static double s_stopped;        // seconds the BBC has spent stopped for us, which is no game time
static uint32_t s_stops;        // how many times it was stopped
static double s_stop_longest;
static char s_st[2048];

typedef struct { uint16_t at, n; } range_t;

static void ranges_param(char *q, size_t qn, const range_t *r, int nr) {
    size_t at = 0;
    for (int i = 0; i < nr; i++) at += (size_t) snprintf(q + at, qn - at, "%s0x%X:%u", i ? "," : "", r[i].at, r[i].n);
}

// a POST to the probe; 0, or -1 with its error in err
static int act(const char *path_query, char *err, size_t errn) {
    int st = http("POST", path_query, s_st, sizeof s_st);
    json_str(s_st, "error", err, errn);
    if (st != 200 && !*err) snprintf(err, errn, "HTTP %d", st);
    return st == 200 && !*err ? 0 : -1;
}

// a stop: halt by rung 1, the Pi's nIRQ (Elite's own IRQ handler passes it on to the OS), the
// stopped PC in *pc; resume() lets it go on.  The time between is all stopped.
static double s_halted_at;
static int halt(long *pc, char *err, size_t errn) {
    s_halted_at = now_s();
    if (act("/bbc/halt?rung=1&wait=1", err, errn)) { s_stopped += now_s() - s_halted_at; return -1; }
    s_stops++;
    *pc = json_num(s_st, "pc");
    return 0;
}
static void resume(void) {
    char err[200];
    if (act("/bbc/continue?wait=1", err, sizeof err)) fprintf(stderr, "continue: %s\n", err);
    double t = now_s() - s_halted_at;
    s_stopped += t;
    if (t > s_stop_longest) s_stop_longest = t;
}

// bytes from the probe's transfer buffer, from offset (GET /bbc/data, which serves the last peek's
// bytes, up to its length): 0, or -1
static int data(int offset, int total, uint8_t *out) {
    static char hex[8200];
    char path[100];
    for (int off = 0; off < total; off += 4096) {
        int n = total - off < 4096 ? total - off : 4096;
        snprintf(path, sizeof path, "/bbc/data?offset=%d&len=%d", offset + off, n);
        if (http("GET", path, hex, sizeof hex) != 200 || (int) strlen(hex) < 2 * n) return -1;
        for (int i = 0; i < n; i++) {
            unsigned v;
            sscanf(hex + 2 * i, "%2x", &v);
            out[off + i] = (uint8_t) v;
        }
    }
    return 0;
}

// bytes of shared memory, from offset (GET /bbc/shared, no stop): 0, or -1
static int shared_get(int offset, int total, uint8_t *out) {
    static char hex[8200];
    char path[100];
    for (int off = 0; off < total; off += 4096) {
        int n = total - off < 4096 ? total - off : 4096;
        snprintf(path, sizeof path, "/bbc/shared?offset=%d&len=%d", offset + off, n);
        if (http("GET", path, hex, sizeof hex) != 200 || (int) strlen(hex) < 2 * n) return -1;
        for (int i = 0; i < n; i++) {
            unsigned v;
            sscanf(hex + 2 * i, "%2x", &v);
            out[off + i] = (uint8_t) v;
        }
    }
    return 0;
}

// bytes into shared memory at offset (POST /bbc/shared, the bytes as the body, no stop): 0, or -1
static int shared_put(int offset, const uint8_t *bytes, int n) {
    char path[80], ans[512];
    snprintf(path, sizeof path, "/bbc/shared?offset=%d", offset);
    if (http_body("POST", path, bytes, (size_t) n, ans, sizeof ans) == 200) return 0;
    fprintf(stderr, "POST /bbc/shared: %s\n", ans);
    return -1;
}

// the ranges' bytes, back to back into out.  halted: the machine already stopped; otherwise one
// stop for the lot.  0, or -1 with err
static int peek_how(const range_t *r, int nr, uint8_t *out, char *err, size_t errn, int halted) {
    char q[1024], path[1200];
    ranges_param(q, sizeof q, r, nr);
    snprintf(path, sizeof path, "/bbc/peek?ranges=%s%s&wait=1", q, halted ? "" : "&rung=1");
    int total = 0;
    for (int i = 0; i < nr; i++) total += r[i].n;
    double t0 = now_s();
    int bad = act(path, err, errn);
    if (!halted) s_stopped += now_s() - t0, s_stops += !bad;
    if (bad) return -1;
    if (data(0, total, out)) { snprintf(err, errn, "short data"); return -1; }
    return 0;
}
static int peek(const range_t *r, int nr, uint8_t *out, char *err, size_t errn) { return peek_how(r, nr, out, err, errn, 0); }
static int peek_halted(const range_t *r, int nr, uint8_t *out, char *err, size_t errn) { return peek_how(r, nr, out, err, errn, 1); }

static int poke_how(const range_t *r, int nr, const uint8_t *bytes, char *err, size_t errn, int halted) {
    char q[1024];
    static char path[4096];
    ranges_param(q, sizeof q, r, nr);
    int total = 0;
    for (int i = 0; i < nr; i++) total += r[i].n;
    if (total > 512) die("poke of %d bytes", total);
    int at = snprintf(path, sizeof path, "/bbc/poke?ranges=%s%s&wait=1&hex=", q, halted ? "" : "&rung=1");
    for (int i = 0; i < total; i++) at += snprintf(path + at, sizeof path - (size_t) at, "%02X", bytes[i]);
    double t0 = now_s();
    int bad = act(path, err, errn);
    if (!halted) s_stopped += now_s() - t0, s_stops += !bad;
    return bad;
}
static int poke(const range_t *r, int nr, const uint8_t *bytes, char *err, size_t errn) { return poke_how(r, nr, bytes, err, errn, 0); }

static void must(int ok, const char *what, const char *err) { if (ok) die("%s: %s", what, err); }

// ---- shared memory, as this program lays it out (GET /bbc's shared: its services offset, which
// the BBC's services port takes, and its JIM address, which the window's page registers take)
//
// The input page, at +&0000: the window.  DKS4 reads the held keys from it through &FD00, and
// RDKEY its answer; this program writes it with POST /bbc/shared.  The Pi copies the page into
// the window when a JIM page register is written, so both write &FCFF first.
#define KEYPAGE_OFF 0x000
#define WIN_LIST 0x00                // &FD00-&FD05: the keys held (&FF for none)
#define WIN_RDKEY 0x08               // &FD08: what RDKEY returns: the lowest key held from 16 up, or 0
#define WIN_MAGIC 0x10               // &FD10: copied into the docked code's mirror
#define NLIST 6                      // keys held at once: the player holds six at most
//
// The state mirror, at +&0100: what the copy routine writes through the services port, and this
// program reads with GET /bbc/shared.  Its bytes, in order:
//   a sequence number, one more each time;
//   for X = 0 to &BB: zero page &00+X, page 3 &0311+X (FRIN to JSTK), and in the flight code K%
//     &0900+X (the first five ship blocks);
//   flight: ALTIT (&0FD1); K% &09BC-&0ABB (the next seven blocks) if FRIN+4 isn't empty;
//   docked: NA% (&1181, 84 bytes: the last save); ALTIT; the byte at &FD10 (the window's: the
//     check that it shows our page);
//   the sequence number again.  The two match only in a whole copy.
// What the player reads, from its own read audit (elite-agent's cplayer/audit.h, run_sim_b built
// with -DREAD_AUDIT): zero page to &8E, FRIN, page 3 from JUNK to JSTK (NAME among it), ALTIT, the
// ship blocks in use, and NA% after a save.
#define MIRROR_OFF 0x100
#define SPAN 0xBC
#define FLIGHT_SHORT (1 + 3 * SPAN + 1)                // where the second number is, with five blocks
#define FLIGHT_LONG (FLIGHT_SHORT + 256)               // with twelve
#define DOCKED_END (1 + 2 * SPAN + 0x54 + 2)
#define MIRROR_MAX (FLIGHT_LONG + 1)

_Static_assert(KEYPAGE_OFF < 0x100 && (MIRROR_OFF & 0xFF) == 0, "the copy routine's LDX #0 sets both low bytes");
static uint32_t s_shared_at, s_shared_jim;   // shared memory's services offset and JIM address

// ---- the patches, in each code (Moxon's flight and docked sources; disc-ff-compile.txt)

// the original DKS4: LDA #3: SEI: STA &FE40: LDA #&7F: STA &FE43: STX &FE4F: LDX &FE4F: LDA #&0B:
// STA &FE40: CLI: TXA: RTS.  At &4431 in the flight code (D.CODE), &3C6F in the docked (T.CODE).
static const uint8_t DKS4[25] = {0xA9, 0x03, 0x78, 0x8D, 0x40, 0xFE, 0xA9, 0x7F, 0x8D, 0x43, 0xFE, 0x8E, 0x4F,
                                 0xFE, 0xAE, 0x4F, 0xFE, 0xA9, 0x0B, 0x8D, 0x40, 0xFE, 0x58, 0x8A, 0x60};
static const uint16_t DKS4_AT[2] = {0x4431, 0x3C6F};

// The same 25 bytes, which return the key number in A and X with bit 7 set if it's in the list in
// the window.  The flight keys come with bit 7 set (KYTB), which the keyboard ignores, so it's
// masked off first.  Y is kept, as the original does; the last byte stays the RTS that DKS1
// branches to.
static void dks4_patch(uint8_t *p) {
    const uint8_t code[25] = {
        0x8A,                                        // TXA
        0x29, 0x7F,                                  // AND #&7F
        0xA2, 0x00, 0x8E, 0xFF, 0xFC,                // LDX #0: STX &FCFF (the window afresh: the input page's low page register)
        0xA2, NLIST - 1,                             // LDX #5
        0xDD, WIN_LIST, 0xFD,                        // .l CMP &FD00,X
        0xF0, 0x04,                                  // BEQ down
        0xCA,                                        // DEX
        0x10, 0xF8,                                  // BPL l
        0x2C,                                        // BIT &8009 (steps over the ORA)
        0x09, 0x80,                                  // .down ORA #&80
        0xAA, 0x60,                                  // TAX: RTS
        0x60, 0x60,                                  // (spare), and the RTS DKS1 branches to
    };
    memcpy(p, code, 25);
}

// Where the copy routine goes: RDKEY (its entry, rewritten), then code that runs only with a
// joystick (JSTK non-zero): DKJ1, TT17's joystick cursor (TJS1 and before it), DKS2 (the ADC
// read), and in the docked code DOKEY's own joystick reads.  Each with its original bytes,
// checked before anything is written.  Nothing enters any of them but at RDKEY, and through
// Elite's tests of JSTK, which are patched too (JFIX, below).
typedef struct { uint16_t at; uint8_t n; const char *was; } region_t;
#define NREG 5
static const region_t REGION[2][NREG] = {
    {   // flight (D.CODE)
        {0x4337, 15, "A2102031443004E810F88A4980AA60"},
        {0x446B, 49, "AD3F03D04FA001202144C8202144AD40FEAA291049108D0703A201204A4409018D4C03A202204A444DC9038D4D034C4D45"},
        {0x3D1F, 37, "AD4C0349FF202C3D98AAAD4D03A8A900C010E900C040E900C0C06900C0E06900A8AD000360"},
        {0x444A, 10, "A98020F4FF984DCA0360"},
        {0, 0, NULL},
    },
    {   // docked (T.CODE)
        {0x3BFC, 15, "A210206F3C3004E810F88A4980AA60"},
        {0x3CA9, 35, "AD40FEAA291049108D0703A20120883C09018D4C03A20220883C4DC9038D4D034CE63C"},
        {0x35D3, 37, "AD4C0349FF20E03598AAAD4D03A8A900C010E900C040E900C0C06900C0E06900A8AD000360"},
        {0x3CD1, 21, "A20120883C09018D4C03A20220883C4DC9038D4D03"},
        {0x3C88, 10, "A98020F4FF984DCA0360"},
    },
};
static uint8_t s_was[2][NREG][64];

// And Elite's two tests of JSTK in each code made constant, so no joystick path can run into the
// routine: in the pause, B (the Bitstik) sets JSTK as well as BSTK, and the player's reload
// presses B.  TT17's LDA JSTK: BEQ TJ1 becomes JMP TJ1; DOKEY's LDA JSTK, LDA #0 (A is 0 after it
// either way, for STA BSTK or DK9's).
typedef struct { uint16_t at; uint8_t was[5], now[5]; } fix_t;
#define NFIX 2
static const fix_t JFIX[2][NFIX] = {
    {{0x3D1A, {0xAD, 0xCB, 0x03, 0xF0, 0x25}, {0x4C, 0x44, 0x3D, 0x60, 0x60}},     // TT17: JMP TJ1
     {0x44AA, {0xAD, 0xCB, 0x03, 0xD0, 0xBC}, {0xA9, 0x00, 0xEA, 0xEA, 0xEA}}},    // DOKEY: LDA #0 (and no BNE DKJ1)
    {{0x35CE, {0xAD, 0xCB, 0x03, 0xF0, 0x25}, {0x4C, 0xF8, 0x35, 0x60, 0x60}},     // TT17: JMP TJ1
     {0x3CCC, {0xAD, 0xCB, 0x03, 0xF0, 0x65}, {0xA9, 0x00, 0xEA, 0xF0, 0x65}}},    // DOKEY: LDA #0: NOP: BEQ DK9
};

// The copy routine for a code, laid out over its regions in s_code, jumping from one to the
// next; RDKEY is its entry, and it returns RDKEY's answer.  It copies at most once a frame: only
// if DL (&8B) is set, which Elite's IRQ sets at each vsync and only WSCAN otherwise touches (it
// clears it and waits for the next), and it clears it.  So a screen that calls RDKEY over and over
// (waiting for a key to be let go, say) copies once a frame, not back to back, and the mirror is
// whole most of the time.  It uses A and X; Y is kept, as RDKEY keeps it.  It needs the window's
// JIM page's top two registers set already (BOOT types them at BASIC's prompt; nothing in Elite
// or the DFS changes them, and a probe stop puts the page back as it found it); it writes the
// third, which copies the page into the window afresh.  The mirror's address goes into the
// services port byte by byte, from the top.  Shared memory starts on a 64 KB boundary (checked at
// the start), so the mirror's low byte and the input page's low page register are both 0: one
// LDX #0 for them and DL, which keeps the routine as short as the regions need.  Each unit stays in one region, a loop's
// branch with it.
typedef struct { uint8_t b[24]; uint8_t n; int8_t fix[2]; uint8_t to[2]; } unit_t;
enum { TO_SEQ, TO_TAIL };
static uint8_t s_code[2][NREG][64];
static void build_copy(int docked) {
    unit_t u[16];
    int nu = 0;
#define U(...) (u[nu] = (unit_t) {{__VA_ARGS__}, (uint8_t) sizeof((uint8_t[]) {__VA_ARGS__}), {-1, -1}, {0, 0}}, nu++)
#define FIX(k, off, what) (u[nu - 1].fix[k] = (off), u[nu - 1].to[k] = (what))
    U(0xA5, 0x8B, 0xD0, 0x03, 0x4C, 0, 0);                      // LDA DL: BNE go: JMP tail: .go
    FIX(0, 5, TO_TAIL);
    uint32_t mirror = s_shared_at + MIRROR_OFF;
    U(0xA9, (uint8_t) (mirror >> 16), 0x8D, 0xA8, 0xFC);        // LDA #hi: STA &FCA8   the services port at the mirror
    U(0xA9, (uint8_t) (mirror >> 8), 0x8D, 0xA7, 0xFC);         // LDA #mid: STA &FCA7
    U(0xA2, 0x00, 0x8E, 0xA6, 0xFC, 0x8E, 0xFF, 0xFC, 0x86, 0x8B);   // LDX #0: STX &FCA6: STX &FCFF   its low byte; the window afresh
                                                                // STX DL
    U(0xEE, 0, 0, 0xAD, 0, 0, 0x8D, 0xA9, 0xFC);                // INC seq: LDA seq: STA &FCA9
    FIX(0, 1, TO_SEQ), FIX(1, 4, TO_SEQ);
    if (!docked) {
        // .l LDA &00,X: STA: LDA &0311,X: STA: LDA &0900,X: STA: INX: CPX #&BC: BNE l
        U(0xB5, 0x00, 0x8D, 0xA9, 0xFC, 0xBD, 0x11, 0x03, 0x8D, 0xA9, 0xFC, 0xBD, 0x00, 0x09, 0x8D, 0xA9, 0xFC,
          0xE8, 0xE0, SPAN, 0xD0, 0xEA);
        U(0xAD, 0xD1, 0x0F, 0x8D, 0xA9, 0xFC);                  // LDA ALTIT: STA
        // LDA FRIN+4: BEQ s: LDX #0: .k LDA &09BC,X: STA: INX: BNE k: .s
        U(0xAD, 0x15, 0x03, 0xF0, 0x0B, 0xA2, 0x00, 0xBD, 0xBC, 0x09, 0x8D, 0xA9, 0xFC, 0xE8, 0xD0, 0xF7);
    } else {
        // .l LDA &00,X: STA: LDA &0311,X: STA: INX: CPX #&BC: BNE l
        U(0xB5, 0x00, 0x8D, 0xA9, 0xFC, 0xBD, 0x11, 0x03, 0x8D, 0xA9, 0xFC, 0xE8, 0xE0, SPAN, 0xD0, 0xF0);
        // LDX #0: .n LDA NA%,X: STA: INX: CPX #&54: BNE n
        U(0xA2, 0x00, 0xBD, 0x81, 0x11, 0x8D, 0xA9, 0xFC, 0xE8, 0xE0, 0x54, 0xD0, 0xF5);
        U(0xAD, 0xD1, 0x0F, 0x8D, 0xA9, 0xFC);                  // LDA ALTIT: STA
        U(0xAD, WIN_MAGIC, 0xFD, 0x8D, 0xA9, 0xFC);             // LDA &FD10: STA
    }
    U(0xAD, 0, 0, 0x8D, 0xA9, 0xFC);                            // LDA seq: STA
    FIX(0, 1, TO_SEQ);
    U(0xAE, WIN_RDKEY, 0xFD, 0x8A, 0x60, 0x00);                 // .tail LDX &FD08: TXA: RTS, and seq
#undef U
#undef FIX
    const region_t *R = REGION[docked];
    int r = 0, p = 0;
    uint16_t at[16];
    memset(s_code[docked], 0x60, sizeof s_code[docked]);         // (what's left over: RTS)
    for (int i = 0; i < nu; i++) {
        int need = u[i].n + (i == nu - 1 ? 0 : 3);               // (room for a JMP on, after all but the last)
        if (p + need > R[r].n) {
            if (r + 1 >= NREG || !R[r + 1].n || need > R[r + 1].n) die("the copy routine doesn't fit");
            uint8_t *j = s_code[docked][r] + p;
            j[0] = 0x4C, j[1] = (uint8_t) R[r + 1].at, j[2] = (uint8_t) (R[r + 1].at >> 8);
            r++, p = 0;
        }
        memcpy(s_code[docked][r] + p, u[i].b, u[i].n);
        at[i] = (uint16_t) (R[r].at + p);
        p += u[i].n;
    }
    uint16_t to[2] = {(uint16_t) (at[nu - 1] + 5), at[nu - 1]};
    for (int i = 0; i < nu; i++)
        for (int k = 0; k < 2; k++)
            if (u[i].fix[k] >= 0) {
                int a = at[i] + u[i].fix[k];
                for (int g = 0; g < NREG; g++)
                    if (R[g].n && a >= R[g].at && a < R[g].at + R[g].n) {
                        s_code[docked][g][a - R[g].at] = (uint8_t) to[u[i].to[k]];
                        s_code[docked][g][a - R[g].at + 1] = (uint8_t) (to[u[i].to[k]] >> 8);
                    }
            }
}

// ---- the OS's keyboard buffer (MOS 1.20): Elite reads the commander's name with OSWORD 0
#define KEYBUF_START 0x02D8
#define KEYBUF_END 0x02E1
#define KEYBUF_FIRST 0xE0          // the buffer is &03E0-&03FF

// the ASCII for a key the player types at a prompt: letters, digits, SPACE (keys.h's
// key_for_char, the other way) and RETURN; 0 for the rest
static int key_ascii(int code) {
    if (code == K_RETURN) return '\r';
    static const char *chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ";
    for (const char *c = chars; *c; c++) if (key_for_char(*c) == code) return *c;
    return 0;
}

// ---- the player's side

static player_io_t io;
static FILE *journal;
static uint8_t s_ram[0x8000];
static uint8_t s_down[16];             // the keys down now, a bit each
static char s_typed[64];               // characters for the OS's buffer, not yet put in
static int s_ntyped;
static double s_typed_at;              // when the first of them was typed
static int s_dirty, s_toomany;

static void p_hold(void *ctx, const uint8_t *c, int n) { (void) ctx; keyq_hold(io.frame, c, n); }
static void p_tap(void *ctx, uint8_t c, int down, int up) { (void) ctx; keyq_tap(io.frame, c, down, up); }
// (PROBEDIAG=1: when the player says a key did nothing, one stop to see what the BBC is doing)
static void diag(const char *line);
static void p_log(void *ctx, int level, const char *s) {
    (void) ctx;
    static const char *TAG[] = {"", "  ", "    "};
    static int debug = -1;
    if (debug < 0) debug = getenv("PROBEDEBUG") != NULL;
    static int dg = -1;
    if (dg < 0) dg = getenv("PROBEDIAG") != NULL;
    if (dg && strstr(s, ": no (key")) diag(s);
    if (level <= PLOG_INFO || debug) printf("%7.1f %s%s\n", io.frame / 50.0 / 60.0, TAG[level], s), fflush(stdout);
}
static void p_note(void *ctx, const char *s) {
    (void) ctx;
    if (journal) fprintf(journal, "{\"frame\": %u, \"e\": %s}\n", io.frame, s), fflush(journal);
}
static double s_launch_at;             // when f0 (launch) last went down
static double s_hyper_at;              // and H (hyperspace)
static double s_flight_at;             // when the flight code was last patched
static void key(int code, int down) {
    if (code == K_F0 && down) s_launch_at = now_s();
    if (code == K_H && down) s_hyper_at = now_s();
    uint8_t bit = (uint8_t) (1u << (code & 7));
    if (down) s_down[(code & 127) >> 3] |= bit;
    else s_down[(code & 127) >> 3] &= (uint8_t) ~bit;
    s_dirty = 1;
    int ch = down ? key_ascii(code) : 0;
    // (a line is typed as one run of taps; a character 10 frames or more after the last starts a new
    // one: what came before it was the key that opened the prompt, read by RDKEY, or older)
    static uint32_t last;
    if (ch && io.frame - last >= 10) s_ntyped = 0;
    if (ch) last = io.frame;
    if (ch && s_ntyped < (int) sizeof s_typed) s_typed[s_ntyped++] = (char) ch;
}

// ---- booting Elite: *LOAD ELITE2, one instruction of its vector copy changed, CALL &2F23

// put text in the OS's keyboard buffer, 30 characters at a time, each lot once the OS has taken the last
static void type_text(const char *text) {
    char err[200];
    for (const char *p = text; *p; ) {
        uint8_t ptr[10];
        range_t r = {KEYBUF_START, 10};
        must(peek(&r, 1, ptr, err, sizeof err), "keyboard buffer", err);
        if (ptr[0] != ptr[9]) { usleep(50000); continue; }
        uint8_t bytes[40];
        range_t w[33];
        int nw = 0, nd = 0, y = ptr[9];
        for (; *p && nd < 30 && nw < 31; p++) {
            w[nw++] = (range_t) {(uint16_t) (0x300 + y), 1};
            bytes[nd++] = (uint8_t) *p;
            y = y == 0xFF ? KEYBUF_FIRST : y + 1;
        }
        w[nw++] = (range_t) {KEYBUF_END, 1};
        bytes[nd++] = (uint8_t) y;
        must(poke(w, nw, bytes, err, sizeof err), "typing", err);
    }
}

static void boot(void) {
    char err[200];
    uint8_t v[2];
    range_t r = {0xFFFC, 2};
    must(peek(&r, 1, v, err, sizeof err), "reset vector", err);
    if (v[0] != 0xCD || v[1] != 0xD9) die("not a Model B with MOS 1.20 (reset vector &%02X%02X)", v[1], v[0]);
    // the window's JIM page: the input page in shared memory, where DKS4 and RDKEY will read the keys
    uint32_t page = s_shared_jim + KEYPAGE_OFF;
    char line[80];
    snprintf(line, sizeof line, "?&FCFD=%u:?&FCFE=%u:?&FCFF=%u\r", page >> 24, page >> 16 & 255, page >> 8 & 255);
    printf("boot: %.*s\n", (int) strlen(line) - 1, line);
    type_text(line);
    printf("boot: *LOAD ELITE2\n"), fflush(stdout);
    // ELITE2 copies the MOS's default vectors over &0200-&0235, the probe's hooks on IRQ2V and
    // EVNTV with them; then it loads the rest through the DFS, which takes the NMI's &0D00, so the
    // probe could never stop the game again.  Its copy is cut to &0200-&0205 (USERV, BRKV,
    // IRQ1V): CPY &FFB6 (the table's length, &36) becomes CPY #6.  The other vectors keep what
    // BASIC and the DFS gave them.
    type_text("*LOAD ELITE2\r");
    uint8_t e2[256];
    range_t r2 = {0x2F00, 256};
    double until = now_s() + 30;
    int at = -1;
    while (at < 0) {
        if (now_s() > until) die("boot: no ELITE2 at &2F00 (is the Elite disc in drive 0?)");
        usleep(300000);
        if (peek(&r2, 1, e2, err, sizeof err)) continue;
        for (int i = 0; i + 6 <= 256; i++)      // LDA (&01),Y: STA &0200,Y: INY: CPY &FFB6
            if (!memcmp(e2 + i, (const uint8_t[]) {0xB1, 0x01, 0x99, 0x00, 0x02, 0xC8, 0xCC, 0xB6, 0xFF}, 9)) at = i + 6;
    }
    range_t w = {(uint16_t) (0x2F00 + at), 3};
    must(poke(&w, 1, (const uint8_t[]) {0xC0, 0x06, 0xEA}, err, sizeof err), "boot: patching ELITE2", err);
    printf("boot: CALL &2F23 (ELITE2's copy of the vectors cut to &0200-&0205)\n"), fflush(stdout);
    type_text("CALL &2F23\r");
}

// ---- patching a code that has just been loaded

static int s_live = -1;                // which code is patched (0 flight, 1 docked), -1 none known
static int s_dks4_done[2];             // its DKS4 patched too
static uint32_t s_fresh;               // fresh copies seen
static int s_known;                    // the version found (elite_detect)
static uint8_t s_ver[0x8000];          // the stretches its check hashes, as last read
static uint32_t s_patches, s_checks;

static int in_code(long pc, int c) {
    if (pc >= DKS4_AT[c] && pc < DKS4_AT[c] + 25) return 1;
    for (int k = 0; k < NFIX; k++)
        if (pc >= JFIX[c][k].at && pc < JFIX[c][k].at + 5) return 1;
    for (int g = 0; g < NREG; g++)
        if (REGION[c][g].n && pc >= REGION[c][g].at && pc < REGION[c][g].at + REGION[c][g].n) return 1;
    return 0;
}

// One stop: which code is in, and is it as loaded (all its regions and DKS4 as in the original)
// or patched?  Patch one as loaded: the copy routine and DKS4; DKS4 alone where only it is left.
// Not while the BBC is in any of it.  Until the version is known, the stretches its check hashes
// too.
static void check_and_patch(void) {
    char err[200];
    long pc;
    if (halt(&pc, err, sizeof err)) { fprintf(stderr, "check: %s\n", err); return; }
    s_checks++;
    range_t r[32];
    int nr = 0, rc[32], rg[32];          // each range's code, and its region (-1: DKS4; -2-k: JFIX k)
    for (int c = 0; c < 2; c++) {
        for (int g = 0; g < NREG; g++)
            if (REGION[c][g].n) rc[nr] = c, rg[nr] = g, r[nr++] = (range_t) {REGION[c][g].at, REGION[c][g].n};
        rc[nr] = c, rg[nr] = -1, r[nr++] = (range_t) {DKS4_AT[c], 25};
        for (int k = 0; k < NFIX; k++) rc[nr] = c, rg[nr] = -2 - k, r[nr++] = (range_t) {JFIX[c][k].at, 5};
    }
    int ncode = nr;
    if (!s_known) r[nr++] = (range_t) {0x0400, 256}, r[nr++] = (range_t) {0x2000, 256}, r[nr++] = (range_t) {0x3000, 256};
    static uint8_t got[4096];
    if (peek_halted(r, nr, got, err, sizeof err)) { fprintf(stderr, "check: %s\n", err); resume(); return; }
    int off = 0, as_loaded[2] = {1, 1}, dks4_loaded[2] = {0, 0}, patched[2] = {0, 0};
    for (int i = 0; i < ncode; i++) {
        int c = rc[i], g = rg[i];
        if (g == -1) dks4_loaded[c] = !memcmp(got + off, DKS4, 25);
        else if (g < -1) {
            if (memcmp(got + off, JFIX[c][-2 - g].was, 5)) as_loaded[c] = 0;
        } else {
            if (memcmp(got + off, s_was[c][g], r[i].n)) as_loaded[c] = 0;
            if (g == 0 && !memcmp(got + off, s_code[c][0], r[i].n)) patched[c] = 1;
        }
        off += r[i].n;
    }
    for (int i = ncode; i < nr; i++) memcpy(s_ver + r[i].at, got + off, r[i].n), off += r[i].n;
    for (int c = 0; c < 2; c++) as_loaded[c] &= dks4_loaded[c];
    int c = as_loaded[0] ? 0 : as_loaded[1] ? 1 : patched[0] ? 0 : patched[1] ? 1 : -1;
    range_t w[16];
    uint8_t bytes[512];
    int nw = 0, nd = 0, routine = 0, dks4 = 0;
    if (c >= 0 && !in_code(pc, c)) {
        if (as_loaded[c]) {
            for (int g = 0; g < NREG; g++)
                if (REGION[c][g].n) {
                    w[nw++] = (range_t) {REGION[c][g].at, REGION[c][g].n};
                    memcpy(bytes + nd, s_code[c][g], REGION[c][g].n), nd += REGION[c][g].n;
                }
            for (int k = 0; k < NFIX; k++) {
                w[nw++] = (range_t) {JFIX[c][k].at, 5};
                memcpy(bytes + nd, JFIX[c][k].now, 5), nd += 5;
            }
            routine = 1;
        }
        if (dks4_loaded[c]) {
            w[nw++] = (range_t) {DKS4_AT[c], 25};
            dks4_patch(bytes + nd), nd += 25;
            dks4 = 1;
        }
    }
    if (nw && poke_how(w, nw, bytes, err, sizeof err, 1)) fprintf(stderr, "patch: %s\n", err), routine = dks4 = 0;
    resume();
    if (c >= 0) {
        if (routine || patched[c]) {
            s_live = c;
            s_dks4_done[c] = dks4 || !dks4_loaded[c];
            if (routine && c == 0) s_flight_at = now_s();
        }
        if (routine || dks4) {
            s_patches++;
            printf("%7.1f patched %s%s in the %s code\n", io.frame / 3000.0, routine ? "the copy routine" : "",
                   dks4 ? (routine ? " and DKS4" : "DKS4") : "", c ? "docked" : "flight");
        }
    }
}

// ---- reading the mirror: 1 a fresh whole copy (into s_ram), 0 the last one again, -2 a copy under
// way, -1 none (no code patched yet, or no answer)
static uint8_t s_last_seq, s_magic_seen;
static int s_have_seq;
static uint32_t s_torn, s_fetches, s_fetch_fail, s_fetch_same;
static double s_last_fresh;            // when the last fresh copy came
static int fetch_mirror(void) {
    static uint8_t m[MIRROR_MAX];
    s_fetches++;
    if (s_live < 0) return -1;
    if (shared_get(MIRROR_OFF, MIRROR_MAX, m)) { s_fetch_fail++; return -1; }
    int end;
    if (s_live == 0) end = m[2 + 3 * (0x315 - 0x311)] ? FLIGHT_LONG : FLIGHT_SHORT;     // FRIN+4 as copied
    else end = DOCKED_END;
    if (m[0] != m[end]) { s_torn++; return -2; }
    if (s_have_seq && m[0] == s_last_seq) { s_fetch_same++; return 0; }
    s_last_seq = m[0], s_have_seq = 1;
    int stride = s_live == 0 ? 3 : 2;
    for (int i = 0; i < SPAN; i++) {
        s_ram[i] = m[1 + stride * i];
        s_ram[0x311 + i] = m[2 + stride * i];
        if (!s_live) s_ram[0x900 + i] = m[3 + stride * i];
    }
    int at = 1 + stride * SPAN;
    if (s_live == 0) {
        s_ram[0x0FD1] = m[at++];
        if (end == FLIGHT_LONG) memcpy(s_ram + 0x9BC, m + at, 256);
    } else {
        memcpy(s_ram + 0x1181, m + at, 0x54), at += 0x54;
        s_ram[0x0FD1] = m[at++];
        s_magic_seen = m[at++];
    }
    return 1;
}

// ---- the keys: the list DKS4 looks in and RDKEY's answer, written into the window's page
static uint8_t s_win_sent[WIN_MAGIC + 1];
static int s_win_valid;
static uint8_t s_magic = 0x5A;
static uint32_t s_key_writes;
static void write_keys(int force) {
    uint8_t w[WIN_MAGIC + 1];
    memset(w, 0, sizeof w);
    memset(w + WIN_LIST, 0xFF, NLIST);
    int n = 0, first = 0;
    for (int c = 0; c < 128; c++)
        if (s_down[c >> 3] >> (c & 7) & 1) {
            if (n < NLIST) w[WIN_LIST + n++] = (uint8_t) c;
            else if (!s_toomany++) fprintf(stderr, "more than %d keys held: key &%02X left out\n", NLIST, c);
            if (c >= 16 && !first) first = c;
        }
    w[WIN_RDKEY] = (uint8_t) first;
    w[WIN_MAGIC] = s_magic;
    if (!force && s_win_valid && !memcmp(w, s_win_sent, sizeof w)) return;
    if (shared_put(KEYPAGE_OFF, w, sizeof w)) return;
    memcpy(s_win_sent, w, sizeof w), s_win_valid = 1;
    s_key_writes++;
}

// A line typed at an OSWORD 0 prompt into the OS's keyboard buffer, in one stop: only once the
// BBC is in the MOS (PC from &C000), which is where it waits for the line, after MT26 has flushed
// the buffer; a character put in before the flush is lost.  1 typed (or dropped), 0 not yet.
static int type_now(void) {
    char err[200];
    long pc;
    if (halt(&pc, err, sizeof err)) { fprintf(stderr, "typing: %s\n", err); return 0; }
    if (pc < 0xC000) { resume(); return 0; }
    range_t r[2] = {{KEYBUF_START, 1}, {KEYBUF_END, 1}};
    uint8_t got[2];
    if (peek_halted(r, 2, got, err, sizeof err)) { fprintf(stderr, "typing: %s\n", err); resume(); return 0; }
    int start = got[0], y = got[1], used = (y - start + 32) & 31, n = 0;
    range_t w[40];
    uint8_t bytes[64];
    int nw = 0, nd = 0;
    for (; n < s_ntyped && used + n < 30 && nw < 38; n++) {
        w[nw++] = (range_t) {(uint16_t) (0x300 + y), 1};
        bytes[nd++] = (uint8_t) s_typed[n];
        y = y == 0xFF ? KEYBUF_FIRST : y + 1;
    }
    if (getenv("PROBELOG"))
        printf("%7.1f typed %d of %d into the OS's buffer (start %02X end %02X): %.*s\n", io.frame / 3000.0, n, s_ntyped, start, got[1],
               s_ntyped && s_typed[s_ntyped - 1] == '\r' ? s_ntyped - 1 : s_ntyped, s_typed);
    s_ntyped = 0;      // (what didn't fit is dropped: Elite flushes the buffer before it reads a line)
    if (n) {
        w[nw++] = (range_t) {KEYBUF_END, 1};
        bytes[nd++] = (uint8_t) y;
        if (poke_how(w, nw, bytes, err, sizeof err, 1)) fprintf(stderr, "typing: %s\n", err);
    }
    resume();
    return 1;
}

static void diag(const char *line) {
    char err[200];
    long pc;
    (void) line;
    if (halt(&pc, err, sizeof err)) return;
    long sp = json_num(s_st, "s");
    range_t r[8] = {{0x0087, 1}, {0x008A, 1}, {0x008E, 1}, {0x03CB, 1}, {0x008B, 1}, {0x01E0, 32}};
    int nr = 6;
    int c = s_live >= 0 ? s_live : 1;
    r[nr++] = (range_t) {REGION[c][0].at, 15};
    r[nr++] = (range_t) {DKS4_AT[c], 25};
    static uint8_t got[128];
    if (!peek_halted(r, nr, got, err, sizeof err)) {
        uint8_t d4[25];
        dks4_patch(d4);
        printf("        diag: pc &%04lX s &%02lX qq11 %02X mcnt %02X qq12 %02X jstk %02X dl %02X; rdkey %s, dks4 %s; stack",
               pc, sp, got[0], got[1], got[2], got[3], got[4], memcmp(got + 37, s_code[c][0], 15) ? "NOT patched" : "patched",
               memcmp(got + 52, d4, 25) ? "NOT patched" : "patched");
        for (int i = 0; i < 32; i++) printf(" %02X", got[5 + i]);
        printf("; window list");
        for (int i = 0; i < NLIST; i++) printf(" %02X", s_win_sent[WIN_LIST + i]);
        printf(" rdkey %02X; the player sees qq11 %02X, a fresh copy %.2f s ago; fetches %u: %u failed, %u mid-copy, %u the same\n",
               s_win_sent[WIN_RDKEY], s_ram[0x87], now_s() - s_last_fresh, s_fetches, s_fetch_fail, s_torn, s_fetch_same);
    }
    resume();
}

// on the way out, every key let go: the window keeps what it was last given
static volatile sig_atomic_t s_quit;
static void on_signal(int sig) { (void) sig; s_quit = 1; }
static void release_keys(void) {
    memset(s_down, 0, sizeof s_down);
    write_keys(1);
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: run_probe HOST[:PORT] [minutes] [journal.jsonl]\n");
        return 1;
    }
    snprintf(s_host, sizeof s_host, "%s", argv[1]);
    char *colon = strchr(s_host, ':');
    if (colon) *colon = 0, s_port = atoi(colon + 1);
    double minutes = argc > 2 ? atof(argv[2]) : 60;
    journal = fopen(argc > 3 ? argv[3] : "/dev/null", "w");
    if (getenv("NAME")) { extern char g_name[8]; snprintf(g_name, sizeof g_name, "%s", getenv("NAME")); }
    { extern int g_save; if (getenv("SAVE")) g_save = atoi(getenv("SAVE")); }

    char st[2048];
    if (http("GET", "/bbc", st, sizeof st) != 200) die("no probe at %s:%d", s_host, s_port);
    if (!strstr(st, "\"installed\":true")) die("the probe isn't installed: type X%%=23:CALL &FC88 at the BBC's BASIC prompt");
    const char *sh = strstr(st, "\"shared\":{");
    if (!sh) die("this probe has no shared memory (GET /bbc's shared): it needs newer Pi1MHz firmware");
    s_shared_at = (uint32_t) json_num(sh, "at"), s_shared_jim = (uint32_t) json_num(sh, "jim");
    if ((s_shared_at | s_shared_jim) & 0xFFFF)
        die("shared memory at services &%06X, JIM &%08X: the patches want it on a 64 KB boundary", s_shared_at, s_shared_jim);
    for (int c = 0; c < 2; c++)
        for (int g = 0; g < NREG; g++)
            for (int i = 0; i < REGION[c][g].n; i++) {
                unsigned v;
                sscanf(REGION[c][g].was + 2 * i, "%2x", &v);
                s_was[c][g][i] = (uint8_t) v;
            }
    build_copy(0), build_copy(1);
    if (getenv("PROBELOG"))
        for (int c = 0; c < 2; c++)
            for (int g = 0; g < NREG; g++)
                if (REGION[c][g].n) {
                    printf("%s &%04X:", c ? "docked" : "flight", REGION[c][g].at);
                    for (int i = 0; i < REGION[c][g].n; i++) printf(" %02X", s_code[c][g][i]);
                    printf("\n");
                }
    // the input page: all 0 but the list, so its last byte isn't the agent page's number (the
    // probe's way in waits for 22 at &FDFF).  And the mirror: a sequence number at its start that
    // none of its ends matches, so it reads as a copy under way until the routine's first.
    {
        static uint8_t init[MIRROR_OFF + MIRROR_MAX];
        memset(init + KEYPAGE_OFF + WIN_LIST, 0xFF, NLIST);
        init[MIRROR_OFF] = 1;
        if (shared_put(KEYPAGE_OFF, init, sizeof init)) die("can't write shared memory");
    }
    write_keys(1);
    if (getenv("BOOT")) boot();
    signal(SIGINT, on_signal), signal(SIGTERM, on_signal);

    io.ram = s_ram;
    io.hold = p_hold, io.tap = p_tap, io.log = p_log, io.note = p_note;
    io.analogue = NULL;            // (disc Elite on the Model B fires only from the stick's button: it plays on keys)
    io.machine = 1;
    keyq_reset();
    player_init(&io);

    double t0 = now_s(), last_report = t0, last_fresh = t0, last_check = 0, magic_at = t0;
    double report_every = getenv("PROBEREPORT") ? atof(getenv("PROBEREPORT")) : 60;
    uint32_t end = (uint32_t) (minutes * 60 * 50);
    s_stopped = 0, s_stops = 0;
    int magic_bad = 0, stale_checks = 0;
    double load_took = 1.0;          // how long the last load kept the copies away
    struct { uint32_t stops, fresh, fetches, torn, keys; double stopped, t; } was = {0, 0, 0, 0, 0, 0, t0};
    while (io.frame < end && !s_quit) {
        double t = now_s();
        // the mirror, about once a frame
        // (a screen waiting for a key copies once a frame, just after the vsync, and this loop polls once
        // a frame too: the two can lock, every fetch landing in the copy.  So one found under way is
        // fetched again 4 ms on, when a copy of 3-5 ms has finished)
        int got = fetch_mirror();
        for (int tries = 0; got == -2 && tries < 3; tries++) usleep(4000), got = fetch_mirror();
        if (got > 0) {
            s_fresh++, last_fresh = s_last_fresh = t;
            s_ntyped = 0, s_typed_at = 0;      // (Elite isn't at an OSWORD 0 prompt: see the typing, below)
            // the version, from the stretches the checks read, only now that the player's RAM is
            // current: it acts on the first frame it knows the version
            if (!s_known) {
                static const uint16_t VER[3] = {0x0400, 0x2000, 0x3000};
                for (int i = 0; i < 3; i++) memcpy(s_ram + VER[i], s_ver + VER[i], 256);
                if (elite_detect(io.machine, s_ram)) s_known = 1;
            }
            if (s_live == 1) {
                if (s_magic_seen != s_magic) {
                    if (++magic_bad == 100)
                        fprintf(stderr, "the window doesn't show the input page (&FD10 copied as &%02X, not &%02X): "
                                "its page registers are set at BASIC's prompt (BOOT=1)\n", s_magic_seen, s_magic);
                } else magic_bad = 0;
            }
        }
        // a code just loaded (the mirror stops: the routine went with the old code), DKS4 still to
        // patch, or the version not yet known: one stop, no more than every 1.5 s
        // (a quiet spell without a load, a hyperspace or a save, would otherwise cost a stop every
        // 1.5 s: after two, every 3 s)
        int want_dks4 = s_live >= 0 && !s_dks4_done[s_live];
        double gap = stale_checks < 2 ? 1.5 : 3.0;
        // (docked, the only way to the other code is f0: until it's pressed, a spell without copies is
        // a prompt or the disc, and is checked every 10 s only)
        if (s_live == 1 && s_known && s_dks4_done[1] && s_ram[0x8E] && t - s_launch_at > 8) gap = 10;
        // (in flight, the launch's tunnel just after the code is patched, and a hyperspace's after its
        // countdown, stop the copies with no load to follow: every 6 s then)
        if (s_live == 0 && s_known && s_dks4_done[0] && (t - s_flight_at < 15 || t - s_hyper_at < 25)) gap = 6;
        if (t - last_fresh <= 1.0) stale_checks = 0;
        // (in those spells the first check waits as long; otherwise about as long as the last load
        // took, from its last copy to the patch: 5.5 s on the 8271 DFS, less on a faster filing system)
        double quiet = gap > 3 ? gap : load_took - 0.7 > 1.0 ? load_took - 0.7 : 1.0;
        if ((t - last_fresh > quiet || s_live < 0 || want_dks4 || !s_known) && t - last_check > gap) {
            last_check = t, stale_checks++;
            uint32_t was_patches = s_patches;
            int was_live = s_live;
            check_and_patch();
            if (s_patches != was_patches && s_live != was_live && was_live >= 0) load_took = t - last_fresh;
            if (getenv("PROBELOG"))
                printf("%7.1f check: no copy for %.1f s, qq11 %02X qq12 %02X mcnt %02X: %s\n", io.frame / 3000.0, t - last_fresh,
                       s_ram[0x87], s_ram[0x8E], s_ram[0x8A], s_patches != was_patches ? "patched" : "nothing to patch");
            t = now_s();
        }
        if (t - magic_at > 30) magic_at = t, s_magic++, write_keys(1);

        // the game's time: the wall's, less the stops.  (The BBC runs at its own speed between
        // them; the sequence number counts copies, which come once a main-loop iteration, not frames.)
        uint32_t game = (uint32_t) ((t - t0 - s_stopped) * 50);
        if (game > 50 && io.frame < game - 50) io.frame = game - 50;      // (a long stall: don't replay it)
        while (io.frame < game) {
            io.taps_done += (uint32_t) keyq_step(io.frame, key), io.frame++;
            player_frame();
            if (s_dirty) write_keys(0), s_dirty = 0;
            // Typed characters matter only at a prompt that reads a line through the OS (OSWORD 0, in
            // MT26: a commander's name from the disc menu, QQ11 1, or a system's from a chart, QQ11 64
            // or 128), which flushes the buffer first.  While it waits there Elite calls no RDKEY, so
            // no copy comes: a copy drops what's been typed so far (it was for RDKEY, or would be
            // flushed).  A line goes in in one stop, once its RETURN is typed (or a second has passed)
            // with no copy for 150 ms, at no more than 10 tries a second; it's dropped after 3 s.
            if (s_ntyped) {
                double tt = now_s();
                static double tried;
                if (!s_typed_at) s_typed_at = tt;
                int line = s_typed[s_ntyped - 1] == '\r' || tt - s_typed_at > 1.0;
                if (line && tt - last_fresh > 0.15 && tt - tried > 0.1 && (s_ram[0x87] == 1 || (s_ram[0x87] & 0xC0))) {
                    tried = tt;
                    if (type_now()) s_typed_at = 0;
                }
                if (s_ntyped && tt - s_typed_at > 3.0) s_ntyped = 0, s_typed_at = 0;
            }
        }

        t = now_s();
        if (t - last_report > report_every) {
            last_report = t;
            double dt = t - was.t;
            uint32_t ns = s_stops - was.stops, nf = s_fresh - was.fresh, nq = s_fetches - was.fetches;
            printf("%7.1f probe: %.3f stops a second (%u in %.0f s), stopped %.2f%% of the time (longest stop %.0f ms); "
                   "%.1f copies a second, %.1f fetches, %.1f%% mid-copy; %u key writes; %u patches, %u checks in all%s\n",
                   io.frame / 3000.0, ns / dt, ns, dt, 100 * (s_stopped - was.stopped) / dt, 1e3 * s_stop_longest,
                   nf / dt, nq / dt, nq ? 100.0 * (s_torn - was.torn) / nq : 0, s_key_writes - was.keys, s_patches, s_checks,
                   s_live < 0 ? "; no code patched yet" : "");
            if (getenv("PROBEDEBUG"))
                printf("        ev %d frame %u qq11 %02X qq12 %02X type %02X mcnt %02X down %02X%02X%02X%02X%02X%02X%02X%02X\n", g_ev.id, io.frame,
                       s_ram[0x87], s_ram[0x8E], s_ram[0x8C], s_ram[0x8A], s_down[0], s_down[1], s_down[2], s_down[3], s_down[4], s_down[5], s_down[6], s_down[7]);
            was.stops = s_stops, was.fresh = s_fresh, was.fetches = s_fetches, was.torn = s_torn, was.keys = s_key_writes;
            was.stopped = s_stopped, was.t = t;
            fflush(stdout);
        }
        double next = t0 + s_stopped + (io.frame + 1) / 50.0;      // (a frame at a time)
        if (next > t) usleep((useconds_t) ((next - t) * 1e6));
    }
    release_keys();
    double wall = now_s() - t0;
    printf("done: %.1f game minutes in %.1f wall minutes; %u stops (%.3f a second), stopped %.2f%% of the time; "
           "%u copies (%.1f a second), %u key writes, %u patches, %u checks\n",
           io.frame / 3000.0, wall / 60, s_stops, s_stops / wall, 100 * s_stopped / wall, s_fresh, s_fresh / wall,
           s_key_writes, s_patches, s_checks);
    return 0;
}
