"""The autonomous loop: capture -> plan (brain) -> ground (eyes) -> act (hands) -> repeat.
Logs every step (jsonl + a screenshot per step) and pauses at money checkpoints."""
import os, json, time, shutil, subprocess
from .eyes import Eyes
from .config import Config

def safari_navigate(url):
    # The URL is an argv item, never part of the script text (a quote in it would inject AppleScript).
    subprocess.run(["osascript", "-e", "on run argv", "-e",
                    'tell application "Safari" to set URL of front document to item 1 of argv',
                    "-e", "end run", url], check=False)


def web_url(url):
    """navigate takes plain http(s) addresses only (a javascript: URL runs in the open site)."""
    from urllib.parse import urlparse
    u = str(url or "").strip()
    if any(ord(c) < 32 for c in u) or urlparse(u).scheme not in ("http", "https") or not urlparse(u).hostname:
        raise RuntimeError("navigate needs an http(s) address, got %r" % u[:80])
    return u


def _safe_name(name):
    """A downloaded file's name comes from the web; reduce it before the planner reads it."""
    import re
    return (re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)) or "file")[:60]

class Agent:
    def __init__(self, planner, grounder, hands, eyes=None, run_dir=None, browser_nav=True,
                 navigate_fn=None):
        self.planner = planner
        self.grounder = grounder
        self.hands = hands
        self.eyes = eyes or Eyes()
        self.browser_nav = browser_nav
        # How `navigate` opens a URL: an explicit function, else the hands' own (a VM target
        # drives its guest browser), else AppleScript into this Mac's Safari.
        self.navigate_fn = navigate_fn or getattr(hands, "navigate", None) or safari_navigate
        self.run_dir = run_dir or os.path.join(Config.runs_dir, "run")
        os.makedirs(self.run_dir, exist_ok=True)
        self.log_path = os.path.join(self.run_dir, "log.jsonl")
        self.history = []
        self.step = 0

    def _log(self, kind, summary, extra=None):
        obj = {"t": time.strftime("%H:%M:%S"), "step": self.step, "kind": kind, "summary": summary}
        if extra:
            obj.update(extra)
        with open(self.log_path, "a") as f:
            f.write(json.dumps(obj) + "\n")
        print("[%s] step %d %s: %s" % (obj["t"], self.step, kind, summary), flush=True)

    def run(self, goal, guide, max_steps=80):
        self._log("start", goal.strip().splitlines()[0][:100])
        self._seen_downloads = self._downloads_now()
        guide = guide + self._logins_note()
        for _ in range(max_steps):
            self.step += 1
            if getattr(self.planner, "needs_frame", True):
                frame = self.eyes.capture()
                w, h = self.eyes.dims(frame)
                shutil.copyfile(frame, os.path.join(self.run_dir, "step%03d.jpg" % self.step))
            else:
                # Jev planner reads the element table straight from Safari: no screenshot, no eyes.
                frame, (w, h) = None, (0, 0)
            try:
                plan, raw = self.planner.decide(goal, guide, frame, self.history, (w, h))
            except Exception as e:
                if not hasattr(self.planner, "recover"):
                    self._log("planner_error", str(e)); return "planner_error"
                # Jev lane only: a stuck popup or menu blocks the page reader; recover, retry once.
                self._log("planner_retry", str(e)[:200])
                try:
                    self.planner.recover(self.hands)
                    plan, raw = self.planner.decide(goal, guide, frame, self.history, (w, h))
                except Exception as e2:
                    self._log("planner_error", str(e2)[:300]); return "planner_error"
            act = plan.get("action", "")
            self._log("plan", "%s | %s" % (act, (plan.get("observation") or "")[:90]),
                      {"plan": plan})
            if act == "done":
                answer = plan.get("answer") or ""
                if answer:
                    with open(os.path.join(self.run_dir, "answer.txt"), "w") as f:
                        f.write(answer.strip() + "\n")
                self._log("done", answer or plan.get("reasoning", "")); return "done"
            if act == "verify_stop":
                if self._checkpoint(plan.get("reason", "")) == "abort":
                    return "aborted"
                self.history.append("[human reviewed checkpoint and approved -> continue]")
                continue
            try:
                self._execute(plan, (w, h), frame)
            except Exception as e:
                self._log("exec_error", "%s: %s" % (act, e))
                self.history.append("%s FAILED: %s" % (act, e))
                continue
            self.history.append(self._hist(plan))
            self._note_downloads()
            self._nudge_if_repeating()
        self._log("max_steps", "reached %d steps" % max_steps)
        return "max_steps"

    def _logins_note(self):
        """Tell the planner which logins it may use, by name only."""
        fn = getattr(self.hands, "vault", None)
        try:
            logins = fn() if fn else []
        except Exception:
            logins = []
        allowed = self._allowed_logins()
        logins = [l for l in logins if l["name"] in allowed]
        if not logins:
            return ""
        rows = "\n".join("- %s: %s, user %s%s" % (l["name"], l["site"], l["user"] or "?",
                                                    ", authenticator codes available" if l.get("totp") else "")
                         for l in logins)
        return ("\n\nLOGINS YOU MAY USE (by name; you never see the values):\n" + rows +
                "\nTo sign in, use action fill_secret with credential=<name>, field=username|password|totp and "
                "target=<the input box>. Never type a password with 'type'. A push approval or SMS code "
                "needs the owner: verify_stop and say so.")

    @staticmethod
    def _allowed_logins():
        """Vault logins this run may use: GH_ALLOWED_LOGINS (the job worker sets it from
        `ghostbox do --login NAME`). None by default, so a page cannot talk a job into one."""
        return {n.strip() for n in os.environ.get("GH_ALLOWED_LOGINS", "").split(",") if n.strip()}

    def _downloads_now(self):
        """Hands that can see a download folder (the box) report it; others return None."""
        fn = getattr(self.hands, "downloads", None)
        if not fn:
            return None
        try:
            d = fn()
            return {n: b for n, b in d.get("done", [])}, set(d.get("pending", []))
        except Exception:
            return None

    def _note_downloads(self):
        """A download saves silently (no viewer, no prompt), so tell the planner: without this it
        clicks Download again and again. Waits briefly for a download that is still running."""
        if getattr(self, "_seen_downloads", None) is None:
            return
        for _ in range(20):
            now = self._downloads_now()
            if now is None or not now[1]:
                break
            time.sleep(1.0)
        if now is None:
            return
        seen = self._seen_downloads[0]
        for name, size in sorted(now[0].items()):
            if seen.get(name) != size:
                note = "[DOWNLOADED: %s (%d KB) -- saved; do not download it again]" % (_safe_name(name), max(1, size // 1024))
                self.history.append(note)
                self._log("download", note)
        for name in sorted(now[1]):
            self.history.append("[DOWNLOADING: %s -- still in progress]" % _safe_name(name))
        self._seen_downloads = now

    def _nudge_if_repeating(self):
        """A small planner can loop: the same navigate or click again and again while the page
        stays put. After the same action 3 times in the last 4 steps, tell it so, once per run of
        repeats; the next decision then sees the note in its history."""
        acts = [h for h in self.history if not h.startswith("[")][-4:]
        last = acts[-1] if acts else ""
        if acts.count(last) >= 3 and not (self.history[-2:-1] and self.history[-2].startswith("[REPEATING")):
            note = ("[REPEATING: '%s' %d times without reaching the goal. Do NOT do it again. "
                    "If what is on screen already answers the goal, choose done and put it in answer; "
                    "otherwise try a different route or verify_stop.]" % (last[:80], acts.count(last)))
            self.history.append(note)
            self._log("nudge", note[:120])

    def _checkpoint(self, reason):
        cp = os.path.join(self.run_dir, "CHECKPOINT_step%03d.txt" % self.step)
        open(cp, "w").write(reason + "\n")
        self._log("checkpoint", "PAUSED for human review: " + reason)
        cont = os.path.join(self.run_dir, "CONTINUE")
        abrt = os.path.join(self.run_dir, "ABORT")
        while True:
            if os.path.exists(cont):
                os.remove(cont); self._log("resume", "human approved continue"); return "continue"
            if os.path.exists(abrt):
                os.remove(abrt); self._log("abort", "human aborted"); return "abort"
            time.sleep(2.0)

    def _hist(self, plan):
        a = plan.get("action", "")
        if a == "fill_secret":
            return ("fill_secret: %s %s into %s" % (plan.get("credential"), plan.get("field"), plan.get("target", "")))[:130]
        d = plan.get("target") or plan.get("text") or plan.get("keys") or plan.get("url") or ""
        return ("%s: %s" % (a, d))[:130]

    def _point(self, plan, frame, desc, dims):
        """Screen point as (fx, fy) fractions. A plan that already carries a `point`
        (Jev + Safari DOM reader) skips the grounding model entirely."""
        pt = plan.get("point")
        if pt:
            fx, fy = float(pt[0]), float(pt[1])
            self._log("point", "%s -> (%.3f,%.3f) from element table" % (desc[:50], fx, fy))
            return (fx, fy)
        x, y, frac, raw = self.grounder.locate(frame, desc, dims)
        self._log("ground", "%s -> %d,%d (%.3f,%.3f)" % (desc[:50], x, y, frac[0], frac[1]))
        return frac

    def _execute(self, plan, dims, frame):
        w, h = dims
        a = plan.get("action", "")
        if a in ("click", "double_click"):
            desc = plan.get("target", "")
            frac = self._point(plan, frame, desc, dims)
            self.hands.move(frac[0], frac[1]); time.sleep(0.1)
            if a == "double_click" and hasattr(self.hands, "double_click"):
                self.hands.double_click()
            else:
                self.hands.click()
                if a == "double_click":
                    time.sleep(0.09); self.hands.click()
        elif a == "type":
            if plan.get("point"):
                # Jev planner: the field is already located; focus it, clear it, then type.
                frac = self._point(plan, frame, plan.get("target", ""), dims)
                self.hands.move(frac[0], frac[1]); time.sleep(0.1)
                self.hands.click(); time.sleep(0.15)
                if plan.get("select_all"):
                    self.hands.key("cmd+a"); time.sleep(0.08)
            self.hands.type(plan.get("text", ""))
        elif a == "select":
            # Native <select>: open it, type the option (popup type-ahead), confirm with Return.
            # One sequence, because the open popup blocks the page until it closes.
            frac = self._point(plan, frame, plan.get("target", ""), dims)
            self.hands.move(frac[0], frac[1]); time.sleep(0.1)
            self.hands.click(); time.sleep(0.5)
            # Popup type-ahead: a Space would confirm the highlighted item, so type only the
            # option's first word (up to 8 characters), then Return.
            prefix = (plan.get("text", "").split(" ")[0])[:8]
            self.hands.type(prefix); time.sleep(0.3)
            self.hands.key("return"); time.sleep(0.3)
        elif a == "fill_secret":
            if not hasattr(self.hands, "fill_secret"):
                raise RuntimeError("these hands have no vault")
            if plan.get("credential") not in self._allowed_logins():
                raise RuntimeError("login %r was not given to this job" % plan.get("credential"))
            frac = self._point(plan, frame, plan.get("target", ""), dims)
            self.hands.fill_secret(plan.get("credential", ""), plan.get("field", "password"), frac[0], frac[1])
        elif a == "key":
            self.hands.key(plan.get("keys", ""))
        elif a == "scroll":
            if plan.get("point"):
                # Jev planner: wheel input lands under the pointer, so park it over the page.
                self.hands.move(float(plan["point"][0]), float(plan["point"][1])); time.sleep(0.1)
            amt = int(plan.get("amount", 5))
            if plan.get("direction") == "up":
                amt = -abs(amt)
            self.hands.scroll(amt)
        elif a == "navigate":
            if self.browser_nav:
                self.navigate_fn(web_url(plan.get("url", ""))); time.sleep(2.2)
            else:
                raise RuntimeError("navigate disabled")
        elif a == "wait":
            time.sleep(float(plan.get("seconds", 1.5)))
        else:
            raise RuntimeError("unknown action %r" % a)
