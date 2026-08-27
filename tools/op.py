import sys, os, time
sys.path.insert(0, os.path.expanduser("~/ghosthands"))
from ghosthands import Eyes, Grounder, PicoHands
cmd = sys.argv[1] if len(sys.argv) > 1 else "capture"
eyes = Eyes()
if cmd == "capture":
    f = eyes.capture(); print("frame", eyes.dims(f))
elif cmd == "click":
    desc = " ".join(sys.argv[2:]); f = eyes.capture(); d = eyes.dims(f)
    x, y, frac, raw = Grounder().locate(f, desc, d)
    print("ground %d,%d frac %.3f,%.3f (%s)" % (x, y, frac[0], frac[1], raw))
    h = PicoHands(); h.move(frac[0], frac[1]); time.sleep(0.15); h.click()
elif cmd == "type":
    print(PicoHands().type(" ".join(sys.argv[2:])))
elif cmd == "key":
    print(PicoHands().key(sys.argv[2]))
