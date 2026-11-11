#!/usr/bin/env python3
"""Claude writing BBC BASIC on a real BBC Micro, from another computer, through the Pi1MHz probe.

    python3 agent.py --host Pi1MHz.local "a bouncing ball"

Claude tests its program on the BBC (typing it in, running it, reading the screen), then the final
program is typed in, ready for RUN.  Needs the probe's stub in the BBC (bbc.py, install_line()),
and Anthropic credentials: ANTHROPIC_API_KEY, or a profile from `ant auth login`.
"""
import argparse
import json
import re
import sys
import threading
import time

import teletext
from bbc import BBC, ProbeError

MODEL = "claude-opus-5-5"
EFFORT = "high"
MAX_TURNS = 12
BETAS = ["server-side-fallback-2026-07-01"]   # with fallbacks="default": a declined request is re-run

SYSTEM = """\
You write BBC BASIC II programs for the user's real BBC Micro Model B (MOS 1.20, Acorn DFS, \
PAGE=&1900), and you can test them on it while you work.

Tools:
- show_plan puts your plan on the BBC's screen as a checklist, in the *CLAUDE ROM's teletext \
status page, which the user sees between your runs. Call it first, with 2 to 5 short steps \
(under 36 characters each), and again with `step` as you move on.
- run_basic types a whole program in (NEW, your lines, RUN), runs it for up to `seconds`, stops it \
with Escape if it is still going, and returns the screen and any error with its line.
- read_screen returns the screen as text. Only MODE 7 can be read as text: in graphics modes \
(0-6) you get the mode number instead, so test logic in MODE 7 (PRINT the values that matter) \
before switching to a graphics mode for the finished program.
- peek_memory and poke_memory read and write main memory, &0000-&7FFF.

Work in small steps: test a piece, fix what the run shows, then test the whole. Use at most six \
runs. Typing is quick, but keep programs short: MODE 0-2 leave about 5.75 KB for program and \
variables together.

Write plain BBC BASIC II: number every line (10, 20, 30...), no indentation, plain ASCII, no line \
longer than 230 characters. Use integer variables (x%) in loops, *FX19 to wait for the next frame, \
and avoid flicker: don't CLS every frame; erase and redraw only what moved.

Finish with a short sentence for the user and the complete final program in ONE ```basic code \
block. That program is typed into their BBC for them to RUN.
"""

TOOLS = [
    {
        "name": "show_plan",
        "description": "Show your plan on the BBC as a checklist (done steps green, the current one yellow). "
                       "Call it first, then again with the step you're on.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "plan": {"type": "array", "items": {"type": "string"}, "description": "2 to 5 short steps."},
                "step": {"type": "integer", "description": "The step you're on, from 0."},
            },
            "required": ["plan", "step"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_basic",
        "description": "Type a complete BBC BASIC program into the BBC (NEW, the numbered lines, RUN), let it "
                       "run for up to `seconds`, stop it with Escape if it's still going, and return what "
                       "happened: whether it ended by itself, the screen (MODE 7 as text) and any error.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "program": {"type": "string", "description": "Numbered BBC BASIC lines, one per line."},
                "seconds": {"type": "number", "description": "How long to let it run, 1 to 30."},
            },
            "required": ["program", "seconds"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_screen",
        "description": "The BBC's screen now: MODE 7 as 25 lines of text, or which graphics mode it's in.",
        "strict": True,
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "peek_memory",
        "description": "Read up to 1024 bytes of the BBC's memory; returns them in hex.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "address": {"type": "integer", "description": "From 0 (&0000) to 32767 (&7FFF)."},
                "length": {"type": "integer", "description": "1 to 1024."},
            },
            "required": ["address", "length"],
            "additionalProperties": False,
        },
    },
    {
        "name": "poke_memory",
        "description": "Write bytes into the BBC's main memory, &0000-&7FFF, apart from the stack (&100-&1FF) "
                       "and the probe's stub (&D00-&D7F).",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "address": {"type": "integer", "description": "From 0 (&0000) to 32767 (&7FFF)."},
                "hex": {"type": "string", "description": "The bytes, as hex digits: \"01ff3c\"."},
            },
            "required": ["address", "hex"],
            "additionalProperties": False,
        },
    },
]


