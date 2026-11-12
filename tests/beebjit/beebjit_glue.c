/* beebjit_glue.c - the Pi1MHz board on beebjit: a Model B with MOS 1.20, BASIC II and DFS
   0.90 on the 8271, from a checkout of beebjit.  Built by run_beebjit.sh, which patches
   beebjit's bbc.c to call the four pi1mhz_ functions below.

   The real ramprobe.c, ramprobe_api.c, helpers.c, ram_emulator.c, services_emulator.c and
   6502code.bin, on the Pi1MHz checkout's test board (src/tests/ramprobe/board.h), with the
   same HTTP routes as its bbcsim.c: /bbc, and what a person does at the machine.
     POST /sim/type?text=PRINT+1%0D   keys, typed at about 10 a second; answers when done
     GET  /sim/screen                 the MODE 7 screen, as text
     POST /sim/break[?ctrl=1]         Break (Ctrl-Break); answers 3 s later
     GET  /sim/peek?addr=&len=        BBC RAM, read straight, without a stop (up to 256 bytes)
     GET  /sim/stops                  probe stops since the start: each time the probe left running
     POST /sim/quit
   The port is PI1MHZ_PORT in the environment. */
#include <arpa/inet.h>
#include <fcntl.h>
#include <signal.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#define BOARD_EXTRA
#include "board.h"

#include "bbc.h"
#include "cpu_driver.h"
#include "keyboard.h"
#include "state_6502.h"
#include "timing.h"

static const char *board_config(const char *key) { (void)key; return NULL; }
static void board_extra_init(void) {}

static struct bbc_struct *bbc;
static struct timing_struct *timing;
static uint32_t timer;
static int listener = -1;
static bool was_reset;

static uint32_t ticks(void) { return timing != NULL ? (uint32_t)timing_get_total_timer_ticks(timing) : 0u; }

static void board_nmi_line(bool low)
{
   if (bbc != NULL) state_6502_set_irq_level(bbc_get_6502(bbc), k_state_6502_irq_nmi, low ? 1 : 0);
}

#define IRQ_PI1MHZ 16          /* a source of beebjit's own: the Pi's share of nIRQ */
static void board_irq_line(bool low)
{
   if (bbc != NULL) state_6502_set_irq_level(bbc_get_6502(bbc), IRQ_PI1MHZ, low ? 1 : 0);
}

/* beebjit reads data at &FC00-&FDFF through its callback, but its interpreter fetches
   instructions from its own memory, so code run from FRED or the window (the helper entry at
   &FC88, the agent at &FD00) must be there too: the board's bytes, copied after anything that
   can change them.  Interpreter mode only; the JIT would need its code invalidated. */
static void mirror(void)
{
   static uint8_t shown[512];
   static bool primed;
   if (bbc == NULL) return;
   for (unsigned i = 0; i < 512u; i++)          /* beebjit maps it read-only: its own call writes it */
      if (!primed || shown[i] != pi.Memory[i]) {
         bbc_memory_write(bbc, (uint16_t)(0xFC00u + i), pi.Memory[i]);
         shown[i] = pi.Memory[i];
      }
   primed = true;
}

uint8_t pi1mhz_bus_read(uint16_t addr) { board_now = ticks(); return board_bus_read(addr); }
void pi1mhz_bus_write(uint16_t addr, uint8_t val) { board_now = ticks(); board_bus_write(addr, val); mirror(); }

/* nRST: the board sees every reset, a Break or the power coming on. */
void pi1mhz_bbc_reset(void)
{
   was_reset = true;
   board_reset();
   mirror();
}

/* ---- probe stops, counted: each time the probe's state leaves running (a halt asked, by any
   rung, or a peek or poke that stops by itself).  The state changes only in the board's poll and
   the HTTP answer, and a stop lasts much longer than the 100 us between samples. */
static unsigned long stops;
static rp_state_t last_state = RP_RUNNING;

static void count_stops(void)
{
   rp_state_t s = ramprobe_state();
   if (last_state == RP_RUNNING && s != RP_RUNNING && s != RP_OFF) stops++;
   last_state = s;
}

/* ---- the keyboard, at the speed of fingers ---------------------------------------- */
static struct { uint8_t key; bool shift; } keys[512];
static int nkeys, at_key, phase;
static uint32_t phase_us;

