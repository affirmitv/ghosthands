"""Offline tests for the dead-click guard and the loading wait (no network)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OPENROUTER_API_KEY"] = "test"

from ghosthands.dom_reader import Screen  # noqa: E402
from ghosthands.jev import JevPlanner  # noqa: E402

VP = {"w": 1000, "h": 800, "sx": 100, "sy": 50, "ow": 1000, "oh": 900, "sw": 2000, "sh": 1600}
SCR = (2000, 1600)
LOADING = {"label": "Firmi is reading it...", "why": "disabled, loading text",
           "x": 100, "y": 300, "w": 200, "h": 40}


def screen(labels, text="page text", url="https://example.com/x", busy=None, roles=None):
    els = [{"label": l, "role": (roles or {}).get(l, "link"), "value": "", "x": 100, "y": 100 + 40 * i,
            "w": 80, "h": 30} for i, l in enumerate(labels)]
    data = {"title": "T", "url": url, "text": text, "viewport": VP, "elements": els}
    if busy is not None:
        data["busy"] = busy
    return Screen.from_json(data, SCR)


class ListReader:
    """Each snapshot() returns the next screen; the last one repeats."""

    def __init__(self, screens):
        self.screens, self.calls = list(screens), 0

    def snapshot(self):
        s = self.screens[min(self.calls, len(self.screens) - 1)]
        self.calls += 1
        return s


class StaticReader(ListReader):
    def __init__(self, s):
        super().__init__([s])


class PickFirstDecider:
    """Answers the scripted operation; the click target is the first clickable element in the
    table it was given (so a dropped element can never be picked). Records what it saw."""

    model = "fake-jev"

    def __init__(self, ops, p=0.9):
        self.ops, self.p = list(ops), p
        self.calls = []  # (exclude, history, labels offered)
        self.total_calls, self.total_cost = 0, 0.0

    def decide(self, goal, guide, scr, history, exclude=None):
        self.calls.append((set(exclude or ()), list(history), [e.label for e in scr.elements]))
        self.total_calls += 1
        while self.ops and self.ops[0] in (exclude or ()):
            self.ops.pop(0)
        op = self.ops.pop(0) if self.ops else "WAIT"
        clickable = [e.index for e in scr.elements if "CLICK" in e.operations()] or ["1"]
        a = {"operation": {"type": "choice", "choice": op, "probabilities": {op: self.p}},
             "click_target": {"type": "choice", "choice": clickable[0],
                              "probabilities": {clickable[0]: 0.9}}}
        return {"answers": a, "usage": {"cost": 0.0001}, "latency_s": 0.0, "raw": {"answers": a}}


class NoVerifier:
    model = "fake-text"
    total_calls, total_cost = 0, 0.0

    def check(self, *a, **k):
        raise AssertionError("not used")


class FakeText:
    def write(self, *a, **k):
        return "x"


class FakeClock:
    """Stands in for the time module inside ghosthands.jev: sleep advances the clock."""

    def __init__(self):
        self.t = 1000.0
        self.slept = 0.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s
        self.slept += s


def planner(reader, decider, **kw):
    kw.setdefault("verify_done", False)
    p = JevPlanner(reader=reader, decider=decider, text_helper=FakeText(), verifier=NoVerifier(),
                   min_confidence=0.3, min_target_confidence=0.1, **kw)
    p.settle_timeout = 0.0
    p.settle_poll = 0.0
    return p


def hist_for(plan):
    a = plan["action"]
    return "%s: %s" % (a, plan.get("target") or plan.get("keys") or "")


class TestDeadClick(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        patcher = mock.patch("ghosthands.jev.time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dead_click_excludes_target_next_step(self):
        reader = StaticReader(screen(["Nothing link", "Start free"]))  # the click changes nothing
        dec = PickFirstDecider(["CLICK", "CLICK"])
        p = planner(reader, dec, dead_click_guard=True)
        plan1, _ = p.decide("g", "", None, [], (0, 0))
        self.assertEqual(plan1["target"], "[1] link Nothing link")
        plan2, _ = p.decide("g", "", None, [hist_for(plan1)], (0, 0))
        # The dead control is gone from the table Jev saw, and Jev was told why.
        self.assertEqual(dec.calls[1][2], ["Start free"])
        self.assertTrue(any("clicked [1] Nothing link; nothing changed; choose something else" in h
                            for h in dec.calls[1][1]))
        self.assertEqual(plan2["target"], "[2] link Start free")
        self.assertEqual(plan2["jev"]["dead"], ["1"])

    def test_dead_stays_dead_after_table_moves(self):
        # Indices shift when the table changes; the dead key is (url, role, label).
        same = screen(["Nothing link", "B"])
        reader = ListReader([same] * 4 + [screen(["Top", "Nothing link", "B"], text="moved")])
        dec = PickFirstDecider(["CLICK", "SCROLL_DOWN", "CLICK"])
        p = planner(reader, dec)
        h = []
        for _ in range(3):
            plan, _ = p.decide("g", "", None, h, (0, 0))
            h.append(hist_for(plan))
        self.assertNotIn("Nothing link", dec.calls[2][2])
        self.assertIn("Top", dec.calls[2][2])

    def test_click_that_changes_page_is_not_dead(self):
        reader = ListReader([screen(["A", "B"]), screen(["C", "D"], url="https://example.com/y")])
        dec = PickFirstDecider(["CLICK", "CLICK"])
        p = planner(reader, dec)
        plan1, _ = p.decide("g", "", None, [], (0, 0))
        p.decide("g", "", None, [hist_for(plan1)], (0, 0))
        self.assertEqual(p._dead, set())
        self.assertEqual(p._dead_streak, 0)

    def test_text_field_click_is_never_dead(self):
        reader = StaticReader(screen(["Club search", "B"], roles={"Club search": "textbox"}))
        dec = PickFirstDecider(["CLICK", "CLICK"])
        p = planner(reader, dec)
        plan1, _ = p.decide("g", "", None, [], (0, 0))
        p.decide("g", "", None, [hist_for(plan1)], (0, 0))
        self.assertIn("Club search", dec.calls[1][2])
        self.assertEqual(p._dead, set())

    def test_two_dead_clicks_bias_to_scroll(self):
        reader = StaticReader(screen(["A", "B", "C"]))
        dec = PickFirstDecider(["CLICK", "CLICK", "CLICK"])
        p = planner(reader, dec, max_scrolls=3)
        h, actions = [], []
        for _ in range(3):
            plan, _ = p.decide("g", "", None, h, (0, 0))
            actions.append(plan["action"])
            h.append(hist_for(plan))
        self.assertEqual(actions, ["click", "click", "scroll"])
        self.assertEqual(len(dec.calls), 2)  # the scroll cost no decision
        self.assertEqual(p._dead_streak, 0)
        self.assertIn("point", plan)          # parked over the page like any Jev scroll
        # The scroll changed nothing either: the existing scroll guard takes over next.
        p.decide("g", "", None, h, (0, 0))
        self.assertEqual(dec.calls[2][0], {"SCROLL_DOWN", "SCROLL_UP"})
        self.assertTrue(any("2 clicks in a row changed nothing" in x for x in dec.calls[2][1]))

    def test_bias_respects_scroll_guard(self):
        reader = StaticReader(screen(["A", "B", "C"]))
        dec = PickFirstDecider(["CLICK", "CLICK", "CLICK"])
        p = planner(reader, dec, max_scrolls=3)
        plan1, _ = p.decide("g", "", None, [], (0, 0))
        plan2, _ = p.decide("g", "", None, [hist_for(plan1)], (0, 0))
        # Pretend the last scroll was on this same unchanged table: scrolling is guarded.
        p._scroll_streak, p._scroll_fp = 1, reader.screens[0].fingerprint()
        plan3, _ = p.decide("g", "", None, [hist_for(plan1), hist_for(plan2)], (0, 0))
        self.assertEqual(plan3["action"], "click")
        self.assertEqual(plan3["target"], "[3] link C")

    def test_guard_off_restores_old_behavior(self):
        reader = StaticReader(screen(["A", "B"]))
        dec = PickFirstDecider(["CLICK"] * 4)
        p = planner(reader, dec, dead_click_guard=False)
        h = []
        for _ in range(4):
            plan, _ = p.decide("g", "", None, h, (0, 0))
            self.assertEqual(plan["target"], "[1] link A")
            h.append(hist_for(plan))
        self.assertTrue(all(c[2] == ["A", "B"] for c in dec.calls))
        self.assertEqual(len(dec.calls), 4)


class TestLoadingWait(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        patcher = mock.patch("ghosthands.jev.time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_waits_while_loading_then_resumes(self):
        s0 = screen(["Paste link", "Show my games", "Watch a sample"])
        s1 = screen(["Paste link"], text="Firmi is reading it...", busy=[LOADING])
        s2 = screen(["Paste link", "Show my games", "Game 1 vs Fury"], text="results")
        reader = ListReader([s0] + [s1] * 6 + [s2])
        dec = PickFirstDecider(["CLICK", "CLICK"])
        p = planner(reader, dec, loading_wait_s=45)
        plan1, _ = p.decide("g", "", None, [], (0, 0))
        plan2, _ = p.decide("g", "", None, [hist_for(plan1)], (0, 0))
        self.assertEqual(len(dec.calls), 2)  # no Jev decisions spent while it loaded
        self.assertEqual(dec.calls[1][2], ["Paste link", "Show my games", "Game 1 vs Fury"])
        self.assertTrue(any("while 'Firmi is reading it...' loaded" in h for h in dec.calls[1][1]))
        info = plan2["jev"]["loading"]
        self.assertTrue(info["cleared"])
        self.assertGreater(info["waited_s"], 0)
        self.assertLess(info["waited_s"], 10)

    def test_loading_wait_is_bounded(self):
        s0 = screen(["Show my games"])
        s1 = screen(["Other"], text="Firmi is reading it...", busy=[LOADING])
        reader = ListReader([s0, s1])  # never clears
        dec = PickFirstDecider(["CLICK", "WAIT"])
        p = planner(reader, dec, loading_wait_s=45)
        plan1, _ = p.decide("g", "", None, [], (0, 0))
        t0 = self.clock.t
        plan2, _ = p.decide("g", "", None, [hist_for(plan1)], (0, 0))
        self.assertFalse(plan2["jev"]["loading"]["cleared"])
        self.assertGreaterEqual(self.clock.t - t0, 45)
        self.assertLess(self.clock.t - t0, 47)
        self.assertEqual(len(dec.calls), 2)
        self.assertTrue(any("still loading" in h for h in dec.calls[1][1]))

    def test_old_busy_signal_is_not_loading(self):
        # A submit that was disabled before the click (an unfilled form) is not a new signal.
        dis = {"label": "Create account", "why": "disabled submit", "x": 0, "y": 0, "w": 90, "h": 30}
        s0 = screen(["A", "B"], busy=[dis])
        s1 = screen(["A", "C"], busy=[dis])
        reader = ListReader([s0, s1])
        dec = PickFirstDecider(["CLICK", "CLICK"])
        p = planner(reader, dec, loading_wait_s=45)
        plan1, _ = p.decide("g", "", None, [], (0, 0))
        t0 = self.clock.t
        plan2, _ = p.decide("g", "", None, [hist_for(plan1)], (0, 0))
        self.assertNotIn("loading", plan2["jev"])
        self.assertLess(self.clock.t - t0, 2)

    def test_enter_key_also_waits(self):
        s0 = screen(["Search"], roles={"Search": "textbox"})
        s1 = screen(["Search"], roles={"Search": "textbox"}, text="Loading...",
                    busy=[{"label": "Loading...", "why": "busy"}])
        s2 = screen(["Search", "Fury Hoops"], roles={"Search": "textbox"})
        reader = ListReader([s0, s1, s1, s1, s2])
        dec = PickFirstDecider(["KEY_ENTER", "WAIT"])
        p = planner(reader, dec, loading_wait_s=45)
        p.decide("g", "", None, [], (0, 0))
        plan2, _ = p.decide("g", "", None, ["key: return"], (0, 0))
        self.assertTrue(plan2["jev"]["loading"]["cleared"])
        self.assertIn("Fury Hoops", dec.calls[1][2])

    def test_loading_off_restores_old_behavior(self):
        s0 = screen(["Show my games"])
        s1 = screen(["Other"], text="Firmi is reading it...", busy=[LOADING])
        reader = ListReader([s0, s1])
        dec = PickFirstDecider(["CLICK", "CLICK"])
        p = planner(reader, dec, loading_wait_s=0)
        plan1, _ = p.decide("g", "", None, [], (0, 0))
        t0 = self.clock.t
        plan2, _ = p.decide("g", "", None, [hist_for(plan1)], (0, 0))
        self.assertNotIn("loading", plan2["jev"])
        self.assertLess(self.clock.t - t0, 2)
        self.assertEqual(dec.calls[1][2], ["Other"])

    def test_unchanged_wait_does_not_trip_confidence_gate(self):
        reader = StaticReader(screen(["A"]))
        dec = PickFirstDecider(["WAIT", "CLICK", "CLICK", "CLICK"], p=0.2)  # below min_confidence 0.3
        p = planner(reader, dec, loading_wait_s=45)
        h, actions = [], []
        for _ in range(4):
            plan, _ = p.decide("g", "", None, h, (0, 0))
            actions.append(plan["action"])
            h.append(hist_for(plan) if plan["action"] != "verify_stop" else "[human approved]")
        # Two grace waits after an unchanged WAIT, then the gate applies as before.
        self.assertEqual(actions, ["wait", "wait", "wait", "verify_stop"])

    def test_unchanged_wait_gate_off_with_loading_off(self):
        reader = StaticReader(screen(["A"]))
        dec = PickFirstDecider(["WAIT", "CLICK"], p=0.2)
        p = planner(reader, dec, loading_wait_s=0)
        plan1, _ = p.decide("g", "", None, [], (0, 0))
        plan2, _ = p.decide("g", "", None, [hist_for(plan1)], (0, 0))
        self.assertEqual((plan1["action"], plan2["action"]), ("wait", "verify_stop"))


class TestBusyParsing(unittest.TestCase):
    def test_busy_round_trip_and_without(self):
        s = screen(["A", "B"], busy=[LOADING])
        self.assertEqual(s.busy_keys(), {("Firmi is reading it...", "disabled, loading text")})
        self.assertEqual(screen(["A"]).busy, [])
        t = s.without({"1"})
        self.assertEqual([e.index for e in t.elements], ["2"])
        self.assertEqual(t.busy_keys(), s.busy_keys())
        self.assertEqual(len(s.elements), 2)


if __name__ == "__main__":
    unittest.main()
