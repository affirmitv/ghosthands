import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OPENROUTER_API_KEY"] = "test"

from ghosthands.dom_reader import Element, Screen  # noqa: E402
from ghosthands import jev  # noqa: E402
from ghosthands.jev import JevDecider, JevError, JevPlanner, extract_text_candidates  # noqa: E402

VP = {"w": 1000, "h": 800, "sx": 100, "sy": 50, "ow": 1000, "oh": 900, "sw": 2000, "sh": 1600}
SCR = (2000, 1600)


def el(label, role, x, y, w, h, value=""):
    return {"label": label, "role": role, "value": value, "x": x, "y": y, "w": w, "h": h}


def make_screen(elements, viewport=None):
    data = {"title": "T", "url": "https://example.com/x", "text": "hello page",
            "viewport": viewport or VP, "elements": elements}
    return Screen.from_json(data, SCR)


class FakeResp:
    def __init__(self, obj):
        self.b = json.dumps(obj).encode()

    def read(self):
        return self.b


class FakeReader:
    def __init__(self, screen):
        self.screen = screen

    def snapshot(self):
        return self.screen


class FakeDecider:
    def __init__(self, answers, ask_answers=None):
        self.answers, self.ask_answers = answers, ask_answers or {}

    def decide(self, goal, guide, screen, history):
        return {"answers": self.answers, "usage": {"cost": 0.01},
                "latency_s": 0.1, "raw": {"answers": self.answers}}

    def ask(self, state, questions):
        return {"answers": self.ask_answers}


class FakeText:
    def __init__(self):
        self.called = False

    def write(self, *a, **k):
        self.called = True
        return "WRITTEN"


class TestElement(unittest.TestCase):
    def test_screen_point_maps_and_clamps(self):
        e = Element("1", "B", "button", "", 200, 300, 100, 40)
        self.assertEqual(e.screen_point(VP, SCR), (0.175, 0.29375))
        off = Element("2", "B", "button", "", 2500, -500, 100, 40)
        fx, fy = off.screen_point(VP, SCR)
        self.assertEqual((fx, fy), (1.0, 0.0))

    def test_operations(self):
        self.assertEqual(Element("1", "t", "textbox", "", 0, 0, 1, 1).operations(),
                         ["TYPE_TEXT", "CLICK"])
        self.assertEqual(Element("1", "c", "combobox", "", 0, 0, 1, 1).operations(),
                         ["TYPE_TEXT", "CLICK"])
        self.assertEqual(Element("1", "b", "button", "", 0, 0, 1, 1).operations(), ["CLICK"])
        self.assertEqual(Element("1", "l", "link", "", 0, 0, 1, 1).operations(), ["CLICK"])


class TestScreen(unittest.TestCase):
    def test_to_jev_state_shape_and_truncation(self):
        s = make_screen([el("Buy", "button", 0, 0, 10, 10),
                         el("Card", "textbox", 0, 20, 10, 10, "4242")])
        acts = ["a%d" % i for i in range(12)]
        st = s.to_jev_state(acts)
        self.assertEqual(set(st["page"]), {"title", "url", "text"})
        self.assertEqual(st["recent_actions"], acts[-8:])
        e0 = st["elements"][0]
        self.assertEqual(set(e0), {"index", "label", "role", "value", "operations"})
        self.assertEqual(e0["index"], "1")
        self.assertEqual(st["elements"][1]["value"], "4242")

    def test_fingerprint(self):
        s = make_screen([el("A", "button", 1, 2, 5, 5)])
        f1, f2 = s.fingerprint(), make_screen([el("A", "button", 1, 2, 5, 5)]).fingerprint()
        self.assertEqual(f1, f2)
        s2 = make_screen([el("A", "button", 1, 2, 5, 5, "changed")])
        self.assertNotEqual(f1, s2.fingerprint())


class TestBuildQuestions(unittest.TestCase):
    def test_type_target_only_with_typable(self):
        d = JevDecider(model="m", url="http://x", api_key="k")
        s = make_screen([el("Buy", "button", 0, 0, 1, 1),
                         el("Email", "textbox", 0, 5, 1, 1)])
        q = d.build_questions("g", "", s, [])
        self.assertIn("TYPE_TEXT", q["operation"]["criteria"])
        self.assertIn("type_target", q)
        self.assertEqual(set(q["click_target"]["criteria"]), {"1", "2"})
        self.assertEqual(set(q["type_target"]["criteria"]), {"2"})
        s2 = make_screen([el("Buy", "button", 0, 0, 1, 1)])
        q2 = d.build_questions("g", "", s2, [])
        self.assertNotIn("TYPE_TEXT", q2["operation"]["criteria"])
        self.assertNotIn("type_target", q2)
        self.assertEqual(set(q2["click_target"]["criteria"]), {"1"})