static bool key_for(char c, uint8_t *key, bool *shift)
{
   /* The BBC's shifted characters, and the key each is on, by beebjit's name for it: its
      keys are where a PC's are, so the BBC's : is on ' and its ^ on =. */
   static const struct { char c, key; } Shifted[] = {
      {'!', '1'}, {'"', '2'}, {'#', '3'}, {'$', '4'}, {'%', '5'}, {'&', '6'}, {'\'', '7'},
      {'(', '8'}, {')', '9'}, {'*', '\''}, {'+', ';'}, {'=', '-'}, {'?', '/'}, {'|', '\\'},
      {'<', ','}, {'>', '.'}, {'~', '='},
   };
   *shift = false;
   if (c >= 'A' && c <= 'Z') { *key = (uint8_t)c; return true; }
   if (c >= 'a' && c <= 'z') { *key = (uint8_t)(c - 32); *shift = true; return true; }   /* CAPS LOCK is on */
   if ((c >= '0' && c <= '9') || strchr(" ,./;-", c)) { *key = (uint8_t)c; return true; }
   if (c == '\r') { *key = k_keyboard_key_enter; return true; }
   if (c == 27) { *key = k_keyboard_key_escape; return true; }
   if (c == ':') { *key = '\''; return true; }
   for (size_t i = 0; i < sizeof Shifted / sizeof Shifted[0]; i++)
      if (Shifted[i].c == c) { *key = (uint8_t)Shifted[i].key; *shift = true; return true; }
   return false;
}

static bool typing(void) { return at_key < nkeys; }

static void type_poll(void)
{
   if (!typing()) return;
   struct keyboard_struct *kb = bbc_get_keyboard(bbc);
   uint32_t gap = Pi1MHz_now_us - phase_us;
   switch (phase) {
   case 0:
      if (keys[at_key].shift) keyboard_system_key_pressed(kb, k_keyboard_key_shift_left);
      phase = 1; phase_us = Pi1MHz_now_us;
      break;
   case 1:
      if (gap < 30000u) break;
      keyboard_system_key_pressed(kb, keys[at_key].key);
      phase = 2; phase_us = Pi1MHz_now_us;
      break;
   case 2:
      if (gap < 50000u) break;
      keyboard_system_key_released(kb, keys[at_key].key);
      if (keys[at_key].shift) keyboard_system_key_released(kb, k_keyboard_key_shift_left);
      phase = 3; phase_us = Pi1MHz_now_us;
      break;
   default:
      if (gap < 50000u) break;
      at_key++;
      phase = 0;
      break;
   }
}

/* ---- HTTP ------------------------------------------------------------------------ */
static int waiting_fd = -1;           /* a request answered when the machine has done it */
static bool waiting_for_probe;        /* wait=1: answered with GET /bbc's status once the probe is done */
static uint32_t waiting_until;
static bool waiting_for_keys;
static bool releasing_ctrl, breaking;
static uint32_t ctrl_up_at, break_at;

static void reply(int fd, int status, const char *body)
{
   char head[160];
   int h = snprintf(head, sizeof head, "HTTP/1.1 %d %s\r\nContent-Type: application/json\r\nContent-Length: %zu\r\nConnection: close\r\n\r\n",
                    status, status == 200 ? "OK" : "Error", strlen(body));
   if (write(fd, head, (size_t)h) < 0 || write(fd, body, strlen(body)) < 0) perror("write");
   close(fd);
}

static void decode(const char *in, size_t len, char *out, size_t n)
{
   size_t o = 0;
   for (size_t i = 0; i < len && o + 1 < n; i++) {
      if (in[i] == '+') out[o++] = ' ';
      else if (in[i] == '%' && i + 2 < len) {
         char h[3] = { in[i + 1], in[i + 2], 0 };
         out[o++] = (char)strtol(h, NULL, 16);
         i += 2;
      } else out[o++] = in[i];
   }
   out[o] = 0;
}

static bool value(const char *query, const char *key, char *out, size_t n)
{
   size_t klen = strlen(key);
   for (const char *p = query; p && *p; ) {
      const char *end = strchr(p, '&');
      size_t len = end ? (size_t)(end - p) : strlen(p);
      if (len > klen && p[klen] == '=' && !strncmp(p, key, klen)) {
         decode(p + klen + 1, len - klen - 1, out, n);
         return true;
      }
      p = end ? end + 1 : NULL;
   }
   return false;
}

static void screen_text(char *out, size_t n)
{
   const uint8_t *ram = bbc_get_mem_read(bbc);
   uint16_t a = (uint16_t)(ram[0x350] | (ram[0x351] << 8));
   size_t o = 0;
   for (int r = 0; r < 25; r++) {
      for (int c = 0; c < 40 && o + 2 < n; c++) {
         uint8_t v = ram[0x7C00 + ((a - 0x7C00 + r * 40 + c) & 0x3FF)] & 0x7F;
         out[o++] = v >= 32 && v < 127 ? (char)v : ' ';
      }
      if (o + 2 < n) out[o++] = '\n';
   }
   out[o] = 0;
}

