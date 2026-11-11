#!/usr/bin/env python3
"""Lineup - your AI coding agents as surfers on a live ASCII ocean.

Every agent you connect (Claude Code, Codex, or anything that can run a shell command) gets a
surfer with its own coloured board. The sea mirrors what they're doing: glassy and asleep when
idle, rolling breakers while they work, a storm with a giant question mark when one needs you.

    lineup                      live view in this terminal
    lineup --demo               cycle through every mood with fake agents
    lineup report NAME STATE    tell Lineup what an agent is doing (see README)
    lineup install claude-code  wire up Claude Code's hooks
    lineup status               show what Lineup currently sees

Pure Python 3.8+ standard library. Everything stays on your machine.
"""
import argparse
import fcntl
import json
import math
import os
import random
import select
import re
import shutil
import signal
import subprocess
import sys
import termios
import time
import tty

VERSION = "1.0.0"
FPS = 20

# ------------------------------------------------------------------ colour

WATER = [  # depth gradient: foam-lit surface -> abyss
    (0.00, (200, 240, 252)),
    (0.10, (110, 200, 240)),
    (0.30, (40, 135, 210)),
    (0.60, (14, 70, 150)),
    (1.00, (4, 20, 64)),
]
FACE = (150, 235, 245)   # translucent glow on a standing wave face
FOAM = (245, 252, 255)
ICE = (175, 220, 250)

SKY = {
    "sleeping": ((2, 4, 14), (14, 30, 64)),
    "idle": ((8, 22, 54), (70, 125, 185)),
    "waiting": ((10, 26, 62), (90, 150, 205)),
    "working": ((6, 16, 46), (55, 110, 175)),
    "needs_you": ((8, 20, 60), (95, 155, 215)),
}

# target sea behaviour per mood (lerped smoothly between)
PARAMS = {
    "sleeping": dict(swell=0.7, chop=0.10, brk=0.00, period=9.0),
    "idle": dict(swell=2.6, chop=0.30, brk=0.22, period=7.0),
    "waiting": dict(swell=3.0, chop=0.40, brk=0.38, period=5.5),
    "working": dict(swell=3.4, chop=0.60, brk=1.00, period=4.2),
    "needs_you": dict(swell=3.6, chop=0.90, brk=1.05, period=3.2),
}


