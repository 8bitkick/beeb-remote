# beeb-remote

Example applications built on a web API for a physical BBC Micro Model B, served by a Pi1MHz on its 1 MHz bus. It can stop
the BBC, read and write its memory, call its code and let it continue. No ROM is needed.

This lets you integrate a physical BBC Micro with anything: web services, AI, databases, other
computers. This repository includes a Python client and three applications: `*CLAUDE`, 50 fps
video, and remote debugging.

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
bytes that changed, as many as the BBC can copy in a frame. The whole video is uploaded to the
Pi1MHz first, and a player in the BBC's RAM copies from the Pi1MHz's buffer into screen memory.

| MODE 7: teletext | MODE 0: 2 colours |
|---|---|
| <img src="images/bbb_mode7.gif" alt="Big Buck Bunny in MODE 7" width="400" height="320"> | <img src="images/bbb_mode0.gif" alt="Big Buck Bunny in MODE 0" width="400" height="320"> |
| **MODE 1: a palette for every line** | **MODE 2: 8 colours** |
| <img src="images/bbb_mode1.gif" alt="Big Buck Bunny in MODE 1 with a palette for every line" width="400" height="320"> | <img src="images/bbb_mode2.gif" alt="Big Buck Bunny in MODE 2" width="400" height="320"> |

