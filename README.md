# ghosthands

**Give an AI agent real hands and eyes.** ghosthands drives a *real* computer screen the way a
person does: it looks at the screen, decides what to do, and moves a real mouse and keyboard over
**USB HID** — so the operating system cannot tell it from a human, and it works on **any** app,
not just a browser DOM.

> **The Jev fast lane is optional and experimental.** The vision lane is the default and the
> one to rely on. The Jev lane (`--planner jev`) is still under development: it is fast and cheap
> on short, single-goal web tasks in Safari, but it can still stop early or loop on long,
> multi-step tasks. The premise holds (a decision model choosing operation and target from an
> element table, at about $0.0001 and 0.4 s per step); the loop around it is still being hardened.

## What's new (2026-09-24)

The Jev fast lane is still **optional and experimental**; the vision lane remains the default.
Two merged changes hardened the loop on short web tasks:

- **PR #3 (verified DONE, scroll guard).** Jev's DONE is now checked by a small text model with a
  strict yes or no; a false DONE is rejected and the step is asked again. When the playbook says
  "DONE when ...", the check runs after every action, so the run stops as soon as the condition
  is met. A scroll guard allows at most 3 scrolls in a row (or 1 that changed nothing). Before a
  wheel scroll the pointer is parked over the page, which was the real cause of the old scroll
  loop, and a scroll moves 3 notches by default. The text model's reasoning is capped so it never
  returns an empty answer. Targets are guide-aware, and "Type X into ..." literals are taken from
  the guide.
- **PR #4 (dead-click guard, loading wait).** A click that changes nothing marks the control dead
  and drops it from Jev's table; two dead clicks in a row lead to a scroll. When a loading signal
  appears (aria-busy, a progress bar, a disabled submit, or text such as "Loading", "Reading",
  "Please wait"), the loop polls for up to `GH_JEV_LOADING_WAIT_S` (default 45 s) without spending
  decisions. A WAIT that leaves the page unchanged no longer trips the confidence gate.

Live benchmark on a real Mac (Safari, hardware mouse), 5 short web tasks:

| Run | Tasks passed | Cost per task, all-in |
|---|---|---|
| 2026-09-23 night, before #3 | 1/5 | well under $0.002 |
| after #3 | 3/5 | well under $0.002 |
| after #4 | 5/5 | well under $0.002 |

