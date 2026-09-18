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
    jev_min_confidence = float(os.environ.get("GH_JEV_MIN_CONFIDENCE", "0.5"))  # operation below this -> verify_stop
    # A target is one pick among many controls, so its winning probability runs lower than an
    # operation's. 0.3 still means a clear winner over the runner-up on a page of 20 controls.
    jev_min_target_confidence = float(os.environ.get("GH_JEV_MIN_TARGET_CONFIDENCE", "0.3"))
    # After an action the planner re-reads the table (0.1 s each) until the page changes or this
    # many seconds pass. Legacy sites fetch a tab's content over the network; give them time.
    jev_settle_s   = float(os.environ.get("GH_JEV_SETTLE_S", "6.0"))
    frame          = os.environ.get("GH_FRAME", "/tmp/gh_frame.jpg")
    trigger        = os.environ.get("GH_TRIGGER", "/tmp/gh_capture_now")
    pico_port      = os.environ.get("GH_PICO_PORT", "/dev/cu.usbmodem1101")
    hands_backend  = os.environ.get("GH_HANDS", "pico")  # pico | dryrun
    runs_dir       = os.environ.get("GH_RUNS_DIR", os.path.expanduser("~/gh-runs"))
