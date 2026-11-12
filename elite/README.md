# On a real BBC Micro, through the Pi1MHz RAM probe

`elite/run_probe` plays disc Elite on a physical BBC Micro Model B. It runs the same player as
`run_sim` on another computer (a Pi, a Mac), and reaches the BBC over the network through the
[Pi1MHz](https://github.com/dp111/Pi1MHz) RAM probe ([PR #51](https://github.com/dp111/Pi1MHz/pull/51),
with the stream buffer, `POST /bbc/buffer`). There's no ROM to fit and no keyboard rig: the Pi1MHz on
the 1 MHz bus is the only hardware. The game isn't stopped while it plays: it runs at its own speed,
dogfights and all.

```sh
ELITE_AGENT=~/elite-agent elite/build.sh      # run_probe (no emulator in it)
# on the BBC, at BASIC's prompt with the Elite disc in drive 0:  X%=23:CALL &FC88
BOOT=1 elite/run_probe pi1mhz.local 60 journal.jsonl
```

`BOOT=1` loads Elite from the disc; without it the player picks up a game already running (one this
program booted earlier). The other arguments are `run_sim`'s: game minutes, and the journal. `NAME=`
and `SAVE=` work as there.

## What it does to the BBC

**Booting.** Disc Elite's first loader, `ELITE2`, copies the MOS's default vectors over all of
&0200-&0235. That takes the probe's hooks off `IRQ2V` and `EVNTV`, and the loader then reads the rest of
the game through the DFS, which takes the NMI's `&0D00`. After that the probe could never stop the
machine again. So `run_probe` types `*LOAD ELITE2`, changes one instruction in it (`CPY &FFB6`, the
table's length, becomes `CPY #6`, so the copy stops after `USERV`, `BRKV` and `IRQ1V`), and types
`CALL &2F23`, its entry point. The other vectors keep what BASIC and the DFS gave them, which is the
state any program starts in. Nothing else of the loader is changed. Before all that it types
`?&FCFD=hh:?&FCFE=64:?&FCFF=5`, which points the `&FD00` window at a page of the probe's stream buffer
(below).

**RAM out, without stopping.** The probe can't see the BBC's RAM: the 1 MHz bus carries only FRED and
JIM, and a peek stops the BBC (a rung 1 IRQ, with interrupts off for a few ms). So `run_probe` patches a
small routine into Elite that copies what the player reads to the Pi1MHz itself, through the services
port (`&FCA6-&FCA8` the address, `&FCA9` a byte a write, the address going up by one each time), into
the probe's data buffer: the buffer a peek leaves its bytes in, and the only Pi memory `GET /bbc/data`
serves. It writes from `&400` in it (services offset `&E00500`), not from its start (below). This program fetches it over HTTP about once a frame. What it copies is what
the player reads, found by its read audit (elite-agent's `cplayer/audit.h`, `run_sim_b` built with `-DREAD_AUDIT`):
zero page to `&BB`, page 3 from `FRIN` to `JSTK` (`&0311-&03CC`), `ALTIT`, and in flight the ship blocks
at `K%` (the first five always, the next seven when `FRIN` says a fifth is in use); docked, `NA%` (the
last save) instead. A sequence number goes first and again last: the two match only in a whole copy.
About 570 bytes a copy in flight (830 among many ships), 460 docked.

**Sharing the data buffer.** A peek by any client leaves its bytes in the same buffer, from its start,
and `GET /bbc/data` serves no more than the last peek's length. So:

- A client that peeks while Elite runs, then reads `/bbc/data`, gets its own bytes as long as the peek
  is no more than 1 KB (`&400`), below the mirror. A longer peek has the mirror written over its bytes
  from `&400` on, as soon as Elite next calls `RDKEY`: halt the BBC first, peek, read the data, and
  continue, and the routine can't run in between.
- run_probe's own peeks all halt first, and each stop ends with a peek of at least `&400` plus the
  mirror's length, so `/bbc/data` serves the mirror again. Another client's peek leaves it serving
  less: the copies then seem to stop, and the next check (a stop) puts it right.

The routine runs from Elite's `RDKEY`, which every key wait calls: once a main-loop iteration (from
`DOKEY`, through `DK4`), and in every screen that waits for a key (the title, the disc menu, the
market's prompts, the pause). It copies at most once a frame: only if `DL` is set, which Elite's
interrupt sets at each vsync and only `WSCAN` otherwise touches, and it clears it. So a screen that
calls `RDKEY` over and over still leaves the buffer whole most of the time. There the copy comes
just after each vsync, and this program polls once a frame too, so the two can lock, every fetch
landing in the copy: one found under way is fetched again 4 ms later. It then returns what
`RDKEY` would. It runs with interrupts on, so Elite's timer, and the dashboard's split, go on as ever.

It lives where Elite's joystick code was, which never runs on keys (`JSTK` 0, and nothing in the player
turns it on): `RDKEY`'s own 15 bytes, `DKJ1`, `TT17`'s joystick cursor (`TJS1` and before it), `DKS2`
(the ADC read), and in the docked code `DOKEY`'s joystick reads, jumping from one to the next. Moxon's
sources and `disc-ff-compile.txt` show nothing else enters any of them. The only ways in were Elite's
own tests of `JSTK`, two in each code (`TT17`'s and `DOKEY`'s), and the game can set `JSTK`: in the
pause, B toggles the Bitstik and `JSTK` with it, and the player's reload presses B. So those tests are
patched too, with the routine: `TT17`'s `LDA JSTK: BEQ TJ1` becomes `JMP TJ1`, and `DOKEY`'s `LDA JSTK`
becomes `LDA #0`. With `JSTK` set, the game now just plays on keys.

**Keys in, without stopping.** Elite reads every key through one routine, `DKS4`, which asks the System
VIA about one key. It's patched to look the key up in a list of six held keys in the `&FD00` window
instead, `CMP &FD00,X`. The window shows a page of the Pi1MHz's JIM RAM: the page of the probe's stream
buffer at offset `&500`, which this program writes with `POST /bbc/buffer` as soon as the held keys
change, with no stop. The Pi copies a page into the window when a JIM page register is written, so the
patched `DKS4` writes `&FCFF` first, and the copy routine does too. `RDKEY`, which scans every key, reads
its answer from the window (`&FD08`: the lowest key held from 16 up, as its scan would find), which this
program works out. Flight keys come with bit 7 set (`KYTB`), which the keyboard ignores and the patch
masks off. `DKS4` still returns the key number in A and X with bit 7 set if the key is down, keeps Y, and
keeps its last byte the `RTS` that `DKS1` branches to.

The window's page is set once, at BASIC's prompt before Elite loads (`BOOT=1`): nothing in Elite or
the DFS writes the page registers, and a probe stop puts back the page it found when it goes on. Its
top byte is the Pi's JIM RAM size in 16 MB less 2 (the stream buffer is 32 MB below the top); the Pi
gives the size through its status port, which this program reads in one stop at the start (`JIMHI=`
sets it instead). The docked copy includes the byte at `&FD10`, which this program sets, so it knows
the window shows its page.

**Stopping.** The BBC is stopped only to patch a code just loaded, and to type a line. Each time a
launch or a dock loads a code again (`D.CODE` and `T.CODE`, both at `&11E3`), the copies stop (the
routine went with the old code); after a second without one, `run_probe` stops the machine, checks
which code is in and that every byte it will change is as loaded (`T.CODE` is decrypted after it
loads) and that the BBC isn't in any of them, and writes the routine and `DKS4` in, in one stop. A
quiet spell without a load (a hyperspace, a save) costs a check every 1.5 s, then every 3 s. Every stop is
by rung 1, the Pi's nIRQ, which Elite's handler passes on to the OS and the OS to `IRQ2V`; it ends
with a peek of at least the mirror's length, as `GET /bbc/data` serves no more than the last peek
left (a poke, none).

**Typing.** Elite reads two things through the OS (`OSWORD 0`, in `MT26`): the commander's name, from
the disc menu, and a system's name, from a chart (`F`). Each letter, digit, SPACE and RETURN the
player presses is kept, and a line goes into the OS's keyboard buffer (`&03E0`, MOS 1.20's layout) in
one stop, once its RETURN is pressed: only while no copy comes (Elite waits in `OSWORD 0`, not in
`RDKEY`) and the BBC is in the MOS (`MT26` has flushed the buffer, which loses anything put in before).

