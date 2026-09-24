"""Brain: a Planner (vision LLM that decides the next action) and a Grounder
(GUI model that turns a described target into a pixel coordinate). Any
OpenAI-compatible endpoint works; defaults are cheap models on OpenRouter."""
import json, base64, re, time, urllib.request
from .config import Config

def _chat(model, messages, max_tokens=400, temperature=0.0, extra=None, usage_out=None):
    """One chat completion. `extra` merges into the request body (e.g. a reasoning cap);
    `usage_out`, a list, receives the response's usage dict when given."""
    body = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "messages": messages}
    if extra:
        body.update(extra)
    last = "no response"
    for attempt in range(4):
        try:
            req = urllib.request.Request(Config.base_url.rstrip("/") + "/chat/completions",
                data=json.dumps(body).encode(),
                headers={"Authorization": "Bearer " + (Config.api_key or ""), "Content-Type": "application/json",
                         "HTTP-Referer": "https://github.com/affirmi/ghosthands", "X-Title": "ghosthands"})
            r = json.load(urllib.request.urlopen(req, timeout=120))
            c = (((r.get("choices") or [{}])[0]).get("message") or {}).get("content")
            if usage_out is not None and isinstance(r.get("usage"), dict):
                usage_out.append(r["usage"])
            if c and c.strip():
                return c
            last = "empty content %r" % c
        except Exception as e:
            last = "%s" % e
        time.sleep(1.0 + attempt)
        body["temperature"] = min(0.4, (body.get("temperature") or 0.0) + 0.15)
    raise RuntimeError("LLM call failed after retries: " + last)

def _img(path):
    b = base64.b64encode(open(path, "rb").read()).decode()
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b}}

def _parse_json(raw):
    s = raw.strip()
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', s, re.S)
    if m:
        s = m.group(1)
    else:
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            s = s[i:j+1]
    return json.loads(s)

PLANNER_SYSTEM = """You are the planning brain of "ghosthands", an agent that drives a REAL
computer screen using real mouse/keyboard (USB HID). Each turn you see one screenshot and
choose exactly ONE next action.

Reply with ONE JSON object ONLY -- no prose, no markdown fences. Schema:
{
  "observation": "<what is on screen that matters for the goal>",
  "reasoning": "<one line: why this action>",
  "action": "click | double_click | type | key | scroll | navigate | wait | verify_stop | done",
  "target": "<click/double_click: a SHORT unambiguous description of the ONE element, e.g. 'the blue Create subscription button, top right'>",
  "text": "<type: the exact literal text>",
  "keys": "<key: e.g. return | tab | escape | cmd+a>",
  "direction": "down | up",
  "amount": 5,
  "url": "<navigate: full https URL>",
  "seconds": 1.5,
  "reason": "<verify_stop: why a human must look>"
}

Rules:
- ONE action per turn. Never batch.
- Prefer 'navigate' to a known URL over clicking through menus.
- Focus a field (click it) on one turn, then 'type' on the next turn.
- Use 'key' for Enter/Tab/Escape/shortcuts; 'type' only for literal text.
- After an action that changes the page, OBSERVE the result next turn before continuing.
- MONEY SAFETY: this task sets subscription PRICES. Do NOT click any button that COMMITS a
  price (Activate / Save / Apply / Confirm / Update) until, on a PRIOR turn, you have read the
  price shown on screen and confirmed it EXACTLY equals the target in the playbook. If a shown
  price does not match, choose 'verify_stop'.
- On ANYTHING unexpected -- an error, a login/2FA/captcha wall, a dialog not in the playbook,
  a permanent/immutable field you are unsure about -- choose 'verify_stop' with a clear reason.
  Never guess on a money screen. Product IDs are PERMANENT once created; type them EXACTLY.
- Choose 'done' only when the whole GOAL is achieved and visible on screen.
"""

class Planner:
    def __init__(self, model=None):
        self.model = model or Config.planner_model
    def decide(self, goal, guide, frame_path, history, dims):
        w, h = dims
        hist = "\n".join(history[-18:]) if history else "(nothing yet)"
        text = ("GOAL:\n" + goal + "\n\nPLAYBOOK / GUIDANCE:\n" + guide +
                "\n\nACTIONS SO FAR (oldest first):\n" + hist +
                ("\n\nThe screenshot is %dx%d px. Decide the SINGLE next action as JSON." % (w, h)))
        msgs = [{"role": "system", "content": PLANNER_SYSTEM},
                {"role": "user", "content": [{"type": "text", "text": text}, _img(frame_path)]}]
        raw = _chat(self.model, msgs, max_tokens=500)
        return _parse_json(raw), raw

class Grounder:
    def __init__(self, model=None):
        self.model = model or Config.grounder_model
    def locate(self, frame_path, description, dims):
        w, h = dims
        prompt = ("You are a precise GUI grounding agent looking at a %dx%d pixel screenshot. "
                  "Output ONLY the pixel coordinate 'x,y' (two integers within %dx%d) where I "
                  "should click to: %s. No words." % (w, h, w, h, description))
        msgs = [{"role": "user", "content": [{"type": "text", "text": prompt}, _img(frame_path)]}]
        raw = _chat(self.model, msgs, max_tokens=40)
        nums = re.findall(r'-?\d+', raw)
        if len(nums) >= 2:
            x, y = int(nums[0]), int(nums[1])
            return x, y, (x / w, y / h), raw.strip()
        raise RuntimeError("grounder returned no coords: %r" % raw)
