"""Two-faction model of the Doge election protocol.

Follows Mowbray & Gollmann (2007), "Electing the Doge of Venice: Analysis of a
13th Century Protocol", Sections 3.2-3.3 (https://rangevoting.org/HPL-2007-28R1.pdf).

A protocol is a list of steps:
  ("lot", c_next)            current college draws c_next members by lot
  ("elect", c_next, m)       current college elects c_next members; m = min approvals
  ("final",)                 current college elects the Doge by majority (paper's assumption)
State: probability vector p[j] = P(faction has j members in current college).

Run this file to check the implementation against the paper's numbers.
"""
import math


def lot(p, c, c2):
    q = [0.0] * (c2 + 1)
    tot = math.comb(c, c2)
    for j, pj in enumerate(p):
        if pj == 0:
            continue
        for i in range(max(0, c2 - (c - j)), min(j, c2) + 1):
            q[i] += pj * math.comb(j, i) * math.comb(c - j, c2 - i) / tot
    return q


def elect(p, c, c2, m, n, f):
    g = n - f  # other faction size
    lo, hi = max(0, c2 - g), min(f, c2)  # feasible faction count in next college
    q = [0.0] * (c2 + 1)
    for j, pj in enumerate(p):
        if pj == 0:
            continue
        if j >= m:
            q[hi] += pj
        elif c - j >= m:
            q[lo] += pj
        else:
            x = j * c2 / c
            fl = math.floor(x)
            fr = x - fl
            a = min(max(fl, lo), hi)
            b = min(max(fl + 1, lo), hi)
            q[a] += pj * (1 - fr)
            if fr > 0:
                q[b] += pj * fr
    return q


def negotiation_flag(p, c, m):
    """P(neither faction reaches m in this electing college)."""
    return sum(pj for j, pj in enumerate(p) if j < m and c - j < m)


def run(protocol, n, f):
    """Return (P(faction of size f wins), P(negotiation) for each election step)."""
    p = [0.0] * (n + 1)
    p[f] = 1.0
    c = n
    neg_rounds = []
    for step in protocol:
        if step[0] == "lot":
            c2 = step[1]
            p = lot(p, c, c2)
            c = c2
        elif step[0] == "elect":
            c2, m = step[1], step[2]
            neg_rounds.append(negotiation_flag(p, c, m))
            p = elect(p, c, c2, m, n, f)
            c = c2
        elif step[0] == "final":
            win = sum(pj for j, pj in enumerate(p) if j > c / 2)
            if c % 2 == 0:
                win += 0.5 * p[c // 2]
            return win, neg_rounds
    raise ValueError("protocol must end with final")


ORIGINAL = [("lot", 30), ("lot", 9), ("elect", 40, 7), ("lot", 12), ("elect", 25, 9),
            ("lot", 9), ("elect", 45, 7), ("lot", 11), ("elect", 41, 9), ("final",)]
SIMPLIFIED = [("lot", 9), ("elect", 45, 7), ("lot", 9), ("elect", 45, 7),
              ("lot", 9), ("elect", 45, 7), ("final",)]


def a_i(i, n=480, f=240):
    """Prob. faction wins given exactly i of 9 in the round-3 college (paper 3.7)."""
    p = [0.0] * 10
    p[i] = 1.0
    c = 9
    for step in ORIGINAL[2:]:
        if step[0] == "lot":
            p = lot(p, c, step[1]); c = step[1]
        elif step[0] == "elect":
            p = elect(p, c, step[1], step[2], n, f); c = step[1]
        else:
            return sum(pj for j, pj in enumerate(p) if j > c / 2)
    raise ValueError("protocol must end with final")


if __name__ == "__main__":
    print("a_i (paper 3.7: a3~0.1955 a4~0.3929 a5~0.6071 a6~0.8045):")
    print("  ", [round(a_i(i), 4) for i in range(10)])
    n = 480
    print("\nn=480: P(minority Doge), and P(negotiation in the round-3 college) vs the paper's 3.5 closed form")
    print("  share  original  simplified  negotiation  closed-form")
    for share in [0.05, 0.10, 0.20, 0.29, 0.30, 0.40, 0.45]:
        f = round(share * n)
        wo, nego = run(ORIGINAL, n, f)
        ws, _ = run(SIMPLIFIED, n, f)
        closed = sum(math.comb(f, i) * math.comb(n - f, 9 - i) for i in range(3, 7)) / math.comb(n, 9)
        print(f"  {share:4.2f}  {wo:8.4f}  {ws:10.4f}  {nego[0]:11.4f}  {closed:11.4f}")
