"""Smallest possible demo: no LLM, just prove the hands work by tracing a square
and typing a line.  Run:  python3 examples/trace_square.py"""
import time
from ghosthands import make_hands

h = make_hands()
print("ping:", h.ping() if hasattr(h, "ping") else "n/a")
pts = [(0.35, 0.35), (0.65, 0.35), (0.65, 0.65), (0.35, 0.65), (0.35, 0.35)]
for x, y in pts:
    print(h.move(x, y)); time.sleep(0.3)
print(h.type("ghosthands was here"))
