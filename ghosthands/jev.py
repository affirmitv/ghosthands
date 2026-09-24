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

from .brain import _chat, _parse_json
from .config import Config
from .dom_reader import Element, SafariReader, Screen


class JevError(RuntimeError):
    """Raised when the Jev decision endpoint fails or returns invalid answers."""


OPERATIONS: "OrderedDict[str, str]" = OrderedDict([
    ("CLICK", "Click one element (button, link, tab, checkbox)"),
    ("TYPE_TEXT", "Type text into one text field or combobox (clears it first)"),
    ("SELECT", "Choose an option in one dropdown (native select)"),
    ("KEY_ENTER", "Press Enter to submit the focused field"),
    ("SCROLL_DOWN", "Scroll the page down to reveal more"),
    ("SCROLL_UP", "Scroll the page up"),
    ("WAIT", "Wait for the page to finish loading or updating"),
    ("VERIFY_STOP", "Pause for a human: money is about to be committed, a login/2FA/captcha wall blocks the way, or an error message is on the page"),
    ("DONE", "The whole goal is achieved and visible on the page"),
])

_RULES = (
    "never CLICK a button that commits a price/charge (Activate, Save, Apply, "
    "Confirm, Update, Pay) until a previous step read the value back and it "
    "equals the target; on a login wall, an error message or a money step choose VERIFY_STOP; "
    "when the page is not what the playbook expected, navigate (BACK, another row, scroll) rather "
    "than stop; choose DONE only when the goal is visible"
)

_KEY_RE = re.compile(r"^\s*(name|id|product id|email|title|url|price|plan|team|code)\s*:\s*(.+)$", re.I | re.M)
_NUM_RE = re.compile(r"(?<![A-Za-z\d.])\$?\d+(?:\.\d+)?(?![A-Za-z\d])")
_DOTTED_RE = re.compile(r"\b[a-z][a-z0-9]*(?:\.[a-z0-9]+){2,}\b", re.I)
_URL_RE = re.compile(r"https?://\S+")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_TYPE_INTO_RE = re.compile(r"\btype\s+(?!the\b|a\b|an\b|it\b)(\S.{0,79}?)\s+into\b", re.I)


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
    for m in _TYPE_INTO_RE.finditer(text):
        add(m.group(1))  # "Type Fury into the club search box" carries the literal unquoted
    for m in _DOTTED_RE.finditer(text):
        add(m.group(0))
    for m in _NUM_RE.finditer(text):
        add(m.group(0).lstrip("$"))  # a price field wants 8.99, not $8.99
    return out[:24]




_SECRET_WORD_RE = re.compile(r"\b(password|passwd|pwd|token|secret|api[_ -]?key|cvv|cvc|card number)\b", re.I)


def scrub_guide(guide: str) -> str:
    """Drop the playbook sentences that carry a credential. Jev never needs the password to
    decide which field to click; the text helper never needs it either (the human types it, or
    the playbook uses a placeholder). Nothing secret leaves the machine in the decision state.
    Only the sentence goes, not its whole line: a one-line playbook that says "never type a
    password" otherwise loses every step."""
    out = []
    for line in (guide or "").split("\n"):
        if not _SECRET_WORD_RE.search(line):
            out.append(line)
            continue
        sents = re.split(r"(?<=[.!?])\s+", line)
        kept = [x for x in sents if not _SECRET_WORD_RE.search(x)]
        out.append(" ".join(kept + ["[sentence withheld: credential]"]) if kept
                   else "[line withheld: credential]")
    return "\n".join(out)


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
                        history: list[str], exclude: Optional[set] = None,
                        context: Optional[dict] = None) -> dict:
        """Build the operation question plus speculative target heads. Operations named in
        `exclude` are not offered (the scroll guard, a DONE the verifier rejected). `context`
        (a multi-step task's current step and progress trail) goes into every instruction."""
        target_task = "Pick the element to click/type into for the next step toward the goal."
        if context and context.get("current_step"):
            target_task = ("Pick the element to click/type into for the CURRENT STEP (%s). "
                           "Do not redo a step already done." % context["current_step"])
        questions: dict = {
            "operation": {
                "type": "choice",
                "criteria": OrderedDict((k, v) for k, v in OPERATIONS.items()
                                        if k not in (exclude or ())),
                "instructions": {"goal": goal, "playbook": scrub_guide(guide)[:4000],
                                 "rules": _RULES},
            }
        }
        if context:
            questions["operation"]["instructions"].update(context)
        # The target heads see the playbook too: "click a 14U team link" is in the playbook, not the goal.
        playbook = scrub_guide(guide)[:1000]
        clickable = [e for e in screen.elements if "CLICK" in e.operations()]
        typable = [e for e in screen.elements if "TYPE_TEXT" in e.operations()]
        selects = [e for e in screen.elements if "SELECT" in e.operations()]
        if not selects:
            questions["operation"]["criteria"].pop("SELECT", None)
        else:
            questions["select_target"] = {
                "type": "choice",
                "criteria": {e.index: ("%s %s (%s)" % (e.role, e.label, e.value))[:100] for e in selects},
                "instructions": {"goal": goal, "playbook": playbook,
                                 "task": "Pick the dropdown to change for the next step toward the goal."},
            }
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
                "instructions": {"goal": goal, "playbook": playbook,
                                 "task": target_task},
            }
        if typable:
            questions["type_target"] = {
                "type": "choice",
                "criteria": {e.index: ("%s %s (%s)" % (e.role, e.label, e.value))[:100]
                             for e in typable},
                "instructions": {"goal": goal, "playbook": playbook,
                                 "task": target_task},
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
                raw = urllib.request.urlopen(req, timeout=self.timeout).read()
                try:
                    resp = json.loads(raw)
                except ValueError as e:
                    last = "non-JSON 2xx body (%s): %r" % (e, raw[:120])
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                self._validate(resp, questions)
                usage = resp.get("usage") or {}
                self.total_cost += float(usage.get("cost") or 0.0)
                self.total_calls += 1
                return resp
            except urllib.error.HTTPError as e:
                try:
                    err_body = e.read().decode("utf-8", "replace")
                except OSError as read_err:
                    err_body = "(error body unreadable: %s)" % read_err
                if 400 <= e.code < 500 and e.code != 429:   # 429 is a rate limit: retry it
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
               history: list[str], exclude: Optional[set] = None,
               context: Optional[dict] = None) -> dict:
        """Full decision: state -> questions -> validated answers."""
        t0 = time.time()
        state = screen.to_jev_state(history)
        questions = self.build_questions(goal, guide, screen, history, exclude, context)
        resp = self.ask(state, questions)
        return {"answers": resp.get("answers", {}),
                "usage": resp.get("usage", {}),
                "latency_s": time.time() - t0,
                "raw": resp}


