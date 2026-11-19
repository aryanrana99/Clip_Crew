"""Stream Recorder: keeps a rolling buffer of recent footage as short segments."""
from __future__ import annotations

import asyncio
import subprocess
import time

from ..config import BUFFER
from ..util import STREAMLINK, ffmpeg_bin, kill, spawn
from .base import Agent
from .scout import stream_url

SEGMENT_S = 4


class StreamRecorder(Agent):
    role = "recorder"

    def __init__(self, hub, streamer_id: str) -> None:
        super().__init__(hub, streamer_id)
        self.dir = BUFFER / streamer_id
        self.segments: list[dict] = []  # {"path", "start", "end"} in wall-clock time
        self.source = self.muxer = None
        self.runs = 0
        self.errlog = None

    # ---- used by the Clip Cutter --------------------------------------
    def covered_until(self) -> float:
        return self.segments[-1]["end"] if self.segments else 0.0

    def segments_between(self, start: float, end: float) -> list[dict]:
        return [s for s in self.segments if s["end"] > start and s["start"] < end]

    # ---- recording -----------------------------------------------------
    def launch(self) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.runs += 1
        prefix = f"seg_{int(time.time())}_"
        s, st = self.streamer(), self.hub.settings
        cmd = STREAMLINK + ["--stdout", "--loglevel", "error", "--retry-open", "2"]
        if s["platform"] == "twitch":
            cmd.append("--twitch-low-latency")
        cmd += [stream_url(s), st["quality"]]
        self.errlog = open(self.dir / "recorder.log", "ab")
        self.source = spawn(cmd, stdout=subprocess.PIPE, stderr=self.errlog)
        self.muxer = spawn([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-fflags", "+genpts",
                            "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
                            "-f", "segment", "-segment_time", str(SEGMENT_S), "-segment_format", "mpegts",
                            "-reset_timestamps", "1", str(self.dir / f"{prefix}%06d.ts")],
                           stdin=self.source.stdout, stdout=subprocess.DEVNULL, stderr=self.errlog)
        self.source.stdout.close()  # ffmpeg owns the pipe now
        return prefix

    def halt(self) -> None:
        kill(self.muxer)
        kill(self.source)
        self.source = self.muxer = None
        if self.errlog:
            self.errlog.close()
            self.errlog = None

    def cleanup(self) -> None:
        self.halt()

    def scan(self, prefix: str, indexed: set) -> int:
        """Index newly finished segments. The newest file is still being written, so skip it."""
        files = sorted(self.dir.glob(prefix + "*.ts"))
        added = 0
        for path in files[:-1]:
            if path.name in indexed:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            indexed.add(path.name)
            if stat.st_size < 2000:
                continue
            end = stat.st_mtime  # the moment ffmpeg closed it ≈ the wall-clock end of the footage
            prev = self.segments[-1]["end"] if self.segments else 0
            start = prev if 0 < end - prev < SEGMENT_S * 3 else end - SEGMENT_S
            self.segments.append({"path": path, "start": start, "end": end, "size": stat.st_size})
            added += 1
        return added

    def prune(self) -> None:
        horizon = time.time() - self.hub.settings["buffer_minutes"] * 60
        while self.segments and self.segments[0]["end"] < horizon:
            old = self.segments.pop(0)
            try:
                old["path"].unlink()
            except OSError:
                pass

    async def run(self) -> None:
        name = self.streamer()["name"]
        failures = 0
        while True:
            self.set("working", f"Connecting to {name}'s stream")
            prefix = self.launch()
            indexed: set = set()
            last_new = time.time()
            got_any = False
            while True:
                await asyncio.sleep(1)
                if self.scan(prefix, indexed):
                    last_new = time.time()
                    got_any = True
                    failures = 0
                self.prune()
                buffered = sum(s["end"] - s["start"] for s in self.segments)
                size_mb = sum(s["size"] for s in self.segments) / 1e6
                if got_any:
                    self.set("working", f"Recording {name} — last {int(buffered // 60)}m {int(buffered % 60):02d}s on tape",
                             buffered_s=int(buffered), size_mb=round(size_mb), reconnects=self.runs - 1)
                if self.source.poll() is not None or self.muxer.poll() is not None:
                    break
                if time.time() - last_new > 45:
                    self.log(f"No new footage from {name} for 45s — reconnecting", "warn")
                    break
            self.halt()
            failures += 1
            if failures >= 3:
                # Probably offline: ask the Scout to confirm rather than hammering the stream.
                self.hub.rt(self.streamer_id)["next_check"] = 0
                if "scout" in self.hub.agents:
                    self.hub.agents["scout"].poke()
                self.set("waiting", f"Lost {name}'s stream — waiting for the Scout to confirm")
                await asyncio.sleep(30)
            else:
                self.set("waiting", f"Stream dropped — reconnecting to {name}")
                await asyncio.sleep(4)
