"""Chat Monitor: streams live chat messages to the Hype Detector."""
from __future__ import annotations

import asyncio
import json
import random
import re
import shutil
import subprocess
import time

from ..config import BUFFER
from ..util import YTDLP, kill, spawn
from .base import Agent

PRIVMSG = re.compile(r"^(?:@\S+ )?:(\w+)!\S+ PRIVMSG #\w+ :(.*)$")


class ChatMonitor(Agent):
    role = "chat"

    def __init__(self, hub, streamer_id: str) -> None:
        super().__init__(hub, streamer_id)
        self.proc = None
        self.writer = None
        self.count = 0
        self.connected_at = 0.0

    def deliver(self, ts: float, user: str, text: str) -> None:
        rt = self.hub.rt(self.streamer_id)
        queue = rt.get("chat_q")
        if queue is None:
            return
        try:
            queue.put_nowait((ts, user, text))
        except asyncio.QueueFull:
            pass
        rt["last_chat"].append([user, text[:140]])
        self.count += 1

    def report(self, name: str) -> None:
        mins = max(1 / 60, (time.time() - self.connected_at) / 60)
        self.set("working", f"Reading {name}'s chat — {self.count:,} messages so far",
                 messages=self.count, per_min=round(self.count / mins))

    def cleanup(self) -> None:
        kill(self.proc)
        self.proc = None
        if self.writer:
            try:
                self.writer.close()
            except Exception:
                pass
            self.writer = None

    async def run(self) -> None:
        s = self.streamer()
        backoff = 3
        while True:
            self.set("working", f"Joining {s['name']}'s chat")
            started = time.time()
            try:
                if s["platform"] == "twitch":
                    await self.twitch(s)
                else:
                    await self.youtube(s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"Chat connection dropped ({exc}) — reconnecting", "warn")
            self.cleanup()
            backoff = 3 if time.time() - started > 60 else min(backoff * 2, 60)
            self.set("waiting", f"Reconnecting to chat in {backoff}s")
            await asyncio.sleep(backoff)

    # ---- Twitch: anonymous read-only IRC ------------------------------
    async def twitch(self, s: dict) -> None:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("irc.chat.twitch.tv", 6697, ssl=True), 20)
        self.writer = writer
        writer.write(f"NICK justinfan{random.randint(10000, 99999)}\r\nJOIN #{s['handle'].lower()}\r\n".encode())
        await writer.drain()
        self.connected_at = time.time()
        self.count = 0
        last_report = 0.0
        while True:
            raw = await asyncio.wait_for(reader.readline(), 400)  # Twitch pings about every 5 minutes
            if not raw:
                raise ConnectionError("server closed the connection")
            line = raw.decode("utf-8", "ignore").rstrip("\r\n")
            if line.startswith("PING"):
                writer.write(b"PONG :tmi.twitch.tv\r\n")
                await writer.drain()
                continue
            m = PRIVMSG.match(line)
            if m:
                self.deliver(time.time(), m.group(1), m.group(2))
            if time.time() - last_report > 2:
                last_report = time.time()
                self.report(s["name"])

    # ---- YouTube: yt-dlp writes live chat to a file, we follow it ------
    async def youtube(self, s: dict) -> None:
        video_id = self.hub.rt(s["id"]).get("video_id")
        if not video_id:
            raise RuntimeError("no live video id yet")
        folder = BUFFER / s["id"] / "chat"
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True, exist_ok=True)
        self.proc = spawn(YTDLP + ["--skip-download", "--write-subs", "--sub-langs", "live_chat", "--no-warnings",
                                   "-o", str(folder / "chat"), f"https://www.youtube.com/watch?v={video_id}"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.connected_at = time.time()
        self.count = 0
        path = folder / "chat.live_chat.json.part"
        offset, tail, last_report = 0, b"", 0.0
        while True:
            await asyncio.sleep(0.5)
            if path.exists():
                with open(path, "rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                offset += len(chunk)
                lines = (tail + chunk).split(b"\n")
                tail = lines.pop()
                for line in lines:
                    self.parse_youtube(line)
            if self.proc.poll() is not None:
                raise ConnectionError("chat feed ended")
            if time.time() - last_report > 2:
                last_report = time.time()
                self.report(s["name"])

    def parse_youtube(self, line: bytes) -> None:
        try:
            obj = json.loads(line)
        except ValueError:
            return
        for action in obj.get("replayChatItemAction", {}).get("actions", []):
            item = action.get("addChatItemAction", {}).get("item", {})
            r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
            if not r:
                continue
            ts = int(r.get("timestampUsec") or 0) / 1e6 or time.time()
            if ts < self.connected_at - 5:
                continue  # backlog that was sent when we joined
            text = "".join(run.get("text") or (run.get("emoji", {}).get("shortcuts") or [""])[0]
                           for run in r.get("message", {}).get("runs", []))
            user = r.get("authorName", {}).get("simpleText", "?")
            if text:
                self.deliver(ts, user, text)