**Timing.** The player counts game frames, a frame 20 ms of wall time less the time stopped (the
sequence number counts copies, which come once a main-loop iteration, not frames). Keys go through the
same `keyq` as `run_sim`, timed in those frames, and the player runs once a frame.

## What it costs

Measured on beebjit (Model B, MOS 1.20, DFS 0.90 on the 8271) with the probe's firmware on its test
board (`tests/beebjit`), over careers of 35 to 40 game minutes:

| | |
|---|---|
| Stops | 0.03-0.08 a second over a career (1 every 15-30 s): a check and a patch at each code load, a stop for each line typed, a check now and then in a spell without copies (a hyperspace, a save), and a few at the boot |
| Time the BBC is stopped | 0.07-0.15% (the longest stop 30-46 ms, at a patch) |
| Stops in quiet flight and in fights | none |
| Copies | about 12 a second (one each main-loop iteration), 3-6% of fetches mid-copy (fetched again a frame later) |
| Game speed | the wall's, less the stops: 35.0 game minutes in 35.0 wall minutes; the copy itself takes 3-6% of the 6502 (below) |
| Keys | written as they change, with no stop: 5,000-7,000 writes a career |
| Latency the probe adds | the player sees RAM from the last `RDKEY` (once a main-loop iteration) fetched within a frame; keys reach `DKS4` and `RDKEY` as soon as they're written |

