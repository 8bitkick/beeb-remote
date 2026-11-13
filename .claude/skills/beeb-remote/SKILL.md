---
name: beeb-remote
description: Drive a real BBC Micro over the network through the Pi1MHz RAM probe - type commands, read the screen, peek and poke memory, run BASIC, play video (demo50) or Elite (elite/run_probe). Use whenever a task touches the physical BBC or the Pi1MHz, including checking a disc, loading firmware, or testing beeb-remote's apps on hardware.
---

# Driving the BBC through the Pi1MHz probe

The BBC is a real Model B (MOS 1.20, MMFS on a user-port Turbo MMC) with a Pi1MHz on its 1 MHz
bus at `Pi1MHz.local`. Everything goes over HTTP to the probe; `bbc.py` wraps it and `beeb.py`
gives one checked command per action. The user owns the hardware and is usually listening and
watching: say what you are about to do on the BBC before doing it.

## The one rule: stops click the relay

Every probe operation on BBC RAM (peek, poke, halt, typing, reading the screen) is a **stop**:
the BBC freezes for milliseconds and its cassette relay clicks. It was found by ear: page switches,
interrupts off, 1 MHz bus polling and shared-memory traffic are all silent; stops, by any rung, click.

- **Shared memory never stops the BBC:** `bbc.shared_write()` / `shared_read()` (HTTP
  `POST/GET /bbc/shared`). Anything live (video, games, frequent reads) must go through it.
- **For everyday typing and reading, put the resident routine in once:** `./beeb.py soft-install`
  (one stop, after the probe's install line, and again after any Break). From then on `cmd`,
  `basic` and `screen` take **no stops**: they go through shared memory, and `cmd` takes any
  length. `./beeb.py status` ends with "soft typing in" while it is. `./beeb.py soft-uninstall`
  takes it out with no stop. It lives at `&0A00`, the cassette/RS423 input buffer: take it out
  before loading from tape or using RS423 input, and before `*CLAUDE` (star_claude.py's hook
  wants `&0A00` too). demo50, bitmap_play and elite/run_probe take it out themselves.
- Without it, typing and screen reads cost about one stop each. Never poll the screen in a loop;
  wait a calculated time, then read once (`beeb.py cmd` does this).
- Never drive anything real-time with stops. Ask before any run that stops the BBC repeatedly.
- `GET /bbc`'s `stops` counts them: check it before and after anything you claim is stop-free.

## Commands

```sh
export PI1MHZ_HOST=pi1mhz.local
./beeb.py status                 # state, installed, build, stops, shared memory
./beeb.py soft-install           # once: typing and reading with no stops (one stop now)
./beeb.py cmd "*CAT" --expect 1  # one line, its output; exit 1 on a BBC error (<= 29 chars without soft-install)
./beeb.py screen                 # MODE 7 screen as text (graphics modes can't be read)
./beeb.py basic prog.bas         # NEW, type it, RUN, screen
./beeb.py peek 0x70 16 ; ./beeb.py poke 0x5000 A92A60
./beeb.py stops
```

`cmd` raises on what MOS, BASIC and MMFS print when something fails (Bad command, Not found,
Bad Sum, Mistake, Cat full, ...). **Check every command's result before the next step, and never
start a step that depends on a check in the same tool call as the check.**

## Facts that cost time to learn

- **The probe must be installed:** `X%=23:CALL &FC88` typed on the BBC by the user (you can't
  type before it's installed). `GET /bbc` `installed:true` confirms. A soft Break reinstalls it
  via `*KEY10`, unless a program overwrote `&0B00` (Elite and demo50 do; demo50 now restores it).
  If the install prints only blank lines, ask for `GET /bbc`'s `error` and a Pi restart.
- **MMFS:** `*DIN 0 5` puts disc 5 in drive 0. Disc 5 "ELITE-FF" holds flicker-free disc Elite.
  "Bad Sum" means MMFS's workspace at `&0E00` was overwritten: Ctrl-Break clears it. Formatting
  (`*DOP`, `*FORM`) or writing discs needs the user's explicit OK.
- **Elite** (`elite/run_probe`, built with `elite/build.sh` against an elite-agent checkout):
  `*DIN 0 5`, check `*CAT` lists `ELITE2`, then `SAVE=0 BOOT=1 elite/run_probe pi1mhz.local 20 log.jsonl`.
  `CMDR=deadly.cmdr` starts as a Deadly commander. Always `SAVE=0`: saving under MMFS can wipe
  the disc's catalogue. Elite reads keys from its patched key routine, not the keyboard, so the
  user can't type while it runs; Ctrl-Break ends it.
- **Video** (`demo50.py`): encodes (cached in `~/Videos`), uploads into shared memory, plays at
  50 fps with no stops. Shared memory is 4 MB, so about 15 s in any mode. MODE 0 looks best;
  line-palette MODE 1 smears on fast cuts.
- **Firmware:** load a kernel without touching the card with
  `curl -T kernel7.img http://pi1mhz.local/kernel.now` (Pi Zero 2 W: `kernel7.img`). A power
  cycle boots the card's kernel. The card's `6502code.bin` must match the running kernel's
  version or the install refuses. Writing anything to the card needs the user's OK. After any
  Pi restart the user must Ctrl-Break and reinstall.
- **Processes:** kill only your own PIDs. `pkill -f` matches your own shell and other agents' runs.

## When something goes wrong

Read the screen (`./beeb.py screen`) and `./beeb.py status` before guessing. Report what the BBC
actually printed. If the BBC is stuck in a program, ask the user for Ctrl-Break; you can't press it.