static void soft_break(void)
{
   struct cpu_driver *cpu = bbc_get_cpu_driver(bbc);
   cpu->p_funcs->apply_flags(cpu, k_cpu_flag_soft_reset, 0);
}

/* True if it answered; false if it's waiting for the machine (fd kept). */
static bool sim_route(int fd, bool post, const char *path, const char *query)
{
   static char text[512], out[2048];
   if (!post && !strcmp(path, "/sim/screen")) { screen_text(out, sizeof out); reply(fd, 200, out); return true; }
   if (post && !strcmp(path, "/sim/type")) {
      if (!value(query, "text", text, sizeof text) || typing()) { reply(fd, 400, "{\"error\":\"no text, or still typing\"}\n"); return true; }
      nkeys = at_key = phase = 0;
      for (const char *c = text; *c; c++)
         if (nkeys < (int)(sizeof keys / sizeof keys[0]) && !key_for(*c, &keys[nkeys].key, &keys[nkeys].shift)) {
            nkeys = 0;
            reply(fd, 400, "{\"error\":\"not typeable\"}\n");
            return true;
         } else nkeys++;
      waiting_fd = fd; waiting_for_keys = true; waiting_until = 0;
      return false;
   }
   if (post && !strcmp(path, "/sim/break")) {
      bool ctrl = value(query, "ctrl", text, sizeof text) && text[0] == '1';
      if (ctrl) {                     /* held first, as a person does: the queue is read each frame */
         keyboard_system_key_pressed(bbc_get_keyboard(bbc), k_keyboard_key_ctrl);
         releasing_ctrl = true;
         ctrl_up_at = Pi1MHz_now_us + 600000u;
         break_at = Pi1MHz_now_us + 100000u;
         breaking = true;
      } else soft_break();
      waiting_fd = fd; waiting_for_keys = false; waiting_until = Pi1MHz_now_us + 3000000u;
      return false;
   }
   if (!post && !strcmp(path, "/sim/peek")) {      /* BBC RAM straight, without a stop */
      char v[16];
      unsigned addr = value(query, "addr", v, sizeof v) ? (unsigned)strtoul(v, NULL, 0) : 0u;
      unsigned len = value(query, "len", v, sizeof v) ? (unsigned)strtoul(v, NULL, 0) : 1u;
      const uint8_t *ram = bbc_get_mem_read(bbc);
      size_t o = 0;
      for (unsigned i = 0; i < len && i < 256u && addr + i < 0x8000u; i++) o += (size_t)snprintf(out + o, sizeof out - o, "%02x", ram[addr + i]);
      out[o++] = '\n'; out[o] = 0;
      reply(fd, 200, out);
      return true;
   }
   if (!post && !strcmp(path, "/sim/stops")) {
      snprintf(out, sizeof out, "{\"stops\":%lu}\n", stops);
      reply(fd, 200, out);
      return true;
   }
   if (post && !strcmp(path, "/sim/quit")) { reply(fd, 200, "{}\n"); exit(0); }
   reply(fd, 404, "No such page.\n");
   return true;
}