The copy itself costs Elite some time: about 3.4 ms of the 6502's a copy in flight (5.2 ms among many
ships), 3.2 ms docked, from its instructions (the 1 MHz bus writes stretched), at about 10 copies a
second: 3-5% of the CPU, which the game loses. Over WiFi to a real Pi1MHz each request takes longer
(the probe's notes: 2.3 ms for `GET /bbc`, 4 ms for a stop and go), which delays the player's view by as
much, but doesn't slow the game.

## Limits

- **Model B disc Elite only** (the flicker-free version this player knows on the Model B), on MOS 1.20.
  The Master's SNG47 reads its keys differently and isn't patched.
- **The BBC's own keyboard is ignored by Elite** while the patch is in. Ctrl-Break gets it back.
- **No joystick.** Disc Elite on the Model B fires only from the stick's button, so the player plays it
  on keys anyway, as `run_sim_b` does; and the routine is where the joystick code was.
- **A Break takes the window's page with it**: boot again with `BOOT=1` (the page is set at BASIC's
  prompt). Without it, the keys go nowhere, and `run_probe` says so.
- **The probe's limits apply** (its `docs/dev/ramprobe.md`): don't open the Pi1MHz's help screen or SD
  explorer while it plays, and nothing else should use the services port or the JIM page registers.

## Testing it on beebjit

`elite/test_beebjit.sh` does all of it without hardware: `tests/beebjit` builds beebjit
with the probe's own firmware C on its test board (a Model B, MOS 1.20, DFS 0.90 on the 8271) and serves
the probe's HTTP routes on localhost, with disc Elite in its drive. From BASIC's prompt the test installs
the probe, boots Elite and plays 8 game minutes. It passes if the game was found, bought at Lave, saved
the commander to the disc (`E.CLAUDE` in its catalogue), launched and docked at Leesti, with no probe
errors. It also checks the flight code was patched, and that the game was stopped less than 0.5 times a
second and under 2% of the time. It takes about 10 minutes, and prints `SKIPPED` without beebjit,
elite-agent, the Pi1MHz checkout (`stream-fixes` or later: it needs the stream buffer) or the disc.

```sh
ELITE_AGENT=~/elite-agent PI1MHZ=~/Pi1MHz elite/test_beebjit.sh
```

By hand:

```sh
cp elite-disc-flicker-free-sth.ssd /tmp/elite.ssd
BEEBJIT_ARGS="-0 /tmp/elite.ssd -writeable -mutable" PI1MHZ=~/Pi1MHz tests/beebjit/run_beebjit.sh 8091 &
curl -X POST 'localhost:8091/sim/type?text=X%25%3D23%3ACALL%20%26FC88%0D'      # install the probe
BOOT=1 elite/run_probe 127.0.0.1:8091 30 journal.jsonl
```

`PROBELOG=1` also prints the routine's bytes and each line put in the OS's buffer; `PROBEREPORT=10`
reports the stops and copies every 10 seconds instead of every 60; `PROBEDEBUG=1` prints the player's
debug lines too.