*Captured from beebjit running the player and the real Pi1MHz firmware; not yet run on a real
BBC. Big Buck Bunny (c) Blender Foundation, [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/),
[peach.blender.org](https://peach.blender.org).*

```
python3 demo50.py ~/Videos/clip.mp4 --plan 7:8,0:8,1:8,2:8      # 8 s in each mode
python3 demo50.py ~/Videos/bunny.mp4@118 --plan 1:15 --lines    # MODE 1, a palette for every line
```

Run on the Pi 5. Needs ffmpeg and numpy, and Pi1MHz firmware with the stream buffer (see
[Setting up](#setting-up)).

- **MODE 7** draws each frame with teletext characters, text and graphics, whichever fits each
  cell best ([Teletext Express](https://github.com/8bitkick/teletext-express)).
- **MODE 0 and plain MODE 1** pick the best 2 or 4 of the BBC's 8 colours for each scene.
- **MODE 2** has all 8.

### MODE 1 with a palette for every line

With `--lines`, each line of MODE 1 can change one of its four colours from the line above's,
so the screen can show all 8 of the BBC's colours at MODE 1's resolution. The player writes the
new colour in the horizontal blank before the line, timed to the cycle (the idea is Kieran
Connell's).

- **Bytes and colours change together.** A screen byte means nothing without its line's colours,
  so a row's new bytes are copied after the raster has drawn it in one frame and before it comes
  back in the next, when the new colours arrive too.
- **Rows wait whole.** A row that doesn't fit in a frame keeps its old bytes and old colours
  until a frame has room. A scene cut wipes in from the top over a few frames, rather than
  showing old bytes through new colours.
- **The dither** is ordered, with a blue-noise map (no cross-hatching), with colours mixed
  nearer linear light (a dither mixes light: half white, half black looks a light grey).
- **The encoder sends as little as it can.** A pixel keeps its colour while it's still close
  enough, and lines pass only a little of their error to the line below, so a change in one
  part of the picture doesn't ripple down the screen.

The encoder knows every op's cost to the cycle, and every frame of the capture above was checked
in beebjit against what the encoder meant, pixel for pixel.

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

## Game agents (in progress)

[elite-agent](https://github.com/8bitkick/elite-agent) plays Acornsoft Elite by reading the
machine's RAM and pressing keys. It plays the Model B's flicker-free disc version, so far beside
an emulator. Connecting it to a physical BBC Micro through this API is in progress.

<img src="images/elite_agent.png" alt="elite-agent in a fight, watched from the side view" width="420">

*elite-agent on an emulated machine.*

## Setting up

### What you need

- **A BBC Micro Model B** (MOS 1.20). Typing at the keyboard is Model B only for now.
- **A Pi1MHz** on the 1 MHz bus, on your WiFi, running firmware with the **RAM probe**: the
  [`ramprobe-review`](https://github.com/8bitkick/Pi1MHz/tree/ramprobe-review) branch of
  8bitkick/Pi1MHz, the probe as proposed upstream in [dp111/Pi1MHz#51](https://github.com/dp111/Pi1MHz/pull/51)
  with that review's changes. Video also needs the stream buffer:
  [`stream-review`](https://github.com/8bitkick/Pi1MHz/tree/stream-review), one commit on top of
  `ramprobe-review`. Put that build's `Pi1MHz/6502code.bin` on the SD card with the kernel.
- **Another computer on the same network** with Python 3: a Raspberry Pi 5 works well.

### On the BBC

Once after switching on, at the `>` prompt:

```
X%=24:CALL &FC88
```

It answers `Pi1MHz probe on`. Soft Break keeps it; Ctrl-Break or power-off removes it, so type
it again after those.

> **This opens the BBC to remote code execution over your WiFi network** until Ctrl-Break or
> power-off. Use it on your own network. `probe_token` in `Pi1MHz.cfg` asks for a word on every
> request; pass it with `--token`.

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

To ask without the BBC's keyboard, from the Pi 5:

```
.venv/bin/python agent.py --host Pi1MHz.local "a lunar lander in MODE 2"
```

For video, `pip install numpy` too, and install ffmpeg.

## How it works

```
 your computer                     Pi1MHz (Pi Zero 2 W)           BBC Micro Model B
 star_claude.py, demo50.py ...     RAM probe: /bbc routes ──────► small stub at &0D00
   bbc.py ──────── HTTP ─────────► halt, peek, poke, call ◄─bus─► the machine
```

Each stop asks the BBC in as gently as it can: the vsync event first, then an IRQ, and an NMI
only with `max_rung=2`. Typing writes into MOS 1.20's keyboard buffer during a stop; reading the
screen reads MODE 7's screen memory; Escape sets the OS's escape flag.

| File | What |
|---|---|
| `bbc.py` | the client library |
| `star_claude.py` | `*CLAUDE` on the BBC: a small hook on the filing system vector, watched from here |
| `agent.py`, `teletext.py` | Claude writing and testing programs; its status page |
| `demo50.py` | 50 fps video through the stream buffer, with `ttx_stream.py` and `bitmap_play.py` |
| `debug_example.py` | the remote debugging session |
| `asm6502.py` | the small assembler the hook and the player are built with |

## Tests

`tests/` builds [beebjit](https://github.com/scarybeasts/beebjit) with the real Pi1MHz probe
firmware patched in (`tests/beebjit/`), boots a Model B, and runs the clients against it over
HTTP, with a scripted model in place of Claude. No API key needed. It wants a Pi1MHz checkout on
the `ramprobe-review` or `stream-review` branch in `PI1MHZ` (default `~/Pi1MHz`) and a beebjit checkout
in `BEEBJIT`.

```
python3 -I tests/test_agent.py
python3 -I tests/test_star_claude.py
```

## API

Everything above is built on a small HTTP API on the Pi1MHz, and you can build your own
services and applications on it the same way, in any language that can make an HTTP request.
Stop the BBC, read and write its memory, call its code, and let it carry on:

| Route | What it does | In `bbc.py` |
|---|---|---|
| `GET /bbc` | State: running or halted, the stopped registers, the last error | `status()` |
| `POST /bbc/halt?rung=` | Stop the BBC (by the vsync event, an IRQ or an NMI) | `halt()`, `stopped()` |
| `POST /bbc/continue?pc=&a=&x=&y=&p=` | Let it go on, each register optional | `go()` |
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

`rung=1` stops the BBC just for that request, and `wait=1` answers once it's done.

In Python, `bbc.py` wraps these for you, and on top of them types at the keyboard (`type()`), reads the MODE 7 screen
(`screen()`), presses Escape (`escape()`) and runs BASIC programs (`run_basic()`).

The Pi1MHz's [`docs/dev/ramprobe.md`](https://github.com/8bitkick/Pi1MHz/blob/ramprobe/docs/dev/ramprobe.md) has the details.

## Licence

GPL-3.0, like Pi1MHz. `characters.js` is the teletext font from
[teletext-express](https://github.com/8bitkick/teletext-express).