New flags: `GH_JEV_VERIFY_DONE` (default on, `0` turns it off), `GH_JEV_MAX_SCROLLS` (default 3,
`0` turns the guard off), `GH_JEV_SCROLL_NOTCHES` (default 3), `GH_JEV_DEAD_CLICK_GUARD` (default
on, `0` turns it off), `GH_JEV_LOADING_WAIT_S` (default 45, `0` turns it off). Details are in [The Jev fast lane](#the-jev-fast-lane).

It is three cheap parts:

| Part | Role | Default |
|------|------|---------|
| 🧠 **Brain (fast lane, optional, experimental)** | **Jev**, TypeSafe's decision model: reads an indexed table of the controls on screen and answers "which operation, which element" with calibrated probabilities in ~300 ms | `typesafe/jev-1.13` |
| 🧠 **Brain (vision lane, default)** | a small *vision* LLM that sees a screenshot and picks the next action; works on anything with a display (native apps, games, BIOS, any web page) | `z-ai/glm-5.3-flash` |
| 👁️ **Eyes** | a GUI *grounding* model that turns "click the blue Create button" into an (x, y) | `bytedance/ui-tars-1.5-7b` |
| ✋ **Hands** | a $4 Raspberry Pi Pico flashed as a USB-HID mouse+keyboard | Pico over serial |

All of it runs over OpenRouter by default (Jev through OpenRouter's decisions endpoint, the LLMs
through chat completions). On the fast lane a click step is one Jev call: about **$0.0001 and 0.4 s**,
no screenshot and no grounding call, because the element table already carries coordinates. A
typing step adds one more Jev call to pick the literal, and a small text-model call only when
the playbook holds no literal for that field. The
expensive frontier model that *orchestrated* the task is out of the loop; the loop runs on pennies.

## Why real HID instead of software automation

- **Undetectable.** The mouse and keystrokes are indistinguishable from a human's — no `WebDriver`
  flag, no synthetic-event fingerprint, no accessibility hooks. Bot detectors that block Selenium
  and Playwright see nothing unusual.
- **DOM-independent.** It reads *pixels*, not HTML. It drives native apps, Electron, games,
  remote desktops, a BIOS screen — anything with a display.
- **Cheap brain.** Grounding + a small vision planner replace an expensive agent doing per-frame
  reasoning. You supervise; the nickels drive.

## Quickstart

```bash
pip install -r requirements.txt                       # pyserial
export OPENROUTER_API_KEY=sk-or-...

# 1) Build the hands: flash a Raspberry Pi Pico  (see docs/HARDWARE.md)
# 2) Start the eyes IN A GUI TERMINAL (macOS screen-recording grant must attach):
bash scripts/screenfeed.sh

# 3) Prove the hands work (no LLM):
python3 examples/trace_square.py

# 4) Run a task on the vision lane (any app):
python3 run.py --goal "Open TextEdit and type hello" \
               --guide "Use Spotlight (cmd+space) to open TextEdit, then type."

# 5) Optional, experimental: run a task on the Jev fast lane
#    (Safari; enable Develop > Allow JavaScript from Apple Events):
python3 run.py --planner jev \
               --goal "Open the schedule for the Oakland 14U Duckett team in this tournament." \
               --guide "Open the TEAMS tab, click the team, DONE when its games are listed."
# No Pico yet? --hands cliclick (brew install cliclick) moves your real pointer so you can watch the loop.
```

## The Jev fast lane

**Status: optional and experimental, still under development.** The vision lane is the default.
Use the Jev lane for short, single-goal tasks on a Safari page, where it is fast and cheap. On
long, multi-step tasks it can still stop early on the wrong page or loop (scroll again and again,
or click past the result it was sent to find). Two guards below reduce both, but they do not make
it as dependable as the vision lane yet. The premise holds: a decision model choosing the
operation and the target from an element table costs about $0.0001 and 0.4 s per step.

[browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) showed the shape:
every observation becomes a **numbered element table**, and one request to
[TypeSafe's Jev](https://docs.typesafe.ai/introduction) answers the operation and the target at
once, with probabilities. ghosthands borrows that loop and keeps its own hands. Where
jev-ultrafast executes inside Chrome through the DevTools protocol, ghosthands reads the table
from Safari through the AppleScript JavaScript bridge (the page never sees a WebDriver) and
executes with a real USB mouse and keyboard.

```
  Safari front tab ──▶ dom_reader: [1] tab SCHEDULE · [2] tab TEAMS · [3] textbox Search …
                                     │  labels, roles, values, screen coordinates
                                     ▼
                       ONE Jev request:  operation? click_target? type_target?
                                     │  {"CLICK": 0.72, "SCROLL_DOWN": 0.16, …}
                                     ▼
              CLICK [2] ──▶ HANDS move to the element's center ──▶ click
              TYPE_TEXT [3] ──▶ pick the literal from the playbook (Jev) or write it (small LLM)
```

Measured on 2026-09-18 through OpenRouter, one decision each, `usage.cost` as billed:

| Decision | Model | Input tokens | Output tokens | Cost | Latency |
|---|---|---|---|---|---|
| Vision lane: one 1170x2532 screenshot + playbook | `z-ai/glm-5.3-flash` ($0.09/M in, $0.30/M out) | 5,506 | 336 | $0.00093 | 8.7 s |
| Jev fast lane: 18-control tournament page + playbook | `typesafe/jev-1.13` ($0.042/M in, output free) | 2,387 | 223 | $0.00010 | 0.38 s |

That is **9x cheaper and 23x faster per decision**, and the vision lane still needs a grounding
call to turn "the SCHEDULE tab" into a pixel before it can click. The fast lane does not: the
element table already knows where every control is. Reading the table takes 0.08 s.

Whole task, live, 2026-09-18 08:06 PT, real pointer, Safari on a Mac: "Show the games for the
Lakeshow 14U Boyd team" on a Buzzer Beater Events tournament page (an Angular site whose team rows
are plain divs with a pointer cursor, no links, no API). Nine decisions, **10 seconds wall clock,
$0.0015 total**, mean decision latency 0.32 s: open TEAMS, scroll six times, click the team row,
DONE at p=0.98 with the games on screen. The step log is in the pull request.

What Jev adds beyond speed:

- **Calibrated probabilities.** Every answer comes with a distribution. `GH_JEV_MIN_CONFIDENCE`
  (default 0.35) and `GH_JEV_MIN_TARGET_CONFIDENCE` (default 0.15) turn a noise-level operation or
  target into a `verify_stop`. Money is gated deterministically: a click on a control whose label
  reads Pay, Save, Activate, Confirm, Apply, Update, Submit, Delete (and friends) always pauses once
  for a human, whatever the model's confidence. The money rules in [Safety](#safety) now have code
  behind them instead of a prompt.
- **Only real choices are offered.** If nothing on screen can be typed into, `TYPE_TEXT` is not on
  the menu. If nothing is clickable, neither is `CLICK`.
- **Text is separate from decisions.** Jev never writes prose. For `TYPE_TEXT` the planner first
  asks Jev to pick among the literals already in the playbook (a product ID, a price, an email);
  only when none fits does a small text model write the value.

- **Verified DONE.** Jev's DONE is not taken on its word. The small text model is asked a strict
  yes or no ("is the goal's DONE condition satisfied on this page?") given the goal, the playbook,
  the page title, URL, visible text and element table. A no is written into the recent actions Jev
  sees ("DONE rejected by check: ...") and the step is asked again without DONE on the menu. When
  the playbook states its own condition ("DONE when ...", "DONE as soon as ..."), the same check
  runs after every action, so the run stops as soon as the condition shows instead of clicking
  past it. At most one check per step. If the check itself fails, Jev's answer stands.
  `GH_JEV_VERIFY_DONE=0` turns it off.
- **Scroll guard.** After `GH_JEV_MAX_SCROLLS` (default 3) scrolls in a row, or after one scroll
  that left the element table unchanged, the next decision is not offered SCROLL and Jev is told
  "scrolled 3 times without progress; choose a control". `GH_JEV_MAX_SCROLLS=0` turns it off.
  A Jev scroll first parks the pointer over the page (wheel input goes to whatever is under the
  pointer) and moves `GH_JEV_SCROLL_NOTCHES` (default 3) notches, under one viewport on the
  tested Mac, so no row slips past between two reads of the table.
- **Dead-click guard.** A CLICK that leaves the page unchanged once the settle time passes (same
  element table, URL and title) marks that control dead for the rest of the run: it is dropped
  from the table Jev picks from and Jev is told "clicked [N] label; nothing changed; choose
  something else". After two dead clicks in a row the next step scrolls down without a decision
  (unless the scroll guard is holding scrolls back). Text fields and dropdowns are never marked
  dead (a click on them only focuses). `GH_JEV_DEAD_CLICK_GUARD=0` turns it off.
- **Loading wait.** After a CLICK or Enter, a loading signal that was not on the page before (a
  disabled submit, `aria-busy`, a progress bar, a control or status region reading "Loading...",
  "Reading it...", "Please wait") is polled every 0.5 s without Jev decisions until it clears,
  the URL changes, or `GH_JEV_LOADING_WAIT_S` (default 45) passes. A low-confidence decision
  right after a WAIT that left the page unchanged waits again (twice at most) instead of pausing
  for a human. `GH_JEV_LOADING_WAIT_S=0` turns both off.

Configuration: `GH_PLANNER=jev`, `GH_JEV_MODEL` (default `typesafe/jev-1.13`; `~typesafe/jev-latest`
tracks the newest), `GH_JEV_URL` (default OpenRouter's `/api/alpha/decisions`; point it at
`https://api.typesafe.ai/v1/systemone` with a TypeSafe key to go direct), `GH_JEV_TEXT_MODEL`,
`GH_JEV_MIN_CONFIDENCE`, `GH_JEV_VERIFY_DONE`, `GH_JEV_MAX_SCROLLS`, `GH_JEV_SCROLL_NOTCHES`, `GH_JEV_DEAD_CLICK_GUARD`, `GH_JEV_LOADING_WAIT_S`. The vision lane is unchanged
and remains the default.

## Why this exists: Firmi and the systems with no API

ghosthands is the hands behind [Firmi](https://firmi.ai), the agent that runs a youth sports
club's app. Most of what a club depends on lives in systems that were never going to ship an API:
tournament sites that publish brackets as HTML tables, App Store Connect and Google Play Console
screens that only exist as web pages, league registration portals, gym scheduling pages. When a
tournament director moves a game, Firmi has to notice and update every parent's app, and the
only interface to that fact is a web page built for a person with a mouse.

The vision lane made this possible. The Jev fast lane makes it routine: a decision that cost a
tenth of a cent and most of ten seconds now costs a hundredth of a cent and a third of a second,
which is the difference between checking a tournament site a few times a day and checking it
every few minutes for every team in the club. Legacy integrations stop being a project and become
a playbook.

If you run an operation that depends on old-school systems, this is the loop that drives them
like a person would, for pennies. Stars and issues welcome; the code is short enough to read in
one sitting.

## How the loop works

```
  capture screen ──▶ BRAIN (vision LLM) picks ONE action as JSON
        ▲                         │
        │              ┌──────────┴───────────┐
        │           click?                  type/key/scroll/navigate/wait
        │              │                        │
        │          EYES (grounding model)       │
        │        "the X" ──▶ (x, y) fraction    │
        │              │                        │
        └────────── HANDS (Pico USB-HID) ◀───────┘
```

Every step is logged to `~/gh-runs/<run>/log.jsonl` with a screenshot per step. The brain pauses
at **money checkpoints** (see Safety) so a human can eyeball an irreversible click.

## Scroll

The Pico sends wheel input as many small 1-unit reports on a lightly-jittered ease-in/ease-out
cadence (a human flick, not a machine burst), so it stays smooth in native apps and custom web
scroll containers (the Instagram feed included). `{"scroll":5}` scrolls 5 notches; the sign is
the raw wheel direction and which way the page moves follows the host's scroll-direction setting
(on the tested macOS, `+` scrolled the page down). The richer form
`{"scroll":{"amount":5,"steps_per_notch":6,"smooth":true}}` tunes travel per notch, and
`{"scrolltest":n}` scrolls one way then back so you can watch it.

## Safety

- The planner is instructed never to commit a price/charge until it has read the value back and
  confirmed it. When it wants a human to look, it emits `verify_stop`; the agent pauses and waits
  for you to `touch <run_dir>/CONTINUE` (or `ABORT`).
- Real HID means real consequences: it can click anything on your screen. Run tasks you would be
  comfortable doing yourself, watch the log, and keep the `ABORT` flag handy.
- This is an automation tool. Respect the terms of service of whatever you point it at.

## Layout

```
ghosthands/        core library  (config, eyes, hands, brain, agent)
  jev.py           Jev fast lane: JevDecider (decisions endpoint), JevPlanner (drop-in brain)
  dom_reader.py    Safari element table with screen coordinates (no screenshot, no WebDriver)
run.py             CLI  (--planner vision|jev, --hands pico|dryrun|cliclick)
tests/             offline unit tests  (python3 -m unittest discover tests)
examples/          trace_square.py, streamon3_subscriptions.py
scripts/           screenfeed.sh (the eyes) + firmware/ (the hands)
docs/              HARDWARE.md, ARCHITECTURE.md
```

MIT licensed. Built as the screen-driving component behind Firmi and AppSpace's autonomous ops.
The Jev fast lane follows the design published by Browser Use in
[jev-ultrafast](https://github.com/browser-use/jev-ultrafast) (MIT).
