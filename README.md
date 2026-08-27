# ghosthands

**Give an AI agent real hands and eyes.** ghosthands drives a *real* computer screen the way a
person does: it looks at the screen, decides what to do, and moves a real mouse and keyboard over
**USB HID** — so the operating system cannot tell it from a human, and it works on **any** app,
not just a browser DOM.

It is three cheap parts:

| Part | Role | Default |
|------|------|---------|
| 🧠 **Brain** | a small *vision* LLM that sees the screen and picks the next action | `z-ai/glm-5.3-flash` |
| 👁️ **Eyes** | a GUI *grounding* model that turns "click the blue Create button" into an (x, y) | `bytedance/ui-tars-1.5-7b` |
| ✋ **Hands** | a $4 Raspberry Pi Pico flashed as a USB-HID mouse+keyboard | Pico over serial |

Both models run over any OpenAI-compatible endpoint (OpenRouter by default). A full step —
screenshot → decide → locate → click — costs roughly **$0.0002**. The expensive frontier model
that *orchestrated* the task is out of the loop; the loop runs on nickels.

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

# 4) Run a task:
python3 run.py --goal "Open TextEdit and type hello" \
               --guide "Use Spotlight (cmd+space) to open TextEdit, then type."
```

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
run.py             CLI
examples/          trace_square.py, streamon3_subscriptions.py
scripts/           screenfeed.sh (the eyes) + firmware/ (the hands)
docs/              HARDWARE.md, ARCHITECTURE.md
```

MIT licensed. Built as the screen-driving component behind AppSpace's autonomous ops.