static void answer(int fd)
{
   static char req[4096], out[RAMPROBE_API_OUT + 64];
   size_t got = 0;
   req[0] = 0;
   while (got + 1 < sizeof req && !strstr(req, "\r\n\r\n")) {
      ssize_t r = read(fd, req + got, sizeof req - 1 - got);
      if (r <= 0) { close(fd); return; }
      got += (size_t)r;
      req[got] = 0;
   }
   bool post = !strncmp(req, "POST ", 5);
   static uint8_t body[RAMPROBE_API_BODY_MAX];
   if (post && strncmp(req, "POST /bbc/shared", 16) == 0) {          /* straight into shared memory */
      static char bout[512];
      char *qs = strchr(req, '?'), *sp = qs ? strchr(qs, ' ') : NULL;
      char q[256] = "";
      if (qs && sp && (size_t)(sp - qs - 1) < sizeof q) { memcpy(q, qs + 1, (size_t)(sp - qs - 1)); q[sp - qs - 1] = 0; }
      long cl = board_content_length(req);
      uint8_t *dest = NULL;
      int st = ramprobe_api_shared(q, cl > 0 ? (size_t)cl : 0u, &dest, bout, sizeof bout);
      if (st == 200 && board_read_body(fd, req, got, dest, (size_t)cl) == cl)
         snprintf(bout, sizeof bout, "{\"written\":%ld}\n", cl);
      else if (st == 200) { st = 400; snprintf(bout, sizeof bout, "{\"error\":\"short body\"}\n"); }
      char head[160];
      int h = snprintf(head, sizeof head, "HTTP/1.1 %d %s\r\nContent-Type: application/json\r\nContent-Length: %zu\r\nConnection: close\r\n\r\n",
                       st, st == 200 ? "OK" : "Error", strlen(bout));
      if (write(fd, head, (size_t)h) < 0 || write(fd, bout, strlen(bout)) < 0) perror("write");
      close(fd); return;
   }
   long blen = post ? board_read_body(fd, req, got, body, sizeof body) : -1;   /* before req is cut up */
   char *sp1 = strchr(req, ' ');
   char *sp2 = sp1 ? strchr(sp1 + 1, ' ') : NULL;
   if (!sp2) { close(fd); return; }
   *sp2 = 0;
   char *path = sp1 + 1;
   char *query = strchr(path, '?');
   if (query) *query++ = 0;
   if (!strncmp(path, "/sim/", 5)) { (void)sim_route(fd, post, path, query); return; }
   int status = blen > 0 ? ramprobe_api_body(path, query, body, (size_t)blen, out, sizeof out)
                         : ramprobe_api(post, path, query, out, sizeof out);
   if (status == 0) { status = 404; snprintf(out, sizeof out, "No such page.\n"); }
   if (ramprobe_api_hold(post, query, status)) {      /* wait=1: answer when it's over */
      waiting_fd = fd; waiting_for_probe = true;
      return;
   }
   reply(fd, status, out);
}

static void http_poll(void)
{
   if (breaking && (int32_t)(Pi1MHz_now_us - break_at) >= 0) { soft_break(); breaking = false; }
   if (releasing_ctrl && (int32_t)(Pi1MHz_now_us - ctrl_up_at) >= 0) {
      keyboard_system_key_released(bbc_get_keyboard(bbc), k_keyboard_key_ctrl);
      releasing_ctrl = false;
   }
   if (waiting_fd >= 0 && waiting_for_probe) {
      if (ramprobe_api_done()) {
         static char out[RAMPROBE_API_OUT + 64];
         reply(waiting_fd, ramprobe_api_status(out, sizeof out), out);
         waiting_fd = -1; waiting_for_probe = false;
      }
      return;
   }
   if (waiting_fd >= 0) {
      bool done = waiting_for_keys ? !typing() : (int32_t)(Pi1MHz_now_us - waiting_until) >= 0;
      if (done) { reply(waiting_fd, 200, "{}\n"); waiting_fd = -1; }
      return;                         /* one thing at a time, as a person does */
   }
   int fd = accept(listener, NULL, NULL);
   if (fd < 0) return;
   fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) & ~O_NONBLOCK);
   answer(fd);
}

/* ---- every 100 us of the machine: the main loop's pass, the keys, the network ------ */
static void tick(void *p)
{
   (void)p;
   (void)timing_set_timer_value(timing, timer, 200);
   board_poll();
   count_stops();
   type_poll();
   if (Pi1MHz_now_us % 1000u == 0u) { http_poll(); count_stops(); }
   mirror();                         /* last: a request writes the window too, and the BBC reads
                                        its command through the callback the moment it's there */
}

void pi1mhz_attach(struct bbc_struct *b)
{
   const char *port = getenv("PI1MHZ_PORT");
   bbc = b;
   timing = bbc_get_timing(b);
   if (!was_reset) pi1mhz_bbc_reset();
   signal(SIGPIPE, SIG_IGN);      /* a client gone mid-answer is its loss, not the server's end */
   listener = socket(AF_INET, SOCK_STREAM, 0);
   int yes = 1;
   setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof yes);
   struct sockaddr_in a = { .sin_family = AF_INET, .sin_port = htons((uint16_t)atoi(port ? port : "8080")),
                            .sin_addr.s_addr = htonl(INADDR_LOOPBACK) };
   if (bind(listener, (struct sockaddr *)&a, sizeof a) || listen(listener, 8)) { perror("pi1mhz bind"); exit(2); }
   fcntl(listener, F_SETFL, O_NONBLOCK);
   timer = timing_register_timer(timing, "pi1mhz", tick, NULL);
   (void)timing_start_timer_with_value(timing, timer, 200);
   fprintf(stderr, "pi1mhz: listening on 127.0.0.1:%s\n", port ? port : "8080");
}
