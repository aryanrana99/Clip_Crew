"""Live Scout: notices when a streamer goes live or ends, using the calendar to pace its checks."""
from __future__ import annotations

import json
import re
import time

from ..util import STREAMLINK, run_cmd
from .base import Agent

RERUN = re.compile(r"(?i)\b(rerun|re-run|rebroadcast|vod ?cast)\b")


def stream_url(s: dict) -> str:
    return s["url"] if s["platform"] == "twitch" else s["url"].rstrip("/") + "/live"


class LiveScout(Agent):
    role = "scout"

    def interval(self, rt: dict) -> float:
        """How long until the next check — fast when a stream is live or expected."""
        st = self.hub.settings
        if rt.get("live"):
            return 20 if rt.get("misses") else st["poll_live_s"]
        cal = rt.get("calendar")
        if not cal:
            return 120
        now = time.time()
        for slot in cal["upcoming"]:
            if slot["start"] - 20 * 60 <= now <= (slot.get("end") or slot["start"] + 3 * 3600):
                return st["poll_live_s"]
        lt = time.localtime(now)
        if max(cal["heat"][lt.tm_wday][lt.tm_hour], cal["heat"][lt.tm_wday][(lt.tm_hour + 1) % 24]) >= 0.25:
            return 90
        return st["poll_idle_s"]

    async def run(self) -> None:
        self.set("working", "Starting the first sweep")
        while True:
            hub = self.hub
            for s in list(hub.streamers.values()):
                sid = s["id"]
                rt = hub.rt(sid)
                if not s.get("enabled", True):
                    if rt.get("live"):
                        self.go_offline(s, rt)
                        hub.push_streamer(sid)
                    continue
                if rt.get("next_check", 0) <= time.time():
                    await self.check(s, rt)
                    if sid in hub.streamers:
                        rt["next_check"] = time.time() + self.interval(rt)
                        hub.push_streamer(sid)
            watching = [s for s in hub.streamers.values() if s.get("enabled", True)]
            live = sum(1 for s in watching if hub.rt(s["id"]).get("live"))
            if watching:
                nxt = min(hub.rt(s["id"]).get("next_check", 0) for s in watching) - time.time()
                self.set("idle", f"Watching {len(watching)} streamer(s), {live} live — next check in {max(0, int(nxt))}s",
                         live=live, watching=len(watching))
            else:
                self.set("idle", "No streamers to watch yet", live=0, watching=0)
            await self.nap(2)

    async def check(self, s: dict, rt: dict) -> None:
        sid = s["id"]
        self.set("working", f"Checking whether {s['name']} is live")
        rc, out, err = await run_cmd(STREAMLINK + ["--json", stream_url(s)], timeout=60)
        self.stats["checks"] = self.stats.get("checks", 0) + 1
        try:
            result = json.loads(out)
        except ValueError:
            return  # network hiccup — keep whatever state we had
        live = bool(result.get("streams"))
        if not live and "No playable streams" not in str(result.get("error", "")) \
                and "Unable to open URL" in str(result.get("error", "")):
            return  # couldn't reach the site, not a real "offline"
        meta = result.get("metadata") or {}
        title = meta.get("title") or ""
        rt["note"] = ""
        if live and self.hub.settings["skip_reruns"] and RERUN.search(title):
            live = False
            rt["note"] = "A rerun is on air — skipped"
        first_look = "last_check" not in rt
        rt["last_check"] = time.time()

        if live:
            rt["misses"] = 0
            rt.update(title=title, category=meta.get("category") or "", video_id=meta.get("id"))
            if not rt.get("live"):
                rt.update(live=True, live_since=time.time(), saw_start=not first_look)
                self.log(f"{s['name']} just went live: “{title}”" if not first_look
                         else f"{s['name']} is already live: “{title}”", "success")
                self.hub.manager.send({"type": "live_start", "streamer": sid})
        elif rt.get("live"):
            rt["misses"] = rt.get("misses", 0) + 1
            if rt["misses"] >= 2:  # two misses in a row, so a brief drop doesn't end the session
                self.go_offline(s, rt)

    def go_offline(self, s: dict, rt: dict) -> None:
        sid = s["id"]
        if rt.get("saw_start") and rt.get("live_since"):
            sessions = self.hub.history.setdefault(sid, [])
            sessions.append([rt["live_since"], time.time()])
            del sessions[:-80]
            self.hub.save_history()
        rt.update(live=False, misses=0, live_since=None)
        self.log(f"{s['name']} went offline")
        self.hub.manager.send({"type": "live_end", "streamer": sid})