class Show:
    """The *CLAUDE ROM's status page, drawn into the BBC's MODE 7 screen between runs: the bar
    with what Claude is doing and a clock, the prompt, the plan, and the code at the end."""

    def __init__(self, bbc, prompt, log=print):
        self.bbc, self.prompt, self.log = bbc, prompt, log
        self.status, self.plan, self.step, self.tail, self.footer = "is thinking", [], -1, "", "Claude via Pi1MHz"
        self.t0 = time.monotonic()
        self._stop = threading.Event()
        self._thread = None

    def _clock(self):
        return f"{int(time.monotonic() - self.t0)}s"

    def draw(self):
        try:
            if self.bbc.mode() != 7:
                self.bbc.escape()
                time.sleep(0.3)
                self.bbc.type("\rMODE 7\r")
            teletext.show(self.bbc, teletext.page(self.status, self._clock(), self.prompt, self.plan,
                                                  self.step, self.tail, self.footer))
        except ProbeError as e:
            self.log(f"[status page: {e}]")

    def thinking(self, status="is thinking"):
        """Draw the page, then keep its clock going until done() (while the API is answering)."""
        self.status = status
        self.draw()
        self._stop.clear()

        def tick():
            while not self._stop.wait(2.0):
                try:
                    teletext.show(self.bbc, bytes(teletext.bar(self.status, self._clock())).ljust(40, b" "))
                except ProbeError:
                    pass
        self._thread = threading.Thread(target=tick, daemon=True)
        self._thread.start()

    def done(self):
        if self._thread:
            self._stop.set()
            self._thread.join()
            self._thread = None


def run_tool(bbc, name, args, show=None):
    """One tool call: its result as text, and whether it failed."""
    try:
        if name == "show_plan":
            if show is not None:
                show.plan = [str(s) for s in args["plan"]][:7]
                show.step = int(args["step"])
                show.status = "has a plan"
                show.draw()
            return "shown", False
        if name == "run_basic":
            seconds = max(1.0, min(30.0, float(args["seconds"])))
            return json.dumps(bbc.run_basic(args["program"], seconds)), False
        if name == "read_screen":
            return bbc.screen(), False
        if name == "peek_memory":
            addr, length = int(args["address"]), int(args["length"])
            if not (0 <= addr <= 0x7FFF and 1 <= length <= 1024 and addr + length <= 0x8000):
                return "address and length must stay inside &0000-&7FFF, at most 1024 bytes", True
            with bbc.stopped():
                return bbc.peek(addr, length).hex(), False
        if name == "poke_memory":
            addr = int(args["address"])
            try:
                data = bytes.fromhex(args["hex"])
            except ValueError:
                return "hex must be pairs of hex digits", True
            end = addr + len(data)
            if not data or not (0 <= addr and end <= 0x8000) or (addr < 0x200 and end > 0x100) or (addr < 0xD80 and end > 0xD00):
                return "only &0000-&7FFF, and not the stack (&100-&1FF) or the probe's stub (&D00-&D7F)", True
            with bbc.stopped():
                bbc.poke(addr, data)
            return f"wrote {len(data)} bytes at &{addr:04X}", False
        return f"no tool called {name}", True
    except ProbeError as e:
        return f"the BBC didn't do it: {e}", True


def final_program(text):
    m = re.findall(r"```(?:basic|bbcbasic)?\s*\n(.*?)```", text, re.S | re.I)
    return m[-1].strip() if m else None


