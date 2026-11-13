# beeb-remote

Example applications built on a web API for a physical BBC Micro Model B, served by a Pi1MHz on its 1 MHz bus. It can stop
the BBC, read and write its memory, call its code and let it continue, and it shares 4 MB of
memory with programs running on the BBC, which both sides use without stopping. No ROM is needed.

This lets you integrate a physical BBC Micro with anything: web services, AI, databases, other
computers. This repository includes a Python client and the applications below.

| Application | What it does | How it reaches the BBC | Stops while running | On a real Model B |
|---|---|---|---|---|
| [`*CLAUDE`](#claude), `agent.py` | Claude writes a BBC BASIC program, types it in, runs it and fixes it | A doorbell in shared memory; stops to type and read the screen | One every 20 s while it waits; a few a second while it types and reads | Works |
| [`demo50.py`](#video-at-50-frames-a-second) | Video at 50 frames a second in MODE 7, 0, 1 and 2 | The whole video in shared memory, copied to the screen by a player on the BBC | None | 1,600 frames at 49.9 fps, 0 late, in MODE 7, 0, 1 and 2. The line-palette trailer: 750 frames at 50.0 fps, 0 late |
| [`elite/`](#game-agents) | elite-agent plays disc Elite | Keys in and a copy of its RAM out, both through shared memory | About none in flight; one or two at each launch and dock | Zero-stop play, a Deadly commander, witchspace kills |
| [`debug_example.py`](#remote-debugging) | Stop a program, look at it, change it, let it go on | Stops | One for each look | Works |
| `ttx_stream.py` | Live video in MODE 7, from a webcam or a file | Frames poked into a ring in the BBC's RAM | One a frame | Works |

*The real-hardware results were measured with an earlier build of the Pi1MHz firmware, with the
same shared memory at the same place. This build's API is checked on beebjit ([Tests](#tests)).*

To remotely peek and poke a physical BBC Micro in Python:

```python
from bbc import BBC

bbc = BBC("Pi1MHz.local")
bbc.type('PRINT "HELLO"\r')                  # type at the keyboard
print(bbc.screen())                          # read the MODE 7 screen
with bbc.stopped():                          # stop the BBC
    s = bbc.peek(0x044C, 4)                  # read 4 bytes: BASIC's S%
    bbc.poke(0x7C00, b"Hello from Python")   # write 17 bytes to screen memory
                                             # the BBC carries on here
bbc.shared_write(0, b"\x01\x02")             # shared memory: no stop
print(bbc.shared_read(0, 2))
```

## `*CLAUDE`

At the BBC's `>` prompt:

```
>*CLAUDE A LUNAR LANDER IN MODE 2
```

Ask for anything with `*CLAUDE`: Claude
writes it in BBC BASIC, types it in, runs it, reads the screen and any error, and fixes it until
it works, showing its plan on the BBC as it goes. The finished program is left typed in, ready
to `RUN`.

| While Claude works | The game it wrote |
|---|---|
| ![Claude's status page on the BBC](images/claude_page.png) | ![Lunar lander in MODE 2](images/lander.gif) |

*A real `*CLAUDE` run, captured on an emulated Model B running the real Pi1MHz firmware.*

Here it tried its physics in MODE 7 first, then ran the game in MODE 2 and checked its variables
in memory before handing it over. The game is in [`examples/lander.bas`](examples/lander.bas).

`*CLAUDE` needs a small service running on another computer on your network, such as a
Raspberry Pi 5: see [Setting up](#setting-up).

## Video at 50 frames a second

Video at 50 frames a second, in MODE 7, 0, 1 or 2. It's lossy: the encoder on the Pi 5 scales
each frame down to the mode's pixels and dithers it to the mode's colours. Then it sends only the
bytes that changed, as many as the BBC can copy in a frame. The whole video goes into the
Pi1MHz's shared memory first, and a player in the BBC's RAM copies it from there into screen
memory, with no network traffic and no stops while it plays.

**MODE 7**, teletext

<img src="images/bbb_mode7.gif" alt="Big Buck Bunny in MODE 7" width="640">

**MODE 1, with a palette for every line**

<img src="images/bbb_mode1.gif" alt="Big Buck Bunny in MODE 1 with a palette for every line" width="640">

**MODE 0**, black and white

<img src="images/bbb_mode0.gif" alt="Big Buck Bunny in MODE 0" width="640">

**MODE 2**, 8 colours

<img src="images/bbb_mode2.gif" alt="Big Buck Bunny in MODE 2" width="640">

*Captured from beebjit running the player and the real Pi1MHz firmware, at 50 frames a second,
every pixel as the BBC drew it. Big Buck Bunny (c) Blender Foundation,
[CC BY 3.0](https://creativecommons.org/licenses/by/3.0/), [peach.blender.org](https://peach.blender.org).*

On a real Model B it played 1,600 frames at 49.9 a second with none late, through MODE 7, 0, 1
and 2, and the Big Buck Bunny trailer in MODE 1 with line palettes, 750 frames at 50.0 a second,
none late. The player counts the frames it starts late, so that's measured, not claimed.

```
python3 demo50.py ~/Videos/clip.mp4 --plan 7:8,0:8,1:8,2:8      # 8 s in each mode
python3 demo50.py ~/Videos/bunny.mp4@120 --plan 1:15 --lines    # MODE 1, a palette for every line
```

Run on the Pi 5. Needs ffmpeg and numpy. A show must fit in shared memory, 4 MB; demo50 says
so if it doesn't. Afterwards it puts back the
RAM the player used (the `*KEY` strings, BASIC's program, and MMFS's workspace at `&0E00`), so
the filing system and BASIC carry on.

- **MODE 7** draws each frame with teletext characters, text and graphics, whichever fits each
  cell best ([Teletext Express](https://github.com/8bitkick/teletext-express)).
- **MODE 0** is black and white.
- **Plain MODE 1** picks the best 4 of the BBC's 8 colours for each scene.
- **MODE 2** has all 8.

### MODE 1 with a palette for every line

With `--lines`, each line of MODE 1 can change one of its four colours from the line above's,
so the screen can show all 8 of the BBC's colours at MODE 1's resolution. The player writes the
new colour in the horizontal blank before the line, timed to the cycle (the idea is Kieran
Connell's).

- **The encoder knows every op's cost to the cycle.** It places each palette change on its exact
  cycle from the frame's first op, and fills the time between with copies and pads.
- **Lines mix.** Each line passes a little of its colour error to the line below, so neighbouring
  lines mix into colours neither has: a dither down the screen as well as across.
- **The cost is copying time.** A palette change takes 60 cycles that could have copied bytes, so
  fast motion smears more than in plain MODE 1. `--hold` keeps a frame for another field rather
  than move on with rows behind.

Every frame was checked in beebjit against what the encoder meant, pixel for pixel, before it
played on the real machine.

## Remote debugging

Stop the BBC at any point, look at its registers and memory, change them, and let it carry on.
Here a BASIC program counting in `I%` is stopped, `I%` changed, and a banner written straight
into screen memory, all over HTTP.

![Remote debugging session](images/remote_debug.png)

*Captured on an emulated Model B running the real Pi1MHz firmware.*

```python
from bbc import BBC
bbc = BBC("Pi1MHz.local")
with bbc.stopped():
    i = bbc.peek(0x0424, 4)                          # BASIC's I%
    bbc.poke(0x0424, (999990).to_bytes(4, "little"))
```

`debug_example.py` is that session as a script. The Pi1MHz also serves a page with the probe's
state and Halt, Continue and Peek buttons:

<img src="images/probe_page.png" alt="The Pi1MHz probe page" width="420">

## Game agents

[elite-agent](https://github.com/8bitkick/elite-agent) plays Acornsoft Elite by reading the
machine's RAM and pressing keys. `elite/` runs it on a physical Model B, playing the flicker-free
disc version at the game's own speed, dogfights and all. On a real BBC it has played with no
stops in flight, made a commander Deadly and made kills in witchspace.

<img src="images/elite_agent.png" alt="elite-agent in a fight, watched from the side view" width="420">

*elite-agent on an emulated machine.*

It plays through shared memory, without stopping the game:

- **RAM out.** A small routine patched into Elite, where its joystick code was, copies what the
  player reads (about 570 bytes in flight) into shared memory through the services port, once a
  main-loop iteration. The player on the Pi 5 reads it with `GET /bbc/shared`, about once a frame.
- **Keys in.** Elite's key routine is patched to look its keys up in a list in the `&FD00`
  window, which shows the page of shared memory the player writes with `POST /bbc/shared` when
  the held keys change.
- **Stops** come only to patch a code just loaded, at a launch or a dock, and to type a
  commander's name: about one every 50 s over a career, none in flight.

```
ELITE_AGENT=~/elite-agent elite/build.sh
# on the BBC, at BASIC's prompt with the Elite disc in drive 0:  X%=23:CALL &FC88
SAVE=0 BOOT=1 elite/run_probe Pi1MHz.local 60 journal.jsonl
```

The player itself, its tuning and its careers are in elite-agent; `elite/build.sh` builds
against that checkout. [`elite/README.md`](elite/README.md) has the details.

Still in progress:

- **Some docked screens** can catch the player in a loop, pressing the same keys without getting
  anywhere.
- **Saving under MMFS can wreck the disc's catalogue.** MMFS keeps its copy of the catalogue at
  `&0E00`, which is Elite's workspace, so a save can write Elite's bytes over it. Run with
  `SAVE=0`.

## Setting up

### What you need

- **A BBC Micro Model B** (MOS 1.20). Typing at the keyboard is Model B only for now.
- **A Pi1MHz** on the 1 MHz bus, on your WiFi. The **RAM probe** is in
  [dp111/Pi1MHz](https://github.com/dp111/Pi1MHz)'s master ([#51](https://github.com/dp111/Pi1MHz/pull/51)).
  **Shared memory** needs its follow-up, not merged yet: the
  [`shared-memory`](https://github.com/8bitkick/Pi1MHz/tree/shared-memory) branch of
  8bitkick/Pi1MHz. Video, Elite and `*CLAUDE` need shared memory; the debugger and `agent.py`
  don't. Put the build's `Pi1MHz/6502code.bin` on the SD card with the kernel.
- **Another computer on the same network** with Python 3: a Raspberry Pi 5 works well.

### On the BBC

Once after switching on, at the `>` prompt:

```
X%=23:CALL &FC88
```

It answers `Pi1MHz probe on`. Soft Break keeps it; Ctrl-Break or power-off removes it, so type
it again after those.

> **This opens the BBC to remote code execution over your WiFi network** until Ctrl-Break or
> power-off. There's no password, as the Pi1MHz's web pages are served in the clear: use it on
> a network you trust, and `ramprobe_addr=-1` in `Pi1MHz.cfg` turns the probe off.

### On the Pi 5 (or any computer on the network)

```
git clone https://github.com/8bitkick/beeb-remote
cd beeb-remote
python3 -m venv .venv && .venv/bin/pip install anthropic
export ANTHROPIC_API_KEY=...          # a key with a spending limit; it stays on this computer
.venv/bin/python star_claude.py --host Pi1MHz.local
```

Leave it running: it puts the `*CLAUDE` command into the BBC and waits for you to type it.
`--run` also runs each finished program. Use the Pi1MHz's IP address in place of `Pi1MHz.local`
if your network doesn't find it by name.

If the Pi1MHz's web interface has a login, the probe's routes are behind it too: set
`PI1MHZ_USER` and `PI1MHZ_PASSWORD` in the environment, or pass `BBC(host, user=, password=)`.

To ask without the BBC's keyboard, from the Pi 5:

```
.venv/bin/python agent.py --host Pi1MHz.local "a lunar lander in MODE 2"
```

For video, `pip install numpy` too, and install ffmpeg.

## How it works: two ways to reach the BBC

```
 your computer                Pi1MHz (Pi Zero 2 W)                  BBC Micro Model B
 star_claude.py, demo50.py    /bbc routes
   bbc.py ───── HTTP ───────► stops: halt, peek, poke, call ◄─bus─► the probe's hooks, by IRQ or vsync
                              shared memory, 4 MB          ◄─bus─► your code, through JIM
```

**Stops, for an occasional look.** The probe stops the BBC, reads or writes its memory, and lets
it go on. A stop asks the BBC in as gently as it can: the vsync event or an IRQ, and an NMI only
with `max_rung=2`. A stop and go takes 4 ms on a Model B. That suits a debugger, typing a line,
or reading the screen now and then. Typing writes into MOS 1.20's keyboard buffer during a stop;
reading the screen reads MODE 7's screen memory; Escape sets the OS's escape flag.

**Typing and reading the screen with no stops.** `bbc.soft_install()` (or `beeb.py
soft-install`) puts a 254-byte routine into the cassette and RS423 input buffer at `&0A00`, with
one stop. It runs on every vsync, chained behind the probe's own vsync hook, and works through a
mailbox in shared memory: the client writes keys into a ring there and the routine moves them into
the keyboard buffer while there's room; on request it copies MODE 7's screen memory there, 128
bytes a vsync. From then on `type()`, `screen()`, `escape()` and `run_basic()`, and `beeb.py`'s
`cmd`, `basic` and `screen`, make no stops. Idle, it costs about 110 µs a vsync: it saves the
services port's address, reads four bytes of the mailbox, and puts the address back. A tape load
or RS423 input would write over it, so `soft_uninstall()` first (it takes itself out, with no
stop); demo50, bitmap_play and elite/run_probe do that themselves. Any Break takes it out.

**Shared memory, for a live remote application.** The Pi1MHz has 4 MB that both sides use while
the BBC runs. The application writes and reads it over HTTP (`POST` and `GET /bbc/shared`), with
no stop. Its own code on the BBC reaches it through the Pi1MHz's services port (`&FCA6-&FCA9`: an
address, then a data register that moves on by itself), or a page at a time through the `&FD00`
window. Nothing in the firmware reads those bytes: the application decides what goes where.

Why both: a remote application that stops the BBC tens of times a second shows it. On a real
Model B that many stops clicked the cassette relay, and split-screen games such as Elite
glitched, as the stops held up the interrupts that time the split. Video and Elite now run with
no stops at all.

### The memory map

The probe's memory is in the Pi1MHz's services area. `GET /bbc` gives shared memory's place and the
probe's registers' (its data buffer follows them), so clients don't hard-code them:

| Services offset | Name | Owner | What |
|---|---|---|---|
| `&800000-&BFFFFF` | shared memory | remote applications | 4 MB, written over HTTP and by the BBC's own code, with no stop: the services area's program area, now reachable over HTTP too |
| `&E00000-&E000FF` | stopped registers | the probe | the stopped machine's registers, the call result and the install marker (GET /bbc's `report`) |
| `&E00100-&E100FF` | data buffer | the probe | the bytes of the last peek or poke, 64 KB (`GET /bbc/data`) |

Shared memory has no layout of its own: one application at a time owns it, and each here picks
its own. The game agent's is an example of a convention: an input page the BBC reads, and a state
mirror the BBC writes.

| Application | Its use of shared memory |
|---|---|
| `demo50.py`, `bitmap_play.py` | the video's stream, from `+&0000` |
| `elite/run_probe` | input page at `+&0000`: the held keys, read through the `&FD00` window. State mirror at `+&0100`: Elite's RAM, written through the services port |
| `star_claude.py` | its doorbell in the last page |
| `bbc.py`'s resident routine | its mailbox below that: a control page at `size-&200`, the typing ring at `size-&300`, the screen's copy at `size-&700` to `size-&301` |

| File | What |
|---|---|
| `bbc.py` | the client library |
| `star_claude.py` | `*CLAUDE` on the BBC: a small hook on the filing system vector, watched from here |
| `agent.py`, `teletext.py` | Claude writing and testing programs; its status page |
| `demo50.py` | 50 fps video from shared memory, with `ttx_stream.py` and `bitmap_play.py` |
| `ttx_stream.py` | MODE 7 video poked into a ring in the BBC's RAM; demo50's MODE 7 encoder |
| `elite/` | the Elite player's platform: its patches, the mirror and the keys |
| `debug_example.py` | the remote debugging session |
| `asm6502.py` | the small assembler the hook and the players are built with |

## Tests

`tests/` builds [beebjit](https://github.com/scarybeasts/beebjit) with the real Pi1MHz probe
firmware patched in (`tests/beebjit/`), boots a Model B, and runs the clients against it over
HTTP, with a scripted model in place of Claude. No API key needed. It wants a Pi1MHz checkout with
the probe in `PI1MHZ` (default `~/Pi1MHz`) and a beebjit checkout in `BEEBJIT`.

```
python3 -I tests/test_agent.py
python3 -I tests/test_star_claude.py
python3 -I tests/test_demo50.py                          # a short clip: 0 late, 0 stops
python3 -I tests/test_soft.py                            # typing and reading with no stops
ELITE_AGENT=~/elite-agent elite/test_beebjit.sh          # 8 game minutes of Elite, about 12 minutes
```

The emulator counts the probe's stops (`GET /sim/stops`), so the video and Elite tests check they
made none while playing.

## API

Everything above is built on a small HTTP API on the Pi1MHz, and you can build your own
services and applications on it the same way, in any language that can make an HTTP request.

| Route | What it does | In `bbc.py` |
|---|---|---|
| `GET /bbc` | State: running or halted, the stopped registers (`null` after a rung 0 stop), the last error, how many times the BBC has been stopped (`stops`), where the stopped registers are (`report`), and shared memory (`shared`: its services offset, size, JIM address, and where the BBC's services port reads in it) | `status()`, `shared()` |
| `POST /bbc/shared?offset=` | Write shared memory, the bytes as the body, with no stop; answers `{"written": n}` | `shared_write()` |
| `GET /bbc/shared?offset=&len=` | Read shared memory, up to 4 KB a request, with no stop | `shared_read()` |
| `POST /bbc/halt?rung=` | Stop the BBC (by the vsync event, an IRQ or an NMI) | `halt()`, `stopped()` |
| `POST /bbc/continue?pc=&a=&x=&y=&p=` | Let it go on, each register optional; none can be set after a rung 0 stop, which is inside the OS | `go()`; `halt(regs=True)` skips rung 0 |
| `POST /bbc/peek?addr=&len=&bank=` | Read up to 64 KB, sideways banks too | `peek()` |
| `GET /bbc/data?offset=&len=` | The bytes the last peek read | `peek()` |
| `POST /bbc/poke?addr=&hex=` | Write up to 32 KB | `poke()` |
| `POST /bbc/jsr?addr=&a=&x=&y=` | Call code that ends with RTS | |
| `POST /bbc/run?hex=&a=&x=&y=` | Run up to 80 bytes of 6502 sent with the request | |

For example, with nothing but curl, write HELLO at the top of a MODE 7 screen and read it back:

```
curl -X POST "http://Pi1MHz.local/bbc/poke?addr=0x7C00&hex=48454C4C4F&rung=1&wait=1"
curl -X POST "http://Pi1MHz.local/bbc/peek?addr=0x7C00&len=5&rung=1&wait=1"
curl "http://Pi1MHz.local/bbc/data?offset=0&len=5"        # 48454C4C4F
```

`rung=1` stops the BBC just for that request, and `wait=1` answers once it's done. Shared memory
needs neither:

```
curl -X POST --data-binary @frames.bin "http://Pi1MHz.local/bbc/shared?offset=0"
curl "http://Pi1MHz.local/bbc/shared?offset=0&len=16"
```

In Python, `bbc.py` wraps these for you, and on top of them types at the keyboard (`type()`), reads the MODE 7 screen
(`screen()`), presses Escape (`escape()`) and runs BASIC programs (`run_basic()`).

The Pi1MHz's `docs/dev/ramprobe.md` has the details.

## Licence

GPL-3.0, like Pi1MHz. `characters.js` is the teletext font from
[teletext-express](https://github.com/8bitkick/teletext-express).
