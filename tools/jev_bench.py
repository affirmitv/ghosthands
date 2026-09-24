#!/usr/bin/env python3
"""Live benchmark for the Jev fast lane (Safari on a Mac, real hands).

Two suites of read-only web tasks. Each task names a start URL, a goal, a playbook and an end
condition that the benchmark checks itself on the final page (URL and full page text), so a run
is scored independently of the planner's own DONE check. Tasks never submit a form, never enter
card or personal details and never sign in.

    python3 tools/jev_bench.py --list
    python3 tools/jev_bench.py --suite multi --max-steps 20 --out /tmp/bench.json
    python3 tools/jev_bench.py --suite short --only firmi-team-trial

Needs OPENROUTER_API_KEY in the environment, Safari with Develop > Allow JavaScript from Apple
Events, and hands (GH_HANDS, default pico). Each task opens its own Safari window and closes it
at the end.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MULTI = [
    {"name": "lakeshow-14u-fuca-game",
     "url": "https://appspace.affirmi.tv/c/lakeshow-3ssb",
     "goal": "Open the Oakland 14U Fuca team, then open its most recent game page, and report the opponent.",
     "guide": "This is the Lakeshow 3SSB club schedule. First find the team list (scroll down) and "
              "click the Oakland 14U Fuca team link. Then, on the team page, click the first game "
              "in its list (the soonest one). DONE when a game page for the Oakland 14U Fuca team "
              "is showing.",
     "expect": "a game page (/g/) reached through the Oakland 14U Fuca team",
     "url_re": r"/c/lakeshow-3ssb/g/", "text_re": r"14U Fuca"},
    {"name": "try-firmi-then-trial",
     "url": "https://firmi.ai/",
     "goal": "Run the Try Firmi sample schedule, then from the results click Start 14-day free trial.",
     "guide": "First click 'No link handy? Watch Firmi read a real tournament schedule.' and wait "
              "while Firmi reads it until a list of games shows. Then click Start 14-day free "
              "trial (under Firmi Team). DONE when the Stripe checkout page for Firmi Team "
              "shows. Never type card details, never click Pay or Subscribe.",
     "expect": "Stripe checkout for Firmi Team, after the sample ran",
     "url_re": r"checkout\.stripe\.com", "text_re": r"Firmi"},
    {"name": "club-finder-next-game",
     "url": "https://appspace.affirmi.tv/login",
     "goal": "Use the club finder to find Lakeshow, open its club page or team page link, then find the next upcoming game.",
     "guide": "Type Lakeshow into the club search box and pick the Lakeshow 3SSB result. Then "
              "open the club's public page or schedule link (not sign in). Then find the next "
              "upcoming game on the schedule. Never sign in and never type a password. DONE "
              "when an upcoming game with its date is visible.",
     "expect": "a Lakeshow schedule or team page with an upcoming game date",
     "url_re": r"lakeshow", "text_re": r"(Upcoming|Next:)"},
    # The login page's club finder only offers "Sign in to <club>", a sign-in wall with no public
    # link, so the task above cannot finish without signing in. This one keeps a 3-step
    # read-only path on another site.
    {"name": "github-blog-post",
     "url": "https://github.com/affirmitv/ghosthands",
     "goal": "Open the blog folder, then open the smooth-scroll post, then find where it says how many notches a scroll moves.",
     "guide": "Click the blog folder in the file list. Then click smooth-scroll.md. Then scroll "
              "down through the post until a paragraph about notches is in view. DONE when "
              "the smooth-scroll post is showing with a mention of notches in view.",
     "expect": "blog/smooth-scroll.md rendered, a paragraph about notches in view",
     "url_re": r"/blob/main/blog/smooth-scroll\.md", "text_re": r"(?i)notch",
     "view_re": r"(?i)notch"},
    {"name": "github-pr4-files",
     "url": "https://github.com/affirmitv/ghosthands",
     "goal": "Open the Pull requests tab, open the closed PR titled like 'dead-click' (PR #4), and open its Files changed tab.",
     "guide": "Click the Pull requests tab. The PR is closed (merged), so open the Closed list. "
              "Click the PR whose title mentions the dead-click guard (#4). Then click its Files "
              "changed tab. DONE when the list of changed files is showing.",
     "expect": "github.com/affirmitv/ghosthands/pull/4/files",
     "url_re": r"/pull/4/(files|changes)", "text_re": r"jev"},
    {"name": "firmi-faq-cost-then-form",
     "url": "https://firmi.ai/",
     "goal": "Find the FAQ answer about what Firmi costs, then open the Get Firmi form from the Pro plan.",
     "guide": "Scroll down to the FAQ section ('The objections, answered') and find the question "
              "'What does it cost?' (its answer is always open; there is nothing to expand). Then "
              "keep going down to Plans and click Get Firmi under the Pro plan. DONE when the "
              "Get Firmi form (Your name, Email, Club name) is showing. Never type into the "
              "form and never submit it.",
     "expect": "the #get-firmi form in view after passing the FAQ cost answer",
     "url_re": r"firmi\.ai/(#get-firmi)?", "text_re": r"Club name",
     "view_re": r"(Your name|Club name|name of your club)"},
]

SHORT = [
    {"name": "lakeshow-14u-schedule",
     "url": "https://appspace.affirmi.tv/c/lakeshow-3ssb",
     "goal": "Open the schedule for the Oakland 14U Duckett team.",
     "guide": "Scroll to the team list and click Oakland 14U Duckett. DONE when that team's "
              "schedule page is showing.",
     "expect": "the Oakland 14U Duckett team page",
     "url_re": r"/t/oakland-14u-duckett", "text_re": r"14U Duckett"},
    {"name": "firmi-free-signup",
     "url": "https://firmi.ai/",
     "goal": "Get to the free AppSpace signup form from firmi.ai.",
     "guide": "Click Start free (it opens appspace.affirmi.tv). DONE when the AppSpace signup "
              "form is showing. Never type into it and never submit it.",
     "expect": "the AppSpace signup form on appspace.affirmi.tv",
     "url_re": r"appspace\.affirmi\.tv", "text_re": r"(?i)(email|club|organization)"},
    {"name": "try-firmi-sample",
     "url": "https://firmi.ai/",
     "goal": "Run the Try Firmi sample schedule and show the games it found.",
     "guide": "Click 'No link handy? Watch Firmi read a real tournament schedule.' and wait while "
              "Firmi reads it. DONE when a list of games is showing.",
     "expect": "the sample results with a list of games",
     "url_re": r"firmi\.ai", "text_re": r"(?i)\bvs\.?\b|games"},
    {"name": "fury-club-finder",
     "url": "https://appspace.affirmi.tv/login",
     "goal": "Find the Fury club with the club finder and open it.",
     "guide": "Type Fury into the club search box, then click the Fury result. DONE when the "
              "Fury club's sign in or club page shows. Never sign in.",
     "expect": "the Fury club page or its sign in",
     "url_re": r"appspace\.affirmi\.tv", "text_re": r"Fury"},
    {"name": "firmi-team-trial",
     "url": "https://firmi.ai/",
     "goal": "Start the Firmi Team 14-day free trial.",
     "guide": "Click Start 14-day free trial under Firmi Team. DONE when the Stripe checkout page "
              "for Firmi Team shows. Never type card details, never click Pay or Subscribe.",
     "expect": "Stripe checkout for Firmi Team",
     "url_re": r"checkout\.stripe\.com", "text_re": r"Firmi"},
]

SUITES = {"multi": MULTI, "short": SHORT}

_PAGE_JS = ("JSON.stringify({url: location.href, title: document.title, "
            "text: (document.body ? document.body.innerText : '').slice(0, 20000), "
            "view: (() => { const out = []; const w = document.createTreeWalker(document.body || "
            "document.documentElement, NodeFilter.SHOW_TEXT); let n; while ((n = w.nextNode()) && "
            "out.length < 400) { const r = n.parentElement && n.parentElement.getBoundingClientRect(); "
            "if (r && r.bottom > 0 && r.top < innerHeight && n.textContent.trim()) "
            "out.push(n.textContent.trim()); } document.querySelectorAll('input[placeholder],textarea[placeholder]')"
            ".forEach(e => { const r = e.getBoundingClientRect(); if (r.bottom > 0 && r.top < innerHeight) "
            "out.push(e.placeholder); }); return out.join(' ').slice(0, 6000); })()})")


def _osa(*lines: str) -> str:
    args = ["osascript"]
    for ln in lines:
        args += ["-e", ln]
    return subprocess.run(args, capture_output=True, text=True, timeout=30).stdout.strip()


def open_window(url: str) -> None:
    _osa('tell application "Safari" to activate',
         'tell application "Safari" to make new document with properties {URL:"%s"}' % url)


def close_window() -> None:
    _osa('tell application "Safari" to close front window')


def final_page() -> dict:
    from ghosthands.dom_reader import run_safari_js
    try:
        return json.loads(run_safari_js(_PAGE_JS))
    except Exception as e:  # scoring must not crash the suite
        print("final page read failed: %s" % e, flush=True)
        return {"url": "", "title": "", "text": "", "view": ""}


def score(task: dict, page: dict) -> bool:
    ok = bool(re.search(task["url_re"], page.get("url") or ""))
    ok = ok and bool(re.search(task["text_re"], page.get("text") or ""))
    if task.get("view_re"):
        ok = ok and bool(re.search(task["view_re"], page.get("view") or ""))
    return ok


def run_task(task: dict, max_steps: int, runs_dir: str, hands_backend: str | None) -> dict:
    from ghosthands import Agent, JevPlanner, make_hands
    run_dir = os.path.join(runs_dir, task["name"])
    open_window(task["url"])
    time.sleep(6)
    planner = JevPlanner()
    agent = Agent(planner, None, make_hands(hands_backend), eyes=object(), run_dir=run_dir)
    pauses: list = []

    def no_human(reason: str) -> str:
        # Nobody watches a benchmark: a pause for a human ends the task (scored as not passed
        # unless the end condition already holds).
        pauses.append(reason[:160])
        agent._log("checkpoint", "bench: no human, stopping: " + reason)
        return "abort"

    agent._checkpoint = no_human
    t0 = time.time()
    try:
        result = agent.run(task["goal"], task["guide"], max_steps=max_steps)
    except Exception as e:
        result = "crash: %s" % str(e)[:120]
    wall = time.time() - t0
    page = final_page()
    passed = score(task, page)
    d, v, t = planner.decider, planner.verifier, planner.text_helper
    extra_cost = sum(getattr(x, "total_cost", 0.0) for x in getattr(planner, "extra_models", ()))
    all_in = d.total_cost + v.total_cost + getattr(t, "total_cost", 0.0) + extra_cost
    close_window()
    row = {"name": task["name"], "result": result, "passed": passed, "steps": agent.step,
           "jev_calls": d.total_calls, "checks": v.total_calls, "cost": round(all_in, 6),
           "wall_s": round(wall, 1), "final_url": (page.get("url") or "")[:160],
           "final_title": (page.get("title") or "")[:80], "pauses": pauses}
    if not passed:
        row["final_view"] = (page.get("view") or "")[:240]
    print("BENCH %s" % json.dumps(row), flush=True)
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default="multi", choices=sorted(SUITES))
    ap.add_argument("--only", action="append", default=[], help="task name (repeatable)")
    ap.add_argument("--max-steps", type=int, default=20)
    ap.add_argument("--hands", default=None)
    ap.add_argument("--out", default=None, help="write the rows as JSON here")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        for s, tasks in SUITES.items():
            for t in tasks:
                print("%-6s %-26s %s\n       expect: %s" % (s, t["name"], t["url"], t["expect"]))
        return
    tasks = [t for t in SUITES[a.suite] if not a.only or t["name"] in a.only]
    runs_dir = os.environ.get("GH_RUNS_DIR", os.path.expanduser("~/gh-runs"))
    rows = [run_task(t, a.max_steps, os.path.join(runs_dir, a.suite), a.hands) for t in tasks]
    n = sum(1 for r in rows if r["passed"])
    print("\n%-26s %-9s %-6s %5s %5s %9s %7s" % ("task", "result", "pass", "steps", "jev", "cost", "wall"))
    for r in rows:
        print("%-26s %-9s %-6s %5d %5d %9.5f %6.1fs" % (r["name"], r["result"][:9], r["passed"],
                                                       r["steps"], r["jev_calls"], r["cost"], r["wall_s"]))
    print("passed %d/%d, total $%.5f" % (n, len(rows), sum(r["cost"] for r in rows)))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(rows, f, indent=1)


if __name__ == "__main__":
    main()
