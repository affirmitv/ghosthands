"""Central config for ghosthands. Everything overridable via environment."""
import os, re

def _load_key():
    k = os.environ.get("OPENROUTER_API_KEY")
    if k:
        return k.strip()
    kf = os.environ.get("GH_KEY_FILE", os.path.expanduser("~/.config/ghosthands/openrouter.env"))
    if os.path.exists(kf):
        for line in open(kf):
            m = re.match(r'\s*(?:export\s+)?OPENROUTER_API_KEY\s*=\s*(.+)', line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    return None

class Config:
    api_key        = _load_key()
    base_url       = os.environ.get("GH_BASE_URL", "https://openrouter.ai/api/v1")
    # Planner = the "brain": a cheap VISION LLM that sees the screen and decides the next action.
    planner_model  = os.environ.get("GH_PLANNER_MODEL", "z-ai/glm-5.3-flash")
    # Grounder = the "eyes->coords": a GUI grounding model that turns "click X" into a pixel.
    grounder_model = os.environ.get("GH_GROUNDER_MODEL", "bytedance/ui-tars-1.5-7b")
    # Jev = TypeSafe's decision model (System One). Reads a structured element table, answers
    # "which operation / which element" with calibrated probabilities in ~300 ms. Output tokens are free.
    planner        = os.environ.get("GH_PLANNER", "vision")   # vision | jev
    jev_url        = os.environ.get("GH_JEV_URL", "https://openrouter.ai/api/alpha/decisions")
    jev_model      = os.environ.get("GH_JEV_MODEL", "typesafe/jev-1.13")
    jev_text_model = os.environ.get("GH_JEV_TEXT_MODEL", "z-ai/glm-5.3-flash")  # writes TYPE_TEXT values only
    # Operation gate. Jev's top operation on an exploratory page (scroll or click, both fine)
    # often sits near 0.45; this catches noise, not ambiguity. Money is gated by label.
    jev_min_confidence = float(os.environ.get("GH_JEV_MIN_CONFIDENCE", "0.35"))  # operation below this -> verify_stop
    # A target is one pick among many controls, and several are often equally right (three 14u
    # division rows when the playbook says "open a 14u division"). The gate only catches a pick
    # that is noise; money and production commits are gated separately by label (jev.is_commit_control).
    jev_min_target_confidence = float(os.environ.get("GH_JEV_MIN_TARGET_CONFIDENCE", "0.15"))
    # After an action the planner re-reads the table (0.1 s each) until the page changes or this
    # many seconds pass. Legacy sites fetch a tab's content over the network; give them time.
    jev_settle_s   = float(os.environ.get("GH_JEV_SETTLE_S", "6.0"))
    # Verified DONE: a DONE from Jev is checked by the small text model against the playbook's
    # "DONE when ..." clause before the run stops; with such a clause the check also runs after
    # every action so the run stops as soon as the condition shows. At most one check per step.
    jev_verify_done = os.environ.get("GH_JEV_VERIFY_DONE", "1").strip().lower() not in ("0", "false", "no", "off", "")
    # Scroll guard: after this many SCROLL decisions in a row (or one scroll that changed nothing)
    # the next decision is not offered SCROLL. 0 turns the guard off.
    jev_max_scrolls = int(os.environ.get("GH_JEV_MAX_SCROLLS", "3"))
    # Wheel notches per Jev scroll. Travel grows faster than linearly with notches (measured on
    # a Mac: 2 -> ~375 px, 3 -> ~865 px, 5 -> ~2000 px); 3 keeps a scroll under one viewport, so
    # no row passes between two reads of the table.
    jev_scroll_notches = int(os.environ.get("GH_JEV_SCROLL_NOTCHES", "3"))
    # Dead-click guard: a CLICK that leaves the page unchanged (same table, URL and title once
    # the settle time passes) marks that control dead for the rest of the run; it is dropped from
    # the table Jev picks from, and after two dead clicks in a row the next step scrolls instead.
    jev_dead_click_guard = os.environ.get("GH_JEV_DEAD_CLICK_GUARD", "1").strip().lower() not in ("0", "false", "no", "off", "")
    # Loading wait: after a CLICK or Enter, a new loading signal (a disabled submit, aria-busy, a
    # control reading "Loading..." / "Reading it...") is polled, without Jev decisions, until it
    # clears or this many seconds pass. 0 turns it off.
    jev_loading_wait_s = float(os.environ.get("GH_JEV_LOADING_WAIT_S", "45"))
    # Multi-step tasks. Subgoals: the playbook is split into ordered steps (no model call; it
    # parses sentences and "then"); Jev is told the current step and a short trail of the pages
    # already visited, and the per-step check (the same single small-model call as the DONE
    # check) also says when the current step is complete, which advances it. Only a playbook
    # with 2 or more steps turns this on. 0 turns it off.
    jev_subgoals = os.environ.get("GH_JEV_SUBGOALS", "1").strip().lower() not in ("0", "false", "no", "off", "")
    # Back-navigation recovery (multi-step playbooks only): after this many decisions on a page
    # other than the one the current step started on, without the step completing, or when Jev
    # asks for a human there, press Back and mark the link that led there as a wrong turn.
    # 0 turns it off.
    jev_backtrack_after = int(os.environ.get("GH_JEV_BACKTRACK_AFTER", "4"))
    # How Back is done: "url" (default) points the tab at the last good page's URL, the way the
    # navigate action does; a chord such as "cmd+left" presses it instead (a focused text field
    # eats that chord, and the Pico keymap has no "[").
    jev_back = os.environ.get("GH_JEV_BACK", "url")
    frame          = os.environ.get("GH_FRAME", "/tmp/gh_frame.jpg")
    trigger        = os.environ.get("GH_TRIGGER", "/tmp/gh_capture_now")
    pico_port      = os.environ.get("GH_PICO_PORT", "/dev/cu.usbmodem1101")
    hands_backend  = os.environ.get("GH_HANDS", "pico")  # pico | dryrun
    runs_dir       = os.environ.get("GH_RUNS_DIR", os.path.expanduser("~/gh-runs"))