def converse(client, bbc, prompt, log=print, max_turns=MAX_TURNS, show=None):
    """The agent loop.  Returns Claude's last text, or None if it was declined or ran out of turns.
    With a Show, the BBC shows the status page while Claude thinks."""
    messages = [{"role": "user", "content": prompt}]
    runs = 0
    for _ in range(max_turns):
        if show is not None:
            show.thinking("is thinking" if runs == 0 else "is looking at the run")
        try:
            response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": EFFORT},
            cache_control={"type": "ephemeral"},
            betas=BETAS,
            fallbacks="default",
            )
        finally:
            if show is not None:
                show.done()
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            log(f"[declined{': ' + details.category if details is not None and details.category else ''}]")
            return None
        messages.append({"role": "assistant", "content": response.content})   # thinking blocks and all
        for block in response.content:
            if block.type == "text" and block.text.strip():
                log(block.text.strip())
        if response.stop_reason == "pause_turn":
            continue
        calls = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not calls:
            return "\n".join(b.text for b in response.content if b.type == "text")
        results = []
        for call in calls:
            log(f"[{call.name}{' ' + json.dumps(call.input)[:120] if call.input else ''}]")
            if call.name == "run_basic":
                runs += 1
                if show is not None:
                    show.status = f"is testing, run {runs}"
            text, failed = run_tool(bbc, call.name, call.input, show)
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": text, "is_error": failed})
        messages.append({"role": "user", "content": results})
    log("[stopped: too many turns]")
    return None


def type_final(bbc, text, log=print, run=False):
    program = final_program(text or "")
    if program is None:
        log("[no program in the answer]")
        return False
    lines = [l.strip() for l in program.splitlines() if l.strip()]
    if bbc.mode() == 7 and not bbc.at_prompt():
        bbc.escape()
    bbc.type("\rNEW\r" + "".join(l + "\r" for l in lines) + ("RUN\r" if run else ""))
    log(f"[typed {len(lines)} lines into the BBC{', and RUN' if run else ': type RUN'}]")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="Pi1MHz.local")
    ap.add_argument("--no-type", action="store_true", help="don't type the final program in")
    ap.add_argument("--run", action="store_true", help="and RUN it once it's typed")
    ap.add_argument("--max-rung", type=int, default=1, choices=(0, 1, 2),
                    help="the probe's ladder: 2 allows an NMI, the last resort (default 1: never)")
    ap.add_argument("prompt")
    o = ap.parse_args()
    bbc = BBC(o.host, max_rung=o.max_rung)
    try:
        if not bbc.ready():
            print(f"The probe isn't in the BBC. Type this on the BBC, once after switching it on:\n  {BBC.install_line()}")
            return 1
        if not bbc.is_model_b():
            print("This needs a Model B (MOS 1.20): typing goes through its keyboard buffer.")
            return 1
    except ProbeError as e:
        print(e)
        return 1

    import anthropic                   # here, so bbc.py and the tests don't need it
    return run_prompt(anthropic.Anthropic(), bbc, o.prompt, type_it=not o.no_type, run=o.run)


def run_prompt(client, bbc, prompt, type_it=True, run=False, log=print):
    """One prompt, start to end: Claude works on the BBC, then its program is typed in.  0 once
    Claude has answered, 1 on a failure (logged)."""
    import anthropic
    show = Show(bbc, prompt, log) if type_it else None
    try:
        answer = converse(client, bbc, prompt, log=log, show=show)
    except anthropic.AuthenticationError:
        log("No Anthropic credentials: set ANTHROPIC_API_KEY, or run `ant auth login`.")
        return 1
    except anthropic.RateLimitError:
        log("Rate limited by the API: try again shortly.")
        return 1
    except anthropic.APIStatusError as e:
        log(f"API error {e.status_code}: {e.message}")
        return 1
    except anthropic.APIConnectionError:
        log("Can't reach the Anthropic API.")
        return 1
    except ProbeError as e:
        log(f"The BBC stopped answering: {e}")
        return 1
    if answer is not None and type_it:
        program = final_program(answer)
        if program is not None:
            show.status, show.tail = "has finished", program
            show.step = len(show.plan)
            show.draw()
            time.sleep(4)                  # the finished page, a moment on screen
        type_final(bbc, answer, log, run=run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