# The small text model (a reasoning model by default) spends an uncapped token budget on
# reasoning and returns empty content; cap the effort and leave room for the answer.
_TEXT_EXTRA = {"reasoning": {"effort": "low"}}


class TextHelper:
    """Writes the literal text for one TYPE_TEXT field via a small text LLM."""

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model or Config.jev_text_model
        self.total_cost = 0.0
        self.total_calls = 0

    def write(self, goal: str, guide: str, screen: Screen, element: Element,
              history: list[str]) -> str:
        hist = "\n".join(history[-8:]) if history else "(nothing yet)"
        usage: list = []
        user = ("GOAL: %s\nPLAYBOOK: %s\nPAGE TITLE: %s\nFIELD: role=%s label=%s "
                "current value=%s\nRECENT ACTIONS:\n%s\n\nReply with the exact "
                "literal text to type into this field, nothing else."
                % (goal, scrub_guide(guide)[:4000], screen.title, element.role,
                   element.label, element.value or "(empty)", hist))
        raw = _chat(self.model, [
            {"role": "system", "content": "You fill ONE form field for a screen "
             "agent. Reply with the exact literal text to type and NOTHING else. "
             "No quotes, no explanation."},
            {"role": "user", "content": user},
        ], max_tokens=600, extra=_TEXT_EXTRA, usage_out=usage)
        self.total_calls += 1
        self.total_cost += sum(float(u.get("cost") or 0.0) for u in usage)
        return raw.strip().strip('"').strip("'").strip()


_DONE_CLAUSE_RE = re.compile(r"\bDONE\s+(?:when|as soon as|once|if|after)\b", re.I)


def has_done_clause(guide: str) -> bool:
    """True when the playbook states its own success condition ("DONE when ...")."""
    return bool(_DONE_CLAUSE_RE.search(guide or ""))


_STEP_VERB_RE = re.compile(
    r"\b(click|tap|open|type|enter|find|scroll|pick|choose|select|go|wait|run|use|expand|press|"
    r"search|navigate|keep going|look|read|report|start)\b", re.I)
_STEP_SKIP_RE = re.compile(r"^\s*(never|do not|don't|nothing|no)\b|\bDONE\s+(?:when|as soon as|once|if|after)\b",
                           re.I)
_QUOTED_RE = re.compile(r"\"[^\"\n]{1,160}\"|(?<![A-Za-z])'[^'\n]{1,160}?'(?![A-Za-z])")
_THEN_RE = re.compile(r"(?:,\s*|;\s*|\s+)(?:and\s+)?then\b[,:]?\s*|^\s*then\b[,:]?\s*", re.I)
_LEAD_RE = re.compile(r"^\s*(?:first|next|finally|after that|and)\b[,:]?\s*", re.I)


def _step_pieces(text: str, split_commas: bool = False) -> list[str]:
    """Sentences of `text`, each split again on "then"; quoted labels are never split."""
    quoted: list[str] = []

    def hide(m: "re.Match") -> str:
        quoted.append(m.group(0))
        return "\x00%d\x00" % (len(quoted) - 1)

    def show(t: str) -> str:
        return re.sub(r"\x00(\d+)\x00", lambda m: quoted[int(m.group(1))], t)

    body = _QUOTED_RE.sub(hide, text or "")
    out: list[str] = []
    for sent in re.split(r"(?<=[.!?])\s+|\n+", body):
        parts = _THEN_RE.split(sent)
        if split_commas:
            parts = [q for p in parts for q in re.split(r",\s*(?:and\s+)?|\s+and\s+(?=[a-z]+\s)", p)]
        for part in parts:
            part = _LEAD_RE.sub("", show(part)).strip().rstrip(".").strip()
            if part:
                out.append(part)
    return out


def split_subgoals(goal: str, guide: str, max_steps: int = 8) -> list[str]:
    """Ordered steps of a multi-step task, parsed without a model call: the playbook's sentences
    (split again on "then") that name an action, minus the DONE clause and the "never" rules.
    Falls back to the goal ("A, then B, and C"). Fewer than 2 steps means a single-step task: []."""
    def keep(pieces: list[str]) -> list[str]:
        return [p[:200] for p in pieces if _STEP_VERB_RE.search(p) and not _STEP_SKIP_RE.search(p)]

    steps = keep(_step_pieces(scrub_guide(guide)))
    if len(steps) < 2:
        steps = keep(_step_pieces(goal, split_commas=True))
    return steps[:max_steps] if len(steps) >= 2 else []


_SCROLL_STEP_RE = re.compile(r"\b(scroll|keep going|further down|down to|down the page)\b", re.I)
_TOKEN_RE = re.compile(r"[a-z0-9#]+(?:['\u2019][a-z]+)?", re.I)


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def label_named_in(label: str, step: str) -> bool:
    """True when the step text names this control's label word for word (at least 2 words or
    6 characters), e.g. step "click Start 14-day free trial" and label "Start 14-day free trial"."""
    lt, st = _tokens(label), _tokens(step)
    if not lt or (len(lt) < 2 and len(label.strip()) < 6):
        return False
    n = len(lt)
    return any(st[i:i + n] == lt for i in range(len(st) - n + 1))


def near_miss(label: str, step: str) -> Optional[str]:
    """When the step names a control that differs from `label` in exactly one token that holds
    a digit ("Oakland 11U Fuca" vs "Oakland 14U Fuca"), return the phrase the step names."""
    lt, st = _tokens(label), _tokens(step)
    n = len(lt)
    if n < 2:
        return None
    for i in range(len(st) - n + 1):
        win = st[i:i + n]
        diff = [k for k in range(n) if win[k] != lt[k]]
        if len(diff) == 1 and any(c.isdigit() for c in win[diff[0]]) and \
                any(c.isdigit() for c in lt[diff[0]]):
            return " ".join(win)
    return None


_PASSWORD_FIELD_RE = re.compile(r"password|passcode|\u2022{3,}|\*{4,}", re.I)
_EMAIL_FIELD_RE = re.compile(r"e-?mail|@", re.I)


def field_accepts(el: Optional[Element], text: str, check_email: bool = True) -> bool:
    """False for a password field, or (check_email) for an email field and a value that is not
    an email. (The reader never ships a password field's value; its placeholder is often dots.)"""
    if el is None:
        return True
    label = "%s %s" % (el.label, el.role)
    if _PASSWORD_FIELD_RE.search(label) or el.value == "(filled, hidden)":
        return False
    if check_email and _EMAIL_FIELD_RE.search(el.label or "") and "@" not in (text or ""):
        return False
    return True


def _same_text(shown: str, typed: str) -> bool:
    return bool(typed) and (shown or "").strip().lower() == (typed or "").strip().lower()


