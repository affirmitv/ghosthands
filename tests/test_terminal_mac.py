"""Offline tests for the terminal-mac target: a fake `terminal-mac` CLI records every argv and
answers like the real one, so no VM is needed."""
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["OPENROUTER_API_KEY"] = "test"

from ghosthands.agent import Agent  # noqa: E402
from ghosthands.terminal_mac import (TerminalMac, TerminalMacError, TerminalMacEyes,  # noqa: E402
                                     TerminalMacHands)

FAKE = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, sys
    log = os.environ["FAKE_TMAC_LOG"]
    with open(log, "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")
    args = sys.argv[1:]
    verb = args[2]
    if os.environ.get("FAKE_TMAC_FAIL") == verb:
        sys.stderr.write("No logged-in guest session\\n"); sys.exit(1)
    if verb == "status":
        print(json.dumps({"screenRecording": os.environ.get("FAKE_TMAC_SR", "true"),
                          "accessibility": "true", "width": "1280", "height": "800"}))
    elif verb == "screenshot":
        out = args[args.index("--output") + 1]
        src = os.environ["FAKE_TMAC_PNG"]
        open(out, "wb").write(open(src, "rb").read())
        print(json.dumps({"image": out, "width": "1280", "height": "800"}))
    else:
        print(json.dumps({}))
""")


def _png(path, color="ff0000"):
    # A real 8x8 PNG made with sips from a tiny PPM, so the JPEG conversion path runs for real.
    ppm = path + ".ppm"
    with open(ppm, "wb") as f:
        f.write(b"P6 8 8 255\n" + bytes.fromhex(color) * 64)
    os.system("sips -s format png %s --out %s >/dev/null 2>&1" % (ppm, path))
    return path


class FakeCLI(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bin = os.path.join(self.dir, "terminal-mac")
        with open(self.bin, "w") as f:
            f.write(FAKE)
        os.chmod(self.bin, os.stat(self.bin).st_mode | stat.S_IEXEC)
        self.log = os.path.join(self.dir, "calls.jsonl")
        os.environ["FAKE_TMAC_LOG"] = self.log
        os.environ["FAKE_TMAC_PNG"] = _png(os.path.join(self.dir, "guest.png"))
        os.environ.pop("FAKE_TMAC_FAIL", None)
        os.environ.pop("FAKE_TMAC_SR", None)
        self.vm = TerminalMac("work", binary=self.bin, timeout=5)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        # Drop the fixed prefix: ["action", "work", VERB, "--timeout", "5", *args]
        out = []
        with open(self.log) as f:
            lines = f.readlines()
        for line in lines:
            a = json.loads(line)
            self.assertEqual(a[:2], ["action", "work"])
            self.assertEqual(a[3:5], ["--timeout", "5"])
            out.append([a[2]] + a[5:])
        return out

    def reset_log(self):
        if os.path.exists(self.log):
            os.remove(self.log)


class TestHands(FakeCLI):
    def test_fractions_become_guest_points(self):
        h = TerminalMacHands(self.vm); self.reset_log()
        h.move(0.5, 0.25); h.click()
        h.move(1.0, 1.0); h.click("right")
        self.assertEqual(self.calls(), [["move", "640", "200"], ["click", "640", "200"],
                                        ["move", "1279", "799"], ["click", "1279", "799", "--button", "right"]])

    def test_double_click_is_one_request(self):
        h = TerminalMacHands(self.vm); self.reset_log()
        h.move(0.1, 0.1); h.double_click()
        self.assertEqual(self.calls()[-1], ["click", "128", "80", "--double"])

    def test_type_is_literal_after_double_dash(self):
        h = TerminalMacHands(self.vm); self.reset_log()
        h.type("--timeout 99; rm -rf ~")
        self.assertEqual(self.calls(), [["type", "--", "--timeout 99; rm -rf ~"]])

    def test_key_names_are_normalized(self):
        h = TerminalMacHands(self.vm); self.reset_log()
        h.key("cmd+a"); h.key("delete"); h.key("command,shift,4"); h.key(["cmd", "l"]); h.key("Enter")
        self.assertEqual(self.calls(), [["key", "cmd+a"], ["key", "backspace"], ["key", "cmd+shift+4"],
                                        ["key", "cmd+l"], ["key", "return"]])

    def test_scroll_sign_and_clamp(self):
        h = TerminalMacHands(self.vm); h.move(0.5, 0.5); self.reset_log()
        h.scroll(3); h.scroll(-2); h.scroll({"amount": 50})
        self.assertEqual(self.calls(), [["scroll", "--dy", "-360", "--x", "640", "--y", "400"],
                                        ["scroll", "--dy", "240", "--x", "640", "--y", "400"],
                                        ["scroll", "--dy", "-1000", "--x", "640", "--y", "400"]])

    def test_missing_permission_fails_loudly(self):
        os.environ["FAKE_TMAC_SR"] = "false"
        with self.assertRaises(TerminalMacError) as e:
            TerminalMacHands(self.vm)
        self.assertIn("screenRecording", str(e.exception))

    def test_cli_error_is_raised_with_message(self):
        h = TerminalMacHands(self.vm)
        os.environ["FAKE_TMAC_FAIL"] = "click"
        with self.assertRaises(TerminalMacError) as e:
            h.click()
        self.assertIn("No logged-in guest session", str(e.exception))

    def test_navigate_types_into_the_address_bar(self):
        h = TerminalMacHands(self.vm); self.reset_log()
        h.navigate("https://example.com/a b")
        self.assertEqual(self.calls(), [["key", "cmd+l"], ["key", "cmd+a"], ["key", "backspace"],
                                        ["type", "--", "https://example.com/a b"], ["key", "return"],
                                        ["key", "escape"]])


class TestEyes(FakeCLI):
    def test_capture_returns_jpeg_with_guest_dims(self):
        eyes = TerminalMacEyes(self.vm, frame=os.path.join(self.dir, "f.jpg"))
        path = eyes.capture()
        self.assertEqual(open(path, "rb").read(3), b"\xff\xd8\xff")
        self.assertEqual(eyes.dims(path), (1280, 800))


class TestAgentWiring(FakeCLI):
    def test_agent_uses_hands_navigate_and_double_click(self):
        h = TerminalMacHands(self.vm)
        a = Agent(planner=None, grounder=None, hands=h, eyes=object(), run_dir=self.dir)
        self.assertEqual(a.navigate_fn, h.navigate)
        self.reset_log()
        a._execute({"action": "double_click", "target": "x", "point": [0.5, 0.5]}, (1280, 800), None)
        self.assertEqual([c[0] for c in self.calls()], ["move", "click"])
        self.assertIn("--double", self.calls()[-1])


if __name__ == "__main__":
    unittest.main()
