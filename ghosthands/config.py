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

def _load_vnc_current():
    """VNC connection info written by bin/tart-vm-up. The tart experimental VNC
    endpoint (port + password) changes on every tart restart, so the helper
    records the current values here; GH_VNC_* env vars still override."""
    d = {}
    p = os.path.expanduser("~/.config/ghosthands/tart-vnc-current.env")
    try:
        for line in open(p):
            ls = line.lstrip()
            if ls.startswith("#") or not ls.strip():
                continue
            m = re.match(r"(?:export\s+)?(VNC_HOST|VNC_PORT|VNC_PASSWORD|VNC_PASSWORD_FILE)\s*=\s*(.+)", ls)
            if m:
                d[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    except OSError:
        pass
    return d

_VNC = _load_vnc_current()


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
    frame          = os.environ.get("GH_FRAME", "/tmp/gh_frame.jpg")
    trigger        = os.environ.get("GH_TRIGGER", "/tmp/gh_capture_now")
    pico_port      = os.environ.get("GH_PICO_PORT", "/dev/cu.usbmodem1101")
    hands_backend  = os.environ.get("GH_HANDS", "cliclick")  # pico | dryrun | cliclick | vnc
    runs_dir       = os.environ.get("GH_RUNS_DIR", os.path.expanduser("~/gh-runs"))
    # VNC backend (GH_HANDS=vnc): drive a Tart macOS VM's GUI over RFB with zero
    # host-screen interaction. bin/tart-vm-up writes the current endpoint to
    # ~/.config/ghosthands/tart-vnc-current.env (the tart experimental VNC port
    # and password change on every tart restart); GH_VNC_* env vars override.
    vnc_host         = os.environ.get("GH_VNC_HOST", _VNC.get("VNC_HOST", ""))
    vnc_port         = int(os.environ.get("GH_VNC_PORT", _VNC.get("VNC_PORT", "5900")))
    vnc_password     = os.environ.get("GH_VNC_PASSWORD", _VNC.get("VNC_PASSWORD"))
    vnc_password_file = os.environ.get("GH_VNC_PASSWORD_FILE",
                                       _VNC.get("VNC_PASSWORD_FILE",
                                                os.path.expanduser("~/.config/ghosthands/tart-vnc.env")))