def page_id(url: str) -> str:
    """A page's identity for the trail: the URL without its #fragment."""
    return (url or "").split("#", 1)[0]


def compact_page(screen: Screen, max_lines: int = 60, max_chars: int = 4500) -> str:
    """Title, URL, visible text and the element table, trimmed for a cheap text-model call."""
    rows = screen.table().splitlines()[:max_lines]
    vtext = getattr(screen, "vtext", "") or ""
    if vtext:
        # After a scroll the top of the document is not what is on screen: show both.
        text = "TEXT IN VIEW: %s\nTOP OF PAGE: %s" % (vtext[:1500], (screen.text or "")[:500])
    else:
        text = "VISIBLE TEXT: %s" % (screen.text or "")[:1500]
    out = ("PAGE TITLE: %s\nURL: %s\n%s\nCONTROLS:\n%s"
           % (screen.title, screen.url, text, "\n".join(rows) or "(none)"))
    return out[:max_chars]


class DoneVerifier:
    """Asks the small text model a strict yes/no: is the goal's DONE condition met on this page?"""

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model or Config.jev_text_model
        self.total_cost = 0.0
        self.total_calls = 0

    _RULES = ("Check, in order: (1) if the "
              "goal or playbook names a product, site or page (for example an app name, a "
              "checkout, a team), the title and URL must show this page IS that one, not a "
              "different site with a similar box; (2) if the playbook names a condition "
              "(\"DONE when ...\"), every part of it must be visible here; (3) a step the "
              "playbook says to take first that has not led here means false. When in doubt, "
              "false.")
    _SYSTEM = ("You check whether a screen agent has finished its "
               "task. Be strict: answer true only when the success condition is plainly on the "
               "page. Reply with one JSON object and nothing else.")

    def _call(self, user: str) -> dict:
        usage: list = []
        raw = _chat(self.model, [
            {"role": "system", "content": self._SYSTEM},
            {"role": "user", "content": user},
        ], max_tokens=600, extra=_TEXT_EXTRA, usage_out=usage)
        self.total_calls += 1
        self.total_cost += sum(float(u.get("cost") or 0.0) for u in usage)
        try:
            return _parse_json(raw)
        except ValueError as e:
            raise JevError("done check returned non-JSON: %r (%s)" % (raw[:120], e))

    @staticmethod
    def _bool(v) -> bool:
        if isinstance(v, str):
            return v.strip().lower() == "true"
        return v is True

    def check(self, goal: str, guide: str, screen: Screen,
              history: Optional[list[str]] = None) -> tuple[bool, str]:
        """Return (done, reason). Raises on a failed call or an unparseable answer."""
        acts = "\n".join((history or [])[-8:]) or "(none yet)"
        user = ("GOAL: %s\nPLAYBOOK: %s\nACTIONS TAKEN SO FAR:\n%s\n\n%s\n\nIs the goal's DONE condition satisfied on this "
                "page right now? Judge only what this page shows. %s "
                "Answer JSON {\"reason\": \"<one sentence>\", \"done\": true|false}."
                % (goal, scrub_guide(guide)[:2000], acts, compact_page(screen), self._RULES))
        obj = self._call(user)
        return self._bool(obj.get("done")), str(obj.get("reason") or "")[:200]

    def check_progress(self, goal: str, guide: str, screen: Screen, history: Optional[list[str]],
                       steps: list[str], current: int, trail: list[str]) -> tuple[bool, str, bool]:
        """One call for a multi-step task: (done, reason, current_step_done). The same strict
        DONE rules as check(), plus whether the current step of the playbook is complete."""
        acts = "\n".join((history or [])[-8:]) or "(none yet)"
        plan = "\n".join("%d. %s%s" % (i + 1, st, "  (done)" if i < current else
                                         "  <- CURRENT" if i == current else "")
                          for i, st in enumerate(steps))
        user = ("GOAL: %s\nPLAYBOOK: %s\nSTEPS:\n%s\nPAGES AND PROGRESS SO FAR:\n%s\n"
                "ACTIONS TAKEN SO FAR:\n%s\n\n%s\n\n"
                "Two questions. (a) done: is the goal's DONE condition satisfied on this page "
                "right now? Judge only what this page shows. When the playbook states a \"DONE "
                "when ...\" condition, that condition decides; a goal that also asks to report or "
                "read something is met when the page shows it or shows it is not there. %s (b) step_done: is step %d "
                "(\"%s\") complete, meaning this page is where that step leads or plainly shows "
                "its result? An earlier step being complete does not count. "
                "Answer JSON {\"reason\": \"<one sentence>\", \"step_done\": true|false, "
                "\"done\": true|false}."
                % (goal, scrub_guide(guide)[:2000], plan, "\n".join(trail[-8:]) or "(start page)",
                   acts, compact_page(screen), self._RULES, current + 1, steps[current][:160]))
        obj = self._call(user)
        return (self._bool(obj.get("done")), str(obj.get("reason") or "")[:200],
                self._bool(obj.get("step_done")))


# Controls that commit money or mutate production. A click on one of these is never taken on
# the model's word alone: the planner pauses so a human confirms the value on screen, and the
# click is allowed only on the step right after that approval.
_COMMIT_RE = re.compile(
    r"\b(pay|purchase|buy|checkout|subscribe|confirm|activate|save|apply|update|submit|publish|"
    r"approve|delete|remove|cancel plan|place order|charge)\b", re.I)


def is_commit_control(label: str) -> bool:
    """True when a control's label reads like a money or production commit."""
    return bool(_COMMIT_RE.search(label or ""))


def page_center(screen: Screen) -> Optional[tuple[float, float]]:
    """The center of the page viewport as screen fractions, or None when it is off the display."""
    vp = screen.viewport
    if not vp.get("w") or not vp.get("h"):
        return None
    try:
        return Element("0", "", "", "", vp["w"] / 2.0, vp["h"] / 2.0, 0, 0).screen_point(vp, screen.screen)
    except ValueError:
        return None


