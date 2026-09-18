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

## Jev fast lane (2026-09)

A second brain sits beside the vision planner. `ghosthands/dom_reader.py` reads the front Safari
tab through the AppleScript `do JavaScript` bridge and returns an indexed table of visible
controls (label, role, value, viewport rect, window position, screen size). `Element.screen_point`
turns a rect into screen fractions the hands already understand, so no grounding call is needed.
`ghosthands/jev.py` posts that table as `state` to TypeSafe's Jev decision model (OpenRouter
`/api/alpha/decisions`, model `typesafe/jev-1.13`) with three choice questions: `operation`,
`click_target`, `type_target`. Only operations with a live target are offered. Answers carry
probabilities; `JevPlanner` maps them to the same plan dicts the vision `Planner` emits, adds a
`point`, and downgrades any low-confidence step to `verify_stop`. `Agent._point` prefers a plan's
`point` over the grounder, and the agent skips the screenshot when the planner sets
`needs_frame = False`. `TYPE_TEXT` values come from the playbook's own literals (Jev picks one) or,
failing that, from a small text model. The design follows browser-use/jev-ultrafast; the execution
stays on real HID.