class TestDeciderValidation(unittest.TestCase):
    def _d(self):
        return JevDecider(model="m", url="http://x", api_key="k")

    def test_bad_choice_raises(self):
        d = self._d()
        s = make_screen([el("B", "button", 0, 0, 1, 1)])
        q = d.build_questions("g", "", s, [])
        bad = {"answers": {"operation": {"type": "choice", "choice": "NOPE",
                                         "probabilities": {}}}}
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(bad)) as m:
            with self.assertRaises(JevError):
                d.ask(s.to_jev_state([]), q)
        self.assertEqual(m.call_count, 1)

    def test_400_no_retry(self):
        d = self._d()
        s = make_screen([el("B", "button", 0, 0, 1, 1)])
        q = d.build_questions("g", "", s, [])
        he = urllib.error.HTTPError("http://x", 400, "bad", {}, io.BytesIO(b"bad"))
        with mock.patch("urllib.request.urlopen", side_effect=he) as m, \
                mock.patch("ghosthands.jev.time.sleep"):
            with self.assertRaises(JevError):
                d.ask(s.to_jev_state([]), q)
        self.assertEqual(m.call_count, 1)

    def test_503_then_200(self):
        d = self._d()
        s = make_screen([el("B", "button", 0, 0, 1, 1)])
        q = d.build_questions("g", "", s, [])
        ok = {"answers": {"operation": {"type": "choice", "choice": "CLICK",
                                        "probabilities": {"CLICK": 0.9}},
                          "click_target": {"type": "choice", "choice": "1",
                                           "probabilities": {"1": 1.0}}}}
        he = urllib.error.HTTPError("http://x", 503, "s", {}, io.BytesIO(b""))
        with mock.patch("urllib.request.urlopen", side_effect=[he, FakeResp(ok)]) as m, \
                mock.patch("ghosthands.jev.time.sleep"):
            resp = d.ask(s.to_jev_state([]), q)
        self.assertEqual(m.call_count, 2)
        self.assertEqual(resp["answers"]["operation"]["choice"], "CLICK")


class TestCandidates(unittest.TestCase):
    def test_extract_text_candidates(self):
        goal = "Subscribe to StreamOn3"
        guide = ('Choose "StreamOn3 Pro (Monthly)"\n'
                 'product ID: com.affirmi.streamon3.pro.monthly\n'
                 'support help@streamon3.tv\n'
                 'costs $8.99 monthly or $79.99 yearly')
        out = extract_text_candidates(goal, guide)
        self.assertEqual(out, ["help@streamon3.tv", "com.affirmi.streamon3.pro.monthly",
                               "StreamOn3 Pro (Monthly)", "8.99", "79.99"])


class TestPlanner(unittest.TestCase):
    def _screen(self):
        # "Next" on purpose: a commit word like "Buy" is gated by design (see TestGates)
        return make_screen([el("Skip", "button", 100, 100, 80, 30),
                            el("Next", "button", 300, 200, 80, 30)])

    def test_click_plan(self):
        answers = {"operation": {"type": "choice", "choice": "CLICK",
                                 "confidence": 0.97, "probabilities": {"CLICK": 0.97}},
                   "click_target": {"type": "choice", "choice": "2",
                                    "confidence": 0.9, "probabilities": {"2": 0.9}}}
        p = JevPlanner(reader=FakeReader(self._screen()), decider=FakeDecider(answers),
                       text_helper=FakeText(), min_confidence=0.8)
        plan, raw = p.decide("g", "", "", [], (2000, 1600))
        self.assertEqual(plan["action"], "click")
        fx, fy = plan["point"]
        self.assertIsInstance(fx, float) and self.assertIsInstance(fy, float)
        self.assertTrue(0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0)
        self.assertEqual(fx, 0.22)
        self.assertIn("[2]", plan["target"])
        self.assertEqual(json.loads(raw), {"answers": answers})

    def test_low_confidence_verify_stop(self):
        answers = {"operation": {"type": "choice", "choice": "CLICK",
                                 "confidence": 0.4, "probabilities": {"CLICK": 0.4}},
                   "click_target": {"type": "choice", "choice": "2",
                                    "confidence": 0.9, "probabilities": {"2": 0.9}}}
        p = JevPlanner(reader=FakeReader(self._screen()), decider=FakeDecider(answers),
                       text_helper=FakeText(), min_confidence=0.8)
        plan, _ = p.decide("g", "", "", [], (2000, 1600))
        self.assertEqual(plan["action"], "verify_stop")
        self.assertIn("low confidence", plan["reason"])

    def _typable(self):
        return make_screen([el("Skip", "button", 100, 100, 80, 30),
                            el("Email", "textbox", 300, 200, 80, 30)])

    def test_type_text_candidate(self):
        answers = {"operation": {"type": "choice", "choice": "TYPE_TEXT",
                                 "confidence": 0.95, "probabilities": {"TYPE_TEXT": 0.95}},
                   "type_target": {"type": "choice", "choice": "2",
                                   "confidence": 0.9, "probabilities": {"2": 0.9}}}
        dec = FakeDecider(answers, ask_answers={"text_value": {"choice": "8.99", "confidence": 0.9,
                                                                "probabilities": {"8.99": 0.9}}})
        p = JevPlanner(reader=FakeReader(self._typable()), decider=dec,
                       text_helper=FakeText(), min_confidence=0.8)
        plan, _ = p.decide("g", "costs $8.99 monthly", "", [], (2000, 1600))
        self.assertEqual(plan["action"], "type")
        self.assertEqual(plan["text"], "8.99")
        self.assertTrue(plan["select_all"])

    def test_type_text_write_fallback(self):
        answers = {"operation": {"type": "choice", "choice": "TYPE_TEXT",
                                 "confidence": 0.95, "probabilities": {"TYPE_TEXT": 0.95}},
                   "type_target": {"type": "choice", "choice": "2",
                                   "confidence": 0.9, "probabilities": {"2": 0.9}}}
        dec = FakeDecider(answers, ask_answers={"text_value": {"choice": "__WRITE__"}})
        th = FakeText()
        p = JevPlanner(reader=FakeReader(self._typable()), decider=dec,
                       text_helper=th, min_confidence=0.8)
        plan, _ = p.decide("g", "", "", [], (2000, 1600))
        self.assertEqual(plan["text"], "WRITTEN")
        self.assertTrue(th.called)



