"""Human-like pointer motion for scripted actions.

- Path:   cubic Bézier curve with randomly placed control points (slight arc).
- Speed:  duration from Fitts's law; minimum-jerk profile (accelerate, then ease into the target).
- Flaws:  1D Perlin noise tremor, strongest mid-move and fading out at the target; small aim offset.
- Timing: random reaction delay before acting and random click-hold duration.
"""

import math
import random

REPORT_INTERVAL = 0.008  # seconds between HID reports while gliding


class Perlin1D:
    def __init__(self):
        self.gradients = [random.uniform(-1, 1) for _ in range(256)]

    def __call__(self, x):
        i = math.floor(x)
        f = x - i
        g0, g1 = self.gradients[i & 255], self.gradients[(i + 1) & 255]
        u = f * f * (3 - 2 * f)
        return 2 * (g0 * f * (1 - u) + g1 * (f - 1) * u)  # roughly -1..1


def fitts_duration(distance, target_width):
    """Movement time MT = a + b * log2(D / W + 1), varied per move."""
    a, b = 0.10, 0.12
    return (a + b * math.log2(distance / target_width + 1)) * random.uniform(0.85, 1.2)


def min_jerk(t):
    return t * t * t * (10 - 15 * t + 6 * t * t)


def _bezier(p0, c1, c2, p1, s):
    m = 1 - s
    return tuple(m**3 * p0[i] + 3 * m * m * s * c1[i] + 3 * m * s * s * c2[i] + s**3 * p1[i] for i in (0, 1))


def path(p0, p1, target_width=40.0, duration=None):
    """Points from p0 to p1, one per REPORT_INTERVAL, ending exactly at p1."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    dist = math.hypot(dx, dy)
    if dist < 1:
        return [p1]
    nx, ny = -dy / dist, dx / dist  # unit normal to the straight line
    bend = dist * random.uniform(0.04, 0.18) * random.choice((-1, 1))

    def control(frac):
        k = bend * random.uniform(0.6, 1.2)
        return (p0[0] + dx * frac + nx * k, p0[1] + dy * frac + ny * k)

    c1, c2 = control(random.uniform(0.2, 0.4)), control(random.uniform(0.6, 0.8))
    duration = duration or fitts_duration(dist, target_width)
    steps = max(2, round(duration / REPORT_INTERVAL))
    tremor_x, tremor_y = Perlin1D(), Perlin1D()
    amplitude = random.uniform(0.5, 1.5)  # pixels

    points = []
    for k in range(1, steps + 1):
        t = k / steps
        x, y = _bezier(p0, c1, c2, p1, min_jerk(t))
        fade = math.sin(math.pi * t)
        points.append((x + amplitude * fade * tremor_x(k * 0.2), y + amplitude * fade * tremor_y(k * 0.2)))
    points[-1] = p1
    return points


def aim(x, y, spread=3.0):
    """Where a person would actually hit when aiming at (x, y)."""
    return (x + max(-2 * spread, min(2 * spread, random.gauss(0, spread))),
            y + max(-2 * spread, min(2 * spread, random.gauss(0, spread))))


def reaction_delay():
    return min(0.9, max(0.12, random.lognormvariate(math.log(0.25), 0.35)))


def settle_delay():
    return random.uniform(0.03, 0.12)


def click_hold():
    return random.uniform(0.06, 0.14)


def watch_time():
    """Seconds spent on one short video before swiping on: mostly 5-20 s, sometimes a quick
    skip. Capped under iOS's shortest auto-lock (30 s) so the screen never turns off."""
    if random.random() < 0.12:
        return random.uniform(1.5, 3.5)
    return min(25, max(4, random.lognormvariate(math.log(10), 0.5)))
