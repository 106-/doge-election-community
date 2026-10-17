"""Closed-halfplane Tukey depth at each observed member's position."""
import math


def evaluate(members, winner=None):
    if not members or any(p.get('truth') is None for p in members):
        return None
    points = [(p['truth']['x'], p['truth']['y']) for p in members]
    values = {}
    for person, (x, y) in zip(members, points):
        angles = sorted({(math.atan2(py-y, px-x)+turn) % (2*math.pi)
                         for px, py in points if (px, py) != (x, y)
                         for turn in (math.pi/2, 3*math.pi/2)})
        if not angles:
            values[person['user_id']] = len(points)
            continue
        probes = [(a+((angles[(i+1) % len(angles)]-a) % (2*math.pi))/2) % (2*math.pi)
                  for i, a in enumerate(angles)]
        scale = max(1.0, *(abs(px-x)+abs(py-y) for px, py in points))
        depth = min(sum((px-x)*math.cos(a)+(py-y)*math.sin(a) >= -1e-10*scale
                        for px, py in points) for a in angles+probes)
        values[person['user_id']] = depth
    best = max(values.values())
    centers = [person for person, depth in values.items() if depth == best]
    result = {'depths': values, 'max_depth': best, 'centers': centers}
    if winner:
        by_id = {p['user_id']: p['truth'] for p in members}
        w = by_id[winner]
        result.update(winner_depth=values[winner],
                      rank=1+sum(d > values[winner] for d in values.values()),
                      gap=best-values[winner],
                      distance=min(math.hypot(w['x']-by_id[c]['x'], w['y']-by_id[c]['y'])
                                   for c in centers))
    return result