class JevPlanner:
    """Drop-in replacement for brain.Planner, driven by Jev decisions."""

    needs_frame = False  # reads Safari's element table; the agent skips the screenshot

    def __init__(self, reader: Optional[SafariReader] = None,
                 decider: Optional[JevDecider] = None,
                 text_helper: Optional[TextHelper] = None,
                 min_confidence: Optional[float] = None,
                 min_target_confidence: Optional[float] = None,
                 verifier: Optional[DoneVerifier] = None,
                 verify_done: Optional[bool] = None,
                 max_scrolls: Optional[int] = None,
                 dead_click_guard: Optional[bool] = None,
                 loading_wait_s: Optional[float] = None,
                 subgoals: Optional[bool] = None,
                 backtrack_after: Optional[int] = None) -> None:
        self.reader = reader or SafariReader()
        self.decider = decider or JevDecider()
        self.text_helper = text_helper or TextHelper()
        self.verify_done = Config.jev_verify_done if verify_done is None else verify_done
        self.verifier = verifier or DoneVerifier()
        self.max_scrolls = Config.jev_max_scrolls if max_scrolls is None else max_scrolls
        self._notes: list[tuple[int, str]] = []  # (history position, note) shown to Jev as recent actions
        self._scroll_streak = 0
        self._scroll_fp: Optional[str] = None  # table fingerprint the last scroll was decided on
        self.min_confidence = Config.jev_min_confidence if min_confidence is None else min_confidence
        self.min_target_confidence = (Config.jev_min_target_confidence if min_target_confidence is None
                                      else min_target_confidence)
        self.last_screen: Optional[Screen] = None
        self._pending_commit: Optional[tuple] = None  # (url, control) a human is being asked to approve
        self.settle_timeout = Config.jev_settle_s  # seconds to wait for the page to change after an action
        self.settle_poll = 0.1
        # Dead-click guard: controls whose click changed nothing, keyed (url, role, label).
        self.dead_click_guard = Config.jev_dead_click_guard if dead_click_guard is None else dead_click_guard
        self._dead: set = set()
        self._dead_streak = 0
        self._last_click: Optional[dict] = None  # the click the previous step planned
        # Loading wait: poll (no Jev decisions) while a new loading signal is on the page.
        self.loading_wait_s = Config.jev_loading_wait_s if loading_wait_s is None else loading_wait_s
        self.loading_poll = 0.5
        self._wait_grace = 0
        self._last_loading: Optional[dict] = None
        # Multi-step tasks: ordered steps parsed from the playbook, the current one, a trail of
        # the pages visited, and the page the current step started on (the last good page).
        self.subgoals = Config.jev_subgoals if subgoals is None else subgoals
        self.backtrack_after = Config.jev_backtrack_after if backtrack_after is None else backtrack_after
        self.max_backtracks = 4
        self._steps: Optional[list[str]] = None
        self._step_i = 0
        self._trail: list[str] = []
        self._home: Optional[str] = None      # page id the current step started on
        self._home_url: str = ""              # its full URL, where Back returns to
        self._focus_single: Optional[str] = None  # single-step playbook's action sentences
        self._named_tries: dict = {}          # (page id, label) -> scrolls toward it
        self._page: Optional[str] = None      # page id of the previous decision
        self._off_home = 0                    # decisions since leaving the home page
        self._left_home_via: Optional[tuple] = None  # dead-key of the click that left home
        self._backs = 0
        self._backed_from: set = set()
        self._pending_back: Optional[str] = None  # page id a Back was pressed on
        self._step_clicks: dict = {}  # (page id, role, label) -> clicks during the current step
        self._filled: dict = {}  # (page id, role, label) -> text typed there and shown back
        self._last_type: Optional[tuple] = None  # (key, text) the previous step typed

    def _note(self, history: list[str], note: str) -> None:
        self._notes.append((len(history), note))

    def _jev_history(self, history: list[str]) -> list[str]:
        """The agent's action history with the planner's own notes woven in where they happened."""
        if not self._notes:
            return history
        out: list[str] = []
        notes = sorted(self._notes, key=lambda n: n[0])
        i = 0
        for pos, h in enumerate(history):
            while i < len(notes) and notes[i][0] <= pos:
                out.append(notes[i][1]); i += 1
            out.append(h)
        out.extend(n for _, n in notes[i:])
        return out

    def _ask(self, goal: str, guide: str, screen: Screen, history: list[str],
             exclude: set) -> dict:
        hist = self._jev_history(history)
        ctx = self._step_context()
        if ctx:
            return self.decider.decide(goal, guide, screen, hist, exclude=exclude, context=ctx)
        if exclude:
            return self.decider.decide(goal, guide, screen, hist, exclude=exclude)
        return self.decider.decide(goal, guide, screen, hist)

    # ---- multi-step tasks -------------------------------------------------------------------

    def _step_context(self) -> Optional[dict]:
        if not self._steps:
            return None
        n, i = len(self._steps), self._step_i
        ctx = {"current_step": "step %d of %d: %s" % (i + 1, n, self._steps[i])}
        if i:
            ctx["steps_done"] = ["step %d: %s" % (k + 1, st) for k, st in enumerate(self._steps[:i])]
        if self._trail:
            ctx["progress"] = self._trail[-6:]
        return ctx

    def _track_page(self, history: list[str], screen: Screen, last_click: Optional[dict]) -> None:
        """Keep the trail of pages visited, the home page of the current step, and how long the
        run has been away from it."""
        pid = page_id(screen.url)
        if self._home is None:
            self._home, self._home_url = pid, screen.url
        if self._pending_back is not None:
            if pid == self._pending_back:  # Back did nothing (a focused field ate the chord)
                self._note(history, "Back did not navigate; backtracking is off for this run")
                self._backs = self.max_backtracks
            self._pending_back = None
        if self._page is not None and pid != self._page:
            via = history[-1] if history else ""
            self._trail.append(("opened '%s' (%s) via %s" % (screen.title[:60], pid[-70:], via[:60]))[:200])
            if self._page == self._home and last_click is not None and via.startswith("click:"):
                self._left_home_via = last_click["key"]
        self._page = pid
        if pid == self._home:
            self._off_home, self._left_home_via = 0, None
        else:
            self._off_home += 1

    def _check_progress(self, goal: str, guide: str, screen: Screen,
                        history: list[str]) -> tuple[Optional[bool], str]:
        """The per-step check of a multi-step task: advances the current step when it is complete.
        Returns (done, reason) like _verify, or (None, error) when the check failed."""
        fn = getattr(self.verifier, "check_progress", None)
        if fn is None:
            return self._verify(goal, guide, screen, history)
        try:
            done, why, step_done = fn(goal, guide, screen, history, self._steps, self._step_i,
                                      self._trail)
        except Exception as e:  # a failed check never ends a run
            return None, "done check failed: %s" % str(e)[:160]
        if step_done and not done and self._step_i < len(self._steps) - 1:
            k = self._step_i
            self._step_i += 1
            self._home, self._off_home, self._left_home_via = page_id(screen.url), 0, None
            self._home_url = screen.url
            self._step_clicks = {}
            self._trail.append(("step %d done on '%s': %s" % (k + 1, screen.title[:50], why[:80]))[:200])
            self._note(history, "step %d done; now step %d: %s" % (k + 1, k + 2, self._steps[self._step_i][:100]))
        return done, why

    def _focus_text(self, guide: str) -> str:
        """What the named-control heuristics read: the current step, or on a single-step task
        the playbook's action sentences. Empty when subgoals are off."""
        if self._steps:
            return self._steps[self._step_i]
        if not self.subgoals:
            return ""
        if self._focus_single is None:
            self._focus_single = " ".join(
                p for p in _step_pieces(scrub_guide(guide))
                if _STEP_VERB_RE.search(p) and not _STEP_SKIP_RE.search(p))
        return self._focus_single

    def _named_offscreen(self, screen: Screen, focus: str) -> Optional[dict]:
        """A control the focus text names word for word that sits above or below the viewport."""
        pid = page_id(screen.url)
        cands = [o for o in screen.offscreen
                 if label_named_in(str(o.get("label") or ""), focus)
                 and (screen.url, o.get("role"), o.get("label")) not in self._dead
                 and self._named_tries.get((pid, o.get("label")), 0) < 8]
        if not cands:
            return None
        top = float(screen.viewport.get("scrollY") or 0)
        mid = top + float(screen.viewport.get("h") or 0) / 2.0
        # The most specific label first, then the nearest.
        return sorted(cands, key=lambda o: (-len(str(o["label"])), abs(float(o["y"]) - mid)))[0]

    def _can_backtrack(self, screen: Screen) -> bool:
        return (bool(self._steps) and self.backtrack_after > 0 and self._backs < self.max_backtracks
                and self._home is not None and page_id(screen.url) != self._home)

    def _back_plan(self, history: list[str], screen: Screen, why: str) -> tuple[dict, str]:
        """Press Back: the current step did not happen on this page. The link that led here from
        the step's home page is marked dead there (a wrong turn), so it is not offered again."""
        pid = page_id(screen.url)
        self._backs += 1
        self._backed_from.add(pid)
        self._pending_back = pid
        wrong = self._left_home_via
        if wrong is not None and self.dead_click_guard:
            self._dead.add(wrong)
        self._note(history, "went back from '%s': step %d was not reached there%s"
                   % (screen.title[:50], self._step_i + 1,
                      ("; '%s' was a wrong turn" % wrong[2][:50]) if wrong else ""))
        self._trail.append(("went back from '%s' (%s)" % (screen.title[:50], why))[:200])
        self._off_home = max(0, self.backtrack_after - 1)  # still lost after Back: back again
        self._scroll_streak, self._scroll_fp = 0, None
        back = Config.jev_back
        if back in ("", "url") and self._home_url:
            plan = {"action": "navigate", "url": self._home_url}
        else:
            plan = {"action": "key", "keys": back if back not in ("", "url") else "cmd+left"}
        plan.update({
                "observation": ("%d controls on '%s'" % (len(screen.elements), screen.title))[:160],
                "reasoning": ("backtrack: %s" % why)[:200],
                "jev": {"backtrack": why, "wrong_turn": list(wrong) if wrong else None,
                        "step": self._step_i + 1}})
        return plan, json.dumps({"backtrack": why})

    def _verify(self, goal: str, guide: str, screen: Screen,
                history: list[str]) -> tuple[Optional[bool], str]:
        """(True|False, reason), or (None, error) when the check itself failed."""
        try:
            return self.verifier.check(goal, guide, screen, history)
        except Exception as e:  # a failed check never ends a run; the caller decides
            return None, "done check failed: %s" % str(e)[:160]

    def _done_plan(self, screen: Screen, settled_s: float, why: str) -> tuple[dict, str]:
        plan = {"action": "done",
                "observation": ("%d controls on '%s'" % (len(screen.elements), screen.title))[:160],
                "reasoning": ("verified DONE: %s" % why)[:200],
                "jev": {"verify": why, "settle_s": round(settled_s, 2),
                        "controls": len(screen.elements)}}
        if self._last_loading:
            plan["jev"]["loading"] = self._last_loading
        self._scroll_streak, self._scroll_fp = 0, None
        return plan, json.dumps({"verify": {"done": True, "reason": why}})

    def decide(self, goal: str, guide: str, frame_path: str, history: list[str],
               dims: tuple[int, int]) -> tuple[dict, str]:
        screen, settled_s = self._settled_snapshot(history)
        prev = self.last_screen
        last = history[-1] if history else ""
        self._last_loading = None
        if self.loading_wait_s > 0 and prev is not None and last.startswith(("click:", "key:")):
            screen, waited = self._wait_for_loading(history, prev, screen)
            settled_s += waited
        self.last_screen = screen
        after_idle_wait = (last.startswith("wait:") and prev is not None
                           and screen.fingerprint() == prev.fingerprint())
        last_click = self._last_click
        self._check_dead_click(history, prev, screen)
        if self._steps is None:
            self._steps = split_subgoals(goal, guide) if self.subgoals else []
        verified: Optional[tuple[Optional[bool], str]] = None  # at most one check per step
        if self._steps:
            self._track_page(history, screen, last_click)
            if self.verify_done and history:
                # Multi-step: one check per action says whether the goal is done and whether
                # the current step is complete (which advances it).
                verified = self._check_progress(goal, guide, screen, history)
                if verified[0] is True:
                    return self._done_plan(screen, settled_s, verified[1])
            if self._can_backtrack(screen) and self._off_home >= self.backtrack_after:
                return self._back_plan(history, screen, "%d decisions here without finishing step %d"
                                       % (self._off_home, self._step_i + 1))
        # Controls whose click changed nothing are not offered again this run.
        dead_idx = {e.index for e in screen.elements if self._is_dead(screen, e)}
        if self.dead_click_guard:
            # A control already clicked twice on this page (during the current step, when the
            # task has steps) is not offered a third time: an open/close toggle such as a chat
            # bubble, or two in-page anchors, loop forever otherwise.
            pid = page_id(screen.url)
            dead_idx |= {e.index for e in screen.elements if e.operations() == ["CLICK"]
                         and self._step_clicks.get((pid, e.role, e.label), 0) >= 2}
        if self.dead_click_guard:
            # A field that shows the text just typed into it is done: it is not offered again
            # while it still holds that text (retyping it looped on the club finder).
            lt, self._last_type = self._last_type, None
            if lt is not None and last.startswith("type:"):
                pid = page_id(screen.url)
                for e in screen.elements:
                    if (pid, e.role, e.label) == lt[0] and _same_text(e.value, lt[1]):
                        self._filled[lt[0]] = lt[1]
            if self._filled:
                pid = page_id(screen.url)
                dead_idx |= {e.index for e in screen.elements
                             if (pid, e.role, e.label) in self._filled
                             and _same_text(e.value, self._filled[(pid, e.role, e.label)])}
        jev_screen = screen.without(dead_idx) if dead_idx else screen
        if not self._steps and self.verify_done and history and has_done_clause(guide):
            # The playbook names its success condition: stop the moment it shows, before Jev
            # has a chance to click past it.
            verified = self._verify(goal, guide, screen, history)
            if verified[0] is True:
                return self._done_plan(screen, settled_s, verified[1])

        exclude: set = set()
        if self.max_scrolls > 0 and self._scroll_streak > 0:
            unchanged = self._scroll_fp is not None and screen.fingerprint() == self._scroll_fp
            cap = self.max_scrolls
            if self._steps and _SCROLL_STEP_RE.search(self._steps[self._step_i]):
                cap = self.max_scrolls * 3  # the step itself is a long way down the page
            if unchanged or self._scroll_streak >= cap:
                exclude |= {"SCROLL_DOWN", "SCROLL_UP"}
                self._note(history, "scrolled and the page did not change; choose a control"
                           if unchanged else "scrolled %d times without progress; choose a control"
                           % self._scroll_streak)

        if self.dead_click_guard and self._dead_streak >= 2 and "SCROLL_DOWN" not in exclude:
            # Two clicks in a row changed nothing: the control that matters is off screen.
            self._dead_streak = 0
            self._note(history, "2 clicks in a row changed nothing; scrolled down to reveal more")
            plan = self._scroll_plan(screen, "down")
            plan["observation"] = ("%d controls on '%s'" % (len(screen.elements), screen.title))[:160]
            plan["reasoning"] = "dead-click guard: 2 clicks changed nothing, scroll instead"
            plan["jev"] = {"settle_s": round(settled_s, 2), "controls": len(screen.elements),
                           "dead": sorted(dead_idx)}
            if exclude:
                plan["jev"]["excluded"] = sorted(exclude)
            self._scroll_streak += 1
            self._scroll_fp = screen.fingerprint()
            return plan, json.dumps({"dead_click_scroll": True})

        focus = self._focus_text(guide)
        if focus and screen.offscreen and not any(
                label_named_in(e.label, focus) for e in jev_screen.elements if "CLICK" in e.operations()):
            o = self._named_offscreen(screen, focus)
            if o is not None:
                # The playbook names this control and it is on the page, just not in view: scroll
                # straight to it, no decision needed.
                key = (page_id(screen.url), o["label"])
                self._named_tries[key] = self._named_tries.get(key, 0) + 1
                top = float(screen.viewport.get("scrollY") or 0)
                h = float(screen.viewport.get("h") or 800)
                y = float(o["y"]) - top
                direction = "up" if y < 0 else "down"
                dist = -y if y < 0 else y - h
                plan = self._scroll_plan(screen, direction)
                plan["amount"] = 2 if dist < 500 else (Config.jev_scroll_notches if dist < 2500 else 5)
                plan["observation"] = ("%d controls on '%s'" % (len(screen.elements), screen.title))[:160]
                plan["reasoning"] = ("scroll to named control '%s' (%s, %d px)"
                                     % (str(o["label"])[:60], direction, dist))[:200]
                plan["jev"] = {"scroll_to": str(o["label"])[:80], "settle_s": round(settled_s, 2),
                               "controls": len(screen.elements)}
                if self._steps:
                    plan["jev"]["step"] = "%d/%d" % (self._step_i + 1, len(self._steps))
                if verified is not None:
                    plan["jev"]["verify"] = {"done": verified[0], "reason": verified[1]}
                self._note(history, "scrolled %s toward '%s'" % (direction, str(o["label"])[:60]))
                return plan, json.dumps({"scroll_to": o["label"]})

        res = self._ask(goal, guide, jev_screen, history, exclude)
        verified_done = False
        if self.verify_done and res["answers"]["operation"]["choice"] == "DONE":
            if verified is None:
                verified = self._verify(goal, guide, screen, history)
            if verified[0] is False:
                # Not done: tell Jev why and ask again without DONE on the menu.
                self._note(history, "DONE rejected by check: %s" % verified[1])
                exclude = exclude | {"DONE"}
                res = self._ask(goal, guide, jev_screen, history, exclude)
            elif verified[0] is True:
                verified_done = True
        answers = res["answers"]
        usage = res["usage"]
        op = answers["operation"]
        operation = op["choice"]
        op_p = float(op.get("confidence") or 0.0)
        probs = op.get("probabilities") or {}
        if not op_p and probs:
            op_p = float(probs.get(operation) or 0.0)

        target_ans = {"CLICK": answers.get("click_target"), "TYPE_TEXT": answers.get("type_target"),
                      "SELECT": answers.get("select_target")}.get(operation)
        tgt_p = 1.0
        if target_ans:
            tgt_p = float(target_ans.get("confidence") or 0.0)
            if not tgt_p:
                tgt_p = float((target_ans.get("probabilities") or {}).get(target_ans.get("choice")) or 0.0)

        step_named = False
        if focus and operation == "CLICK" and target_ans:
            step = focus
            chosen = screen.by_index(target_ans["choice"])
            if label_named_in(chosen.label, step):
                step_named = True
            else:
                named = [e for e in jev_screen.elements
                         if "CLICK" in e.operations() and label_named_in(e.label, step)]
                miss = near_miss(chosen.label, step)
                if len(named) == 1:
                    # The step names exactly one control on screen word for word: click that one.
                    self._note(history, "step names '%s'; clicked it instead of '%s'"
                               % (named[0].label[:50], chosen.label[:50]))
                    target_ans = {"type": "choice", "choice": named[0].index,
                                  "probabilities": {named[0].index: 1.0}, "named_by_step": True}
                    tgt_p, step_named = 1.0, True
                elif miss and "SCROLL_DOWN" not in exclude:
                    # "Oakland 11U Fuca" when the step says "Oakland 14U Fuca": the right one is
                    # not on screen yet. Do not take the look-alike; scroll to find it.
                    self._step_clicks[(page_id(screen.url), chosen.role, chosen.label)] = 2
                    self._note(history, "'%s' is not the '%s' the step names; scrolled to find it"
                               % (chosen.label[:50], miss[:50]))
                    plan = self._scroll_plan(screen, "down")
                    plan["observation"] = ("%d controls on '%s'" % (len(screen.elements), screen.title))[:160]
                    plan["reasoning"] = "near miss: '%s' is not '%s'" % (chosen.label[:50], miss[:50])
                    plan["jev"] = {"near_miss": [chosen.label[:80], miss[:80]], "cost": usage.get("cost")}
                    if self._steps:
                        plan["jev"]["step"] = "%d/%d" % (self._step_i + 1, len(self._steps))
                    self._scroll_streak += 1
                    self._scroll_fp = screen.fingerprint()
                    return plan, json.dumps(res["raw"], separators=(",", ":"))

        plan: dict = {}
        if operation in ("CLICK", "TYPE_TEXT", "SELECT"):
            el = screen.by_index(target_ans["choice"])
            try:
                fx, fy = el.screen_point(screen.viewport, screen.screen)
            except ValueError as e:
                return ({"action": "verify_stop", "reason": str(e),
                         "observation": "control off the main display", "reasoning": "jev %s" % operation},
                        json.dumps(res["raw"], separators=(",", ":")))
            desc = "[%s] %s %s" % (el.index, el.role, el.label)
            plan["action"] = {"CLICK": "click", "TYPE_TEXT": "type", "SELECT": "select"}[operation]
            plan["target"] = desc
            plan["point"] = [fx, fy]
            if operation == "TYPE_TEXT":
                plan["text"] = self._type_value(goal, guide, screen, el, res, history)
                plan["select_all"] = True
                self._last_type = ((page_id(screen.url), el.role, el.label), plan["text"])
            elif operation == "SELECT":
                plan["text"] = self._select_option(goal, screen, el, history)
        elif operation == "KEY_ENTER":
            plan = {"action": "key", "keys": "return"}
        elif operation in ("SCROLL_DOWN", "SCROLL_UP"):
            plan = self._scroll_plan(screen, "down" if operation == "SCROLL_DOWN" else "up")
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

        # On a multi-step task a click whose target is near certain (p >= 0.8) is not noise even
        # when the operation head is split (0.2 to 0.35 between CLICK and SCROLL).
        sure_target = (bool(self._steps) and operation == "CLICK" and tgt_p >= 0.8 and op_p >= 0.2)
        low_conf = (operation not in ("WAIT", "SCROLL_DOWN", "SCROLL_UP") and not verified_done
                    and not step_named and not sure_target
                    and (op_p < self.min_confidence or tgt_p < self.min_target_confidence))
        bad_type = operation == "TYPE_TEXT" and not field_accepts(
            screen.by_index(target_ans["choice"]) if target_ans else None, plan.get("text", ""),
            check_email=bool(self._steps))
        if (operation == "VERIFY_STOP" or low_conf or bad_type) and self._can_backtrack(screen) and \
                page_id(screen.url) not in self._backed_from and not (after_idle_wait and self._wait_grace < 2):
            # Lost on a page the current step did not lead to (a sign-in wall, a wrong link):
            # go back to the step's page instead of pausing for a human.
            return self._back_plan(history, screen, "Jev asked for a human" if operation == "VERIFY_STOP"
                                   else "sign-in field or a value that does not fit it" if bad_type
                                   else "low confidence %s p=%.2f" % (operation, op_p))
        if bad_type:
            # Never type into a password field, and never put a non-email into an email field:
            # that is a sign-in form the playbook did not ask for (a human signs in).
            plan = {"action": "verify_stop",
                    "reason": "TYPE_TEXT %r into %s: a sign-in field or a value that does not fit it"
                              % (plan.get("text", "")[:40], plan.get("target", ""))}
        elif verified_done:
            pass  # a DONE the check confirmed stands whatever Jev's confidence
        elif low_conf:
            if after_idle_wait and self.loading_wait_s > 0 and self._wait_grace < 2:
                # A WAIT that left the page unchanged is not evidence against the page: the
                # content is still coming. Wait again (twice at most) rather than pause.
                self._wait_grace += 1
                self._note(history, "waited; page unchanged; waiting again before deciding")
                plan = {"action": "wait", "seconds": 1.5}
            else:
                plan = {"action": "verify_stop",
                        "reason": "low confidence %s p=%s target p=%s"
                                  % (operation, round(op_p, 2), round(tgt_p, 2))}
        else:
            commit_desc = None
            if operation == "CLICK" and is_commit_control(plan.get("target", "")):
                commit_desc = plan["target"]
            elif operation == "KEY_ENTER" and any(is_commit_control(e.label) for e in screen.elements):
                commit_desc = "KEY_ENTER on a page with a commit control"
            if commit_desc is not None:
                approved = (history and history[-1].startswith("[human")
                            and self._pending_commit == (screen.url, commit_desc))
                if not approved:
                    self._pending_commit = (screen.url, commit_desc)
                    plan = {"action": "verify_stop",
                            "reason": "commit step %s: confirm the value on screen, then CONTINUE"
                                      % commit_desc}
                else:
                    self._pending_commit = None

        plan["observation"] = "%d controls on '%s'" % (len(screen.elements), screen.title)
        plan["observation"] = plan["observation"][:160]
        plan["reasoning"] = "jev %s p=%.2f" % (operation, op_p)
        if target_ans:
            plan["reasoning"] += " target [%s] p=%.2f" % (target_ans.get("choice"), tgt_p)
        plan["jev"] = {"operation": probs, "target": (target_ans or {}).get("probabilities", {}),
                       "cost": usage.get("cost"), "latency_s": res["latency_s"],
                       "settle_s": round(settled_s, 2), "controls": len(screen.elements)}
        if exclude:
            plan["jev"]["excluded"] = sorted(exclude)
        if self._steps:
            plan["jev"]["step"] = "%d/%d" % (self._step_i + 1, len(self._steps))
        if dead_idx:
            plan["jev"]["dead"] = sorted(dead_idx)
        if self._last_loading:
            plan["jev"]["loading"] = self._last_loading
        if verified is not None:
            plan["jev"]["verify"] = {"done": verified[0], "reason": verified[1]}
            if verified_done:
                plan["reasoning"] += " (verified: %s)" % verified[1][:120]
        if plan["action"] == "scroll":
            self._scroll_streak += 1
            self._scroll_fp = screen.fingerprint()
        elif plan["action"] != "verify_stop":
            self._scroll_streak, self._scroll_fp = 0, None
        if plan["action"] != "wait":
            self._wait_grace = 0
        if plan["action"] == "click" and operation == "CLICK":
            el = screen.by_index(target_ans["choice"])
            if self.dead_click_guard:
                ck = (page_id(screen.url), el.role, el.label)
                self._step_clicks[ck] = self._step_clicks.get(ck, 0) + 1
            self._last_click = {"index": el.index, "label": el.label, "key": self._dead_key(screen, el),
                                "fp": screen.fingerprint(), "url": screen.url, "title": screen.title,
                                "typable": el.operations() != ["CLICK"]}
        return plan, json.dumps(res["raw"], separators=(",", ":"))

    def _scroll_plan(self, screen: Screen, direction: str) -> dict:
        plan = {"action": "scroll", "direction": direction, "amount": Config.jev_scroll_notches}
        # Wheel input goes to whatever is under the pointer: park it over the page first,
        # or a pointer left on the Dock or another window scrolls nothing.
        pt = page_center(screen)
        if pt is not None:
            plan["point"] = list(pt)
        return plan

    @staticmethod
    def _dead_key(screen: Screen, el: Element) -> tuple:
        return (screen.url, el.role, el.label)

    def _is_dead(self, screen: Screen, el: Element) -> bool:
        # Only plain click targets: a text field or dropdown may show no change on a click
        # (focus only) and must stay available for TYPE_TEXT / SELECT.
        return (self.dead_click_guard and el.operations() == ["CLICK"]
                and self._dead_key(screen, el) in self._dead)

    def _check_dead_click(self, history: list[str], prev: Optional[Screen], screen: Screen) -> None:
        """After a CLICK (and the settle time), an unchanged table, URL and title mark the target
        dead for the rest of the run."""
        lc, self._last_click = self._last_click, None
        if not self.dead_click_guard or not history:
            return
        last = history[-1]
        if lc is None or not last.startswith("click:"):
            if not last.startswith(("wait:", "[human")):
                self._dead_streak = 0  # "in a row" means clicks only
            return
        unchanged = (screen.fingerprint() == lc["fp"] and screen.url == lc["url"]
                     and screen.title == lc["title"])
        if not unchanged or lc["typable"]:
            self._dead_streak = 0
            return
        self._dead.add(lc["key"])
        self._dead_streak += 1
        self._note(history, "clicked [%s] %s; nothing changed; choose something else"
                   % (lc["index"], lc["label"][:60]))

    def _wait_for_loading(self, history: list[str], prev: Screen,
                          screen: Screen) -> tuple[Screen, float]:
        """After a CLICK or Enter: when a loading signal appeared that the page did not show
        before (a disabled submit, aria-busy, a control reading "Reading it..."), poll the page
        without asking Jev until that signal clears, the URL changes, or the wait runs out."""
        new = screen.busy_keys() - prev.busy_keys()
        lc = self._last_click
        if lc is not None and history and history[-1].startswith("click:"):
            # A disabled submit with no loading text is a signal only when it is the control that
            # was clicked. Opening an empty form or chat panel shows a disabled submit too, and
            # that one never clears on its own (a 45 s wait for nothing).
            new = {k for k in new if k[1] != "disabled submit"
                   or k[0].strip().lower() == (lc.get("label") or "").strip().lower()}
        if not new:
            return screen, 0.0
        label = sorted(new)[0][0] or sorted(new)[0][1]
        url0 = screen.url
        t0 = time.time()
        cleared = False
        while time.time() - t0 < self.loading_wait_s:
            time.sleep(self.loading_poll)
            try:
                screen = self.reader.snapshot()
            except Exception as e:  # mid-navigation reads fail; keep polling
                print("loading wait: page read failed (%s); retrying" % str(e)[:120], flush=True)
                continue
            if not (screen.busy_keys() & new) or screen.url != url0:
                cleared = True
                break
        if cleared:
            screen = self._hold_still(screen)
        waited = time.time() - t0
        self._last_loading = {"signal": label[:80], "waited_s": round(waited, 1), "cleared": cleared}
        self._note(history, ("waited %.0f s while '%s' loaded" % (waited, label[:60])) if cleared else
                   ("waited %.0f s; '%s' is still loading" % (waited, label[:60])))
        return screen, waited

    def recover(self, hands) -> None:
        """Unblock the page reader. A native <select> popup or context menu freezes Safari's
        AppleEvents until it closes; Escape alone does not always close it, a click on the
        window's own toolbar does. Uses the last known window geometry."""
        scr = self.last_screen
        if scr and scr.viewport.get("ow"):
            vp = scr.viewport
            fx = (vp.get("sx", 0) + vp["ow"] / 2.0) / max(1, scr.screen[0])
            fy = (vp.get("sy", 0) + 12) / max(1, scr.screen[1])
            hands.move(fx, fy); time.sleep(0.15); hands.click(); time.sleep(0.3)
        hands.key("escape"); time.sleep(0.5)

    def _settled_snapshot(self, history: list[str]) -> tuple[Screen, float]:
        """Read the element table; after an action, keep re-reading (cheap, ~0.1 s) until the
        page differs from the table that action was planned on, or the settle timeout passes.
        Mirrors jev-ultrafast's "wait for useful state": the next decision is not made on the
        table the last action has just invalidated. (A page can still move during the ~0.4 s
        decision call; the hands click the point that was true when the table was read.)"""
        screen = self.reader.snapshot()
        prev = self.last_screen
        acted = bool(history) and not history[-1].startswith("[human")
        if prev is None or not acted:
            return screen, 0.0
        t0 = time.time()
        before = prev.fingerprint()
        # Unchanged, or a page with no controls yet (a route change that is still rendering):
        # keep reading until it moves on or the timeout passes.
        while (screen.fingerprint() == before or not screen.elements) and \
                time.time() - t0 < self.settle_timeout:
            time.sleep(self.settle_poll)
            screen = self.reader.snapshot()
        return self._hold_still(screen), time.time() - t0

    def _hold_still(self, screen: Screen) -> Screen:
        """Require the table to hold still: two reads 0.2 s apart with the same positions. A
        smooth scroll or a slide-in panel otherwise hands out coordinates that are already wrong
        by the time the hands arrive."""
        t1 = time.time()
        while time.time() - t1 < 0.8:
            time.sleep(0.2)
            again = self.reader.snapshot()
            if again.fingerprint() == screen.fingerprint():
                break
            screen = again
        return screen

    def _select_option(self, goal: str, screen: Screen, el: Element, history: list[str]) -> str:
        """Ask Jev which option of a native <select> serves the goal."""
        if not el.options:
            raise JevError("select %s has no options to choose from" % el.index)
        state = screen.to_jev_state(history)
        state["page"]["field"] = {"index": el.index, "label": el.label, "value": el.value}
        q = {"select_option": {"type": "choice", "criteria": {o: "option" for o in el.options},
                               "instructions": {"goal": goal,
                                                "task": "Pick the dropdown option that serves the goal."}}}
        ans = self.decider.ask(state, q)["answers"]["select_option"]
        p = float(ans.get("confidence") or (ans.get("probabilities") or {}).get(ans["choice"]) or 0.0)
        if p < self.min_target_confidence:
            raise JevError("no confident option for select %s (best %r p=%.2f)" % (el.index, ans["choice"], p))
        return ans["choice"]

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
            p = float(ans.get("confidence") or (ans.get("probabilities") or {}).get(ans["choice"]) or 0.0)
            if ans["choice"] != "__WRITE__" and p >= self.min_target_confidence:
                return ans["choice"]
        value = self.text_helper.write(goal, guide, screen, el, history)
        if not value or len(value) > 200 or "\n" in value:
            raise JevError("text helper returned an unusable value: %r" % value[:80])
        return value