def mix(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def grad(stops, t):
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            return mix(c0, c1, (t - t0) / (t1 - t0))
    return stops[-1][1]


def smooth(x):
    x = 0.0 if x < 0 else 1.0 if x > 1 else x
    return x * x * (3 - 2 * x)


_CUBE = [0, 95, 135, 175, 215, 255]


def to256(r, g, b):
    def c(v):
        return 0 if v < 48 else 1 if v < 115 else min(5, (v - 35) // 40)
    ri, gi, bi = c(r), c(g), c(b)
    dc = (_CUBE[ri] - r) ** 2 + (_CUBE[gi] - g) ** 2 + (_CUBE[bi] - b) ** 2
    gx = max(0, min(23, ((r + g + b) // 3 - 3) // 10))
    gv = 8 + 10 * gx
    dg = (gv - r) ** 2 + (gv - g) ** 2 + (gv - b) ** 2
    return 16 + 36 * ri + 6 * gi + bi if dc <= dg else 232 + gx


class Painter:
    def __init__(self, truecolor):
        self.tc = truecolor
        self.cache = {}

    def esc(self, fg, bg=(0, 0, 0)):
        key = (fg, bg)
        e = self.cache.get(key)
        if e is None:
            w = "1" if len(fg) > 3 else "22"   # 4-tuple colour = bold
            if self.tc:
                e = f"\033[{w};38;2;{fg[0]};{fg[1]};{fg[2]};48;2;{bg[0]};{bg[1]};{bg[2]}m"
            else:
                e = f"\033[{w};38;5;{to256(*fg[:3])};48;5;{16 if bg == (0, 0, 0) else to256(*bg)}m"
            self.cache[key] = e
        return e


def qc(c):  # quantise a float colour so runs merge and the cache stays small
    return (int(c[0]) & ~3, int(c[1]) & ~3, int(c[2]) & ~3)


# ------------------------------------------------------------------ the sea

QBM = [
    " ##### ",
    "##   ##",
    "     ##",
    "    ## ",
    "   ##  ",
    "   ##  ",
    "       ",
    "   ##  ",
    "   ##  ",
]


class Breaker:
    def __init__(self, t, period, h0, W):
        self.t0, self.L, self.h0, self.W = t, max(3.5, period * 1.6), h0, W
        self.crashed = False

    def p(self, t):
        return (t - self.t0) / self.L

    def geom(self, t):
        p, h0 = self.p(t), self.h0
        c = self.W * (-0.15 + 1.05 * p)
        if p < 0.5:
            hb = h0 * smooth(p / 0.5)
        elif p < 0.72:
            hb = h0
        else:
            hb = h0 * max(0.2, 1 - 0.8 * smooth((p - 0.72) / 0.15))   # rolls on as whitewater
        if p < 0.72:
            fw = h0 * 2.0 + (0.6 - h0 * 2.0) * smooth(p / 0.55)
        else:
            fw = 0.6 + (h0 * 1.5 - 0.6) * smooth((p - 0.72) / 0.2)
        bw = max(3.0, h0 * 2.3)
        curl = h0 >= 3
        theta = 3.8 * smooth((p - 0.45) / 0.22) if curl and 0.45 <= p < 0.82 else 0.0
        drop = h0 * 0.9 * smooth((p - 0.72) / 0.1) if p >= 0.72 else 0.0
        foam = smooth((p - 0.66) / 0.1) * (1 - 0.7 * smooth((p - 0.8) / 0.2))
        return p, c, hb, fw, bw, theta, drop, foam


class Surfer:
    def __init__(self, aid, name, board, slot):
        self.id, self.name, self.board, self.slot = aid, name, board, slot
        self.home = 0.18 + 0.37 * slot          # phase seed so surfers don't move in lockstep
        self.body = (255, 255, 255)              # body colour: tells sessions of one agent apart
        self.sx = self.sy = None
        self.svx = 0.0
        self.riding = self.ride_b = self.wipe = None
        self.row = None
        self.air = None      # launched off the lip: {"vx", "vy"}
        self.push = 0.0      # sideways shove from shaking the window (cols/s)
        self.duck = None     # duck diving under a big wave: {"t0", "b"}
        self.mood, self.label, self.present = "idle", "", True


class Sea:
    def __init__(self, state):
        self.state = state
        self.par = dict(PARAMS[state])
        self.sky = SKY[state]
        self.breakers = []
        self.spray = []    # [x, y, vx, vy, life]
        self.qp = []       # question-mark particles [x, y, vx, vy, tx, ty]
        self.next_spawn = 0.0
        self.t = 0.0
        self.dims = None
        self.stars = []
        self.flash = 0.0
        self.surfers = {}        # agent id -> Surfer
        self.reduce = False      # reduced motion: calmer sea, no flicker, fewer particles
        self.positions = {}      # agent id -> (col, row) of the surfer's head
        self.next_turn = None
        self.tilt = self.tiltv = 0.0     # sloshing from window shakes (rows at the edges)
        self.heave = self.heavev = 0.0   # whole sea bobbing up/down
        self.shake_energy = 0.0
        self.fish = []
        self.bubbles = []   # [x, y, life]
        self.egg = None     # rare easter egg in progress
        self.reef_top = []
        self.reef_cells = {}

    # -------------------------------------------------- geometry helpers
    def layout(self, W, H):
        if self.dims != (W, H):
            self.dims = (W, H)
            self.compact = H < 34
            # short windows: keep enough sea below for the reef, and leave headroom above the
            # biggest crest for the text line + a standing surfer (4 rows) so nobody leaves the screen
            # sea level low in the window so the breakers get the sky; the reef shrinks to fit below
            self.base = max(int(H * 0.68), H - 7) if self.compact else int(H * 0.68)
            # swells shrink harder than breakers in small windows: the breakers are the show
            self.scale = max(0.3, min(1.0, (H / 42) ** 1.5))
            # headroom above the biggest crest: a standing surfer (4 rows) + 1; text may be covered
            reserve = 5
            swell_peak = 4.9 * self.scale
            self.hmax = max(2.0, min((self.base - 3) * 0.92, (self.base - reserve - swell_peak) / 1.08))
            rnd = random.Random(7)
            self.stars = [(rnd.randrange(W), rnd.randrange(max(1, self.base - 4)), rnd.random() * 6)
                          for _ in range(W * H // 60)]
            self._make_reef(W, H)
            self.fish = []
            if self.qp:
                self._set_q_targets()

    def _make_reef(self, W, H):
        rnd = random.Random(11)
        ph = [rnd.random() * 6 for _ in range(3)]
        rock = []
        for x in range(W):
            v = (math.sin(x * 0.11 + ph[0]) + 0.6 * math.sin(x * 0.27 + ph[1]) + 0.4 * math.sin(x * 0.53 + ph[2]))
            r = max(0, int(round((v + 0.7) * 2.2)) - 1)
            rock.append(min(r, 2) if H < 34 else r)          # low reef in small windows
        self.reef_top = [H - 1 - r for r in rock]
        cells = {}   # static rocks + sand: (x, y) -> (char, colour)
        for x in range(W):
            if rock[x] == 0:
                cells[(x, H - 1)] = (rnd.choice("_.,_~_"), (150, 130, 90))
            for y in range(H - rock[x], H):
                dep = (y - (H - rock[x])) / max(1, rock[x])
                cells[(x, y)] = (rnd.choice("#%@&8#"), qc(mix((95, 105, 130), (45, 50, 70), dep)))
        self.reef_cells = cells
        items, x = [], rnd.randrange(2, 6)
        while x < W - 4:
            kind = rnd.choice(["weed", "weed", "weed", "coralA", "coralB", "fan", "shell"])
            items.append((x, kind, rnd.randint(2, 3) if H < 34 else rnd.randint(2, 7), rnd.random() * 6))
            x += rnd.randint(4, 11)
        self.reef_items = items

    def _spawn_fish(self, W, top, bot, school=False):
        right = random.random() < 0.5
        y = random.uniform(top, bot)
        col = random.choice([(255, 215, 80), (255, 140, 60), (110, 230, 240), (255, 120, 170), (240, 245, 255)])
        n = random.randint(3, 5) if school else 1
        spr = "><>" if school else random.choice(["><>", "><'>", "><(('>", "><((('>"])
        if not right:
            spr = spr[::-1].translate(str.maketrans("<>()", "><)("))
        vx = random.uniform(3, 9) * (1 if right else -1)
        x0 = -len(spr) - 1 if right else W + 1
        for i in range(n):
            self.fish.append([x0 - (i * 5 if right else -i * 5), y + random.uniform(-1, 1) * (i > 0),
                              vx, spr, col, random.random() * 6])

    def _q_targets(self):
        W = self.dims[0]
        qh = max(5, min(self.base - 4, 13))
        f = qh / 9.0
        cx, top = int(W * 0.76), 1
        pts = []
        for i, row in enumerate(QBM):
            for j, ch in enumerate(row):
                if ch != "#":
                    continue
                for yy in range(int(i * f), max(int(i * f) + 1, int((i + 1) * f))):
                    for xx in range(int(j * f), max(int(j * f) + 1, int((j + 1) * f))):
                        pts.append((cx - int(3.5 * f) + xx, top + yy))
        return pts

    def _set_q_targets(self):
        pts = self._q_targets()
        while len(self.qp) < len(pts):
            self.qp.append(list(self.qp[-1]) if self.qp else [self.dims[0] / 2, self.base, 0, 0, 0, 0])
        del self.qp[len(pts):]
        for q, (tx, ty) in zip(self.qp, pts):
            q[4], q[5] = tx, ty

    def surface(self, W, t):
        par, base = self.par, self.base
        sw, ch = par["swell"] * self.scale, par["chop"] * self.scale
        geoms = [b.geom(t) for b in self.breakers]
        env = 0.6 + 0.75 * (0.5 + 0.5 * math.sin(t * 0.19)) ** 2   # swells arrive in sets
        tilt, heave = self.tilt, self.heave
        slosh = abs(tilt) * 0.3 + abs(heave) * 0.3
        s = []
        for x in range(W):
            a = sw * env * (0.62 * math.sin(0.065 * x - 0.85 * t) + 0.38 * math.sin(0.17 * x - 1.6 * t + 1.3))
            a += ch * 0.5 * math.sin(0.7 * x - 3.1 * t) + ch * 0.25 * math.sin(1.3 * x + 2.3 * t)
            for _, c, hb, fw, bw, *_ in geoms:
                u = x - c
                w = bw if u < 0 else fw
                if -4 * bw < u < 4 * fw + 1:
                    a += hb * math.exp(-(u / w) ** 2)
            if tilt or heave:
                a += tilt * (1 - 2 * x / max(1, W)) + heave + slosh * math.sin(0.3 * x - 5 * t)
            s.append(max(1.0, base - a))
        return s, geoms

    # -------------------------------------------------- simulation
    def set_state(self, state):
        if state == self.state:
            return
        if state == "needs_you":
            W = self.dims[0] if self.dims else 80
            self.qp = [[random.uniform(0, W), self.base if self.dims else 20,
                        random.uniform(-10, 10), random.uniform(-40, -15), 0, 0] for _ in range(1)]
            if self.dims:
                self._set_q_targets()
                for q in self.qp:
                    q[0], q[1] = random.uniform(0, W), self.base + random.uniform(0, 3)
                    q[2], q[3] = random.uniform(-10, 10), random.uniform(-45, -20)
        elif self.state == "needs_you":
            for q in self.qp:   # let the question mark rain back into the sea
                self.spray.append([q[0], q[1], q[2] + random.uniform(-3, 3), q[3], 3.0])
            self.qp = []
        self.state = state

    def step(self, dt, W, H):
        self.layout(W, H)
        self.t += dt
        t = self.t
        k = 1 - math.exp(-dt * 1.5)
        for key, v in PARAMS[self.state].items():
            self.par[key] += (v - self.par[key]) * k
        tgt = SKY[self.state]
        self.sky = (mix(self.sky[0], tgt[0], k), mix(self.sky[1], tgt[1], k))

        # spawn breakers
        if t >= self.next_spawn:
            self.set_n = getattr(self, "set_n", 0) + 1
            pos = self.set_n % 5                      # 5-wave cycle: lull, lull, building set...
            size = (random.uniform(0.45, 0.6) if pos in (1, 2) else
                    random.uniform(0.7, 0.85) if pos == 3 else random.uniform(0.85, 0.97))
            if random.random() < 0.12:
                size = random.uniform(0.98, 1.06)     # a bomb
            h0 = self.par["brk"] * self.hmax * size
            if h0 >= 1.2:
                b = Breaker(t, self.par["period"] * (1.5 if self.reduce else 1.0), h0, W)
                b.riders, b.dropin = None, False   # claimed in the lineup when it arrives
                self.breakers.append(b)
            self.next_spawn = t + self.par["period"] * random.uniform(0.85, 1.15)
        p_end = (1.15 + 8.0 / max(1, W)) / 1.05          # crest has rolled past the right edge
        self.breakers = [b for b in self.breakers if b.p(t) < p_end]

        # sloshing: damped springs pushed by window shakes
        lim = self.hmax * 0.7
        self.tiltv += (-12.0 * self.tilt - 0.8 * self.tiltv) * dt
        self.tilt = max(-lim, min(lim, self.tilt + self.tiltv * dt))
        self.heavev += (-16.0 * self.heave - 1.0 * self.heavev) * dt
        self.heave = max(-3.0, min(3.0, self.heave + self.heavev * dt))
        if abs(self.tilt) < 0.02 and abs(self.tiltv) < 0.02:
            self.tilt = self.tiltv = 0.0
        if abs(self.heave) < 0.02 and abs(self.heavev) < 0.02:
            self.heave = self.heavev = 0.0

        surf, geoms = self.surface(W, t)
        g = 38.0 * max(0.6, H / 30)
        if self.shake_energy > 0.3:
            for _ in range(int(self.shake_energy * 2)):
                x = random.randrange(W)
                self.spray.append([x, surf[x], random.uniform(-8, 8),
                                   -random.uniform(4, 6 + self.shake_energy * 3), random.uniform(0.6, 1.4)])
        self.shake_energy *= math.exp(-dt * 3)
        self.g = g

        # crash spray
        for b, (p, c, hb, fw, bw, theta, drop, foam) in zip(self.breakers, geoms):
            R = b.h0 * 0.32
            if 0.7 <= p < 0.86:
                n = int(b.h0 * 0.7 * dt * FPS * (0.3 if self.reduce else 1.0)) + 1
                for _ in range(n):
                    x = c + R * random.uniform(0.6, 2.8)
                    xi = int(x)
                    if 0 <= xi < W:
                        v = math.sqrt(2 * g * b.h0 * random.uniform(0.2, 0.75))
                        self.spray.append([x, surf[xi], random.uniform(-4, 14), -v, random.uniform(0.8, 1.8)])
            if p >= 0.72 and not b.crashed:
                b.crashed = True
                on = [sf for sf in self.surfers.values()
                      if sf.present and sf.riding and sf.ride_b is b and sf.wipe is None]
                if len(on) == 2 and b.dropin and random.random() < 0.55:
                    for k, sf in enumerate(on):          # tangled up: both go down together
                        self._start_wipe(sf, t, direction=(-1 if k else 1), burst=True)
                else:
                    for sf in on:
                        if random.random() < 0.14:
                            self._start_wipe(sf, t)
                self.flash = 1.0 if self.state == "needs_you" and not self.reduce else self.flash
                for q in self.qp:   # blow the question mark apart; it re-forms
                    q[2] += random.uniform(-28, 28)
                    q[3] += random.uniform(-30, 14)
        # little whitecaps blown off swell crests
        if self.par["chop"] > 0.25 and not self.reduce:
            for _ in range(int(self.par["chop"] * 3)):
                x = random.randrange(W)
                if 0 < x < W - 1 and surf[x] <= surf[x - 1] and surf[x] <= surf[x + 1] and surf[x] < self.base - 1:
                    self.spray.append([x, surf[x], random.uniform(1, 6), -random.uniform(3, 8), 0.5])

        alive = []
        for sp in self.spray:
            sp[3] += g * dt
            sp[0] += sp[2] * dt
            sp[1] += sp[3] * dt
            sp[4] -= dt
            xi = int(sp[0])
            if sp[4] > 0 and 0 <= xi < W and sp[1] < surf[xi] + 0.5 and sp[1] > -2:
                alive.append(sp)
        self.spray = alive[-1500:]

        for q in self.qp:
            ax = 34 * (q[4] - q[0]) - 7 * q[2] + random.uniform(-6, 6)
            ay = 34 * (q[5] - q[1]) - 7 * q[3] + random.uniform(-6, 6)
            q[2] += ax * dt
            q[3] += ay * dt
            q[0] += q[2] * dt
            q[1] += q[3] * dt
        self.flash = max(0.0, self.flash - dt * 2.2)

        # fish + bubbles
        ftop, fbot = (self.base + 1, H - 3) if self.compact else (self.base + 2, H - 6)
        if fbot - ftop >= 1:
            want = max(2, min(10, int(W * (fbot - ftop) / 160)))
            if len(self.fish) < want and random.random() < dt * 1.5:
                self._spawn_fish(W, ftop, fbot, school=random.random() < 0.25)
            for f in self.fish:
                f[0] += f[2] * dt
                f[1] = min(fbot, max(ftop, f[1] + math.sin(t * 1.3 + f[5]) * 0.4 * dt))
            self.fish = [f for f in self.fish if -30 < f[0] < W + 30]
            if random.random() < dt * 1.2 and self.reef_items:
                x = random.choice(self.reef_items)[0]
                self.bubbles.append([x, self.reef_top[x] - 2, 4.0])
        else:
            self.fish = []
        for bb in self.bubbles:
            bb[1] -= 3.2 * dt
            bb[0] += math.sin(t * 4 + bb[1]) * 1.5 * dt
            bb[2] -= dt
        self.bubbles = [bb for bb in self.bubbles
                        if bb[2] > 0 and 0 <= int(bb[0]) < W and bb[1] > surf[int(bb[0])] + 0.5]


        # ---- rare easter eggs (about one every few minutes)
        egg = self.egg
        if egg is None and not getattr(self, "reduce", False) and random.random() < dt / 150.0:
            kinds = ["shark", "gull", "dolphin", "bottle"] + (["ufo", "ufo"] if self.state == "sleeping" else [])
            k = random.choice(kinds)
            if k == "shark":
                egg = dict(kind=k, x=-4.0, vx=random.uniform(5, 8))
            elif k == "gull":
                egg = dict(kind=k, x=float(W + 3), vx=-random.uniform(9, 13),
                           y=float(random.randint(3, max(3, int(self.base - self.hmax * 0.9) - 3))))
            elif k == "dolphin":
                egg = dict(kind=k, x=random.uniform(W * 0.15, W * 0.45), hops=random.randint(2, 3), ph=0.0)
            elif k == "bottle":
                egg = dict(kind=k, x=-3.0, vx=random.uniform(2.5, 4))
            else:
                egg = dict(kind=k, x=W * random.uniform(0.35, 0.65), y=2.0, age=0.0)
            self.egg = egg
        if egg is not None:
            kind = egg["kind"]
            if kind in ("shark", "gull", "bottle"):
                egg["x"] += egg["vx"] * dt
                if egg["x"] < -6 or egg["x"] > W + 6:
                    self.egg = None
            elif kind == "dolphin":
                egg["ph"] += dt / 1.3
                egg["x"] += 9 * dt
                if egg["ph"] >= 1.0:
                    xi = max(0, min(W - 1, int(egg["x"])))
                    for _ in range(8):
                        self.spray.append([egg["x"], surf[xi], random.uniform(-4, 4), -random.uniform(4, 9), 0.6])
                    egg["ph"] = 0.0
                    egg["hops"] -= 1
                    if egg["hops"] <= 0 or egg["x"] > W - 4:
                        self.egg = None
            elif kind == "ufo":
                egg["age"] += dt
                if egg["age"] > 9:
                    egg["x"] += 60 * dt                      # zoom off
                    egg["y"] -= 8 * dt
                    if egg["x"] > W + 10:
                        self.egg = None

        def sfc(x):
            return surf[max(0, min(W - 1, int(x)))]

        self._assign_spots(W)
        for sf in self.surfers.values():
            if sf.present:
                self._move_surfer(sf, dt, t, W, surf, geoms, g, sfc)
        self._keep_apart(dt, W)
        return surf, geoms

    def _waiting(self, sf, W):
        """Sitting at his spot in the lineup, ready for the next wave."""
        return (not sf.wipe and not sf.air and sf.ride_b is None and sf.sx is not None
                and abs(sf.sx - self._home_x(sf, W)) < 4 + W * 0.015)

    def _home_x(self, sf, W):
        """Lineup spots: spot 0 is the peak, the rest further out, never on top of each other."""
        spot = sf.spot if getattr(sf, "spot", None) is not None else sf.slot
        gap = max(13.0, W * 0.14)
        peak = W * 0.32
        x = peak - spot * gap
        if x < 4.0:                       # out of room on the left: fill in to the right of the peak
            k = int((peak - 4.0) // gap)  # spots that fit on the left
            x = peak + (spot - k) * gap * 0.85
        return max(4.0, min(W * 0.62, x))

    def _after(self, sf):
        """Whose turn is it after this surfer (round-robin over working agents)."""
        workers = sorted((o for o in self.surfers.values() if o.present and o.mood == "working"),
                         key=lambda o: o.slot)
        if len(workers) < 2:
            return None
        ids = [o.id for o in workers]
        i = ids.index(sf.id) if sf.id in ids else -1
        return ids[(i + 1) % len(ids)]

    def _assign_spots(self, W):
        """Whoever is closer to the beach (further right) gets the spot nearer the peak, so a surfer
        paddling back never has to swim through someone sitting in the lineup."""
        free = [o for o in self.surfers.values()
                if o.present and o.sx is not None and o.ride_b is None and not o.air]
        if not free:
            return
        free.sort(key=lambda o: -(o.wipe["bx"] if o.wipe else o.sx))
        probe = type("Spot", (), {})()
        def spot_x(i):
            probe.spot, probe.slot = i, i
            return self._home_x(probe, W)
        spots = sorted(range(len(free)), key=lambda i: -spot_x(i))   # spot indices, rightmost first
        want = {o.id: spots[i] for i, o in enumerate(free)}
        if any(getattr(o, "spot", None) != want[o.id] for o in free):
            cur = sorted(free, key=lambda o: -spot_x(o.spot) if getattr(o, "spot", None) is not None else 0)
            if [o.id for o in cur] != [o.id for o in free] or any(getattr(o, "spot", None) is None for o in free):
                for o in free:
                    o.spot = want[o.id]

    def _keep_apart(self, dt, W, gap=11.0):
        """Boards never overlap unless they're sharing a wave (drop-in) or someone is airborne."""
        fl = [o for o in self.surfers.values() if o.present and o.sx is not None and not o.air]

        def pos(sf):
            return sf.wipe["bx"] if sf.wipe else sf.sx

        def shift(sf, d):
            if sf.wipe:
                sf.wipe["bx"] = max(3.0, min(W - 4.0, sf.wipe["bx"] + d))
            else:
                sf.sx = max(3.0, min(W - 4.0, sf.sx + d))

        for i in range(len(fl)):
            for j in range(i + 1, len(fl)):
                a, b = fl[i], fl[j]
                if a.riding and b.riding:
                    continue
                d = pos(a) - pos(b)
                if abs(d) >= gap:
                    continue
                sgn = 1.0 if d >= 0 else -1.0
                step = min(gap - abs(d), 14.0 * dt)      # ease apart, no teleporting
                if a.riding:      # the one riding has right of way
                    shift(b, -sgn * step)
                elif b.riding:
                    shift(a, sgn * step)
                else:
                    shift(a, sgn * step / 2)
                    shift(b, -sgn * step / 2)

    def _start_wipe(self, sf, t, direction=1, burst=False):
        room = max(1.0, sf.sy - 1 - 4)            # the cartwheeling board must stay on screen
        vmax = math.sqrt(2 * getattr(self, "g", 38.0) * room)
        sf.wipe = dict(t0=t, bx=sf.sx, by=sf.sy - 1, bvx=random.uniform(3, 9) * direction,
                       bvy=-min(random.uniform(16, 26), vmax), air=True, px=sf.sx, py=sf.sy)
        if burst:
            for _ in range(25):
                self.spray.append([sf.sx, sf.sy - 1, random.uniform(-10, 10), -random.uniform(8, 22), 1.2])

    def shake(self, dx, dy):
        """Window moved by (dx cols, dy rows): the water lags behind and sloshes."""
        self.tiltv += dx * 2.2
        self.heavev += -dy * 2.0
        m = math.hypot(dx, dy)
        self.shake_energy = min(12.0, self.shake_energy + m)
        for f in self.fish:
            f[0] -= dx * 0.6
        hard = (m > 5 or self.shake_energy > 9) and self.t - getattr(self, "knock_t", -9.0) > 2.5
        knocked = False
        for sf in self.surfers.values():
            if not sf.present or sf.sx is None or sf.wipe:
                continue
            sf.push -= dx * 3.0 * random.uniform(0.6, 1.5)          # everyone slides a little differently
            if hard and not sf.air and random.random() < 0.6:       # a hard shake knocks them off
                self._start_wipe(sf, self.t, direction=(-1 if dx > 0 else 1), burst=True)
                knocked = True
        if knocked:
            self.knock_t = self.t

    def sync_agents(self, agents):
        """agents: [{id, name, mood, label, board(rgb)}] in lineup order."""
        seen = set()
        for slot, ag in enumerate(agents):
            sf = self.surfers.get(ag["id"])
            if sf is None:
                sf = self.surfers[ag["id"]] = Surfer(ag["id"], ag["name"], (*ag["board"], 1), slot)
            sf.slot, sf.name, sf.board = slot, ag["name"], (*ag["board"], 1)
            sf.home = 0.18 + 0.37 * slot
            sf.mood, sf.label, sf.present = ag["mood"], ag.get("label", ""), True
            sf.body = tuple(ag.get("body") or (255, 255, 255))
            seen.add(ag["id"])
        for k in list(self.surfers):
            if k not in seen:
                del self.surfers[k]
        combined = max((ag["mood"] for ag in agents), key=lambda m: PRIO.get(m, 0), default="sleeping")
        self.set_state(combined)

    def _move_surfer(self, sf, dt, t, W, surf, geoms, g, sfc):
        # wipeout in progress: board cartwheels, he tumbles, goes under, swims back
        if sf.wipe:
            w = sf.wipe
            age = t - w["t0"]
            if w["air"]:
                w["bvy"] += g * dt
                w["bx"] += w["bvx"] * dt
                w["by"] += w["bvy"] * dt
                if w["bvy"] > 0 and w["by"] >= sfc(w["bx"]):
                    w["air"] = False
            else:
                w["bx"] += 1.5 * dt
                w["by"] = sfc(w["bx"])
            w["bx"] = max(3.0, min(W - 4.0, w["bx"]))
            if age < 0.7:
                w["px"] += 8 * dt
            elif age < 1.8:
                w["px"] += 3 * dt
                if random.random() < dt * 14:
                    self.bubbles.append([w["px"] + random.uniform(-1, 1), sfc(w["px"]) + 2, 1.0])
            else:
                d = w["bx"] - w["px"]
                w["px"] += max(-5 * dt, min(5 * dt, d))
            w["px"] = max(2.0, min(W - 3.0, w["px"]))
            w["py"] = sfc(w["px"])
            if (age > 2.3 and abs(w["px"] - w["bx"]) < 1.0) or age > 6:
                sf.sx, sf.sy, sf.svx, sf.wipe = w["bx"], sfc(w["bx"]), 0.0, None
                sf.row = None
            sf.riding, sf.ride_b = None, None
            return

        # airborne off the lip: fly, then land back in the water
        if sf.air:
            a_ = sf.air
            a_["vy"] += g * dt
            old = sf.sx
            sf.sx = max(3.0, min(W - 4.0, sf.sx + a_["vx"] * dt))
            sf.sy += a_["vy"] * dt
            sf.row = int(round(sf.sy))
            landing_b = a_.get("b")
            if a_["vy"] > 0 and sf.sy >= sfc(sf.sx) - 0.3:
                sf.sy, sf.air = sfc(sf.sx), None
                for _ in range(12):
                    self.spray.append([sf.sx, sf.sy, random.uniform(-6, 6), -random.uniform(5, 12), 0.8])
            sf.riding, sf.svx = None, 0.0
            sf.ride_b = landing_b if landing_b in self.breakers else None   # land and keep riding
            return

        if sf.sx is None:
            sf.sx = self._home_x(sf, W)
        best = None
        alive = dict(zip(map(id, self.breakers), zip(self.breakers, geoms)))
        if sf.ride_b is not None and id(sf.ride_b) in alive:            # already riding: go to the edge
            b, g_ = alive[id(sf.ride_b)]
            if g_[1] < W - 4:
                best = (b, g_)
        elif sf.mood == "working" and self._waiting(sf, W):              # sitting at the peak
            ready = [o for o in self.surfers.values()
                     if o is not sf and o.present and o.mood == "working" and self._waiting(o, W)]
            for b, g_ in zip(self.breakers, geoms):
                p_, c_ = g_[0], g_[1]
                if b.h0 < min(2.5, self.hmax * 0.4) or not 0.15 < p_ < 0.62:
                    continue
                if b.riders is None:
                    if abs(c_ - sf.sx) > 4 + b.h0 * 0.3:
                        continue
                    if self.next_turn not in (None, sf.id) and any(o.id == self.next_turn for o in ready):
                        continue                                          # their turn - let it pass
                    b.riders = [sf.id]
                    if ready and random.random() < 0.12:                  # someone drops in on someone
                        b.riders, b.dropin = [sf.id, random.choice(ready).id], True
                    self.next_turn = self._after(sf)
                elif sf.id not in b.riders:
                    continue
                if best is None or b.h0 > best[0].h0:
                    best = (b, g_)
        old = sf.sx
        if best:
            b, (p, c, hb, fw, bw, theta, drop, foam) = best
            R = b.h0 * 0.32
            carve = math.sin(t * 1.7 + sf.home * 7) * min(3.0, b.h0 * 0.3)   # up and down the face
            tx = c + (R * 1.0 if theta > 1.2 else max(1.0, fw * 0.7) + carve)
            behind = b.dropin and b.riders[-1] == sf.id
            if behind:
                tx -= 7          # the one dropping in, just up the face
            sf.riding = "tube" if theta > 1.2 and not behind else "ride"
            sf.ride_b = b
            sf.sx += (max(3.0, min(W - 4.0, tx)) - sf.sx) * (1 - math.exp(-dt * 5))
            tried = b.__dict__.setdefault("air_tried", set())
            if 0.64 < p < 0.7 and sf.id not in tried:   # boost off the lip before it closes out
                tried.add(sf.id)
                if random.random() < 0.4:
                    room = max(1.0, sf.sy - 6)
                    sf.air = dict(vx=random.uniform(5, 10), vy=-math.sqrt(2 * g * min(room, 3 + b.h0 * 0.5)), b=b)
        else:
            sf.riding, sf.ride_b = None, None
            tx = self._home_x(sf, W) + math.sin(t * 0.12 + sf.home * 9) * W * 0.015   # my spot in the lineup
            v = max(5.0, W * 0.07)                                            # steady paddling speed
            sf.sx += max(-v * dt, min(v * dt, tx - sf.sx))
        if sf.push:
            sf.sx += sf.push * dt
            sf.push *= math.exp(-dt * 2.5)
            if abs(sf.push) < 0.05:
                sf.push = 0.0
        sf.sx = max(3.0, min(W - 4.0, sf.sx))
        if dt > 0:
            sf.svx += ((sf.sx - old) / dt - sf.svx) * (1 - math.exp(-dt * 4))
        # duck dive: a big wave about to land on a surfer who isn't riding it -> push the board under
        if best:
            sf.duck = None
        elif sf.duck is None:
            for b, g_ in zip(self.breakers, geoms):
                c_, hb_ = g_[1], g_[2]
                if hb_ >= max(2.5, self.hmax * 0.7) and 0 < sf.sx - c_ < 3 + hb_ * 0.3:
                    if random.random() < 0.12:          # mistimed it: caught inside, washed back
                        sf.push += random.uniform(7, 11)
                        for _ in range(14):
                            self.spray.append([sf.sx, sf.sy - 1, random.uniform(-4, 10), -random.uniform(6, 14), 0.9])
                    else:
                        sf.duck = dict(t0=t, b=b)
                    break
        else:
            gone = sf.duck["b"] not in self.breakers
            passed = (not gone) and sf.duck["b"].geom(t)[1] > sf.sx + 4
            if gone or passed or t - sf.duck["t0"] > 1.8:
                sf.duck = None                          # pop back up behind the wave
                for _ in range(6):
                    self.spray.append([sf.sx, sf.sy - 1, random.uniform(-3, 3), -random.uniform(3, 7), 0.5])
            elif random.random() < dt * 10:
                self.bubbles.append([sf.sx + random.uniform(-2, 2), sfc(sf.sx) + 1.5, 0.8])

        # sit on the average water height under the whole board (ignores chop spikes),
        # follow it gently, and only hop to a new text row once clearly past the midpoint
        ty = sum(sfc(sf.sx + k) for k in range(-3, 4)) / 7.0 + (2.4 if sf.duck else 0.0)
        follow = 9.0 if sf.duck else 3.5 if sf.riding else 5.0
        sf.sy = ty if sf.sy is None else sf.sy + (ty - sf.sy) * (1 - math.exp(-dt * follow))
        if sf.row is None or abs(sf.sy - sf.row) > 0.8:
            sf.row = int(round(sf.sy))

    # -------------------------------------------------- drawing
    def draw(self, W, H, surf, geoms, label, painter, plain=False):
        t, base, hmax = self.t, self.base, self.hmax
        BLACK = (0, 0, 0)
        deep = max(4.0, H - base + hmax * 0.3)
        ch = [[" "] * W for _ in range(H)]
        fg = [[BLACK] * W for _ in range(H)]
        ccache = {}
        rnd = random.random
        flow = 0.0 if self.reduce else t * 7.0

        for x in range(W):
            s = surf[x]
            r0 = int(s)
            slope = surf[min(W - 1, x + 1)] - surf[max(0, x - 1)]
            white = smooth((base - s) / max(1.0, hmax * 0.55))
            sx_ = math.sin(x * 0.13 - t * 1.2)
            for y in range(max(0, r0), H):
                d = y - s
                if y == r0:  # the skin of the sea
                    if slope < -1.6:
                        c = "/"
                    elif slope > 1.6:
                        c = "\\"
                    else:
                        c = "~^*'-~"[int(x * 0.7 + flow) % 6] if (self.reduce or rnd() > 0.15) else random.choice("~*^.")
                    ch[y][x] = c
                    fg[y][x] = qc(mix(ICE, FOAM, 0.4 + 0.6 * white))
                    continue
                dabs = (y - (base - hmax * 0.15)) / deep
                inwave = y <= base and s < base - 1.5
                k1 = int(max(0.0, min(1.0, dabs)) * 32)
                k2 = int(max(0.0, min(1.0, d / max(1.5, base + 1 - s))) * 12) if inwave else 99
                key = (k1, k2, d < 2)
                ent = ccache.get(key)
                if ent is None:
                    c = grad(WATER, 0.04 + k1 / 32 * 0.96)
                    if k2 != 99:
                        q = k2 / 12
                        c = grad([(0.0, FOAM), (0.25, FACE), (0.6, (70, 178, 230)), (1.0, c)], q)
                        cs = "@#%&*" if q < 0.2 else "*+=%~" if q < 0.5 else "~=+-:"
                    elif d < 2:
                        c = mix(c, WATER[1][1], 0.5)
                        cs = "~=+*~-"
                    elif k1 < 11:
                        cs = "~=-:~+"
                    elif k1 < 18:
                        cs = "-:.~ - =  . "
                    else:   # deep, dark and sparse so the reef + fish read clearly
                        cs = " .    :       '        .   "
                    ent = (qc(c), cs)
                    ccache[key] = ent
                cs = ent[1]
                i = int(x * 0.55 - flow + y * 2.3 + 2.5 * math.sin(y * 0.37) * sx_)
                if not self.reduce and rnd() < 0.07:
                    i = int(rnd() * 97)
                ch[y][x] = cs[i % len(cs)]
                fg[y][x] = ent[0]

        def put(x, y, c, f):
            xi, yi = int(round(x)), int(round(y))
            if 0 <= xi < W and 0 <= yi < H:
                ch[yi][xi] = c
                fg[yi][xi] = qc(f)

        # night: stars, ascii moon, moonlight on the water
        if self.par["swell"] < 1.0 or self.state == "sleeping":
            dim = smooth((1.0 - self.par["swell"]) / 0.5)
            for sx, sy, ph in self.stars:
                if sy < int(surf[sx]) - 1 and math.sin(t * 1.7 + ph) > 0.2:
                    put(sx, sy, "." if math.sin(t + ph) < 0.8 else "*", mix((0, 0, 0), (200, 220, 255), dim))
            mx, my = int(W * 0.8), max(1, base // 4)
            for i, row in enumerate([" .-. ", "(   )", " '-' "]):
                for j, cc in enumerate(row):
                    if cc != " ":
                        put(mx - 2 + j, my - 1 + i, cc, mix((0, 0, 0), (240, 245, 255), dim))
            for x in range(max(0, mx - 3), min(W, mx + 4)):
                y = int(surf[x])
                if y < H and rnd() < 0.6 * dim:
                    put(x, y + random.randint(0, 3), random.choice("-~="), (220, 235, 255))

        # reef: rocks, sand, swaying weed + coral
        for (x, y), (cc, col) in self.reef_cells.items():
            if y < H and y > surf[x]:
                ch[y][x] = cc
                fg[y][x] = col
        for x, kind, h, ph in self.reef_items:
            top = self.reef_top[x]
            if kind == "weed":
                for i in range(h):
                    sway = int(round(math.sin(t * 1.4 + ph + i * 0.45) * 0.5 * i / 2))
                    if top - i <= surf[max(0, min(W - 1, x + sway))] + 1:
                        break
                    put(x + sway, top - i, "()"[(i + int(t * 2 + ph)) % 2],
                        mix((70, 200, 120), (40, 130, 90), i / max(1, h)))
                continue
            spr, col = {
                "coralA": (["\\ | /", " \\|/ ", "\\ | /", " \\|/", "  |"], (240, 110, 150)),
                "coralB": (["  ,@,", " (@@@)", "(@@@@@)"], (250, 150, 80)),
                "fan": ([" }{", "}{}{", " }{", " }{"], (180, 120, 235)),
                "shell": (["@"], (235, 220, 200)),
            }[kind]
            for i, row in enumerate(reversed(spr)):
                for j, cc in enumerate(row):
                    xx = max(0, min(W - 1, x + j - 1))
                    if cc != " " and top - i > surf[xx] + 1:
                        put(x + j - 1, top - i, cc, col)
        for x, y, life in self.bubbles:
            put(x, y, "o" if life > 1.5 else ".", (190, 230, 255))
        for fx, fy, vx, spr, col, ph in self.fish:
            yi = int(round(fy))
            xs = int(round(fx))
            for j in range(-1, len(spr) + 1):
                xx = xs + j
                if 0 <= xx < W and 0 <= yi < H and yi > surf[xx] + 1:
                    if 0 <= j < len(spr):
                        ch[yi][xx] = spr[j]
                        fg[yi][xx] = (*col, 1)
                    else:
                        ch[yi][xx] = " "

        # curling lips + whitewater
        for p, c, hb, fw, bw, theta, drop, foam in geoms:
            h0 = hb if hb > 0 else 1
            R = max(1.0, h0 * 0.32)
            if theta > 0.05:
                n = int(theta * R * 3) + 4
                for i in range(n):
                    th = theta * i / (n - 1)
                    lx = c + R * (2.0 * math.sin(th) + 0.8 * th / math.pi)
                    ly = base - (hb - R * (1 - math.cos(th))) + drop
                    xi = int(round(lx))
                    if not 0 <= xi < W or ly > surf[xi] + 0.5:
                        continue
                    dx = R * (2 * math.cos(th) + 0.8 / math.pi)
                    dy = R * math.sin(th)
                    if abs(dx) > 2 * abs(dy):
                        g = "~"
                    elif abs(dy) > 2 * abs(dx):
                        g = "|" if th < 2.5 else "("
                    else:
                        g = "\\" if dx * dy > 0 else "/"
                    tip = i / (n - 1)
                    put(lx, ly, g, mix(ICE, FOAM, tip))
                    ccx, ccy = c + R * 0.8 * th / math.pi, base - hb + R + drop
                    thick = max(1, int(R * 0.5 * (1 - tip * 0.6)))
                    for kk in range(1, thick + 1):
                        f = kk / max(1.0, R)
                        ix, iy = lx + (ccx - lx) * f, ly + (ccy - ly) * f
                        xi2 = int(round(ix))
                        if 0 <= xi2 < W and iy < surf[xi2]:
                            put(ix, iy, random.choice("%*=+~#"),
                                mix((160, 230, 250), (40, 130, 210), kk / thick))
            if foam > 0.02:
                x0 = int(c - R)
                x1 = int(c + R * 3 + (p - 0.66) * W * 0.25)
                for x in range(max(0, x0), min(W, x1)):
                    edge = 1 - abs((x - (x0 + x1) / 2) / max(1, (x1 - x0) / 2))
                    if rnd() < foam * (0.4 + 0.6 * edge):
                        y = int(surf[x])
                        for yy in range(y, min(H, y + 1 + int(foam * 2 * edge + rnd()))):
                            put(x, yy, random.choice("@o*%#:'"), FOAM)


        # ---- easter eggs
        egg = self.egg
        if egg is not None:
            kind = egg["kind"]
            if kind == "shark":
                xi = int(round(egg["x"]))
                sy = int(surf[max(0, min(W - 1, xi))])
                for dx_, dy_, c_ in ((1, -1, "/"), (2, -1, "|"), (0, 0, "/"), (1, 0, "_"), (2, 0, "|")):
                    put(xi + dx_, sy + dy_, c_, (150, 160, 178))
            elif kind == "gull":
                wing = "\\v/" if int(t * 5) % 2 else "-v-"
                for j, c_ in enumerate(wing):
                    put(egg["x"] + j, egg["y"], c_, (235, 240, 250))
            elif kind == "dolphin":
                ph = egg["ph"]
                xi = max(0, min(W - 1, int(egg["x"])))
                y = surf[xi] - 1 - math.sin(math.pi * ph) * max(2.0, self.hmax * 0.35)
                spr = "_/o>" if ph < 0.4 else "--o>" if ph < 0.6 else "\\_o>"
                for j, c_ in enumerate(spr):
                    put(egg["x"] - 2 + j, y, c_, (170, 205, 235))
            elif kind == "bottle":
                xi = max(0, min(W - 1, int(egg["x"])))
                y = surf[xi] - (1 if int(t * 2) % 2 else 0)
                for j, c_ in enumerate("[]=o"):
                    put(egg["x"] + j, y, c_, (140, 225, 170) if c_ != "o" else (235, 220, 190))
            elif kind == "ufo":
                ux, uy = egg["x"], egg["y"]
                for j, c_ in enumerate(" .-\"\"-. "):
                    if c_ != " ":
                        put(ux - 4 + j, uy, c_, (200, 230, 210))
                for j, c_ in enumerate("(_o_o_o_)"):
                    put(ux - 4 + j, uy + 1, c_, (200, 230, 210) if c_ != "o" else (255, 240, 140))
                if egg["age"] < 9:                          # tractor beam down to the sea
                    for k_ in range(2, int(self.base - uy) - 1):
                        if int(t * 6 + k_) % 3:
                            put(ux - 1 - k_ // 3, uy + k_, "/", (120, 220, 160))
                            put(ux + 1 + k_ // 3, uy + k_, "\\", (120, 220, 160))

        for x, y, vx, vy, life in self.spray:
            put(x, y, "*" if abs(vy) > 14 else "'" if vy < 0 else ".", mix(ICE, FOAM, min(1.0, life)))

        # the surfers (bold, coloured boards, cleared halo so they pop off the water)
        cells = {}
        WHT = (255, 255, 255, 1)
        for sf in self.surfers.values():
            if not sf.present or sf.sx is None:
                continue
            BRD = sf.board
            WHT = (*sf.body[:3], 1)
            if sf.wipe:
                w = sf.wipe
                age = t - w["t0"]
                bx, byy = int(round(w["bx"])), int(round(w["by"]))
                if w["air"]:
                    fr = int(t * 12 + sf.slot * 3) % 4
                    shape = [[(j - 3, 0, c) for j, c in enumerate("<=====>")],
                             [(-2, 2, "/"), (-1, 1, "/"), (0, 0, "/"), (1, -1, "/"), (2, -2, "/")],
                             [(0, -2, "^"), (0, -1, "|"), (0, 0, "|"), (0, 1, "|"), (0, 2, "v")],
                             [(-2, -2, "\\"), (-1, -1, "\\"), (0, 0, "\\"), (1, 1, "\\"), (2, 2, "\\")]][fr]
                else:
                    shape = [(j - 3, 0, c) for j, c in enumerate("<=====>")]
                for dx, dy, c in shape:
                    cells[(bx + dx, byy + dy)] = (c, BRD)
                px, py = int(round(w["px"])), int(round(w["py"]))
                if age < 0.7:   # tumbling in the whitewater
                    fr = int(t * 10) % 4
                    for j, c in enumerate(["\\O/", "-O-", "/O\\", "|O|"][fr]):
                        cells[(px - 1 + j, py - 2)] = (c, WHT)
                    for j, c in enumerate(["/ \\", " | ", "\\ /", "_|_"][fr]):
                        if c != " ":
                            cells[(px - 1 + j, py - 1)] = (c, WHT)
                elif age >= 1.8:  # popped up, swimming back to the board
                    cells[(px, py - 1)] = ("O", WHT)
                    cells[(px - 1 if int(t * 4) % 2 else px + 1, py - 1)] = ("/" if int(t * 4) % 2 else "\\", WHT)
                continue
            sx, by = int(round(sf.sx)), (sf.row if sf.row is not None else int(round(sf.sy)))
            if sf.duck:
                for j, cc in enumerate("<=====>"):
                    put(sx - 3 + j, by, cc, mix(sf.board[:3], (40, 120, 200), 0.45))
                for j, cc in enumerate("_O__"):
                    put(sx - 2 + j, by - 1, cc, (200, 225, 245))
                continue
            beat = int(t * 2.5 + sf.slot) % 2
            if sf.air:
                body = ["\\ O /", "  |  ", " / \\ "]          # stomping an air
            elif sf.mood == "needs_you":
                body = [["\\ O /", " \\|/ ", "  |  ", " / \\ "],
                        [" _O_ ", "/ | \\", "  |  ", " / \\ "]][int(t * 5) % 2]
            elif sf.riding:
                # crouched, knees bent - his default stance on a wave; the occasional arm-up flourish
                style = (t * 0.22 + sf.home * 3) % 1.0
                if sf.riding == "ride" and style < 0.15:
                    body = [["  O/ ", " /|  ", "  |  ", " / \\ "],
                            [" \\O  ", "  |\\ ", "  |  ", " / \\ "]][beat]
                else:
                    body = [["  _O ", " /(\\ ", " /  \\"], ["  _O ", " /(\\ ", "  / \\"]][int(t * 1.6) % 2]
            elif sf.mood == "sleeping":
                body = ["    z" if int(t) % 2 else "   z ", " ___O"]
            elif abs(sf.svx) > 1.5:  # lying down, paddling back out
                stroke = int(t * 3) % 2
                row = "/O____" if stroke else "_O__\\_"
                if sf.svx > 0:
                    row = row[::-1].translate(str.maketrans("/\\", "\\/"))
                body = [" " + row]
                sp = sx - 4 if (sf.svx > 0) else sx + 4   # kick splash off the tail
                for k in range(2):
                    put(sp + (k if sf.svx < 0 else -k), by - stroke, random.choice(".'*"), FOAM)
            elif sf.mood == "waiting":
                body = [["  O/ ", " /|  "], ["  O  ", " /|\\ "]][beat]   # sitting, waving
            else:
                body = ["  O  ", " /|\\ "]   # sitting on the board, legs in the water
            for j, cc in enumerate("<=====>"):
                cells[(sx - 3 + j, by)] = (cc, BRD)
            for i, row in enumerate(reversed(body)):
                for j, cc in enumerate(row):
                    if cc != " ":
                        cells[(sx - (3 if len(row) > 5 else 2) + j, by - 1 - i)] = (cc, WHT if cc != "z" else (*ICE, 1))
        if cells:
            for (x, y) in list(cells):
                for dx in (-1, 0, 1):
                    for dy in (-1, 0):
                        xx, yy = x + dx, y + dy
                        if (xx, yy) not in cells and 0 <= xx < W and 0 <= yy < H:
                            ch[yy][xx] = " "
            for (x, y), (cc, col) in cells.items():
                if 0 <= x < W and 0 <= y < H:
                    ch[y][x] = cc
                    fg[y][x] = tuple(int(v) for v in col)

        for q in self.qp:
            v = math.hypot(q[2], q[3])
            glyph = random.choice("@#") if v < 4 else "+" if v < 12 else "'" if v < 25 else "."
            top = q[5] / max(1.0, self.base)
            put(q[0], q[1], glyph, mix(FOAM, (90, 170, 245), top * 0.9 + (0.3 if math.sin(t * 6 + q[4]) > 0.7 else 0)))

        # one next-action banner on a clear backdrop + a short personality tag above each surfer
        present = sorted((o for o in self.surfers.values() if o.present and o.sx is not None),
                         key=lambda o: o.slot)
        self.positions = {}
        for o in present:                              # head position, also used as click target
            if o.wipe:
                hx, hy = o.wipe["px"], o.wipe["py"] - 2
            else:
                tall = 4 if (o.air or o.mood == "needs_you" or (o.riding == "ride" and not o.wipe)) else 3
                hy = (o.row if o.row is not None else o.sy) - tall
                if o.mood == "sleeping" and not o.riding:
                    hy = (o.row if o.row is not None else o.sy) - 2
                hx = o.sx
            self.positions[o.id] = (int(round(hx)), int(round(hy)))

        def write(x0, y, segs, pad=1):
            n = sum(len(tx) for tx, _ in segs)
            if not 0 <= y < H:
                return
            for x in range(max(0, x0 - pad), min(W, x0 + n + pad)):
                ch[y][x] = " "
            x = x0
            for tx, col in segs:
                for c_ in tx:
                    if 0 <= x < W:
                        ch[y][x] = c_
                        fg[y][x] = col
                    x += 1

        DIM = (150, 172, 205)
        taken = []                                     # (row, x0, x1) of tags already drawn
        if not self.compact or len(present) <= 2:
            for o in sorted(present, key=lambda o: -PRIO.get(o.mood, 0)):   # most urgent tag wins
                hx, hy = self.positions[o.id]
                ty = hy - 1
                if ty < 3:                              # never collide with the banner row
                    continue
                quip = personality(o)
                segs = [(o.name, o.board)] + ([(": " + quip, DIM)] if quip and not self.compact else [])
                n = sum(len(tx) for tx, _ in segs)
                x0 = max(1, min(W - n - 1, hx - n // 2))
                if any(r == ty and x0 - 2 < b1 and a0 < x0 + n + 2 for r, a0, b1 in taken):
                    segs = [(o.name, o.board)]             # crowded: just the name
                    n = len(o.name)
                    x0 = max(1, min(W - n - 1, hx - n // 2))
                    if any(r == ty and x0 - 2 < b1 and a0 < x0 + n + 2 for r, a0, b1 in taken):
                        continue                            # still overlapping: skip this tag
                write(x0, ty, segs)
                taken.append((ty, x0, x0 + n))

        for (x, y), (cc, col) in cells.items():       # surfers stay in front of their tags
            if 0 <= x < W and 0 <= y < H:
                ch[y][x] = cc
                fg[y][x] = tuple(int(v) for v in col)

        segs = next_action(present, label)
        n = sum(len(tx) for tx, _ in segs)
        if n > W - 4:                                  # trim the last segment to fit
            over = n - (W - 4)
            tx, col = segs[-1]
            segs[-1] = (tx[: max(0, len(tx) - over - 3)] + "...", col)
            n = sum(len(tx) for tx, _ in segs)
        write(max(1, (W - n) // 2), 1, segs, pad=2)

        if plain:
            return "\n".join("".join(r).rstrip() for r in ch)
        out = []
        for y in range(H):
            out.append(f"\033[{y + 1};1H")
            cur = None
            row_c, row_f = ch[y], fg[y]
            for x in range(W):
                f = row_f[x] if row_c[x] != " " else cur or BLACK
                if f != cur:
                    out.append(painter.esc(f))
                    cur = f
                out.append(row_c[x])
        out.append("\033[0m")
        return "".join(out)


# ================================================================== banner + tags

def personality(o):
    """A few words over a surfer's head."""
    if o.wipe:
        return "wiped out!"
    if o.air:
        return "woo!"
    if o.duck:
        return "duck diving"
    if o.riding == "tube":
        return "in the barrel"
    if o.riding:
        return "shredding"
    if o.mood == "needs_you":
        return "need you!"
    if o.mood == "waiting":
        return "your turn"
    if o.mood == "sleeping":
        return "zzz"
    if abs(o.svx) > 1.5:
        return "paddling out"
    return "waiting for a set" if o.mood == "working" else "chilling"


def next_action(present, fallback=""):
    """One line telling you what (if anything) to do next. [(text, rgb), ...]"""
    W_, D_ = (240, 246, 255), (150, 172, 205)
    need = [o for o in present if o.mood == "needs_you"]
    if need:
        o = need[0]
        msg = (o.label or "").strip()
        if msg.lower().startswith(o.name.lower()):
            msg = msg[len(o.name):].strip()
        if not msg.lower().startswith("needs"):
            msg = "needs you" + (": " + msg if msg else "")
        return [("> ", o.board[:3]), (o.name, o.board[:3]), (" " + msg, W_), ("  (click it for details)", D_)]
    wait = [o for o in present if o.mood == "waiting"]
    if wait:
        return [("> your turn: reply to ", W_), (wait[0].name, wait[0].board[:3])]
    done = [o for o in present if o.mood == "idle" and "done" in (o.label or "")]
    working = [o for o in present if o.mood == "working"]
    if done:
        return [("> ", W_), (done[0].name, done[0].board[:3]), (" finished - click it to review", W_)]
    if working:
        segs = [("nothing needed from you - ", D_)]
        shown = working[:3]
        for i, o in enumerate(shown):
            sep = "" if i == 0 else (" and " if i == len(shown) - 1 else ", ")
            segs += [(sep, D_), (o.name, o.board[:3])]
        return segs + [(" " + ("is" if len(working) == 1 else "are") + " working", D_)]
    if present and all(o.mood == "sleeping" for o in present):
        return [("everyone's asleep", D_)]
    if present:
        return [("all quiet at the peak", D_)]
    return [(fallback or "no agents in the water - connect one with `lineup report` or `lineup install`", D_)]


VENDOR_WORDS = {"claude", "gpt", "openai", "anthropic", "chatgpt", "codex", "models", "latest", "preview", "instruct",
                "chat", "high", "low", "medium", "xhigh", "mini", "thinking"}


def short_model(model):
    """'opus 5.5' -> 'opus', 'gpt 6.1 sol' -> 'sol', 'sonnet 5.5' -> 'sonnet', 'llama 4' -> 'llama'."""
    words = [w for w in re.split(r"[\s\-_/:.]+", (model or "").lower()) if w]
    for w in words:
        if w not in VENDOR_WORDS and re.search(r"[a-z]", w) and not re.fullmatch(r"v?\d+[a-z]?", w):
            return w
    return re.sub(r"\s+", "", model or "")     # e.g. 'gpt 5' -> 'gpt5'


def name_by_model(agents, usage):
    """Rename surfers after their model; repeats become 'opus', 'opus 2', ... Agents without a known
    model keep their agent name."""
    counts = {}
    for a in agents:
        u = usage.get(a["id"]) or {}
        m = short_model(u.get("model", ""))
        base = m or a.get("agent_name", a["name"])
        counts[base] = counts.get(base, 0) + 1
        a["name"] = base if counts[base] == 1 else f"{base} {counts[base]}"
    return agents


def pretty_title(path):
    """'~/code/2026-01-31-surf-shop-9f8e7d6c' -> 'surf shop'."""
    base = os.path.basename((path or "").rstrip("/"))
    t = re.sub(r"^\d{4}-\d{2}-\d{2}[-_ T]?", "", base)
    t = re.sub(r"[-_][0-9a-f]{6,}$", "", t)
    t = re.sub(r"[-_]+", " ", t).strip()
    return t or base or "-"


# ================================================================== agents

LINEUP_HOME = os.path.expanduser(os.environ.get("LINEUP_HOME", "~/.lineup"))
AGENTS_DIR = os.path.join(LINEUP_HOME, "agents")
CONFIG_FILE = os.path.join(LINEUP_HOME, "config.json")
PRIO = {"needs_you": 4, "working": 3, "waiting": 2, "idle": 1, "sleeping": 0}
STATES = ("working", "needs_you", "waiting", "idle", "sleeping", "off")
PALETTE = ["#ff8c14", "#2878ff", "#2ecc71", "#ff4fa0", "#f5c518", "#a066ff", "#19c3b1", "#ff5a4f"]
DEFAULT_CONFIG = {
    "agents": {
        "claude": {"name": "claude", "board": "#ff8c14"},
        "codex": {"name": "codex", "board": "#2878ff"},
    },
    "detect_codex": True,
    "bell": True,
    "idle_to_sleep_min": 15,
    "forget_after_hours": 6,
    "reduce_motion": False,
    "split_sessions": True,     # one surfer per session (claude, claude 2, ...) with different body colours
    "name_by": "agent",         # "agent" (claude, codex) or "model" (opus, sonnet, sol, ...)
}
BODY_TINTS = [(255, 255, 255), (255, 205, 160), (160, 240, 200), (205, 185, 255), (255, 238, 140), (150, 225, 255)]
MAX_SESSIONS = 4


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_FILE) as f:
            user = json.load(f)
        for k, v in user.items():
            if k == "agents" and isinstance(v, dict):
                for aid, a in v.items():
                    cfg["agents"].setdefault(aid, {}).update(a or {})
            else:
                cfg[k] = v
    except (OSError, ValueError):
        pass
    return cfg


def hex_rgb(h, fallback=(255, 140, 20)):
    try:
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return fallback


def _safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(name)).strip("-")[:64] or "agent"


class _Lock:
    def __enter__(self):
        os.makedirs(LINEUP_HOME, exist_ok=True)
        self.f = open(os.path.join(LINEUP_HOME, ".lock"), "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def report(agent, state, label=None, session="default", **extra):
    """Record what an agent is doing. state='off' removes it. label=None keeps the previous label."""
    if state not in STATES:
        raise ValueError(f"state must be one of {', '.join(STATES)}")
    os.makedirs(AGENTS_DIR, exist_ok=True)
    path = os.path.join(AGENTS_DIR, f"{_safe(agent)}@{_safe(session)}.json")
    now = time.time()
    with _Lock():
        if state == "off":
            try:
                os.remove(path)
            except OSError:
                pass
            return
        try:
            with open(path) as f:
                old = json.load(f)
        except (OSError, ValueError):
            old = {}
        rec = dict(old)
        rec.update(agent=str(agent), session=str(session), state=state, ts=now,
                   since=old.get("since", now) if old.get("state") == state else now,
                   label=(("back at it" if old.get("state") == "needs_you" else old.get("label", ""))
                          if label is None else str(label)))
        add_file = extra.pop("add_file", None)
        for k, v in extra.items():
            if v is not None:
                rec[k] = v
        if add_file:
            files = [f for f in rec.get("files", []) if f != add_file] + [add_file]
            rec["files"] = files[-12:]
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f)
        os.replace(tmp, path)


def read_reports(now, forget_after):
    recs = []
    try:
        names = os.listdir(AGENTS_DIR)
    except OSError:
        return recs
    for fn in names:
        if not fn.endswith(".json"):
            continue
        p = os.path.join(AGENTS_DIR, fn)
        try:
            with open(p) as f:
                r = json.load(f)
        except (OSError, ValueError):
            continue
        if now - r.get("ts", 0) > r.get("ttl", forget_after):
            try:
                os.remove(p)      # stale: the agent went away without saying goodbye
            except OSError:
                pass
            continue
        recs.append(r)
    return recs


# ------------------------------------------------------------------ Claude Code (hooks)

TOOL_VERBS = {
    "Read": "reading", "Edit": "editing", "MultiEdit": "editing", "Write": "writing",
    "NotebookEdit": "editing notebook", "Bash": "running", "Grep": "searching",
    "Glob": "looking for files", "WebFetch": "browsing", "WebSearch": "searching the web",
    "Agent": "delegating", "Task": "delegating", "TodoWrite": "planning",
    "Skill": "loading a skill", "ToolSearch": "finding tools",
}
HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Notification", "Stop", "SessionEnd"]


def _tool_label(name, inp):
    inp = inp or {}
    verb = TOOL_VERBS.get(name) or "using " + (name.split("__")[-1] if name.startswith("mcp__") else name)
    if name == "Bash" and inp.get("description"):
        return str(inp["description"])[:60]
    for key in ("file_path", "command", "pattern", "query", "url", "description"):
        if inp.get(key):
            v = str(inp[key])
            return f"{verb} {os.path.basename(v) if key == 'file_path' else v.splitlines()[0]}"[:60]
    return verb


def claude_hook(data):
    """Map one Claude Code hook event to a report. Prints nothing; never raises."""
    event = data.get("hook_event_name", "")
    sid = data.get("session_id") or "default"
    extra = dict(cwd=data.get("cwd"), transcript=data.get("transcript_path"))
    if event == "SessionEnd":
        report("claude", "off", session=sid)
    elif event == "SessionStart":
        report("claude", "idle", "hello!", session=sid, **extra)
    elif event == "UserPromptSubmit":
        report("claude", "working", "thinking...", session=sid, **extra)
    elif event == "PreToolUse":
        tool, inp = data.get("tool_name", ""), data.get("tool_input") or {}
        if tool in ("Edit", "MultiEdit", "Write", "NotebookEdit") and inp.get("file_path"):
            extra["add_file"] = str(inp["file_path"])
        report("claude", "working", _tool_label(tool, inp), session=sid, **extra)
    elif event == "PostToolUse":
        report("claude", "working", None, session=sid, **extra)   # also clears needs_you after an approval
    elif event == "Stop":
        report("claude", "idle", "done - your turn", session=sid, **extra)
    elif event == "Notification":
        msg, kind = str(data.get("message") or ""), str(data.get("notification_type") or "")
        if "permission" in kind or "permission" in msg.lower() or "approv" in msg.lower():
            report("claude", "needs_you", msg or "needs your permission", session=sid, **extra)
        else:
            report("claude", "waiting", msg or "waiting for you", session=sid, **extra)


def _claude_settings_path():
    return os.path.join(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")), "settings.json")


def _hook_command():
    return f'"{sys.executable}" "{os.path.abspath(__file__)}" hook claude-code'


def _is_ours(cmd):
    return "lineup.py" in cmd and "hook claude-code" in cmd


def install_claude_code(uninstall=False):
    path = _claude_settings_path()
    try:
        with open(path) as f:
            settings = json.load(f)
    except FileNotFoundError:
        settings = {}
    except ValueError:
        print(f"! {path} isn't valid JSON - not touching it.")
        return 1
    if os.path.exists(path):
        backup = f"{path}.lineup-backup-{time.strftime('%Y%m%d-%H%M%S')}"
        with open(backup, "w") as f:
            json.dump(settings, f, indent=2)
    hooks = settings.setdefault("hooks", {})
    for ev in HOOK_EVENTS:      # drop any earlier Lineup entries (also makes install idempotent)
        groups = []
        for g in hooks.get(ev, []):
            hs = [h for h in g.get("hooks", []) if not _is_ours(h.get("command", ""))]
            if hs:
                groups.append(dict(g, hooks=hs))
        if groups:
            hooks[ev] = groups
        else:
            hooks.pop(ev, None)
    if not uninstall:
        for ev in HOOK_EVENTS:
            g = {"hooks": [{"type": "command", "command": _hook_command(), "timeout": 5}]}
            if ev in ("PreToolUse", "PostToolUse"):
                g["matcher"] = "*"
            hooks.setdefault(ev, []).append(g)
    if not hooks:
        settings.pop("hooks", None)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, path)
    print(("removed Lineup hooks from " if uninstall else "added Lineup hooks to ") + path)
    if not uninstall:
        print("new Claude Code sessions will show up in the lineup (restart running ones).")
    return 0


# ------------------------------------------------------------------ Codex (auto-detected)

CODEX_DIR = os.path.expanduser("~/.codex/sessions")
CODEX_EVENTS = (b'"task_started"', b'"task_complete"', b'"turn_aborted"')


def codex_running():
    """True if a Codex agent process is alive (CLI, desktop app or `codex app-server`)."""
    try:
        out = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return False
    for line in out.splitlines():
        path = line.strip()
        name = os.path.basename(path).lower()
        if name.startswith("codex") and "/.codex/" not in path and "chrome" not in path.lower():
            return True
    return False


def _codex_files(now):
    files = []
    for back in (0, 1):
        day = os.path.join(CODEX_DIR, time.strftime("%Y/%m/%d", time.localtime(now - back * 86400)))
        try:
            files += [os.path.join(day, f) for f in os.listdir(day) if f.endswith(".jsonl")]
        except OSError:
            pass
    return files


_CODEX_STATE = {}   # log path -> {"off": bytes read, "last": last task event seen}


def _codex_last_event(path):
    """Last task_started / task_complete / turn_aborted in a Codex log, read incrementally.
    A long task can write megabytes between its start and end, so we never rely on a fixed tail."""
    st = _CODEX_STATE.setdefault(path, {"off": 0, "last": None})
    try:
        size = os.path.getsize(path)
        if size < st["off"]:                      # rotated / rewritten
            st.update(off=0, last=None)
        if size > st["off"]:
            with open(path, "rb") as fh:
                start = st["off"]
                if start == 0 and size > 16_000_000:
                    start = size - 16_000_000       # first look at a huge log: the last 16 MB is plenty
                fh.seek(max(0, start - 32))         # small overlap: an event name split across reads
                chunk = fh.read()
            st["off"] = size
            found = max(((chunk.rfind(e), e) for e in CODEX_EVENTS), key=lambda x: x[0])
            if found[0] >= 0:
                st["last"] = found[1]
    except OSError:
        pass
    return st["last"]


def read_codex(now):
    """(mood, label, [working session files]). A session is working if its last task event is a
    start - or, failing that, if the log is being written right now and hasn't said it finished."""
    working = []
    for f in _codex_files(now):
        try:
            age = now - os.path.getmtime(f)
        except OSError:
            continue
        if age > 20 * 60:
            continue
        last = _codex_last_event(f)
        if last == b'"task_started"' or (last is None and age < 90):
            working.append(f)
    working.sort(key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0, reverse=True)
    if working:
        return "working", "on it", working
    return "idle", "chilling at the peak", []


def codex_usage(now, path=None):
    if path is None:
        files = [(os.path.getmtime(f), f) for f in _codex_files(now) if os.path.exists(f)]
        if not files:
            return None
        path = max(files)[1]
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - 400_000))
            tail = fh.read().split(b"\n")
    except OSError:
        return None
    tc = ctxl = None
    for ln in reversed(tail):
        try:
            if tc is None and b'"token_count"' in ln:
                tc = json.loads(ln).get("payload")
            elif ctxl is None and b'"turn_context"' in ln:
                ctxl = json.loads(ln).get("payload")
        except ValueError:
            pass
        if tc and ctxl:
            break
    model = (ctxl or {}).get("model") or ""
    if not model:
        try:
            m = re.search(r'^model\s*=\s*"([^"]+)"', open(os.path.expanduser("~/.codex/config.toml")).read(), re.M)
            model = m.group(1) if m else "codex"
        except OSError:
            model = "codex"
    info = (tc or {}).get("info") or {}
    rl = (tc or {}).get("rate_limits") or {}
    lim = [f"{n} {(rl.get(k) or {}).get('used_percent'):.0f}%" for k, n in (("primary", "5h"), ("secondary", "wk"))
           if isinstance(rl.get(k), dict) and rl[k].get("used_percent") is not None]
    if rl.get("plan_type"):
        lim.append(str(rl["plan_type"]))
    return dict(model=pretty_model(model), effort=(ctxl or {}).get("effort") or "",
                ctx=(info.get("last_token_usage") or {}).get("total_tokens", 0),
                window=info.get("model_context_window") or 0,
                total=(info.get("total_token_usage") or {}).get("total_tokens", 0), total_label="total",
                limits="  ".join(lim))


def codex_last_text(now, path=None):
    if path is None:
        files = [(os.path.getmtime(f), f) for f in _codex_files(now) if os.path.exists(f)]
        if not files:
            return ""
        path = max(files)[1]
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - 400_000))
            tail = fh.read().split(b"\n")
    except OSError:
        return ""
    for ln in reversed(tail):
        if b'"assistant"' not in ln:
            continue
        try:
            p = json.loads(ln).get("payload") or {}
        except ValueError:
            continue
        if p.get("type") == "message" and p.get("role") == "assistant":
            texts = [c.get("text", "") for c in p.get("content", []) if isinstance(c, dict) and c.get("text")]
            if texts and not texts[-1].lstrip().startswith("{"):
                return texts[-1].strip()
    return ""


# ------------------------------------------------------------------ usage

_TRANSCRIPTS = {}


def pretty_model(m):
    m = (m or "").replace("claude-", "")
    m = re.sub(r"-(\d{8})$", "", m)
    return re.sub(r"(\d+)-(\d+)", r"\1.\2", m).replace("-", " ")


def fmt_tok(n):
    n = int(n or 0)
    return f"{n / 1e6:.1f}M" if n >= 1_000_000 else f"{n / 1e3:.0f}k" if n >= 1000 else str(n)


def claude_usage(path):
    """Model, current context and session output tokens, parsed incrementally from the transcript."""
    if not path or not os.path.exists(path):
        return None
    st = _TRANSCRIPTS.setdefault(path, dict(off=0, part=b"", model="", ctx=0, peak=0, out=0, text=""))
    try:
        size = os.path.getsize(path)
        if size < st["off"]:
            st.update(off=0, part=b"", out=0, peak=0)
        if size > st["off"]:
            with open(path, "rb") as f:
                f.seek(st["off"])
                data = st["part"] + f.read()
                st["off"] = f.tell()
            lines = data.split(b"\n")
            st["part"] = lines.pop()
            for ln in lines:
                if b'"assistant"' not in ln:
                    continue
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                m = d.get("message") or {}
                u = m.get("usage") or {}
                if d.get("type") != "assistant":
                    continue
                texts = [c.get("text", "") for c in (m.get("content") or []) if isinstance(c, dict) and c.get("type") == "text"]
                if texts and texts[-1].strip():
                    st["text"] = texts[-1].strip()
                if not u:
                    continue
                ctx = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                       + u.get("cache_creation_input_tokens", 0) + u.get("output_tokens", 0))
                st["ctx"], st["peak"] = ctx, max(st["peak"], ctx)
                st["out"] += u.get("output_tokens", 0)
                if m.get("model") and not str(m["model"]).startswith("<"):
                    st["model"] = m["model"]
    except OSError:
        return None
    if not st["model"]:
        return None
    window = 1_000_000 if ("1m" in st["model"].lower() or st["peak"] > 200_000) else 200_000
    return dict(model=pretty_model(st["model"]), effort="", ctx=st["ctx"], window=window,
                total=st["out"], total_label="out", limits="")


def report_usage(rec):
    """Usage an agent reported itself via `lineup report --model/--ctx/--window/--tokens`."""
    if not any(rec.get(k) for k in ("model", "ctx", "tokens")):
        return None
    return dict(model=str(rec.get("model") or ""), effort="", ctx=int(rec.get("ctx") or 0),
                window=int(rec.get("window") or 0), total=int(rec.get("tokens") or 0), total_label="tok",
                limits=str(rec.get("limits") or ""))


def hud_lines(agents, usage, W):
    """Info panel rows: [(text, rgb, bold)...] per agent; long form when wide, short when narrow."""
    rows = []
    for ag in agents:
        u = usage.get(ag["id"])
        if not u:
            continue
        col, name = ag["board"], ag["name"]
        pct = (u["ctx"] / u["window"]) if u["window"] else 0.0
        model = u["model"] + (f" {u['effort']}" if u["effort"] else "")
        barc = (120, 200, 250) if pct < 0.6 else (250, 220, 120) if pct < 0.85 else (255, 110, 90)
        dim = (160, 190, 230)
        if W >= 96:
            n = 16
            fill = int(round(min(1.0, pct) * n))
            segs = [(f" {name[:10]:<11}", col, True), (f"{model:<20.20}", (235, 242, 255), False)]
            if u["window"]:
                segs += [(" ctx [", (110, 150, 200), False), ("#" * fill, barc, True), ("-" * (n - fill), (50, 70, 110), False),
                         (f"] {pct:4.0%} ", barc, False), (f"{fmt_tok(u['ctx'])}/{fmt_tok(u['window'])}", dim, False)]
            elif u["ctx"]:
                segs.append((f" ctx {fmt_tok(u['ctx'])}", dim, False))
            if u["total"]:
                segs.append((f"   {u['total_label']} {fmt_tok(u['total'])}", dim, False))
            if u["limits"]:
                segs.append((f"   {u['limits']}", dim, False))
        else:
            n = 8
            fill = int(round(min(1.0, pct) * n))
            segs = [(f" {name[:6]} ", col, True), (f"{model[:14]} ", (235, 242, 255), False)]
            if u["window"]:
                segs += [("[", (110, 150, 200), False), ("#" * fill, barc, True), ("-" * (n - fill), (50, 70, 110), False),
                         (f"]{pct:4.0%} ", barc, False)]
            if u["total"]:
                segs.append((f"{u['total_label'][0]}:{fmt_tok(u['total'])}", dim, False))
        rows.append(segs)
    return rows


def render_hud(rows, top, W, painter):
    out = []
    for i, segs in enumerate(rows):
        out.append(f"\033[{top + i};1H")
        used = 0
        for text, rgb, bold in segs:
            text = text[: max(0, W - used)]
            if not text:
                break
            out.append(painter.esc((*rgb, 1) if bold else rgb) + text)
            used += len(text)
        out.append(painter.esc((0, 0, 0)) + " " * max(0, W - used))
    return "".join(out)


# ------------------------------------------------------------------ who's in the water

DEMO = [   # (seconds per scene = 9) [(id, mood, label), ...]
    [("claude", "idle", "done - your turn"), ("codex", "idle", "chilling at the peak")],
    [("claude", "working", "editing lineup.py"), ("codex", "working", "running the tests")],
    [("claude", "working", "refactoring the reef"), ("codex", "working", "on it"), ("my-agent", "working", "indexing docs")],
    [("claude", "needs_you", "needs permission to run Bash"), ("codex", "working", "on it")],
    [("claude", "working", "fixing the login bug"), ("claude#2", "working", "writing the docs"), ("codex", "idle", "chilling at the peak")],
    [("claude", "waiting", "waiting for your input"), ("my-agent", "idle", "")],
    [("claude", "sleeping", "drifted off waiting for you")],
]
DEMO_USAGE = {
    "claude": dict(model="opus 5.5", effort="", ctx=318_000, window=1_000_000, total=812_000, total_label="out", limits=""),
    "codex": dict(model="gpt 6.1 sol", effort="high", ctx=156_000, window=258_400, total=4_242_619, total_label="total", limits="plus"),
    "my-agent": dict(model="llama 4", effort="", ctx=41_000, window=128_000, total=96_000, total_label="tok", limits=""),
    "claude#2": dict(model="sonnet 5.5", effort="", ctx=64_000, window=1_000_000, total=120_000, total_label="out", limits=""),
}


class Agents:
    """Collects every agent: reports (hooks + `lineup report`), Codex auto-detection, or the demo."""

    def __init__(self, demo=False):
        self.demo, self.t0 = demo, time.time()
        if demo:   # LINEUP_DEMO_SCENE=2 starts the demo at a given scene
            self.t0 -= float(os.environ.get("LINEUP_DEMO_SCENE", 0) or 0) * 9
        self.cfg = load_config()
        self.order = list(self.cfg["agents"])          # configured agents keep their lineup spots
        self.poll_t = self.run_t = self.cx_t = self.use_t = 0.0
        self.cached, self.codex_on, self.codex_mood = [], False, ("idle", "", [])
        self.usage, self.recs = {}, {}
        self.seen = {}        # agent -> session keys in first-seen order (stable body colours)
        self.name_by = self.cfg.get("name_by", "agent")

    def _style(self, aid):
        """(name, board) for an agent id; 'claude#<session>' styles like 'claude'."""
        base = aid.split("#", 1)[0]
        a = self.cfg["agents"].get(base, {})
        for k in (base, aid):
            if k not in self.order:
                self.order.append(k)
        board = a.get("board") or PALETTE[self.order.index(base) % len(PALETTE)]
        return a.get("name") or base, hex_rgb(board)

    def _names(self, agents, usage):
        for a in agents:
            a.setdefault("agent_name", a["name"])
        if self.name_by == "model":
            return name_by_model(agents, usage)
        for a in agents:
            a["name"] = a["agent_name"]
        return agents

    def _session_look(self, base, key, shown_keys):
        """Body colour fixed per session; name numbered among the sessions on screen."""
        seen = self.seen.setdefault(base, [])
        for k in list(shown_keys) + [key]:
            if k is not None and k not in seen:
                seen.append(k)
        body = BODY_TINTS[seen.index(key) % len(BODY_TINTS)]
        rank = sorted(shown_keys, key=seen.index).index(key) if len(shown_keys) > 1 else 0
        return body, rank

    def poll(self, now):
        if self.demo:
            scene = DEMO[int((now - self.t0) / 9) % len(DEMO)]
            since = self.t0 + int((now - self.t0) / 9) * 9
            out = []
            for aid, mood, label in scene:
                name, board = self._style(aid)
                n = int(aid.split("#")[1]) if "#" in aid else 1
                out.append(dict(id=aid, name=name if n == 1 else f"{name} {n}", board=board, mood=mood, label=label,
                                since=since, cwd="demo", sessions=1, body=BODY_TINTS[(n - 1) % len(BODY_TINTS)]))
            out = sorted(out, key=lambda a: (self.order.index(a["id"].split("#", 1)[0]), self.order.index(a["id"])))
            return self._names(out, {a["id"]: DEMO_USAGE.get(a["id"], {}) for a in out})
        if now - self.poll_t < 0.25:
            return self.cached
        self.poll_t = now
        cfg = self.cfg
        forget = cfg.get("forget_after_hours", 6) * 3600
        split = cfg.get("split_sessions", True)
        groups = {}
        for r in read_reports(now, forget):
            groups.setdefault(r.get("agent", "agent"), []).append(r)
        agents, recs = [], {}
        sleep_after = cfg.get("idle_to_sleep_min", 15) * 60
        for base, rs in groups.items():
            looked = []
            for r in rs:
                mood, label, age = r.get("state", "idle"), r.get("label", ""), now - r.get("ts", now)
                if mood in ("idle", "waiting") and age > sleep_after:
                    mood, label = "sleeping", "drifted off waiting for you"
                elif mood == "working" and age > 30 * 60:
                    mood, label = "sleeping", "lost track (no updates for 30m)"
                looked.append((mood, label, r))
            looked.sort(key=lambda x: (PRIO.get(x[0], 0), x[2].get("ts", 0)), reverse=True)
            if split:   # every awake session surfs; if they're all asleep, just the latest one naps
                chosen = [x for x in looked if x[0] != "sleeping"][:MAX_SESSIONS] or \
                         [max(looked, key=lambda x: x[2].get("ts", 0))]
            else:
                chosen = looked[:1]
            keys = [str(x[2].get("session", "default")) for x in chosen]
            name, board = self._style(base)
            for (mood, label, r), key in zip(chosen, keys):
                aid = f"{base}#{key}" if split else base
                body, rank = self._session_look(base, key, keys) if split else ((255, 255, 255), 0)
                agents.append(dict(id=aid, name=name if rank == 0 else f"{name} {rank + 1}", board=board,
                                   body=body, mood=mood, label=label, since=r.get("since", now),
                                   cwd=r.get("cwd") or "", sessions=len(rs)))
                recs[aid] = r
        self.recs = recs
        if cfg.get("detect_codex", True) and "codex" not in groups:
            if now - self.run_t > 3.0:
                self.codex_on, self.run_t = codex_running(), now
            if self.codex_on:
                if now - self.cx_t > 1.0:
                    self.codex_mood, self.cx_t = read_codex(now), now
                mood, label, files = self.codex_mood
                name, board = self._style("codex")
                keys = [os.path.basename(f)[-14:-6] for f in files][:MAX_SESSIONS] if split else []
                # the main codex surfer always stays; extra *working* sessions paddle out next to him
                main_key = keys[0] if keys else None
                agents.append(dict(id="codex", name=name, board=board, mood=mood, label=label, since=now, cwd="",
                                   sessions=max(1, len(keys)), auto="codex",
                                   codex_file=files[0] if files else None,
                                   body=self._session_look("codex", main_key, keys)[0] if main_key else BODY_TINTS[0]))
                for i, key in enumerate(keys[1:], start=1):
                    body, _ = self._session_look("codex", key, keys)
                    agents.append(dict(id=f"codex#{key}", name=f"{name} {i + 1}", board=board, body=body,
                                       mood="working", label="on it", since=now, cwd="", sessions=len(keys),
                                       auto="codex", codex_file=files[i]))
        for a in agents:
            self._style(a["id"])
        agents.sort(key=lambda a: (self.order.index(a["id"].split("#", 1)[0]), self.order.index(a["id"])))
        self.cached = self._names(agents, self.usage)
        return agents

    def get_usage(self, now, agents):
        if self.demo:
            return {a["id"]: DEMO_USAGE[a["id"]] for a in agents if a["id"] in DEMO_USAGE}
        if now - self.use_t > 2.0:
            self.use_t = now
            usage = {}
            for a in agents:
                r = self.recs.get(a["id"], {})
                u = None
                if r.get("transcript"):
                    u = claude_usage(r["transcript"])
                if u is None and r:
                    u = report_usage(r)
                if u is None and a.get("auto") == "codex":
                    u = codex_usage(now, a.get("codex_file"))
                if u:
                    usage[a["id"]] = u
            self.usage = usage
            self._names(agents, usage)
        return self.usage


def agent_detail(ag, a, now):
    """Everything the click card shows for one agent."""
    r = ag.recs.get(a["id"], {}) if not ag.demo else {}
    result = ""
    if r.get("transcript"):
        claude_usage(r["transcript"])
        result = _TRANSCRIPTS.get(r["transcript"], {}).get("text", "")
    elif a.get("auto") == "codex":
        result = codex_last_text(now, a.get("codex_file"))
    elif r.get("detail"):
        result = str(r["detail"])
    if ag.demo:
        result = {"claude": "Refactored the reef renderer and added tests.\n\n3 files changed, all tests pass.",
                  "codex": "Running the test suite: 214 passed, 2 skipped."}.get(a["id"], "Indexed 1,204 docs.")
    question = a["label"] if a["mood"] in ("needs_you", "waiting") else ""
    cwd = a.get("cwd") or r.get("cwd") or ""
    return dict(id=a["id"], name=a["name"], board="#%02x%02x%02x" % tuple(a["board"][:3]), mood=a["mood"],
                label=a["label"], question=question, result=result[-1500:],
                files=list(r.get("files", []))[-8:] if not ag.demo else ["reef.py", "sea.py", "tests/test_reef.py"],
                cwd=cwd, title=pretty_title(cwd), since=int(now - a.get("since", now)))


def overall(agents):
    return max((a["mood"] for a in agents), key=lambda m: PRIO.get(m, 0), default="sleeping")


def status_bar(agents, now, extra):
    if not agents:
        return f" ~ NO AGENTS IN THE WATER  |  {extra} "
    urgent = max(agents, key=lambda a: PRIO.get(a["mood"], 0))
    moods = "  ".join(f"{a['name'].upper()} {a['mood'].replace('_', ' ')}" for a in agents[:4])
    proj = pretty_title(urgent.get("cwd") or "")
    return f" ~ {moods}  |  {proj}  |  for {fmt_age(now - urgent.get('since', now))}  |  {extra} "


def fmt_age(sec):
    sec = int(max(0, sec))
    return f"{sec // 60}m{sec % 60:02d}s" if sec >= 60 else f"{sec}s"


# ------------------------------------------------------------------ front ends

def _frame(sea, agents, ag, now, W, H, painter, opt, extra, dt):
    """One full frame: sea + info panel + status bar. Returns (ansi, rang_bell, sea_rows)."""
    hud = hud_lines(agents, ag.get_usage(now, agents), W) if opt["info"] and not opt["compact"] else []
    if H - 1 - len(hud) < 10:
        hud = []
    rows = H - 1 - len(hud)
    prev = sea.state
    sea.reduce = opt["reduce"]
    sea.sync_agents(agents)
    ring = sea.state != prev and sea.state == "needs_you" and opt["bell"]
    if W < 30 or rows < 8:
        return "\033[1;1H" + painter.esc((90, 160, 230)) + "make me a bit bigger :)", ring, rows
    surf, geoms = sea.step(dt, W, rows)
    buf = [sea.draw(W, rows, surf, geoms, "", painter), render_hud(hud, rows + 1, W, painter)]
    buf.append(f"\033[{H};1H{painter.esc((90, 160, 230))}{status_bar(agents, now, extra)[:W].ljust(W)}")
    return "".join(buf), ring, rows


def _keys_hint(opt, app=False):
    k = ["i info", f"n names: {opt.get('names', 'agent')}", f"m motion {'low' if opt['reduce'] else 'full'}",
         f"b bell {'on' if opt['bell'] else 'off'}"]
    if app:
        k += ["c compact", "[ ] text size", f"t pin {'on' if opt['pinned'] else 'off'}", "click a surfer"]
    return "  |  ".join(k + ["q quit"])


def run(demo=False, truecolor=None, reduce=False):
    if truecolor is None:
        truecolor = (os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
                     or os.environ.get("TERM_PROGRAM") in ("iTerm.app", "WezTerm", "ghostty", "vscode"))
    painter = Painter(truecolor)
    out = sys.stdout
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd) if os.isatty(fd) else None
    resized = [True]
    signal.signal(signal.SIGWINCH, lambda *_: resized.__setitem__(0, True))
    ag = Agents(demo)
    opt = dict(info=True, bell=ag.cfg.get("bell", True), reduce=reduce or ag.cfg.get("reduce_motion", False),
               compact=False, pinned=False, names=ag.cfg.get("name_by", "agent"))
    sea = Sea("sleeping")
    last = time.time()
    try:
        if old:
            tty.setcbreak(fd)
        out.write("\033[?1049h\033[?25l")
        while True:
            now = time.time()
            dt, last = min(0.15, now - last), now
            agents = ag.poll(now)
            tw, th = shutil.get_terminal_size((80, 24))
            buf = []
            if resized[0]:
                buf.append("\033[0m" + painter.esc((0, 0, 0)) + "\033[2J")
                resized[0] = False
            frame, ring, _ = _frame(sea, agents, ag, now, tw, th, painter, opt, _keys_hint(opt), dt)
            if ring:
                buf.append("\a")
            buf.append(frame + "\033[0m")
            out.write("".join(buf))
            out.flush()
            wait = max(0.0, 1 / (8 if opt["reduce"] else FPS) - (time.time() - now))
            if old:
                r, _, _ = select.select([sys.stdin], [], [], wait)
                if r:
                    k = os.read(fd, 1).lower()
                    if k in (b"q", b"\x1b"):
                        break
                    if k == b"b":
                        opt["bell"] = not opt["bell"]
                    if k == b"n":
                        ag.name_by = "agent" if ag.name_by == "model" else "model"
                        opt["names"], ag.poll_t = ag.name_by, 0.0
                    if k == b"m":
                        opt["reduce"] = not opt["reduce"]
                    if k == b"i":
                        opt["info"], resized[0] = not opt["info"], True
            else:
                time.sleep(wait)
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\033[0m\033[2J\033[?25h\033[?1049l")
        out.flush()
        if old:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def run_stream(demo=False):
    """Frame feed for the macOS app.
    stdin lines:  size COLS ROWS | bell | info | pin on|off | shake DX DY | motion reduced|full
                  | compact on|off | detail AGENT_ID                              (EOF = quit)
    stdout:       per frame: [\x1d META_JSON \x1e] ANSI_FRAME \f   (BEL inside = ring the bell)
                  META = {"agents":[{id,name,x,y}], "rows", "status_row", "path", "title", "detail"?}"""
    painter = Painter(True)
    fd = sys.stdin.fileno()
    os.set_blocking(fd, False)
    inbuf = b""
    W, H = 100, 30
    ag = Agents(demo)
    opt = dict(info=True, bell=ag.cfg.get("bell", True), reduce=ag.cfg.get("reduce_motion", False),
               compact=False, pinned=False, names=ag.cfg.get("name_by", "agent"))
    sea = Sea("sleeping")
    last = time.time()
    want_detail = None
    while True:
        try:
            data = os.read(fd, 4096)
            if data == b"":
                return
        except BlockingIOError:
            data = b""
        inbuf += data
        while b"\n" in inbuf:
            line, inbuf = inbuf.split(b"\n", 1)
            p = line.decode(errors="ignore").split()
            try:
                if p[:1] == ["size"] and len(p) == 3:
                    W, H = max(10, int(p[1])), max(5, int(p[2]))
                elif p[:1] == ["bell"]:
                    opt["bell"] = not opt["bell"]
                elif p[:1] == ["info"]:
                    opt["info"] = not opt["info"]
                elif p[:1] == ["pin"]:
                    opt["pinned"] = p[1:] == ["on"]
                elif p[:1] == ["motion"]:
                    opt["reduce"] = p[1:] == ["reduced"]
                elif p[:1] == ["names"] and p[1:] in (["model"], ["agent"]):
                    ag.name_by = opt["names"] = p[1]
                    ag.poll_t = 0.0
                elif p[:1] == ["compact"]:
                    opt["compact"] = p[1:] == ["on"]
                elif p[:1] == ["detail"] and len(p) == 2:
                    want_detail = p[1]
                elif p[:1] == ["shake"] and len(p) == 3 and not opt["reduce"]:
                    sea.shake(float(p[1]), float(p[2]))
            except ValueError:
                pass
        now = time.time()
        dt, last = min(0.15, now - last), now
        agents = ag.poll(now)
        frame, ring, rows = _frame(sea, agents, ag, now, W, H, painter, opt, _keys_hint(opt, app=True), dt)
        urgent = max(agents, key=lambda a: PRIO.get(a["mood"], 0), default=None)
        meta = {"agents": [{"id": a["id"], "name": a["name"], "x": sea.positions.get(a["id"], (-99, -99))[0],
                            "y": sea.positions.get(a["id"], (-99, -99))[1]} for a in agents],
                "rows": rows, "status_row": H - 1,
                "path": (urgent or {}).get("cwd", ""), "title": pretty_title((urgent or {}).get("cwd", ""))}
        if want_detail:
            a = next((x for x in agents if x["id"] == want_detail), None)
            if a:
                meta["detail"] = agent_detail(ag, a, now)
            want_detail = None
        try:
            sys.stdout.write("\x1d" + json.dumps(meta) + "\x1e" + ("\a" if ring else "") + frame + "\f")
            sys.stdout.flush()
        except BrokenPipeError:
            return
        time.sleep(max(0.0, 1 / (8 if opt["reduce"] else FPS) - (time.time() - now)))


def still(scene_index=1, secs=5.0, W=110, H=32):
    ag = Agents(demo=True)
    ag.t0 = time.time() - scene_index * 9 - 1
    sea = Sea("sleeping")
    agents = ag.poll(time.time())
    sea.sync_agents(agents)
    sea.par, sea.sky = dict(PARAMS[sea.state]), SKY[sea.state]
    surf = geoms = None
    for _ in range(int(secs * FPS)):
        surf, geoms = sea.step(1 / FPS, W, H)
    return sea.draw(W, H, surf, geoms, "", None, plain=True)


def print_status():
    ag = Agents()
    now = time.time()
    agents = ag.poll(now)
    usage = ag.get_usage(now, agents)
    print(f"Lineup {VERSION}  ·  data: {LINEUP_HOME}")
    if not agents:
        print("  no agents in the water. Connect one: `lineup install claude-code`, start Codex,")
        print("  or `lineup report my-agent working \"doing stuff\"`.")
    for a in agents:
        u = usage.get(a["id"])
        bits = [u["model"]] if u and u["model"] else []
        if u and u["ctx"]:
            bits.append(f"ctx {fmt_tok(u['ctx'])}")
        if u and u["total"]:
            bits.append(f"{u['total_label']} {fmt_tok(u['total'])}")
        extra = f"  [{', '.join(bits)}]" if bits else ""
        src = " (auto-detected)" if a.get("auto") else f" ({a['sessions']} session{'s' if a['sessions'] != 1 else ''})"
        print(f"  {a['name']:<12} {a['mood']:<10} {a['label'][:50]}{extra}{src}")
    return 0


# ------------------------------------------------------------------ CLI

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0].startswith("-"):
        argv = ["run"] + argv
    ap = argparse.ArgumentParser(prog="lineup", description="Your AI coding agents, surfing.")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="live view in this terminal (default)")
    r.add_argument("--demo", action="store_true")
    r.add_argument("--truecolor", action="store_true")
    r.add_argument("--256", dest="c256", action="store_true")
    r.add_argument("--reduce-motion", action="store_true", help="calmer sea, no flicker")
    s = sub.add_parser("stream", help="frame feed for the macOS app")
    s.add_argument("--demo", action="store_true")
    rp = sub.add_parser("report", help="tell Lineup what an agent is doing")
    rp.add_argument("agent")
    rp.add_argument("state", choices=STATES)
    rp.add_argument("label", nargs="*")
    rp.add_argument("--session", default="default")
    rp.add_argument("--model")
    rp.add_argument("--ctx", type=int, help="tokens currently in context")
    rp.add_argument("--window", type=int, help="context window size")
    rp.add_argument("--tokens", type=int, help="tokens used this session")
    rp.add_argument("--limits", help="free text, e.g. '5h 40%%'")
    rp.add_argument("--cwd")
    rp.add_argument("--detail", help="latest result text, shown when you click the surfer")
    rp.add_argument("--file", action="append", help="a file this agent changed (repeatable)")
    rp.add_argument("--ttl", type=int, help="forget this report after N seconds without updates")
    h = sub.add_parser("hook", help="hook entry point for agent CLIs")
    h.add_argument("which", choices=["claude-code"])
    for name in ("install", "uninstall"):
        i = sub.add_parser(name, help=f"{name} an integration")
        i.add_argument("which", choices=["claude-code"])
    sub.add_parser("status", help="show what Lineup currently sees")
    fr = sub.add_parser("frame", help="print one still frame (no colour)")
    fr.add_argument("scene", nargs="?", type=int, default=1)
    fr.add_argument("secs", nargs="?", type=float, default=5.0)
    sub.add_parser("version")
    a = ap.parse_args(argv)

    if a.cmd == "run":
        run(demo=a.demo, truecolor=True if a.truecolor else False if a.c256 else None, reduce=a.reduce_motion)
    elif a.cmd == "stream":
        run_stream(demo=a.demo)
    elif a.cmd == "report":
        report(a.agent, a.state, " ".join(a.label) if a.label else None, session=a.session, model=a.model,
               ctx=a.ctx, window=a.window, tokens=a.tokens, limits=a.limits, cwd=a.cwd or os.getcwd(), ttl=a.ttl,
               detail=a.detail)
        for f in a.file or []:
            report(a.agent, a.state, None, session=a.session, add_file=os.path.abspath(f))
    elif a.cmd == "hook":
        try:
            claude_hook(json.load(sys.stdin))
        except Exception:
            pass                      # a hook must never break the agent
        return 0
    elif a.cmd in ("install", "uninstall"):
        return install_claude_code(uninstall=a.cmd == "uninstall")
    elif a.cmd == "status":
        return print_status()
    elif a.cmd == "frame":
        print(still(a.scene, a.secs))
    elif a.cmd == "version":
        print(VERSION)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
