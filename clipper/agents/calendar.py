"""Calendar Analyst: learns when each streamer goes live."""
from __future__ import annotations

import json
import math
import os
import re
import statistics
import time
from datetime import datetime

import httpx

from ..util import YTDLP, run_cmd
from .base import Agent

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEK = 7 * 86400
# Twitch archive thumbnails carry the broadcast start time: ..._<login>_<id>_<unix>/thumb/...
TWITCH_START = re.compile(r"_(\d{10})/+thumb")


def _iso(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def build_calendar(sessions: list[tuple[float, float]], official: list[dict], now: float | None = None) -> dict:
    """Turn past sessions [(start, duration_s)] and official slots into a weekly
    heat-map, a plain-language summary and a list of upcoming streams."""
    now = now or time.time()
    recent = sorted((s, d) for s, d in sessions if now - 8 * WEEK <= s <= now)
    # collapse duplicates (same broadcast seen from two sources)
    merged: list[tuple[float, float]] = []
    for s, d in recent:
        if merged and s - merged[-1][0] < 1800:
            merged[-1] = (merged[-1][0], max(merged[-1][1], d))
        else:
            merged.append((s, d))
    recent = merged
    weeks = max(1.0, min(8.0, (now - recent[0][0]) / WEEK)) if recent else 1.0

    heat = [[0.0] * 24 for _ in range(7)]
    starts: dict[int, list[float]] = {}
    durations = [d for _, d in recent if d > 600]
    typical_dur = statistics.median(durations) if durations else 3 * 3600
    for s, d in recent:
        lt = time.localtime(s)
        starts.setdefault(lt.tm_wday, []).append(lt.tm_hour + lt.tm_min / 60)
        t, end = s, s + min(d if d > 600 else typical_dur, 16 * 3600)
        while t < end:
            lt = time.localtime(t)
            heat[lt.tm_wday][lt.tm_hour] += 1
            t = (int(t) // 3600 + 1) * 3600
    heat = [[round(min(1.0, v / weeks), 2) for v in row] for row in heat]

    typical = {}
    for wd, hours in starts.items():
        # circular mean so 23:30 and 00:30 average to midnight, not noon
        x = sum(math.cos(h / 24 * 2 * math.pi) for h in hours)
        y = sum(math.sin(h / 24 * 2 * math.pi) for h in hours)
        hour = (math.atan2(y, x) / (2 * math.pi) * 24) % 24
        typical[wd] = {"hour": hour, "prob": min(1.0, len(hours) / weeks)}

    upcoming = [dict(o, source="official", confidence=1.0) for o in official if (o.get("end") or o["start"] + 3600) > now]
    today = time.localtime(now)
    for offset in range(8):
        midnight = time.mktime((today.tm_year, today.tm_mon, today.tm_mday + offset, 0, 0, 0, 0, 0, -1))
        wd = time.localtime(midnight + 43200).tm_wday
        slot = typical.get(wd)
        if not slot or slot["prob"] < 0.3:
            continue
        start = midnight + slot["hour"] * 3600
        if start + typical_dur < now:
            continue
        if any(abs(u["start"] - start) < 4 * 3600 for u in upcoming):
            continue
        upcoming.append({"start": start, "end": start + typical_dur, "title": "Usual time slot",
                         "source": "predicted", "confidence": round(slot["prob"], 2)})
    upcoming.sort(key=lambda u: u["start"])

    regular = sorted(wd for wd, slot in typical.items() if slot["prob"] >= 0.3)
    if regular:
        x = sum(math.cos(typical[wd]["hour"] / 24 * 2 * math.pi) for wd in regular)
        y = sum(math.sin(typical[wd]["hour"] / 24 * 2 * math.pi) for wd in regular)
        hour = (math.atan2(y, x) / (2 * math.pi) * 24) % 24
        days = "every day" if len(regular) == 7 else "/".join(DAYS[wd] for wd in regular)
        summary = (f"Usually live {days} around {int(hour):02d}:{int(hour % 1 * 60):02d} "
                   f"for about {typical_dur / 3600:.1f}h")
    elif recent:
        summary = "No steady weekly pattern yet — checking at a regular pace"
    else:
        summary = "No past streams found yet — the pattern is learned as streams happen"

    return {"updated": now, "upcoming": upcoming[:10], "heat": heat, "summary": summary,
            "sessions": len(recent), "official": sum(1 for u in upcoming if u["source"] == "official")}


class CalendarAnalyst(Agent):
    role = "calendar"
    REFRESH = 6 * 3600

    async def run(self) -> None:
        self.set("idle", "Waiting for streamers to study")
        while True:
            hub = self.hub
            for s in list(hub.streamers.values()):
                rt = hub.rt(s["id"])
                if not s.get("enabled", True) or time.time() - rt.get("calendar_at", 0) < self.REFRESH:
                    continue
                self.set("working", f"Studying {s['name']}'s stream calendar")
                try:
                    await self.analyse(s)
                    rt["calendar_at"] = time.time()
                except Exception as exc:
                    rt["calendar_at"] = time.time() - self.REFRESH + 900  # try again in 15 minutes
                    self.log(f"Couldn't read {s['name']}'s calendar ({exc}) — will retry", "warn")
            known = sum(1 for sid in hub.streamers if hub.rt(sid).get("calendar"))
            self.set("idle", f"{known} calendar(s) up to date — refreshing every 6 hours" if known
                     else "Waiting for streamers to study", calendars=known)
            await self.nap(30)

    async def analyse(self, s: dict) -> None:
        sid = s["id"]
        if s["platform"] == "twitch":
            sessions = await self.twitch_history(s["handle"])
            official = await self.twitch_schedule(s["handle"])
        else:
            sessions, official = await self.youtube_history(s["handle"])
        sessions += [(a, b - a) for a, b in self.hub.history.get(sid, []) if b and b > a]
        cal = build_calendar(sessions, official)
        self.hub.rt(sid)["calendar"] = cal
        self.hub.rt(sid)["next_check"] = 0  # let the Scout re-plan its cadence
        self.hub.push_streamer(sid)
        self.stats["analysed"] = self.stats.get("analysed", 0) + 1
        nxt = next((u for u in cal["upcoming"] if u["start"] > time.time()), None)
        when = time.strftime("%a %H:%M", time.localtime(nxt["start"])) if nxt else "unknown"
        self.log(f"{s['name']}: studied {cal['sessions']} past stream(s). {cal['summary']}. Next expected: {when}")

    async def twitch_history(self, handle: str) -> list[tuple[float, float]]:
        url = f"https://www.twitch.tv/{handle}/videos?filter=archives&sort=time"
        rc, out, err = await run_cmd(YTDLP + ["--flat-playlist", "--playlist-end", "40", "-J", "--no-warnings", url], timeout=120)
        try:
            entries = json.loads(out).get("entries") or []
        except ValueError:
            if "does not exist" in err or "404" in err:
                return []
            raise RuntimeError((err.strip().splitlines() or ["no data"])[-1][:160])
        sessions = []
        for e in entries:
            m = TWITCH_START.search(e.get("thumbnail") or "")
            if m:
                sessions.append((float(m.group(1)), float(e.get("duration") or 0)))
        return sessions

    async def twitch_schedule(self, handle: str) -> list[dict]:
        """Official schedule through Twitch's Helix API — used only if the user supplied API credentials."""
        cid, secret = os.environ.get("TWITCH_CLIENT_ID"), os.environ.get("TWITCH_CLIENT_SECRET")
        if not (cid and secret):
            return []
        async with httpx.AsyncClient(timeout=20) as client:
            tok = await client.post("https://id.twitch.tv/oauth2/token",
                                    params={"client_id": cid, "client_secret": secret,
                                            "grant_type": "client_credentials"})
            tok.raise_for_status()
            headers = {"Client-Id": cid, "Authorization": f"Bearer {tok.json()['access_token']}"}
            users = await client.get("https://api.twitch.tv/helix/users", params={"login": handle}, headers=headers)
            users.raise_for_status()
            data = users.json().get("data") or []
            if not data:
                return []
            r = await client.get("https://api.twitch.tv/helix/schedule",
                                 params={"broadcaster_id": data[0]["id"], "first": 10}, headers=headers)
            if r.status_code != 200:  # 404 = streamer has no published schedule
                return []
            segments = (r.json().get("data") or {}).get("segments") or []
        return [{"start": _iso(seg["start_time"]),
                 "end": _iso(seg["end_time"]) if seg.get("end_time") else None,
                 "title": seg.get("title") or "Scheduled stream"}
                for seg in segments if not seg.get("canceled_until")]

    async def youtube_history(self, handle: str) -> tuple[list[tuple[float, float]], list[dict]]:
        url = f"https://www.youtube.com/{handle}/streams"
        rc, out, err = await run_cmd(YTDLP + ["-J", "--skip-download", "--ignore-no-formats-error", "--ignore-errors",
                                              "--no-warnings", "--playlist-end", "10", url], timeout=300)
        try:
            entries = json.loads(out).get("entries") or []
        except ValueError:
            if "does not have a streams tab" in err:
                return [], []
            raise RuntimeError((err.strip().splitlines() or ["no data"])[-1][:160])
        sessions, official = [], []
        for e in entries:
            if not e:
                continue
            status = e.get("live_status")
            ts = e.get("release_timestamp") or e.get("timestamp")
            if status == "was_live" and ts:
                sessions.append((float(ts), float(e.get("duration") or 0)))
            elif status == "is_upcoming" and e.get("release_timestamp"):
                official.append({"start": float(e["release_timestamp"]), "end": None,
                                 "title": e.get("title") or "Scheduled stream"})
        return sessions, official
