"""Pick the protocol setting for a community of N people (REQUIREMENTS.md section 9).

Searches lot size l, elected college size e and lot/elect cycles K, and keeps the
setting whose minority-win probabilities (two-faction model in model.py) stay
closest to the 1268 protocol at n=480.

Usage: python3 analysis/auto_config.py [N ...]
"""
import math
import sys

if __package__:
    from .model import ORIGINAL, run
else:
    from model import ORIGINAL, run

SHARES = [0.10, 0.20, 0.30, 0.40, 0.45]
REFERENCE_N = 480
WARNING_BELOW = 15


def reference_curve():
    return [run(ORIGINAL, REFERENCE_N, round(s * REFERENCE_N))[0] for s in SHARES]


def protocol(l, e, K):
    m = math.ceil(3 * l / 4)
    steps = []
    for _ in range(K):
        steps += [("lot", l), ("elect", e, m)]
    return steps + [("final",)]


def minority_curve(N, steps):
    return [run(steps, N, max(1, round(s * N)))[0] for s in SHARES]


def candidates(N):
    """Settings allowed by REQUIREMENTS.md section 9.2."""
    if N <= 3:
        raise ValueError("3 people or fewer cannot form the colleges")
    if N >= WARNING_BELOW:
        for l in range(3, 10):
            for e in range(2 * l, N // 2 + 1):
                for K in (2, 3):
                    yield l, e, K
    else:
        for l in range(2, 10):
            for e in range(l + 1, N):
                for K in (1, 2, 3):
                    yield l, e, K


def auto_config(N, ref=None):
    ref = ref or reference_curve()
    best = None
    for l, e, K in candidates(N):
        dev = max(abs(a - b) for a, b in zip(minority_curve(N, protocol(l, e, K)), ref))
        key = (round(dev, 3), -K)  # smallest deviation first, then more cycles
        if best is None or key < best[0]:
            best = (key, l, e, K, dev)
    _, l, e, K, dev = best
    return {"l": l, "e": e, "K": K, "m": math.ceil(3 * l / 4), "final": math.ceil(3 * e / 5),
            "deviation": dev, "warning": N < WARNING_BELOW}


if __name__ == "__main__":
    ns = [int(a) for a in sys.argv[1:]] or [50, 45, 40, 35, 30, 25, 20, 15, 14, 12, 10, 6]
    ref = reference_curve()
    print("reference (1268 protocol, n=480):", " / ".join(f"{x:.3f}" for x in ref))
    print("  N |  l   e  K | elect | final | deviation | warning")
    for N in ns:
        c = auto_config(N, ref)
        elect = f"{c['m']}/{c['l']}"
        final = f"{c['final']}/{c['e']}"
        print(f"{N:3d} | {c['l']:2d}  {c['e']:2d}  {c['K']} | {elect:5s} | {final:5s} | {c['deviation']:.3f}     | {'yes' if c['warning'] else ''}")
