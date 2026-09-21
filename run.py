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
    ap.add_argument("--hands", default=None, help="pico | dryrun | cliclick | vnc")
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
    backend = a.hands or Config.hands_backend
    if planner_kind == "jev":
        if backend == "vnc":
            # Jev over VNC: the element table is read from the guest's Safari
            # (SSH + AppleScript bridge), not the host's. Pixels never involved.
            from ghosthands.guest_reader import GuestSafariReader
            planner = JevPlanner(reader=GuestSafariReader())
        else:
            planner = JevPlanner()
    else:
        planner = Planner()
    if backend == "vnc":
        # VNC run: eyes read the guest framebuffer over RFB and navigation drives
        # the guest's Safari -- the host screen is never touched.
        # One persistent RFB session for the whole run: tart's _VZVNCServer has
        # crashed under connection churn, so hands, eyes and navigation share it.
        from ghosthands.vnc import VNCEyes, VNCHands, open_session, vnc_navigate, RFBError
        import subprocess as _sp
        rfb = None
        for _attempt in range(3):
            try:
                rfb = open_session()
                break
            except RFBError as e:
                # The experimental VNC server cycles its endpoint when tart
                # restarts; refresh it and retry instead of dying on launch.
                print("vnc connect failed (%s); refreshing endpoint via tart-vm-up" % e)
                _sp.run([os.path.expanduser("~/Development/ghosthands/bin/tart-vm-up")],
                        capture_output=True, timeout=180)
                # re-read the refreshed endpoint into the env open_session uses
                _env = {}
                try:
                    with open(os.path.expanduser("~/.config/ghosthands/tart-vnc-current.env")) as f:
                        for _line in f:
                            _line = _line.strip()
                            if _line.startswith("VNC_HOST="):
                                os.environ["GH_VNC_HOST"] = _line.split("=", 1)[1]
                            elif _line.startswith("VNC_PORT="):
                                os.environ["GH_VNC_PORT"] = _line.split("=", 1)[1]
                except OSError:
                    pass
        if rfb is None:
            raise RuntimeError("could not establish VNC session after 3 attempts")
        agent = Agent(planner, Grounder(), VNCHands(rfb=rfb), eyes=VNCEyes(rfb=rfb),
                      run_dir=a.run_dir,
                      navigate_fn=lambda url: vnc_navigate(url, rfb=rfb))
    else:
        agent = Agent(planner, Grounder(), make_hands(backend), run_dir=a.run_dir)
    print("planner=%s grounder=%s hands=%s run_dir=%s" % (
        Config.jev_model if planner_kind == "jev" else Config.planner_model,
        Config.grounder_model, backend, a.run_dir))
    try:
        result = agent.run(goal, guide, max_steps=max_steps)
    finally:
        if backend == "vnc":
            rfb.close()
    print("RESULT:", result)
    if planner_kind == "jev":
        d = planner.decider
        print("jev: %d decisions, $%.6f total (%s)" % (d.total_calls, d.total_cost, d.model))

if __name__ == "__main__":
    main()
