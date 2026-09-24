"""Offline tests for the verified DONE check and the scroll guard (no network)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OPENROUTER_API_KEY"] = "test"

from ghosthands.agent import Agent  # noqa: E402
from ghosthands.dom_reader import Screen  # noqa: E402
from ghosthands.jev import (DoneVerifier, JevDecider, JevPlanner, extract_text_candidates,  # noqa: E402
                            has_done_clause)

VP = {"w": 1000, "h": 800, "sx": 100, "sy": 50, "ow": 1000, "oh": 900, "sw": 2000, "sh": 1600}
SCR = (2000, 1600)


def screen(labels, y0=100, text="page text", url="https://example.com/x"):
    els = [{"label": l, "role": "link", "value": "", "x": 100, "y": y0 + 40 * i, "w": 80, "h": 30}
           for i, l in enumerate(labels)]
    return Screen.from_json({"title": "T", "url": url, "text": text, "viewport": VP,
                             "elements": els}, SCR)


def ans(op, target="1"):
    a = {"operation": {"type": "choice", "choice": op, "probabilities": {op: 0.9}}}
    a["click_target"] = {"type": "choice", "choice": target, "probabilities": {target: 0.9}}
    return a


class SeqReader:
    """Returns the screens in order, repeating the last one."""

    def __init__(self, screens):
        self.screens, self.i = list(screens), 0

    def snapshot(self):
        s = self.screens[min(self.i, len(self.screens) - 1)]
        return s

    def advance(self):
        self.i += 1


class ScriptedDecider:
    """Answers from a script. A step whose op was excluded falls through to the next entry,
    the way a real re-ask would pick a different operation."""

    model = "fake-jev"

    def __init__(self, ops):
        self.ops = list(ops)
        self.calls = []  # (exclude, recent_actions)
        self.total_calls, self.total_cost = 0, 0.0

    def decide(self, goal, guide, scr, history, exclude=None):
        self.calls.append((set(exclude or ()), list(history)))
        self.total_calls += 1
        while self.ops and self.ops[0] in (exclude or ()):
            self.ops.pop(0)
        op = self.ops.pop(0) if self.ops else "WAIT"
        a = ans(op)
        return {"answers": a, "usage": {"cost": 0.0001}, "latency_s": 0.0, "raw": {"answers": a}}

    def ask(self, state, questions):
        raise AssertionError("not used")


class FakeVerifier:
    model = "fake-text"

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0
        self.total_calls, self.total_cost = 0, 0.0

    def check(self, goal, guide, scr, history=None):
        self.calls += 1
        self.total_calls += 1
        r = self.results.pop(0) if self.results else (False, "not yet")
        if isinstance(r, Exception):
            raise r
        return r


class FakeText:
    def write(self, *a, **k):
        return "x"


def planner(reader, decider, verifier, **kw):
    p = JevPlanner(reader=reader, decider=decider, text_helper=FakeText(), verifier=verifier,
                   min_confidence=0.3, min_target_confidence=0.1, **kw)
    p.settle_timeout = 0.0
    p.settle_poll = 0.0
    return p


class NullHands:
    def __init__(self, reader=None):
        self.reader = reader
        self.moves = []

    def move(self, fx, fy):
        self.moves.append((fx, fy))

    def click(self):
        if self.reader:
            self.reader.advance()

    def key(self, k):
        pass

    def type(self, t):
        pass

    def scroll(self, n):
        pass


def run_agent(p, hands, tmp, guide, max_steps=6):
    ag = Agent(p, None, hands, eyes=object(), run_dir=tmp)
    with mock.patch("ghosthands.jev.time.sleep"), mock.patch("ghosthands.agent.time.sleep"):
        return ag.run("goal", guide, max_steps=max_steps), ag


class TestDoneClause(unittest.TestCase):
    def test_has_done_clause(self):
        self.assertTrue(has_done_clause("Click Start free. DONE when a signup form shows."))
        self.assertTrue(has_done_clause("DONE as soon as the Stripe checkout page is showing"))
        self.assertFalse(has_done_clause("Open the TEAMS tab and click the team."))

    def test_type_into_candidate(self):
        out = extract_text_candidates("Find Fury", "Type Fury into the club search box.")
        self.assertIn("Fury", out)


class TestVerifiedDone(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def test_verify_rejects_false_done_and_loop_continues(self):
        s1 = screen(["Try Firmi", "Start free"])
        s2 = screen(["Club name", "Email"], url="https://example.com/signup")
        reader = SeqReader([s1, s2])
        dec = ScriptedDecider(["DONE", "CLICK", "DONE"])
        ver = FakeVerifier([(False, "no club name field on this page"), (True, "club name field showing")])
        p = planner(reader, dec, ver)
        # No DONE-when clause: the check runs only when Jev says DONE.
        result, ag = run_agent(p, NullHands(reader), self.tmp, "Click Start free.")
        self.assertEqual(result, "done")
        # Step 1: DONE asked, rejected, re-asked without DONE and got CLICK.
        self.assertEqual(dec.calls[1][0], {"DONE"})
        self.assertTrue(any("DONE rejected by check: no club name field" in h for h in dec.calls[1][1]))
        self.assertEqual(ag.history[0].split(":")[0], "click")
        self.assertEqual(ver.calls, 2)

    def test_verify_accepts(self):
        reader = SeqReader([screen(["Club name"])])
        dec = ScriptedDecider(["DONE"])
        ver = FakeVerifier([(True, "form is showing")])
        p = planner(reader, dec, ver)
        plan, _ = p.decide("g", "Click Start free.", None, [], (0, 0))
        self.assertEqual(plan["action"], "done")
        self.assertIn("verified", plan["reasoning"])
        self.assertEqual(ver.calls, 1)

    def test_done_when_clause_stops_before_overshoot(self):
        # Jev would click past the results; the clause check stops the run first.
        reader = SeqReader([screen(["Search"]), screen(["Fury Hoops"])])
        dec = ScriptedDecider(["CLICK", "CLICK", "CLICK"])
        ver = FakeVerifier([(True, "Fury Hoops is in the results")])
        p = planner(reader, dec, ver)
        result, ag = run_agent(p, NullHands(reader), self.tmp,
                               "Type Fury. DONE when Fury Hoops appears in the results.")
        self.assertEqual(result, "done")
        self.assertEqual(len(dec.calls), 1)  # one decision, then the check stopped the run
        self.assertEqual(ver.calls, 1)       # no check before the first action

    def test_one_check_per_step(self):
        reader = SeqReader([screen(["A"])])
        dec = ScriptedDecider(["DONE", "CLICK"])
        ver = FakeVerifier([(False, "not there")])
        p = planner(reader, dec, ver)
        plan, _ = p.decide("g", "DONE when X shows.", None, ["click: [1] link A"], (0, 0))
        self.assertEqual(ver.calls, 1)  # the pre-check result is reused for Jev's DONE
        self.assertEqual(plan["action"], "click")

    def test_failed_check_keeps_jev_done(self):
        reader = SeqReader([screen(["A"])])
        dec = ScriptedDecider(["DONE"])
        ver = FakeVerifier([RuntimeError("LLM call failed")])
        p = planner(reader, dec, ver)
        plan, _ = p.decide("g", "", None, [], (0, 0))
        self.assertEqual(plan["action"], "done")

    def test_verify_off_restores_old_behavior(self):
        reader = SeqReader([screen(["A"])])
        dec = ScriptedDecider(["DONE"])
        ver = FakeVerifier([(False, "never asked")])
        p = planner(reader, dec, ver, verify_done=False)
        plan, _ = p.decide("g", "DONE when X shows.", None, ["click: x"], (0, 0))
        self.assertEqual(plan["action"], "done")
        self.assertEqual(ver.calls, 0)


class TestVerifierParsing(unittest.TestCase):
    def test_parses_json_and_string_bools(self):
        v = DoneVerifier(model="m")
        s = screen(["A"])
        with mock.patch("ghosthands.jev._chat", return_value='```json\n{"done": "false", "reason": "nope"}\n```'):
            self.assertEqual(v.check("g", "DONE when x", s), (False, "nope"))
        with mock.patch("ghosthands.jev._chat", return_value='{"done": true, "reason": "yes"}'):
            self.assertEqual(v.check("g", "DONE when x", s), (True, "yes"))

    def test_passes_reasoning_cap(self):
        v = DoneVerifier(model="m")
        with mock.patch("ghosthands.jev._chat", return_value='{"done": false}') as m:
            v.check("g", "", screen(["A"]), ["click: [4] link TRY IT FREE"])
        self.assertEqual(m.call_args.kwargs["extra"], {"reasoning": {"effort": "low"}})
        self.assertIn("click: [4] link TRY IT FREE", m.call_args.args[1][1]["content"])


class TestScrollGuard(unittest.TestCase):
    def _moving_reader(self):
        # Every snapshot is a new table (the page really scrolls).
        class R:
            n = 0

            def snapshot(self):
                R.n += 1
                return screen(["Row %d" % R.n, "Row %d" % (R.n + 1)], y0=100 + R.n)
        return R()

    def test_guard_after_n_scrolls(self):
        dec = ScriptedDecider(["SCROLL_DOWN"] * 3 + ["SCROLL_DOWN", "CLICK"])
        p = planner(self._moving_reader(), dec, FakeVerifier([]), max_scrolls=3)
        hist = []
        actions = []
        for _ in range(4):
            plan, _ = p.decide("g", "", None, hist, (0, 0))
            actions.append(plan["action"])
            hist.append("%s: x" % plan["action"])
        self.assertEqual(actions, ["scroll", "scroll", "scroll", "click"])
        self.assertEqual(dec.calls[3][0], {"SCROLL_DOWN", "SCROLL_UP"})
        self.assertTrue(any("scrolled 3 times without progress; choose a control" in h
                            for h in dec.calls[3][1]))
        self.assertEqual(p._scroll_streak, 0)

    def test_guard_on_unchanged_table(self):
        reader = SeqReader([screen(["A", "B"])])  # scrolling changes nothing
        dec = ScriptedDecider(["SCROLL_DOWN", "SCROLL_DOWN", "CLICK"])
        p = planner(reader, dec, FakeVerifier([]), max_scrolls=3)
        plan1, _ = p.decide("g", "", None, [], (0, 0))
        plan2, _ = p.decide("g", "", None, ["scroll: "], (0, 0))
        self.assertEqual((plan1["action"], plan2["action"]), ("scroll", "click"))
        self.assertEqual(dec.calls[1][0], {"SCROLL_DOWN", "SCROLL_UP"})
        self.assertTrue(any("page did not change" in h for h in dec.calls[1][1]))

    def test_guard_off_restores_old_behavior(self):
        reader = SeqReader([screen(["A", "B"])])
        dec = ScriptedDecider(["SCROLL_DOWN"] * 5)
        p = planner(reader, dec, FakeVerifier([]), max_scrolls=0)
        hist = []
        for _ in range(5):
            plan, _ = p.decide("g", "", None, hist, (0, 0))
            self.assertEqual(plan["action"], "scroll")
            hist.append("scroll: ")
        self.assertTrue(all(not c[0] for c in dec.calls))

    def test_scroll_parks_pointer_over_page(self):
        reader = SeqReader([screen(["A"])])
        p = planner(reader, ScriptedDecider(["SCROLL_DOWN"]), FakeVerifier([]))
        plan, _ = p.decide("g", "", None, [], (0, 0))
        # viewport center (500, 400) + window (100, 50) + chrome 100 -> (600, 550) on 2000x1600
        self.assertEqual(plan["point"], [0.3, 0.34375])
        hands = NullHands()
        Agent(p, None, hands, eyes=object(), run_dir=self._tmp())._execute(plan, (0, 0), None)
        self.assertEqual(hands.moves, [(0.3, 0.34375)])

    def _tmp(self):
        import tempfile
        return tempfile.mkdtemp()

    def test_excluded_ops_leave_the_question(self):
        d = JevDecider(model="m", url="http://x", api_key="k")
        q = d.build_questions("g", "", screen(["A"]), [], exclude={"SCROLL_DOWN", "SCROLL_UP", "DONE"})
        crit = q["operation"]["criteria"]
        self.assertNotIn("SCROLL_DOWN", crit)
        self.assertNotIn("DONE", crit)
        self.assertIn("CLICK", crit)
        self.assertIn("SCROLL_DOWN", d.build_questions("g", "", screen(["A"]), [])["operation"]["criteria"])


if __name__ == "__main__":
    unittest.main()
