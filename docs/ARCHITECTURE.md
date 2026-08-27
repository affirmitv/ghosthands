# Architecture

Four moving parts, each swappable:

- **Eyes (`scripts/screenfeed.sh` + `eyes.py`).** A tiny shell loop screenshots the display to a
  file; `Eyes.capture()` triggers a fresh grab and returns the path. On macOS it MUST run inside a
  GUI Terminal — the Screen-Recording TCC grant does not attach to an SSH-spawned process, but
  children of Terminal.app inherit it. Frames are downscaled (default 1600px wide) so model calls
  are fast and cheap; grounding is resolution-independent so accuracy is unaffected.
- **Brain (`brain.py::Planner`).** A vision LLM. Given the goal, a playbook, the action history,
  and the current screenshot, it returns ONE next action as strict JSON. Default
  `z-ai/glm-5.3-flash` — cheap, fast, vision-capable.
- **Eyes→coords (`brain.py::Grounder`).** A GUI grounding model (default `bytedance/ui-tars-1.5-7b`)
  that turns a natural-language target into a pixel coordinate.
- **Hands (`hands.py::PicoHands`).** Real USB-HID injection via the Pico. Swap in `DryRunHands` to
  log actions without moving anything.

`agent.py::Agent.run(goal, guide)` is the loop: capture → plan → (ground if click) → act → append
to history → repeat, with per-step screenshots, a jsonl log, and money checkpoints.

### Notes
- Browser navigation uses AppleScript (`set URL of front document`) rather than typing into the
  address bar — it is far more reliable than HID-typing a URL.
- Long `type` strings are chunked to ≤18 chars; the HID stack drops characters on long bursts.
- Keyboard `pagedown`/arrows often scroll where a synthetic wheel event is ignored.
