"""ghosthands.jev — a drop-in JevPlanner that replaces the vision Planner.

Uses TypeSafe's Jev decision model (System One): a structured element table is
sent as state, and one decision request answers "which operation" plus
speculative "which element to click / type into" heads with calibrated
probabilities in ~300 ms. A small text LLM writes literal text only for
TYPE_TEXT when no playbook candidate matches.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from typing import Optional

from .brain import _chat
from .config import Config
from .dom_reader import Element, SafariReader, Screen


class JevError(RuntimeError):
    """Raised when the Jev decision endpoint fails or returns invalid answers."""


OPERATIONS: "OrderedDict[str, str]" = OrderedDict([
    ("CLICK", "Click one element (button, link, tab, checkbox)"),
    ("TYPE_TEXT", "Type text into one text field or combobox (clears it first)"),
    ("KEY_ENTER", "Press Enter to submit the focused field"),
    ("SCROLL_DOWN", "Scroll the page down to reveal more"),
    ("SCROLL_UP", "Scroll the page up"),
    ("WAIT", "Wait for the page to finish loading or updating"),
    ("VERIFY_STOP", "Pause for a human: money is about to be committed, a login/2FA/captcha wall, an error, or the page does not match the playbook"),
    ("DONE", "The whole goal is achieved and visible on the page"),
])

_RULES = (
    "never CLICK a button that commits a price/charge (Activate, Save, Apply, "
    "Confirm, Update, Pay) until a previous step read the value back and it "
    "equals the target; on anything unexpected choose VERIFY_STOP; choose DONE "
    "only when the goal is visible"
)

_KEY_RE = re.compile(r"^\s*(name|id|product id|email|password|title|url|price|plan|team|code)\s*:\s*(.+)$", re.I | re.M)
_NUM_RE = re.compile(r"(?<![A-Za-z\d.])\$?\d+(?:\.\d+)?(?![A-Za-z\d])")
_DOTTED_RE = re.compile(r"\b[a-z][a-z0-9]*(?:\.[a-z0-9]+){2,}\b", re.I)
_URL_RE = re.compile(r"https?://\S+")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def extract_text_candidates(goal: str, guide: str) -> list[str]:
    """Literal values the playbook carries, deduped, order-preserving, cap 24."""
    text = (goal or "") + "\n" + (guide or "")
    out: list[str] = []

    def add(v: str) -> None:
        v = v.strip().strip('"').strip("'").strip()
        if v and len(v) <= 120 and v.lower() not in {x.lower() for x in out}:
            out.append(v[:120])

    for m in _URL_RE.finditer(text):
        add(m.group(0).rstrip(".,;)"))
    for m in _EMAIL_RE.finditer(text):
        add(m.group(0))
    for m in _KEY_RE.finditer(text):
        add(m.group(2))
    for m in re.finditer(r'"([^"\n]{1,120})"', text):
        add(m.group(1))
    for m in _DOTTED_RE.finditer(text):
        add(m.group(0))
    for m in _NUM_RE.finditer(text):
        add(m.group(0).lstrip("$"))  # a price field wants 8.99, not $8.99
    return out[:24]


class JevDecider:
    """Posts structured state + choice questions to the Jev decisions endpoint."""

    def __init__(self, model: Optional[str] = None, url: Optional[str] = None,
                 api_key: Optional[str] = None, timeout: float = 30.0) -> None:
        self.model = model or Config.jev_model
        self.url = url or Config.jev_url
        self.api_key = api_key or Config.api_key
        self.timeout = timeout
        self.total_cost = 0.0
        self.total_calls = 0

    def build_questions(self, goal: str, guide: str, screen: Screen,
                        history: list[str]) -> dict:
        """Build the operation question plus speculative target heads."""
        questions: dict = {
            "operation": {
                "type": "choice",
                "criteria": dict(OPERATIONS),
                "instructions": {"goal": goal, "playbook": (guide or "")[:4000],
                                 "rules": _RULES},
            }
        }
        clickable = [e for e in screen.elements if "CLICK" in e.operations()]
        typable = [e for e in screen.elements if "TYPE_TEXT" in e.operations()]
        # Offer only operations that have a target (jev-ultrafast: "only supported
        # operations and targets are offered").
        if not clickable:
            questions["operation"]["criteria"].pop("CLICK", None)
        if not typable:
            questions["operation"]["criteria"].pop("TYPE_TEXT", None)
        if clickable:
            questions["click_target"] = {
                "type": "choice",
                "criteria": {e.index: ("%s %s (%s)" % (e.role, e.label, e.value))[:100]
                             for e in clickable},
                "instructions": {"goal": goal,
                                 "task": "Pick the element to click/type into for the next step toward the goal."},
            }
        if typable:
            questions["type_target"] = {
                "type": "choice",
                "criteria": {e.index: ("%s %s (%s)" % (e.role, e.label, e.value))[:100]
                             for e in typable},
                "instructions": {"goal": goal,
                                 "task": "Pick the element to click/type into for the next step toward the goal."},
            }
        return questions

    def ask(self, state: dict, questions: dict) -> dict:
        """One Jev decision request; retries transient failures, validates answers."""
        body = {"model": self.model, "state": state, "questions": questions}
        headers = {"Authorization": "Bearer " + (self.api_key or ""),
                   "Content-Type": "application/json",
                   "HTTP-Referer": "https://github.com/affirmitv/ghosthands",
                   "X-Title": "ghosthands"}
        last = "no response"
        for attempt in range(3):
            try:
                req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                             headers=headers)
                resp = json.load(urllib.request.urlopen(req, timeout=self.timeout))
                self._validate(resp, questions)
                usage = resp.get("usage") or {}
                self.total_cost += float(usage.get("cost") or 0.0)
                self.total_calls += 1
                return resp
            except urllib.error.HTTPError as e:
                err_body = ""
                try:
                    err_body = e.read().decode("utf-8", "replace")
                except Exception:
                    pass
                if 400 <= e.code < 500:
                    raise JevError("Jev HTTP %d: %s" % (e.code, err_body))
                last = "HTTP %d %s" % (e.code, err_body)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = str(e)
            time.sleep(0.5 * (2 ** attempt))
        raise JevError("Jev call failed after retries: " + last)

    @staticmethod
    def _validate(resp: dict, questions: dict) -> None:
        answers = resp.get("answers")
        if not isinstance(answers, dict):
            raise JevError("Jev response missing answers dict")
        for name, q in questions.items():
            a = answers.get(name)
            if not isinstance(a, dict) or a.get("type") != "choice":
                raise JevError("Jev missing/invalid answer for %r" % name)
            if a.get("choice") not in (q.get("criteria") or {}):
                raise JevError("Jev answer %r choice %r not in criteria"
                               % (name, a.get("choice")))
            if not isinstance(a.get("probabilities"), dict):
                raise JevError("Jev answer %r missing probabilities" % name)

    def decide(self, goal: str, guide: str, screen: Screen,
               history: list[str]) -> dict:
        """Full decision: state -> questions -> validated answers."""
        t0 = time.time()
        state = screen.to_jev_state(history)
        questions = self.build_questions(goal, guide, screen, history)
        resp = self.ask(state, questions)
        return {"answers": resp.get("answers", {}),
                "usage": resp.get("usage", {}),
                "latency_s": time.time() - t0,
                "raw": resp}


class TextHelper:
    """Writes the literal text for one TYPE_TEXT field via a small text LLM."""

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model or Config.jev_text_model

    def write(self, goal: str, guide: str, screen: Screen, element: Element,
              history: list[str]) -> str:
        hist = "\n".join(history[-8:]) if history else "(nothing yet)"
        user = ("GOAL: %s\nPLAYBOOK: %s\nPAGE TITLE: %s\nFIELD: role=%s label=%s "
                "current value=%s\nRECENT ACTIONS:\n%s\n\nReply with the exact "
                "literal text to type into this field, nothing else."
                % (goal, (guide or "")[:4000], screen.title, element.role,
                   element.label, element.value or "(empty)", hist))
        raw = _chat(self.model, [
            {"role": "system", "content": "You fill ONE form field for a screen "
             "agent. Reply with the exact literal text to type and NOTHING else. "
             "No quotes, no explanation."},
            {"role": "user", "content": user},
        ], max_tokens=120)
        return raw.strip().strip('"').strip("'").strip()


class JevPlanner:
    """Drop-in replacement for brain.Planner, driven by Jev decisions."""

    needs_frame = False  # reads Safari's element table; the agent skips the screenshot

    def __init__(self, reader: Optional[SafariReader] = None,
                 decider: Optional[JevDecider] = None,
                 text_helper: Optional[TextHelper] = None,
                 min_confidence: Optional[float] = None) -> None:
        self.reader = reader or SafariReader()
        self.decider = decider or JevDecider()
        self.text_helper = text_helper or TextHelper()
        self.min_confidence = Config.jev_min_confidence if min_confidence is None else min_confidence
        self.last_screen: Optional[Screen] = None
        self.settle_timeout = 2.0   # seconds to wait for the page to change after an action
        self.settle_poll = 0.15

    def decide(self, goal: str, guide: str, frame_path: str, history: list[str],
               dims: tuple[int, int]) -> tuple[dict, str]:
        screen, settled_s = self._settled_snapshot(history)
        self.last_screen = screen
        res = self.decider.decide(goal, guide, screen, history)
        answers = res["answers"]
        usage = res["usage"]
        op = answers["operation"]
        operation = op["choice"]
        op_p = float(op.get("confidence") or 0.0)
        probs = op.get("probabilities") or {}
        if not op_p and probs:
            op_p = float(probs.get(operation) or 0.0)

        target_ans = answers.get("click_target") if operation == "CLICK" else \
            answers.get("type_target") if operation == "TYPE_TEXT" else None
        tgt_p = float(target_ans.get("confidence") or 0.0) if target_ans else 1.0

        plan: dict = {}
        if operation in ("CLICK", "TYPE_TEXT"):
            el = screen.by_index(target_ans["choice"])
            fx, fy = el.screen_point(screen.viewport, screen.screen)
            desc = "[%s] %s %s" % (el.index, el.role, el.label)
            plan["action"] = "click" if operation == "CLICK" else "type"
            plan["target"] = desc
            plan["point"] = [fx, fy]
            if operation == "TYPE_TEXT":
                plan["text"] = self._type_value(goal, guide, screen, el, res, history)
                plan["select_all"] = True
        elif operation == "KEY_ENTER":
            plan = {"action": "key", "keys": "return"}
        elif operation == "SCROLL_DOWN":
            plan = {"action": "scroll", "direction": "down", "amount": 5}
        elif operation == "SCROLL_UP":
            plan = {"action": "scroll", "direction": "up", "amount": 5}
        elif operation == "WAIT":
            plan = {"action": "wait", "seconds": 1.5}
        elif operation == "VERIFY_STOP":
            top = sorted(probs.items(), key=lambda kv: -kv[1])[:2]
            plan = {"action": "verify_stop",
                    "reason": "Jev asked for a human: " + ", ".join(
                        "%s=%s" % (k, round(v, 2)) for k, v in top)}
        elif operation == "DONE":
            plan = {"action": "done"}
        else:
            raise JevError("unknown operation %r" % operation)

        if operation not in ("WAIT", "SCROLL_DOWN", "SCROLL_UP") and \
                (op_p < self.min_confidence or tgt_p < self.min_confidence):
            plan = {"action": "verify_stop",
                    "reason": "low confidence %s p=%s target p=%s"
                              % (operation, round(op_p, 2), round(tgt_p, 2))}

        plan["observation"] = "%d controls on '%s'" % (len(screen.elements), screen.title)
        plan["observation"] = plan["observation"][:160]
        plan["reasoning"] = "jev %s p=%.2f" % (operation, op_p)
        if target_ans:
            plan["reasoning"] += " target [%s] p=%.2f" % (target_ans.get("choice"), tgt_p)
        plan["jev"] = {"operation": probs, "target": (target_ans or {}).get("probabilities", {}),
                       "cost": usage.get("cost"), "latency_s": res["latency_s"],
                       "settle_s": round(settled_s, 2), "controls": len(screen.elements)}
        return plan, json.dumps(res["raw"], separators=(",", ":"))

    def _settled_snapshot(self, history: list[str]) -> tuple[Screen, float]:
        """Read the element table; after an action, keep re-reading (cheap, ~0.1 s) until the
        page differs from the table that action was planned on, or the settle timeout passes.
        Mirrors jev-ultrafast's "wait for useful state" so a click is never planned on a stale table."""
        screen = self.reader.snapshot()
        prev = self.last_screen
        acted = bool(history) and not history[-1].startswith("[human")
        if prev is None or not acted:
            return screen, 0.0
        t0 = time.time()
        before = prev.fingerprint()
        while screen.fingerprint() == before and time.time() - t0 < self.settle_timeout:
            time.sleep(self.settle_poll)
            screen = self.reader.snapshot()
        return screen, time.time() - t0

    def _type_value(self, goal: str, guide: str, screen: Screen, el: Element,
                    res: dict, history: list[str]) -> str:
        candidates = extract_text_candidates(goal, guide)
        if candidates:
            criteria = {c: "candidate literal value from the playbook" for c in candidates}
            criteria["__WRITE__"] = "none of these; a helper must write the text"
            state = screen.to_jev_state(history)
            state["page"]["field"] = {"index": el.index, "label": el.label,
                                      "role": el.role, "value": el.value}
            q = {"text_value": {"type": "choice", "criteria": criteria,
                                "instructions": {"goal": goal,
                                                 "task": "Pick the exact literal text to type into the field."}}}
            ans = self.decider.ask(state, q)["answers"]["text_value"]
            if ans["choice"] != "__WRITE__":
                return ans["choice"]
        return self.text_helper.write(goal, guide, screen, el, history)