class TestGates(unittest.TestCase):
    """Deterministic gates added after review: commit controls, probability fallback, text checks."""

    def _planner(self, elements, answers, min_confidence=0.5):
        from ghosthands.jev import JevPlanner
        screen = make_screen(elements)

        class R:
            def snapshot(self):
                return screen

        class D:
            model = "t"
            def decide(self, goal, guide, scr, history):
                return {"answers": answers, "usage": {"cost": 0.0}, "latency_s": 0.0, "raw": {}}
            def ask(self, state, questions):
                return {"answers": {"text_value": {"type": "choice", "choice": "__WRITE__",
                                                   "probabilities": {"__WRITE__": 1.0}}}}

        class T:
            value = ""
            def write(self, *a, **k):
                return self.value

        t = T()
        return JevPlanner(reader=R(), decider=D(), text_helper=t,
                          min_confidence=min_confidence, min_target_confidence=0.3), t

    def test_commit_control_pauses_then_runs_after_approval(self):
        p, _ = self._planner([el("Activate base plan", "button", 10, 10, 100, 30)],
                             {"operation": {"type": "choice", "choice": "CLICK",
                                            "probabilities": {"CLICK": 0.95}},
                              "click_target": {"type": "choice", "choice": "1",
                                               "probabilities": {"1": 0.9}}})
        plan, _ = p.decide("g", "", None, ["click: something"], (0, 0))
        self.assertEqual(plan["action"], "verify_stop")
        self.assertIn("commit control", plan["reason"])
        plan, _ = p.decide("g", "", None, ["[human reviewed checkpoint and approved -> continue]"], (0, 0))
        self.assertEqual(plan["action"], "click")

    def test_target_confidence_falls_back_to_probability(self):
        p, _ = self._planner([el("Teams", "tab", 10, 10, 100, 30)],
                             {"operation": {"type": "choice", "choice": "CLICK",
                                            "probabilities": {"CLICK": 0.95}},
                              "click_target": {"type": "choice", "choice": "1",
                                               "probabilities": {"1": 0.9}}})
        plan, _ = p.decide("g", "", None, [], (0, 0))
        self.assertEqual(plan["action"], "click")
        self.assertIn("p=0.90", plan["reasoning"])

    def test_empty_text_helper_output_is_refused(self):
        from ghosthands.jev import JevError
        p, t = self._planner([el("Name", "textbox", 10, 10, 100, 30)],
                             {"operation": {"type": "choice", "choice": "TYPE_TEXT",
                                            "probabilities": {"TYPE_TEXT": 0.95}},
                              "type_target": {"type": "choice", "choice": "1",
                                              "probabilities": {"1": 0.9}}})
        t.value = ""
        with self.assertRaises(JevError):
            p.decide("g", 'name: "Pro"', None, [], (0, 0))
        t.value = "Pro"
        plan, _ = p.decide("g", "", None, [], (0, 0))
        self.assertEqual(plan["text"], "Pro")

    def test_password_values_are_redacted_in_snapshot_js(self):
        from ghosthands.dom_reader import SNAPSHOT_JS
        self.assertIn("ty === 'password'", SNAPSHOT_JS)
        self.assertIn("(filled, hidden)", SNAPSHOT_JS)


if __name__ == "__main__":
    unittest.main()
