"""Hype Detector: finds the moments when chat suddenly moves much faster than normal."""
from __future__ import annotations

import asyncio
import re
import statistics
import time
from collections import Counter, deque

from .base import Agent

HYPE = re.compile(
    r"(?i)(?<!\w)(lul\w*|kek\w*|omegalul|lmf?ao+|rofl|pog\w*|wtf+|omg+|clip(?:ped| it| that)?|insane|bruh+|"
    r"monka\w*|pepelaugh|icant|lo+l+|xd+|a?(?:ha){2,}h?|no ?way|ain'?t no way|sheesh+|yo{3,}|ayo+|holy|"
    r"goat(?:ed)?|cooked|let'?s go+|w|l)(?!\w)|[💀😂🤣🔥😭😱]|\?{3,}|!{3,}")
CLIP_REQUEST = re.compile(r"(?i)(?<!\w)clip(?:ped| it| that)?(?!\w)")
REACTIONS = {
    "funny": re.compile(r"(?i)(lul|kek|lmf?ao|rofl|lo+l|haha|xd|icant|pepelaugh|[😂🤣💀])"),
    "hype": re.compile(r"(?i)(pog|let'?s go|sheesh|goat|insane|holy|🔥|(?<!\w)w(?!\w))"),
    "shock": re.compile(r"(?i)(wtf|omg|no ?way|monka|\?{3,}|😱|(?<!\w)huh(?!\w))"),
    "fail": re.compile(r"(?i)(bruh|cooked|😭|(?<!\w)l(?!\w)|(?<!\w)rip(?!\w))"),
}
WARMUP, IDLE, SPIKE, COOLDOWN = 0, 1, 2, 3


class HypeDetector(Agent):
    role = "detector"

    async def run(self) -> None:
        hub, sid = self.hub, self.streamer_id
        rt = hub.rt(sid)
        queue: asyncio.Queue = rt["chat_q"]
        name = self.streamer()["name"]
        msgs: deque = deque()               # (ts, user, text) for the last two minutes
        baseline: deque = deque(maxlen=300)  # chat speed samples from calm periods
        state, spike, calm, cooldown_until = WARMUP, None, 0, 0.0
        next_tick = time.time() + 1
        self.set("working", f"Learning the normal pace of {name}'s chat", moments=0)

        while True:
            wait = next_tick - time.time()
            if wait > 0:
                try:
                    msgs.append(await asyncio.wait_for(queue.get(), wait))
                    continue
                except asyncio.TimeoutError:
                    pass
            next_tick += 1.0
            now = time.time()
            if next_tick < now - 5:
                next_tick = now + 1

            st = hub.settings
            window = max(4, st["chat_window_s"])
            while msgs and msgs[0][0] < now - 120:
                msgs.popleft()
            recent = [m for m in msgs if m[0] >= now - window]
            n = len(recent)
            # one person spamming shouldn't look like the whole chat reacting
            effective = min(n, 3 * len({m[1] for m in recent}))
            rate = effective / window
            hype = sum(1 for m in recent if HYPE.search(m[2]))
            clip_asks = sum(1 for m in recent if CLIP_REQUEST.search(m[2]))

            base = statistics.median(baseline) if baseline else rate
            mad = statistics.median(abs(r - base) for r in baseline) if len(baseline) >= 10 else 0.0
            sigma = max(1.4826 * mad, 0.25 * base, 0.12)
            z = (rate - base) / sigma
            threshold = max(st["spike_ratio"] * base, base + st["sensitivity"] * sigma,
                            st["min_msgs_per_10s"] / 10)
            if n and hype / n >= 0.4:
                threshold *= 0.85  # chat full of reactions needs a little less speed to count

            if state == WARMUP:
                baseline.append(rate)
                if len(baseline) >= min(st["warmup_s"], 300):
                    state = IDLE
                    self.log(f"Baseline learned for {name}: about {base:.1f} msg/s is normal")
            elif state == COOLDOWN:
                baseline.append(rate)
                if now >= cooldown_until:
                    state = IDLE
            elif state == IDLE:
                asked = clip_asks >= 4 and clip_asks >= 0.15 * n
                if rate >= threshold or asked:
                    state, calm = SPIKE, 0
                    spike = {"start": now - window * 0.6, "trigger": now, "end": now, "peak_rate": rate,
                             "peak_ts": now, "base": base, "z": z,
                             "reason": "chat asked for a clip" if asked and rate < threshold else "chat spike"}
                    self.log(f"Chat is spiking for {name}: {rate:.1f} msg/s vs normal {base:.1f}", "alert")
                else:
                    baseline.append(rate)
            elif state == SPIKE:
                if rate > spike["peak_rate"]:
                    spike.update(peak_rate=rate, peak_ts=now, z=max(spike["z"], z))
                if rate >= max(1.5 * spike["base"], 0.5 * spike["peak_rate"]):
                    spike["end"], calm = now, 0
                else:
                    calm += 1
                if calm >= 6 or now - spike["trigger"] > 55:
                    during = [m for m in msgs if m[0] >= spike["start"] - 2]
                    spike["sample"] = [t for t, _ in Counter(m[2].strip()[:80] for m in during).most_common(14)]
                    counts = {k: sum(1 for m in during if rx.search(m[2])) for k, rx in REACTIONS.items()}
                    spike["reaction"] = max(counts, key=counts.get) if any(counts.values()) else "hype"
                    hub.manager.send({"type": "moment", "streamer": sid, "moment": spike})
                    self.stats["moments"] = self.stats.get("moments", 0) + 1
                    state, cooldown_until, spike = COOLDOWN, now + st["cooldown_s"], None

            point = [round(now, 1), round(rate, 2), round(base, 2), round(threshold, 2), state]
            rt["chat"].append(point)
            hub.emit("chat", {"id": sid, "p": point, "last": list(rt["last_chat"])[-3:]})
            if state == SPIKE:
                self.set("alert", f"🔥 {name}'s chat is exploding — {rate:.1f} msg/s (normal {base:.1f})")
            elif state == WARMUP:
                left = max(0, min(st["warmup_s"], 300) - len(baseline))
                self.set("working", f"Learning the normal pace of {name}'s chat ({left}s left)")
            else:
                label = "Cooling down after a moment" if state == COOLDOWN else "Watching"
                self.set("working", f"{label} — {rate:.1f} msg/s, normal {base:.1f}, triggers at {threshold:.1f}")
