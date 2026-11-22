# ghostbox: ghosthands in a Linux container, no hardware

A small Linux computer in Docker that browses the web the way a person does, and works through
a queue of jobs you hand it. The brain and grounder are the same as ghosthands' vision lane;
the screen and the hands are virtual:

| Part | Real Mac target | ghostbox |
|---|---|---|
| Screen | your display, `screenfeed.sh` | Xvfb 1440x900 inside the container |
| Browser | Safari | Chromium, headed (not headless), persistent profile |
| Hands | Pico USB-HID | X server XTEST input: curved, eased pointer paths, per-key typing jitter |
| Watch it | look at the screen | noVNC at http://localhost:6080 (view-only by default) |

```bash
box/ghostbox up                     # build + start; prints the viewer URL
box/ghostbox do "Find the latest stable Python release on python.org" \
     -g "Go to https://www.python.org/downloads/; put the version number in answer"
box/ghostbox jobs                   # ID, state, steps, goal
box/ghostbox show ID                # step log, checkpoints, answer
box/ghostbox ok ID | abort ID       # a job paused at a checkpoint waits for you
box/ghostbox shots ID               # copy its screenshots out
box/ghostbox do "Download Detlefsen 1980 from JSTOR" --login PW_UNI   # a job that may use a vault login
cred box PW_UNI                     # (your password tool) give the box a login, confirmed on screen
box/ghostbox up --drive             # viewer can click/type too
box/make-mac-app.sh --dock          # macOS: a Ghostbox app (Dock icon) that starts it all and opens the view
```

## What the page sees

`box/smoke.py` drives a local test page that logs every event. A run shows every mouse, key,
wheel and input event as `isTrusted=true`, `navigator.webdriver=false`, and the pointer moving
along a curved path (about 120 mousemove events for three targets), not jumping. There is no
WebDriver, no DevTools port and no `--enable-automation`. (A private DevTools *pipe* exists for
the vault and navigation, below; pages cannot reach or detect it.)

Input is not where a container differs from a real computer; the environment is: no GPU (WebGL
renders through SwiftShader), Chromium rather than Chrome on Linux, a bare screen and font set.
Sites that fingerprint hard can still tell. For those, use a real computer (the Pico target).

## Jobs

Each job is `/data/jobs/<id>/` in the `ghostbox-data` volume: `job.json`, `state`
(`queued | running | waiting | done | failed | aborted | max_steps`), `log.jsonl`, a screenshot
per step, `answer.txt` when the goal asked it to find something, and `CHECKPOINT_stepNNN.txt`
when it stopped for you, and `files/` for anything it downloaded (`ghostbox files ID` copies them
out, quarantined for Gatekeeper). After each step the agent is told which downloads finished.
The planner is told to stop at money commits, 2FA, captchas and anything unexpected
(`verify_stop`); the job then shows `waiting` until `ghostbox ok` or `ghostbox abort`. This is
the model's judgement, not a code guard: in testing it once typed a simple text captcha itself.
One job runs at a time. The browser profile persists, so a site signed in once stays signed in.

## Vault and 2FA

`cred box NAME` (or any tool that POSTs `vault_import`) gives the box a login; inside, only the
agent user can read it. A job may use only the logins named with `--login`. The planner sees
names, never values, and asks for `fill_secret` with a field (`username`, `password`, `totp`).
boxd then clicks the field and, over a private DevTools pipe (`box/cdp.py`; two FIFOs in a
root-owned folder, handed to Chromium as `--remote-debugging-pipe`), checks in one session on
the one visible, focused tab that the origin is exactly `https://<the login's host>` and focus
is a writable top-level input of the right type, then inserts the value into that element. A
lookalike page, a toolbar click, a search box or a text box for a password are all refused.
An entry with an authenticator secret (`totp`) gets RFC 6238 codes; push or SMS 2FA stops the
job for the owner. Navigation goes over the same pipe as a typed navigation, http(s) only, so a
page cannot jail the agent by swallowing Ctrl+L and the omnibox cannot autocomplete a URL.

Tested against `box/phish.html`, a page that swallows Ctrl+L and fakes the clipboard (the
attack that broke the first, keystroke-based site check): every fill refused.

## Models

Any OpenAI-compatible vision model. Settings come from the environment or
`~/.config/ghosthands/box.env`:

```bash
# OpenRouter defaults (GLM-5.3-flash planner, UI-TARS grounder)
OPENROUTER_API_KEY=sk-or-...

# or z.ai directly: GLM-5.3-flash plans and grounds (measured within 2 px on the smoke page)
GH_API_KEY=...
GH_BASE_URL=https://api.z.ai/api/paas/v4
GH_PLANNER_MODEL=glm-5.3-flash
GH_GROUNDER_MODEL=glm-5.3-flash
GH_LLM_EXTRA='{"thinking":{"type":"disabled"}}'
```

## Security

- Ports 8780 (control API) and 6080 (viewer) are published on 127.0.0.1 only.
- The box's own browser can reach those ports too, so neither is open: the API requires
  `BOX_TOKEN` (random unless set) and refuses any request with an `Origin` header or a non-JSON
  body; VNC listens on loopback with a password (random unless `BOX_VNC_PASSWORD`), which
  `ghostbox watch` puts in the viewer URL.
- Chromium runs as its own user (`browser`) with an empty environment; the X server, VNC, the
  control daemon, the job worker and the agent run as `ghost` and hold the API key, token, VNC
  password and job files. A renderer exploit lands as `browser` and can read none of them.
  It can still see and act on the box's screen, which is the browser's own.
- Chromium's inner sandbox is off (Docker's default seccomp profile blocks the user namespaces it
  needs); the user split, `cap_drop: ALL` (only the start-up user switch keeps a few) and
  `no-new-privileges` stand in. `BOX_CHROME_SANDBOX=1` turns it back on for a runtime that
  allows it.
- Known limits: with the sandbox off, one renderer bug gives code as `browser`, which can then
  watch the X display (keystrokes the box types, not vault values, which go over the pipe) and
  the box's screen. The vault is plaintext JSON on the volume, readable to anyone with `docker`
  access on the host. A browser that is itself compromised sees whatever is typed into it.
- The volume holds the browser's cookies, the vault and job files. Treat it like a signed-in
  laptop.

## Files

`Dockerfile`, `compose.yml`, `entrypoint.sh` (X, window manager, VNC, Chromium, job worker,
control daemon), `boxd.py` (control daemon), `jobs.py` (queue), `ghostbox` (host CLI),
`cdp.py` (private DevTools pipe), `smoke.py` + `testpage.html` (no-LLM input check),
`phish.html` (attack page for the vault), `make-mac-app.sh` (macOS launcher),
`chromium-policy.json` (PDFs download, no prompts). The client is `ghosthands/box.py`
(`--target box`).
