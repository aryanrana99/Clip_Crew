"""Shared state for all agents plus the event stream that feeds the dashboard."""
from __future__ import annotations

import asyncio
import os
import re
import time
from collections import deque

from .config import CONFIG, DATA, load_json, load_settings, save_json
from .util import slug

JOB_PUBLIC = ("id", "streamer", "streamer_name", "platform", "created", "start", "end",
              "signal", "stage", "status", "progress", "title", "error", "duration",
              "stage_started", "title_source", "captions")


def parse_streamer(text: str, platform: str = "") -> dict | None:
    """Turn a channel URL or a bare handle into a streamer record."""
    text = (text or "").strip()
    if not text:
        return None
    m = re.search(r"twitch\.tv/([A-Za-z0-9_]{2,30})", text)
    if m:
        platform, handle = "twitch", m.group(1).lower()
    else:
        m = re.search(r"youtube\.com/(@[\w.\-]+|channel/[\w\-]+|c/[\w.\-]+|user/[\w.\-]+)", text)
        if m:
            platform, handle = "youtube", m.group(1)
        elif "/" in text or " " in text:
            return None
        else:
            platform = (platform or "twitch").lower()
            handle = text.lstrip("@")
            if platform == "youtube":
                handle = "@" + handle
            else:
                handle = handle.lower()
    name = handle.split("/")[-1].lstrip("@")
    sid = ("tw_" if platform == "twitch" else "yt_") + slug(name, 30).replace("-", "_")
    url = (f"https://www.twitch.tv/{handle}" if platform == "twitch"
           else f"https://www.youtube.com/{handle}")
    return {"id": sid, "platform": platform, "handle": handle, "name": name, "url": url,
            "enabled": True, "added": time.time()}


class Hub:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.streamers: dict[str, dict] = {s["id"]: s for s in load_json(CONFIG / "streamers.json", [])}
        self.runtime: dict[str, dict] = {}
        self.agents: dict = {}
        self.jobs: dict[str, dict] = {}
        self.clips: list[dict] = load_json(DATA / "clips.json", [])
        self.history: dict[str, list] = load_json(DATA / "history.json", {})
        self.logs: deque = deque(maxlen=500)
        self.clients: set[asyncio.Queue] = set()
        self.manager = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.llm = {"mode": "template", "note": "Not tried yet"}

    # ---- runtime state -------------------------------------------------
    def rt(self, sid: str) -> dict:
        return self.runtime.setdefault(sid, {"live": False, "chat": deque(maxlen=240),
                                             "last_chat": deque(maxlen=6)})

    # ---- events --------------------------------------------------------
    def emit(self, kind: str, data) -> None:
        msg = {"type": kind, "data": data}
        for q in list(self.clients):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # a stalled browser tab just misses updates

    def log(self, agent: str, msg: str, level: str = "info", streamer: str | None = None) -> None:
        entry = {"t": time.time(), "agent": agent, "msg": msg, "level": level, "streamer": streamer}
        self.logs.append(entry)
        self.emit("log", entry)

    # ---- views ---------------------------------------------------------
    def streamer_view(self, sid: str) -> dict:
        s = dict(self.streamers[sid])
        rt = self.rt(sid)
        for key in ("live", "live_since", "title", "category", "note", "last_check",
                    "next_check", "calendar"):
            s[key] = rt.get(key)
        s["chat"] = list(rt["chat"])
        s["clips"] = sum(1 for c in self.clips if c.get("streamer") == sid)
        return s

    def push_streamer(self, sid: str) -> None:
        if sid in self.streamers:
            self.emit("streamer", self.streamer_view(sid))

    @staticmethod
    def job_view(job: dict) -> dict:
        return {k: job.get(k) for k in JOB_PUBLIC}

    def push_job(self, job: dict) -> None:
        self.emit("job", self.job_view(job))

    def snapshot(self) -> dict:
        from .agents.base import ROLES
        return {
            "now": time.time(),
            "roles": ROLES,
            "agents": [a.info() for a in self.agents.values()],
            "streamers": [self.streamer_view(sid) for sid in self.streamers],
            "jobs": [self.job_view(j) for j in self.jobs.values()],
            "clips": self.clips[:200],
            "logs": list(self.logs)[-200:],
            "settings": self.settings,
            "llm": self.llm,
            "twitch_api": bool(os.environ.get("TWITCH_CLIENT_ID") and os.environ.get("TWITCH_CLIENT_SECRET")),
        }

    # ---- actions shared by the web routes and the Manager chat -----------
    def add_streamer(self, channel: str, platform: str = "") -> dict:
        """Returns the streamer view. Raises ValueError with a readable message."""
        s = parse_streamer(channel, platform)
        if not s:
            raise ValueError("Enter a Twitch or YouTube channel link, or a channel name.")
        if s["id"] in self.streamers:
            raise LookupError(f"{s['name']} is already on the watch list.")
        self.streamers[s["id"]] = s
        self.save_streamers()
        self.log("system", f"Added {s['name']} ({s['platform']}) to the watch list")
        self.push_streamer(s["id"])
        self.manager.send({"type": "streamer_added", "streamer": s["id"]})
        return self.streamer_view(s["id"])

    def find_streamer(self, text: str) -> str | None:
        text = (text or "").strip().lower().lstrip("@")
        for sid, s in self.streamers.items():
            if text in (sid.lower(), s["name"].lower(), s["handle"].lower().lstrip("@"), s["url"].lower()):
                return sid
        guess = parse_streamer(text)
        return guess["id"] if guess and guess["id"] in self.streamers else None

    def remove_streamer(self, sid: str) -> dict:
        s = self.streamers.pop(sid, None)
        if not s:
            raise KeyError("Unknown streamer")
        self.save_streamers()
        self.runtime.get(sid, {}).update(live=False)
        self.manager.send({"type": "streamer_removed", "streamer": sid})
        self.emit("streamer_gone", {"id": sid})
        self.log("system", f"Removed {s['name']} from the watch list")
        return s

    def apply_settings(self, patch: dict, who: str = "system") -> dict:
        from .config import coerce_settings
        clean = coerce_settings(patch)
        self.settings.update(clean)
        if self.settings["clip_max_s"] < self.settings["clip_min_s"]:
            self.settings["clip_max_s"] = self.settings["clip_min_s"]
        self.save_settings()
        self.emit("settings", self.settings)
        if clean:
            self.log(who, "Settings updated: " + ", ".join(sorted(clean)))
        return clean

    # ---- persistence ---------------------------------------------------
    def save_streamers(self) -> None:
        save_json(CONFIG / "streamers.json", list(self.streamers.values()))

    def save_clips(self) -> None:
        save_json(DATA / "clips.json", self.clips)

    def save_history(self) -> None:
        save_json(DATA / "history.json", self.history)

    def save_settings(self) -> None:
        save_json(CONFIG / "settings.json", self.settings)
