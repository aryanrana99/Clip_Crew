"""Agent base classes and the roster shown on the dashboard."""
from __future__ import annotations

import asyncio
import time
import traceback

ROLES = [
    dict(role="manager", title="Manager", icon="🧠", step=0, per_stream=False,
         desc="Runs the whole operation. Deploys a watch crew the moment a stream goes live, turns hype "
              "signals into clip jobs, routes every job through production in order, retries failed "
              "steps and restarts any agent that crashes."),
    dict(role="calendar", title="Calendar Analyst", icon="📅", step=1, per_stream=False,
         desc="Studies each streamer's official schedule and past broadcasts, learns their weekly rhythm "
              "and predicts when they will go live next so the crew is ready in time."),
    dict(role="scout", title="Live Scout", icon="📡", step=2, per_stream=False,
         desc="Checks whether each streamer is live — every minute around the times the Calendar Analyst "
              "expects a stream, less often otherwise — and alerts the Manager when a stream starts or ends."),
    dict(role="recorder", title="Stream Recorder", icon="⏺️", step=3, per_stream=True,
         desc="Watches the live stream and keeps a rolling buffer of the last few minutes of footage, so a "
              "moment can still be clipped after chat has reacted to it."),
    dict(role="chat", title="Chat Monitor", icon="💬", step=3, per_stream=True,
         desc="Reads live chat in real time and passes every message to the Hype Detector."),
    dict(role="detector", title="Hype Detector", icon="🔥", step=4, per_stream=True,
         desc="Measures chat speed against the channel's normal pace and flags the moments when chat "
              "suddenly explodes, spams reactions or asks for a clip."),
    dict(role="cutter", title="Clip Cutter", icon="✂️", step=5, per_stream=False,
         desc="Pulls the flagged 45–90 seconds out of the recording buffer as a raw clip."),
    dict(role="captioner", title="Caption Writer", icon="📝", step=6, per_stream=False,
         desc="Transcribes the clip's speech with word-level timing so captions can pop on screen in sync."),
    dict(role="titler", title="Title Writer", icon="🏷️", step=7, per_stream=False,
         desc="Reads the transcript and how chat reacted, then writes the on-video title, description "
              "and hashtags, and scores how viral the moment looks."),
    dict(role="editor", title="Video Editor", icon="🎬", step=8, per_stream=False,
         desc="Renders the final 9:16 video for Shorts and Reels: blurred backdrop, title on top, "
              "animated captions below, then files it in the clip library."),
]


class Agent:
    role = "agent"

    def __init__(self, hub, streamer_id: str | None = None) -> None:
        self.hub = hub
        self.streamer_id = streamer_id
        self.name = self.role if not streamer_id else f"{self.role}:{streamer_id}"
        self.status = "idle"
        self.task = ""
        self.stats: dict = {}
        self.restarts = 0
        self.started = time.time()
        self.beat = time.time()
        self.error: str | None = None
        self.stopping = False
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> "Agent":
        self.hub.agents[self.name] = self
        self._task = asyncio.create_task(self._runner(), name=self.name)
        self.hub.emit("agent", self.info())
        return self

    async def _runner(self) -> None:
        try:
            await self.run()
            if not self.stopping:
                self.set("error", "Stopped unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the Manager notices the dead task and restarts us
            self.error = f"{type(exc).__name__}: {exc}"
            self.log(f"Crashed: {self.error}", "error")
            traceback.print_exc()
            self.set("error", self.error)
        finally:
            try:
                self.cleanup()
            except Exception:
                traceback.print_exc()

    async def stop(self) -> None:
        self.stopping = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self.hub.agents.get(self.name) is self:
            del self.hub.agents[self.name]
            self.hub.emit("agent_gone", {"name": self.name})

    @property
    def dead(self) -> bool:
        return self._task is not None and self._task.done()

    async def run(self) -> None:
        raise NotImplementedError

    def cleanup(self) -> None:
        """Release processes/sockets. Must be safe to call more than once."""

    # ---- helpers -------------------------------------------------------
    def send(self, msg) -> None:
        self.inbox.put_nowait(msg)

    def poke(self) -> None:
        self.wake.set()

    async def nap(self, seconds: float) -> None:
        """Sleep, but wake early if someone pokes us."""
        try:
            await asyncio.wait_for(self.wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        self.wake.clear()

    def set(self, status: str | None = None, task: str | None = None, **stats) -> None:
        if status is not None:
            self.status = status
        if task is not None:
            self.task = task
        self.stats.update(stats)
        self.beat = time.time()
        self.hub.emit("agent", self.info())

    def log(self, msg: str, level: str = "info") -> None:
        self.hub.log(self.name, msg, level, self.streamer_id)

    def info(self) -> dict:
        return {"name": self.name, "role": self.role, "streamer": self.streamer_id,
                "status": self.status, "task": self.task, "stats": self.stats,
                "restarts": self.restarts, "started": self.started, "beat": self.beat,
                "error": self.error}

    def streamer(self) -> dict:
        return self.hub.streamers[self.streamer_id]


class StageAgent(Agent):
    """A production-line worker: takes one clip job at a time from its queue,
    does its step and reports the outcome to the Manager."""

    stage = ""
    idle_text = "Waiting for the next clip"

    async def run(self) -> None:
        self.set("idle", self.idle_text)
        while True:
            job = await self.inbox.get()
            if job.get("status") != "active":
                continue
            job["stage"] = self.stage
            job["stage_started"] = time.time()
            job["progress"] = 0
            self.hub.push_job(job)
            try:
                await self.process(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                traceback.print_exc()
                self.hub.manager.send({"type": "stage_failed", "job": job["id"], "stage": self.stage,
                                       "error": f"{exc}"[:300]})
            else:
                self.stats["done"] = self.stats.get("done", 0) + 1
                self.hub.manager.send({"type": "stage_done", "job": job["id"], "stage": self.stage})
            waiting = self.inbox.qsize()
            self.set("idle", self.idle_text if not waiting else f"{waiting} clip(s) queued")

    async def process(self, job: dict) -> None:
        raise NotImplementedError
