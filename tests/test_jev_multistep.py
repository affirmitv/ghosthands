"""Offline tests for multi-step tasks: subgoals, the progress trail, back-navigation recovery,
the repeat-click and near-miss guards (no network)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OPENROUTER_API_KEY"] = "test"

from ghosthands.dom_reader import Screen  # noqa: E402
from ghosthands.dom_reader import Element  # noqa: E402
from ghosthands.jev import (DoneVerifier, JevDecider, JevPlanner, compact_page,  # noqa: E402
                            field_accepts, label_named_in, near_miss, page_id, scrub_guide,
                            split_subgoals)

VP = {"w": 1000, "h": 800, "sx": 100, "sy": 50, "ow": 1000, "oh": 900, "sw": 2000, "sh": 1600}
SCR = (2000, 1600)
GUIDE = ("First click the Teams tab. Then click the Oakland 14U Fuca link. "
         "Then click the first game. DONE when a game page shows. Never sign in.")


def screen(labels, url="https://example.com/home", text="page text", title="T", busy=None,
           roles=None, vtext="", offscreen=None, scroll_y=0):
    els = [{"label": l, "role": (roles or {}).get(l, "link"), "value": "", "x": 100,
            "y": 100 + 40 * i, "w": 80, "h": 30} for i, l in enumerate(labels)]
    data = {"title": title, "url": url, "text": text, "viewport": dict(VP, scrollY=scroll_y),
            "elements": els, "vtext": vtext, "offscreen": offscreen or []}
    if busy is not None:
        data["busy"] = busy
    return Screen.from_json(data, SCR)


class ListReader:
    def __init__(self, screens):
        self.screens, self.calls = list(screens), 0

    def snapshot(self):
        s = self.screens[min(self.calls, len(self.screens) - 1)]
        self.calls += 1
        return s


class SwitchReader:
    """Returns `cur`; tests move it by hand between decisions."""

    def __init__(self, s):
        self.cur = s

    def snapshot(self):
        return self.cur


class CtxDecider:
    """Scripted operations; the click target is picked by label (or the first clickable).
    Records the context and the labels offered, and accepts the context keyword."""

    model = "fake-jev"

    def __init__(self, script, op_p=0.9, tgt_p=0.9):
        self.script = list(script)  # op or (op, label)
        self.op_p, self.tgt_p = op_p, tgt_p
        self.calls = []  # dicts
        self.total_calls, self.total_cost = 0, 0.0

    def decide(self, goal, guide, scr, history, exclude=None, context=None):
        self.calls.append({"exclude": set(exclude or ()), "history": list(history),
                           "labels": [e.label for e in scr.elements], "context": context})
        self.total_calls += 1
        while self.script and (self.script[0] if isinstance(self.script[0], str)
                               else self.script[0][0]) in (exclude or ()):
            self.script.pop(0)
        item = self.script.pop(0) if self.script else "WAIT"
        op, label = (item, None) if isinstance(item, str) else item
        clickable = [e for e in scr.elements if "CLICK" in e.operations()]
        pick = next((e for e in clickable if e.label == label), clickable[0] if clickable else None)
        idx = pick.index if pick else "1"
        a = {"operation": {"type": "choice", "choice": op, "probabilities": {op: self.op_p}},
             "click_target": {"type": "choice", "choice": idx, "probabilities": {idx: self.tgt_p}}}
        return {"answers": a, "usage": {"cost": 0.0001}, "latency_s": 0.0, "raw": {"answers": a}}


class OldDecider(CtxDecider):
    """The pre-multistep signature: no context keyword."""

    def decide(self, goal, guide, scr, history, exclude=None):
        return CtxDecider.decide(self, goal, guide, scr, history, exclude)


class ProgressVerifier:
    """check_progress answers from a script of (done, step_done); check() is the old path."""

    model = "fake-text"

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []  # (current step index, trail)
        self.old_calls = 0
        self.total_calls, self.total_cost = 0, 0.0

    def check(self, goal, guide, scr, history=None):
        self.old_calls += 1
        self.total_calls += 1
        return False, "old check"

    def check_progress(self, goal, guide, scr, history, steps, current, trail):
        self.calls.append((current, list(trail)))
        self.total_calls += 1
        done, step_done = self.script.pop(0) if self.script else (False, False)
        return done, "scripted", step_done


class FakeText:
    def write(self, *a, **k):
        return "x"


def planner(reader, decider, verifier=None, **kw):
    kw.setdefault("loading_wait_s", 0)
    p = JevPlanner(reader=reader, decider=decider, text_helper=FakeText(),
                   verifier=verifier or ProgressVerifier(), min_confidence=0.3,
                   min_target_confidence=0.1, **kw)
    p.settle_timeout = 0.0
    p.settle_poll = 0.0
    return p


def hist_for(plan):
    return "%s: %s" % (plan["action"], plan.get("target") or plan.get("keys") or plan.get("url") or "")


class TestSplitSubgoals(unittest.TestCase):
    def test_sentences_and_then(self):
        steps = split_subgoals("g", GUIDE)
        self.assertEqual(steps, ["click the Teams tab", "click the Oakland 14U Fuca link",
                                 "click the first game"])

    def test_quoted_label_is_not_split(self):
        steps = split_subgoals("g", "First click 'No link handy? Watch it read.' and wait. "
                                    "Then click Start trial.")
        self.assertEqual(len(steps), 2)
        self.assertIn("'No link handy? Watch it read.'", steps[0])

    def test_single_step_is_off(self):
        self.assertEqual(split_subgoals("Open the team.", "Click Oakland 14U. DONE when it shows."), [])

    def test_goal_fallback(self):
        steps = split_subgoals("Open the Pull requests tab, open PR #4, and open its Files tab.", "")
        self.assertEqual(steps, ["Open the Pull requests tab", "open PR #4", "open its Files tab"])

    def test_context_sentence_without_action_is_dropped(self):
        steps = split_subgoals("g", "This is the club page. Click Teams. Then click 14U.")
        self.assertEqual(steps, ["Click Teams", "click 14U"])


class TestHelpers(unittest.TestCase):
    def test_scrub_keeps_other_sentences(self):
        out = scrub_guide("Type Fury into the box. Then click Fury. Never type a password.")
        self.assertIn("Then click Fury", out)
        self.assertNotIn("password", out.replace("credential", ""))
        self.assertEqual(scrub_guide("Password: hunter2"), "[line withheld: credential]")

    def test_named_and_near_miss(self):
        self.assertTrue(label_named_in("Start 14-day free trial", "then click Start 14-day free trial"))
        self.assertFalse(label_named_in("Start", "click Start free"))
        self.assertEqual(near_miss("Oakland 11U Fuca", "click the Oakland 14U Fuca link"), "oakland 14u fuca")
        self.assertIsNone(near_miss("Oakland 14U Boyd", "click the Oakland 14U Fuca link"))
        self.assertIsNone(near_miss("Oakland 14U Fuca", "click the Oakland 14U Fuca link"))

    def test_page_id_drops_fragment(self):
        self.assertEqual(page_id("https://firmi.ai/#try"), "https://firmi.ai/")

    def test_compact_page_uses_text_in_view(self):
        s = screen(["A"], text="top of page", vtext="What does it cost? Basic is $100 a month.")
        out = compact_page(s)
        self.assertIn("TEXT IN VIEW: What does it cost?", out)
        self.assertIn("TOP OF PAGE: top of page", out)
        self.assertIn("VISIBLE TEXT: top", compact_page(screen(["A"], text="top")))

    def test_build_questions_carries_context(self):
        q = JevDecider(api_key="x").build_questions(
            "g", "p", screen(["A"]), [], context={"current_step": "step 2 of 3: click A"})
        self.assertEqual(q["operation"]["instructions"]["current_step"], "step 2 of 3: click A")
        self.assertIn("CURRENT STEP (step 2 of 3: click A)", q["click_target"]["instructions"]["task"])


class TestSubgoalTracking(unittest.TestCase):
    def test_step_advances_and_context_follows(self):
        home = screen(["Teams", "Other"])
        teams = screen(["Oakland 14U Fuca", "Other"], url="https://example.com/teams", title="Teams")
        reader = SwitchReader(home)
        dec = CtxDecider([("CLICK", "Teams"), ("CLICK", "Oakland 14U Fuca")])
        ver = ProgressVerifier([(False, True)])
        p = planner(reader, dec, ver)
        plan1, _ = p.decide("g", GUIDE, None, [], (0, 0))
        self.assertEqual(dec.calls[0]["context"]["current_step"], "step 1 of 3: click the Teams tab")
        reader.cur = teams
        plan2, _ = p.decide("g", GUIDE, None, [hist_for(plan1)], (0, 0))
        ctx = dec.calls[1]["context"]
        self.assertEqual(ctx["current_step"], "step 2 of 3: click the Oakland 14U Fuca link")
        self.assertEqual(ctx["steps_done"], ["step 1: click the Teams tab"])
        self.assertTrue(any(t.startswith("opened 'Teams'") for t in ctx["progress"]))
        self.assertTrue(any(t.startswith("step 1 done") for t in ctx["progress"]))
        self.assertEqual(plan2["jev"]["step"], "2/3")
        self.assertEqual(ver.old_calls, 0)
        self.assertEqual(len(ver.calls), 1)  # one small-model call per step, none on step 1

    def test_progress_check_done_stops(self):
        reader = SwitchReader(screen(["A"]))
        dec = CtxDecider(["CLICK", "CLICK"])
        p = planner(reader, dec, ProgressVerifier([(True, True)]))
        plan1, _ = p.decide("g", GUIDE, None, [], (0, 0))
        reader.cur = screen(["B"], url="https://example.com/game")
        plan2, _ = p.decide("g", GUIDE, None, [hist_for(plan1)], (0, 0))
        self.assertEqual(plan2["action"], "done")
        self.assertEqual(len(dec.calls), 1)

    def test_flag_off_keeps_old_path(self):
        reader = SwitchReader(screen(["A"]))
        dec = OldDecider(["CLICK", "CLICK"])
        ver = ProgressVerifier()
        p = planner(reader, dec, ver, subgoals=False)
        plan1, _ = p.decide("g", GUIDE, None, [], (0, 0))
        reader.cur = screen(["B"], url="https://example.com/2")
        p.decide("g", GUIDE, None, [hist_for(plan1)], (0, 0))
        self.assertEqual(len(ver.calls), 0)
        self.assertEqual(ver.old_calls, 1)  # the DONE clause check as before
        self.assertNotIn("step", plan1["jev"])

    def test_single_step_guide_keeps_old_path(self):
        reader = SwitchReader(screen(["A"]))
        dec = OldDecider(["CLICK"])  # would raise TypeError if a context were passed
        p = planner(reader, dec, ProgressVerifier())
        plan, _ = p.decide("g", "Click A. DONE when B shows.", None, [], (0, 0))
        self.assertEqual(plan["action"], "click")


class TestBacktrack(unittest.TestCase):
    def _lost(self, after=3):
        home = screen(["Teams", "Wrong link"])
        wrong = screen(["X", "Y"], url="https://example.com/wrong", title="Wrong")
        reader = SwitchReader(home)
        dec = CtxDecider([("CLICK", "Wrong link"), "SCROLL_DOWN", "CLICK", "CLICK", "CLICK", "CLICK"])
        p = planner(reader, dec, ProgressVerifier(), backtrack_after=after, dead_click_guard=True)
        return p, reader, dec, home, wrong

    def test_back_after_n_decisions_off_home_marks_wrong_turn(self):
        p, reader, dec, home, wrong = self._lost(after=3)
        hist = []
        plan, _ = p.decide("g", GUIDE, None, hist, (0, 0))
        self.assertEqual(plan["target"], "[2] link Wrong link")
        hist.append(hist_for(plan))
        reader.cur = wrong
        actions = []
        for _ in range(3):
            plan, _ = p.decide("g", GUIDE, None, hist, (0, 0))
            actions.append(plan["action"])
            hist.append(hist_for(plan))
            if plan["action"] == "navigate":
                break
            reader.cur = screen(["X", "Y", "Z%d" % len(hist)], url="https://example.com/wrong", title="Wrong")
        self.assertEqual(actions[-1], "navigate")
        self.assertEqual(plan["url"], "https://example.com/home")  # the last good page
        self.assertEqual(plan["jev"]["wrong_turn"][2], "Wrong link")
        # Back on the home page: the wrong turn is not offered again.
        reader.cur = home
        plan, _ = p.decide("g", GUIDE, None, hist, (0, 0))
        self.assertNotIn("Wrong link", dec.calls[-1]["labels"])
        self.assertTrue(any("was a wrong turn" in h for h in dec.calls[-1]["history"]))

    def test_verify_stop_off_home_goes_back_once(self):
        home = screen(["Sign in to Club", "Other"])
        wall = screen(["Email", "Sign In"], url="https://example.com/portal", title="Portal")
        reader = SwitchReader(home)
        dec = CtxDecider([("CLICK", "Sign in to Club"), "VERIFY_STOP"])
        p = planner(reader, dec, ProgressVerifier(), backtrack_after=4)
        plan, _ = p.decide("g", GUIDE, None, [], (0, 0))
        reader.cur = wall
        plan2, _ = p.decide("g", GUIDE, None, [hist_for(plan)], (0, 0))
        self.assertEqual(plan2["action"], "navigate")
        self.assertIn("asked for a human", plan2["jev"]["backtrack"])

    def test_verify_stop_on_home_page_stands(self):
        reader = SwitchReader(screen(["A"]))
        p = planner(reader, CtxDecider(["VERIFY_STOP"]), ProgressVerifier(), backtrack_after=4)
        plan, _ = p.decide("g", GUIDE, None, [], (0, 0))
        self.assertEqual(plan["action"], "verify_stop")

    def test_back_that_does_not_navigate_turns_backtracking_off(self):
        p, reader, dec, home, wrong = self._lost(after=1)
        plan, _ = p.decide("g", GUIDE, None, [], (0, 0))
        reader.cur = wrong
        with mock.patch("ghosthands.jev.Config.jev_back", "cmd+left"):
            plan, _ = p.decide("g", GUIDE, None, [hist_for(plan)], (0, 0))
        self.assertEqual(plan["action"], "key")
        self.assertEqual(plan["keys"], "cmd+left")
        # The page did not change after Back.
        plan, _ = p.decide("g", GUIDE, None, ["click: x", hist_for(plan)], (0, 0))
        self.assertNotEqual(plan["action"], "key")
        self.assertTrue(any("did not navigate" in h for h in dec.calls[-1]["history"]))

    def test_backtrack_off(self):
        p, reader, dec, home, wrong = self._lost(after=0)
        hist = []
        plan, _ = p.decide("g", GUIDE, None, hist, (0, 0))
        hist.append(hist_for(plan))
        reader.cur = wrong
        for i in range(6):
            plan, _ = p.decide("g", GUIDE, None, hist, (0, 0))
            self.assertNotIn(plan["action"], ("key", "navigate"))
            hist.append(hist_for(plan))
            reader.cur = screen(["X", "Y", "Z%d" % i], url="https://example.com/wrong")


class TestStepGuards(unittest.TestCase):
    def test_toggle_is_not_offered_a_third_time(self):
        a = screen(["Chat", "Teams"], vtext="closed")
        b = screen(["Chat", "Teams"], vtext="open", text="chat open")
        reader = SwitchReader(a)
        dec = CtxDecider([("CLICK", "Chat")] * 3)
        p = planner(reader, dec, ProgressVerifier(), dead_click_guard=True)
        hist = []
        for i in range(3):
            plan, _ = p.decide("g", GUIDE, None, hist, (0, 0))
            hist.append(hist_for(plan))
            reader.cur = b if i % 2 == 0 else a
        self.assertIn("Chat", dec.calls[1]["labels"])
        self.assertNotIn("Chat", dec.calls[2]["labels"])

    def test_toggle_guard_on_single_step_task_and_flag_off(self):
        a = screen(["GET FIRMI", "AppSpace"], url="https://firmi.ai/")
        b = screen(["GET FIRMI", "AppSpace"], url="https://firmi.ai/#get-firmi", text="form")
        for guard, offered in ((True, False), (False, True)):
            reader = SwitchReader(a)
            dec = CtxDecider([("CLICK", "GET FIRMI")] * 3)
            p = planner(reader, dec, ProgressVerifier(), dead_click_guard=guard, verify_done=False)
            hist = []
            for i in range(3):
                plan, _ = p.decide("g", "Click Start free.", None, hist, (0, 0))
                hist.append(hist_for(plan))
                reader.cur = b if i % 2 == 0 else a
            self.assertEqual("GET FIRMI" in dec.calls[2]["labels"], offered)

    def test_step_named_control_replaces_jev_pick(self):
        s = screen(["Oakland 11U Fuca", "Oakland 14U Fuca"])
        dec = CtxDecider([("CLICK", "Oakland 11U Fuca")])
        p = planner(SwitchReader(s), dec, ProgressVerifier())
        p._steps = split_subgoals("g", GUIDE)
        p._step_i = 1
        plan, _ = p.decide("g", GUIDE, None, [], (0, 0))
        self.assertEqual(plan["target"], "[2] link Oakland 14U Fuca")

    def test_near_miss_scrolls_instead(self):
        s = screen(["Oakland 11U Fuca", "Other"])
        dec = CtxDecider([("CLICK", "Oakland 11U Fuca")])
        p = planner(SwitchReader(s), dec, ProgressVerifier())
        p._steps = split_subgoals("g", GUIDE)
        p._step_i = 1
        plan, _ = p.decide("g", GUIDE, None, [], (0, 0))
        self.assertEqual(plan["action"], "scroll")
        self.assertEqual(plan["jev"]["near_miss"][1], "oakland 14u fuca")

    def test_step_named_target_passes_low_confidence(self):
        s = screen(["Start 14-day free trial", "Chat"])
        guide = "Click Run sample and wait. Then click Start 14-day free trial."
        dec = CtxDecider([("CLICK", "Start 14-day free trial")], op_p=0.2)
        p = planner(SwitchReader(s), dec, ProgressVerifier())
        p._steps = split_subgoals("g", guide)
        p._step_i = 1
        plan, _ = p.decide("g", guide, None, [], (0, 0))
        self.assertEqual(plan["action"], "click")

    def test_low_confidence_elsewhere_still_pauses(self):
        s = screen(["Chat", "Other"])
        p = planner(SwitchReader(s), CtxDecider([("CLICK", "Chat")], op_p=0.2, tgt_p=0.5),
                    ProgressVerifier())
        plan, _ = p.decide("g", GUIDE, None, [], (0, 0))
        self.assertEqual(plan["action"], "verify_stop")

    def test_scroll_step_allows_more_scrolls(self):
        guide = "Scroll down to the FAQ and find the cost question. Then click Get Firmi."
        dec = CtxDecider(["SCROLL_DOWN"] * 8)
        p = planner(SwitchReader(screen(["A"])), dec, ProgressVerifier(), max_scrolls=3)
        hist = []
        for i in range(6):
            p.reader.cur = screen(["A%d" % i])
            plan, _ = p.decide("g", guide, None, hist, (0, 0))
            hist.append("scroll: ")
            self.assertEqual(plan["action"], "scroll")


class TypeDecider(CtxDecider):
    """TYPE_TEXT into the first text field, then whatever the script says."""

    def decide(self, goal, guide, scr, history, exclude=None, context=None):
        r = CtxDecider.decide(self, goal, guide, scr, history, exclude, context)
        typable = [e.index for e in scr.elements if "TYPE_TEXT" in e.operations()]
        if typable:
            r["answers"]["type_target"] = {"type": "choice", "choice": typable[0],
                                           "probabilities": {typable[0]: 0.9}}
        return r

    def ask(self, state, questions):
        a = {"text_value": {"type": "choice", "choice": "Fury", "probabilities": {"Fury": 0.9}}}
        return {"answers": a}


class TestFilledField(unittest.TestCase):
    def _run(self, shown_after, guard=True):
        roles = {"Search for your club": "textbox"}
        s0 = screen(["Search for your club", "Help"], roles=roles)
        s1 = Screen.from_json({"title": "T", "url": "https://example.com/home", "text": "t2",
                               "viewport": VP, "elements": [
                                   {"label": "Search for your club", "role": "textbox",
                                    "value": shown_after, "x": 100, "y": 100, "w": 80, "h": 30},
                                   {"label": "Sign in to Fury", "role": "link", "value": "",
                                    "x": 100, "y": 140, "w": 80, "h": 30}]}, SCR)
        reader = SwitchReader(s0)
        dec = TypeDecider(["TYPE_TEXT", "CLICK"])
        p = planner(reader, dec, ProgressVerifier(), dead_click_guard=guard, verify_done=False)
        guide = "Type Fury into the club search box."
        plan, _ = p.decide("g", guide, None, [], (0, 0))
        self.assertEqual(plan["text"], "Fury")
        reader.cur = s1
        p.decide("g", guide, None, ["type: " + plan["target"]], (0, 0))
        return dec.calls[1]["labels"]

    def test_field_holding_typed_text_is_not_offered(self):
        self.assertNotIn("Search for your club", self._run("Fury"))

    def test_field_that_lost_the_text_stays(self):
        self.assertIn("Search for your club", self._run(""))

    def test_guard_off(self):
        self.assertIn("Search for your club", self._run("Fury", guard=False))


class TestScrollToNamed(unittest.TestCase):
    def test_scrolls_toward_named_offscreen_control_without_a_decision(self):
        guide = "Click Run sample and wait. Then click Start 14-day free trial."
        s = screen(["GET FIRMI", "Chat"], scroll_y=5000,
                   offscreen=[{"label": "Start 14-day free trial", "role": "link", "y": 3000}])
        dec = CtxDecider(["CLICK"])
        p = planner(SwitchReader(s), dec, ProgressVerifier())
        p._steps = split_subgoals("g", guide)
        p._step_i = 1
        plan, _ = p.decide("g", guide, None, [], (0, 0))
        self.assertEqual(plan["action"], "scroll")
        self.assertEqual(plan["direction"], "up")
        self.assertEqual(plan["amount"], 3)  # 2,000 px away
        self.assertEqual(plan["jev"]["scroll_to"], "Start 14-day free trial")
        self.assertEqual(len(dec.calls), 0)

    def test_single_step_playbook_scrolls_down_to_it(self):
        s = screen(["GET FIRMI"], offscreen=[{"label": "Start free", "role": "link", "y": 9000}])
        dec = CtxDecider(["CLICK"])
        p = planner(SwitchReader(s), dec, ProgressVerifier(), verify_done=False)
        plan, _ = p.decide("g", "Click Start free (it opens the signup). DONE when it shows.", None, [], (0, 0))
        self.assertEqual((plan["action"], plan["direction"], plan["amount"]), ("scroll", "down", 5))

    def test_visible_named_control_needs_no_scroll(self):
        s = screen(["Start free"], offscreen=[{"label": "Start free", "role": "link", "y": 9000}])
        p = planner(SwitchReader(s), CtxDecider([("CLICK", "Start free")]), ProgressVerifier(),
                    verify_done=False)
        plan, _ = p.decide("g", "Click Start free.", None, [], (0, 0))
        self.assertEqual(plan["action"], "click")

    def test_gives_up_after_eight_tries_and_flag_off(self):
        s = screen(["A"], offscreen=[{"label": "Start free", "role": "link", "y": 9000}])
        p = planner(SwitchReader(s), CtxDecider(["CLICK"] * 12), ProgressVerifier(), verify_done=False,
                    max_scrolls=0)
        acts = [p.decide("g", "Click Start free.", None, ["x"] * i, (0, 0))[0].get("jev", {}).get("scroll_to")
                for i in range(10)]
        self.assertEqual(acts.count("Start free"), 8)
        p = planner(SwitchReader(s), CtxDecider(["CLICK"]), ProgressVerifier(), verify_done=False,
                    subgoals=False)
        plan, _ = p.decide("g", "Click Start free.", None, [], (0, 0))
        self.assertEqual(plan["action"], "click")

    def test_offscreen_parsed_and_kept_by_without(self):
        s = screen(["A", "B"], offscreen=[{"label": "C", "role": "link", "y": 10}])
        self.assertEqual(s.without({"1"}).offscreen[0]["label"], "C")
        self.assertNotEqual(s.fingerprint(), "")


class TestTypeGuard(unittest.TestCase):
    def test_field_accepts(self):
        pw = Element("1", "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022", "textbox", "", 0, 0, 1, 1)
        em = Element("2", "you@example.com", "textbox", "", 0, 0, 1, 1)
        box = Element("3", "Search for your club", "textbox", "", 0, 0, 1, 1)
        self.assertFalse(field_accepts(pw, "Lakeshow"))
        self.assertFalse(field_accepts(em, "Lakeshow"))
        self.assertTrue(field_accepts(em, "Lakeshow", check_email=False))
        self.assertTrue(field_accepts(em, "a@b.co"))
        self.assertTrue(field_accepts(box, "Lakeshow"))

    def test_typing_a_club_name_into_sign_in_goes_back(self):
        roles = {"you@example.com": "textbox"}
        home = screen(["Sign in to Club", "Other"])
        wall = screen(["you@example.com", "Sign In"], url="https://example.com/portal", roles=roles)
        reader = SwitchReader(home)
        dec = TypeDecider([("CLICK", "Sign in to Club"), "TYPE_TEXT"])
        p = planner(reader, dec, ProgressVerifier(), backtrack_after=4)
        guide = "Type Fury into the club search box. Then click the Fury result. Then find a game."
        plan, _ = p.decide("g", guide, None, [], (0, 0))
        reader.cur = wall
        plan2, _ = p.decide("g", guide, None, [hist_for(plan)], (0, 0))
        self.assertEqual(plan2["action"], "navigate")
        self.assertIn("sign-in field", plan2["jev"]["backtrack"])


class TestProgressPrompt(unittest.TestCase):
    def test_one_call_returns_done_and_step(self):
        seen = {}

        def fake_chat(model, messages, **kw):
            seen["user"] = messages[1]["content"]
            kw["usage_out"].append({"cost": 0.0002})
            return '{"reason": "team page open", "step_done": true, "done": false}'

        with mock.patch("ghosthands.jev._chat", fake_chat):
            v = DoneVerifier(model="m")
            done, why, step_done = v.check_progress("g", "Click A. Then click B. DONE when C shows.",
                                                    screen(["A"]), ["click: A"],
                                                    ["Click A", "click B"], 0, ["opened 'A'"])
        self.assertEqual((done, step_done, v.total_calls), (False, True, 1))
        self.assertIn("1. Click A  <- CURRENT", seen["user"])
        self.assertIn("that condition decides", seen["user"])
        self.assertIn("opened 'A'", seen["user"])


class TestSureTarget(unittest.TestCase):
    def test_near_certain_target_passes_split_operation_on_multistep(self):
        s = screen(["Sat Oct 3 game", "Other"], url="https://example.com/team")
        p = planner(SwitchReader(s), CtxDecider([("CLICK", "Sat Oct 3 game")], op_p=0.3, tgt_p=0.9),
                    ProgressVerifier())
        plan, _ = p.decide("g", GUIDE, None, [], (0, 0))
        self.assertEqual(plan["action"], "click")

    def test_single_step_keeps_the_gate(self):
        s = screen(["Sat Oct 3 game", "Other"])
        p = planner(SwitchReader(s), CtxDecider([("CLICK", "Sat Oct 3 game")], op_p=0.2, tgt_p=0.9),
                    ProgressVerifier(), verify_done=False)
        plan, _ = p.decide("g", "Click the game.", None, [], (0, 0))
        self.assertEqual(plan["action"], "verify_stop")


class TestLoadingSignal(unittest.TestCase):
    def test_unrelated_disabled_submit_is_not_loading(self):
        dis = {"label": "Send", "why": "disabled submit", "x": 0, "y": 0, "w": 90, "h": 30}
        s0 = screen(["Chat with us", "B"])
        s1 = screen(["Close chat", "B"], busy=[dis])
        reader = ListReader([s0, s1])
        p = planner(reader, CtxDecider(["CLICK", "CLICK"]), ProgressVerifier(), loading_wait_s=45,
                    subgoals=False, verify_done=False)
        plan1, _ = p.decide("g", "", None, [], (0, 0))
        with mock.patch("ghosthands.jev.time.sleep") as sl:
            plan2, _ = p.decide("g", "", None, [hist_for(plan1)], (0, 0))
        self.assertNotIn("loading", plan2["jev"])
        self.assertLess(sum(c.args[0] for c in sl.call_args_list), 2)

    def test_clicked_submit_turning_disabled_is_loading(self):
        dis = {"label": "Show my games", "why": "disabled submit", "x": 0, "y": 0, "w": 90, "h": 30}
        s0 = screen(["Show my games", "B"])
        s1 = screen(["B"], busy=[dis])
        s2 = screen(["B", "Game 1"])
        reader = ListReader([s0, s1, s1, s2])
        p = planner(reader, CtxDecider(["CLICK", "CLICK"]), ProgressVerifier(), loading_wait_s=45,
                    subgoals=False, verify_done=False)
        p.loading_poll = 0.0
        plan1, _ = p.decide("g", "", None, [], (0, 0))
        with mock.patch("ghosthands.jev.time.sleep"):
            plan2, _ = p.decide("g", "", None, [hist_for(plan1)], (0, 0))
        self.assertTrue(plan2["jev"]["loading"]["cleared"])


if __name__ == "__main__":
    unittest.main()
