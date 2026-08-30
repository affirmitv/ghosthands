import sys, os, time
sys.path.insert(0, os.path.expanduser("~/ghosthands"))
from ghosthands import Eyes, Grounder, PicoHands
cmd = sys.argv[1] if len(sys.argv) > 1 else "capture"
eyes = None
def _eyes():
    global eyes
    if eyes is None: eyes = Eyes()
    return eyes
if cmd == "capture":
    f = _eyes().capture(); print("frame", _eyes().dims(f))
elif cmd == "click":
    desc = " ".join(sys.argv[2:]); f = _eyes().capture(); d = _eyes().dims(f)
    x, y, frac, raw = Grounder().locate(f, desc, d)
    print("ground %d,%d frac %.3f,%.3f (%s)" % (x, y, frac[0], frac[1], raw))
    h = PicoHands(); h.move(frac[0], frac[1]); time.sleep(0.15); h.click()
elif cmd == "type":
    print(PicoHands().type(" ".join(sys.argv[2:])))
elif cmd == "key":
    print(PicoHands().key(sys.argv[2]))
elif cmd == "scroll":
    amount = int(sys.argv[2])
    spn = int(sys.argv[3]) if len(sys.argv) > 3 else None
    h = PicoHands()
    if spn is not None:
        print(h.scroll_smooth(amount, spn))
    else:
        print(h.scroll(amount))
