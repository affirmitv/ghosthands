#!/usr/bin/env python3
"""ghosthands CLI:  python3 run.py --goal-file examples/streamon3_subscriptions.py
or drive a one-off:  python3 run.py --goal "..." --guide "..." """
import argparse, importlib.util, os, sys
from ghosthands import Planner, Grounder, JevPlanner, Agent, make_hands, Config

def load_task(path):
    spec = importlib.util.spec_from_file_location("gh_task", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod.GOAL, mod.GUIDE, getattr(mod, "MAX_STEPS", 80)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--goal-file")
    ap.add_argument("--goal"); ap.add_argument("--guide", default="")
    ap.add_argument("--run-dir", default=os.path.join(Config.runs_dir, "run"))
    ap.add_argument("--max-steps", type=int, default=80)
    ap.add_argument("--hands", default=None, help="pico | dryrun | cliclick")
    ap.add_argument("--planner", default=None, help="vision (screenshot LLM) | jev (Safari element table + Jev decisions)")
    a = ap.parse_args()
    if a.goal_file:
        goal, guide, max_steps = load_task(a.goal_file)
    elif a.goal:
        goal, guide, max_steps = a.goal, a.guide, a.max_steps
    else:
        print("need --goal-file or --goal"); sys.exit(2)
    if not Config.api_key:
        print("no OPENROUTER_API_KEY (env or ~/.config/ghosthands/openrouter.env)"); sys.exit(2)
    planner_kind = a.planner or Config.planner
    planner = JevPlanner() if planner_kind == "jev" else Planner()
    agent = Agent(planner, Grounder(), make_hands(a.hands), run_dir=a.run_dir)
    print("planner=%s grounder=%s hands=%s run_dir=%s" % (
        Config.jev_model if planner_kind == "jev" else Config.planner_model,
        Config.grounder_model, a.hands or Config.hands_backend, a.run_dir))
    result = agent.run(goal, guide, max_steps=max_steps)
    print("RESULT:", result)
    if planner_kind == "jev":
        d = planner.decider
        print("jev: %d decisions, $%.6f total (%s)" % (d.total_calls, d.total_cost, d.model))

if __name__ == "__main__":
    main()
