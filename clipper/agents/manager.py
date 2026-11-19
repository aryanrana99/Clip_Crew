"""The Manager: deploys crews, creates clip jobs, routes them through production
and keeps every other agent alive."""
from __future__ import annotations

import asyncio
import shutil
import time
import traceback

from ..config import BUFFER, WORK
from .base import Agent
from .calendar import CalendarAnalyst
from .chat import ChatMonitor
from .detector import HypeDetector
from .production import CaptionWriter, ClipCutter, TitleWriter, VideoEditor
from .recorder import StreamRecorder
from .scout import LiveScout

CORE = [CalendarAnalyst, LiveScout, ClipCutter, CaptionWriter, TitleWriter, VideoEditor]
CREW = [StreamRecorder, ChatMonitor, HypeDetector]
PIPELINE = ["cutter", "captioner", "titler", "editor"]
STAGE_LABEL = {"cutter": "cutting", "captioner": "captioning", "titler": "titling", "editor": "rendering"}
STAGE_TIMEOUT = 15 * 60
MAX_RESTARTS = 5


class Manager(Agent):
    role = "manager"

    def __init__(self, hub) -> None:
        super().__init__(hub)
        # The Manager owns the production queues so queued jobs survive a worker restart.
        self.queues = {role: asyncio.Queue() for role in PIPELINE}
        self.gave_up: set[str] = set()

    # ---- main loop -----------------------------------------------------
    async def run(self) -> None:
        self.set("working", "Bringing the crew online")
        for cls in CORE:
            self.spawn(cls)
        self.log(f"Crew online — {len(CORE)} agents on duty, watching {len(self.hub.streamers)} streamer(s)")
        last_check = 0.0
        while True:
            try:
                msg = await asyncio.wait_for(self.inbox.get(), timeout=2.0)
            except asyncio.TimeoutError:
                msg = None
            try:
                if msg:
                    await self.handle(msg)
                if time.time() - last_check > 3:
                    last_check = time.time()
                    await self.supervise()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                traceback.print_exc()
                self.log(f"Problem while handling {msg and msg.get('type')}: {exc}", "error")

    def spawn(self, cls, sid: str | None = None, restarts: int = 0) -> Agent:
        agent = cls(self.hub, sid) if sid else cls(self.hub)
        agent.restarts = restarts
        if agent.role in self.queues:
            agent.inbox = self.queues[agent.role]
        return agent.start()

    # ---- messages ------------------------------------------------------
    async def handle(self, msg: dict) -> None:
        kind = msg["type"]
        if kind == "live_start":
            await self.deploy_crew(msg["streamer"])
        elif kind == "live_end":
            await self.recall_crew(msg["streamer"], "stream ended")
        elif kind == "streamer_removed":
            await self.recall_crew(msg["streamer"], "streamer removed")
        elif kind == "streamer_added":
            for role in ("calendar", "scout"):
                if role in self.hub.agents:
                    self.hub.agents[role].poke()
        elif kind == "moment":
            self.on_moment(msg["streamer"], msg["moment"])
        elif kind == "clip_now":
            now = time.time()
            self.on_moment(msg["streamer"], {"start": now - 30, "end": now - 4, "peak_ts": now - 20,
                                             "reason": "manual", "manual": True, "sample": [],
                                             "reaction": "manual", "peak_rate": 0, "base": 0, "z": 0})
        elif kind == "stage_done":
            self.on_stage_done(msg["job"], msg["stage"])
        elif kind == "stage_failed":
            self.on_stage_failed(msg["job"], msg["stage"], msg.get("error", ""))
        elif kind == "restart_agent":
            await self.restart(msg["name"], manual=True)

    async def deploy_crew(self, sid: str) -> None:
        if sid not in self.hub.streamers or f"recorder:{sid}" in self.hub.agents:
            return
        rt = self.hub.rt(sid)
        rt["chat_q"] = asyncio.Queue(maxsize=20000)
        rt["chat"].clear()
        for cls in CREW:
            self.spawn(cls, sid)
        name = self.hub.streamers[sid]["name"]
        self.log(f"{name} is live — deployed Recorder, Chat Monitor and Hype Detector")

    async def recall_crew(self, sid: str, why: str) -> None:
        crew = [a for a in list(self.hub.agents.values()) if a.streamer_id == sid]
        for agent in crew:
            await agent.stop()
        self.hub.rt(sid).pop("chat_q", None)
        if crew:
            name = self.hub.streamers.get(sid, {}).get("name", sid)
            self.log(f"Recalled the watch crew for {name} ({why})")

    # ---- clip jobs -----------------------------------------------------
    def on_moment(self, sid: str, m: dict) -> None:
        st = self.hub.settings
        streamer = self.hub.streamers.get(sid)
        if not streamer or f"recorder:{sid}" not in self.hub.agents:
            return
        name = streamer["name"]
        manual = m.get("manual", False)
        mine = [j for j in self.hub.jobs.values() if j["streamer"] == sid]
        if not manual:
            held = next((j for j in mine if j["status"] == "active" and j.get("hold_until")), None)
            if held and self.merge(held, m):
                return
            recent = [j for j in mine if j["created"] > time.time() - 3600]
            if len(recent) >= st["max_clips_per_hour"]:
                self.log(f"Skipped a moment from {name}: hourly clip limit reached", "warn")
                return
            if any(j["end"] > m["start"] for j in mine):
                self.log(f"Skipped a moment from {name}: already covered by the previous clip")
                return

        # Clip window: lead-in before chat reacted + the reaction + a short tail.
        lo, hi = st["clip_min_s"], max(st["clip_max_s"], st["clip_min_s"])
        start = m["start"] - st["pre_roll_s"]
        end = m["end"] + st["post_roll_s"]
        if manual:
            end = time.time() - 3
            start = end - min(hi, 60)
        if end - start < lo:
            start = end - lo
        if end - start > hi:
            start = max(start, m["start"] - 15)
            end = min(end, start + hi)

        job = {
            "id": f"{time.strftime('%m%d-%H%M%S')}-{sid}",
            "streamer": sid, "streamer_name": name, "platform": streamer["platform"],
            "handle": streamer["handle"], "url": streamer["url"],
            "created": time.time(), "start": start, "end": end,
            "signal": {"reason": m["reason"], "peak_rate": round(m.get("peak_rate", 0), 2),
                       "base": round(m.get("base", 0), 2), "z": round(m.get("z", 0), 1),
                       "reaction": m.get("reaction")},
            "chat_sample": m.get("sample", []),
            "stream_title": self.hub.rt(sid).get("title") or "",
            "category": self.hub.rt(sid).get("category") or "",
            "stage": "queued", "status": "active", "progress": 0, "attempts": {},
            # each window is one part of the story: footage range + when chat started/stopped exploding
            "windows": [{"start": start, "end": end, "moment": m.get("start", start), "moment_end": m.get("end", end)}],
        }
        self.hub.jobs[job["id"]] = job
        if manual:
            self.log(f"Manual clip requested for {name} — sending to the Clip Cutter")
            self.dispatch(job, "cutter")
            return
        sig = job["signal"]
        self.log(f"Clip job opened for {name}: {sig['reason']} "
                 f"({sig['peak_rate']} msg/s vs normal {sig['base']}) — {int(end - start)}s window")
        hold = st.get("merge_window_s", 0)
        if hold > 0:
            # wait a little: if chat explodes again soon, both moments belong in the same clip
            job["hold_until"] = time.time() + hold
            self.hub.push_job(job)
        else:
            self.dispatch(job, "cutter")

    def merge(self, job: dict, m: dict) -> bool:
        """Fold a follow-up moment into a held clip job, so the clip tells the whole story."""
        st = self.hub.settings
        last = job["windows"][-1]
        if m["start"] - last["end"] > st.get("merge_window_s", 150) or len(job["windows"]) >= 3:
            return False
        end = m["end"] + st["post_roll_s"]
        if m["start"] - last["end"] <= 20:
            last["end"], last["moment_end"] = end, m["end"]      # one continuous story
            how = "extended"
        else:
            job["windows"].append({"start": m["start"] - st["pre_roll_s"], "end": end,
                                   "moment": m["start"], "moment_end": m["end"]})
            how = f"joined as part {len(job['windows'])}"
        job["end"] = end
        job["signal"]["peak_rate"] = round(max(job["signal"]["peak_rate"], m.get("peak_rate", 0)), 2)
        job["signal"]["z"] = round(max(job["signal"]["z"], m.get("z", 0)), 1)
        job["chat_sample"] = (job.get("chat_sample") or [])[:8] + m.get("sample", [])[:8]
        job["hold_until"] = time.time() + min(60, st.get("merge_window_s", 150))
        self.log(f"Chat exploded again for {job['streamer_name']} — {how} of the same clip")
        self.hub.push_job(job)
        return True

    def dispatch(self, job: dict, role: str) -> None:
        job["stage"] = role
        job["stage_started"] = None
        job["progress"] = 0
        self.hub.push_job(job)
        self.queues[role].put_nowait(job)

    def on_stage_done(self, job_id: str, stage: str) -> None:
        job = self.hub.jobs.get(job_id)
        if not job or job["status"] != "active":
            return
        nxt = PIPELINE.index(stage) + 1
        if nxt < len(PIPELINE):
            self.dispatch(job, PIPELINE[nxt])
            return
        job.update(stage="done", status="done", progress=1, finished=time.time())
        clip = job.get("clip")
        if clip:
            self.hub.clips.insert(0, clip)
            self.hub.save_clips()
            self.hub.emit("clip", clip)
            self.hub.push_streamer(job["streamer"])
        self.hub.push_job(job)
        shutil.rmtree(WORK / job_id, ignore_errors=True)
        took = int(time.time() - job["created"])
        self.log(f"Clip ready: “{job.get('title', '')}” ({job['streamer_name']}, produced in {took}s)", "success")

    def on_stage_failed(self, job_id: str, stage: str, error: str) -> None:
        job = self.hub.jobs.get(job_id)
        if not job or job["status"] != "active":
            return
        tries = job["attempts"].get(stage, 0) + 1
        job["attempts"][stage] = tries
        if tries < 2:
            self.log(f"{STAGE_LABEL[stage].capitalize()} failed for {job['streamer_name']} ({error}) — retrying", "warn")
            self.dispatch(job, stage)
            return
        job.update(status="failed", error=f"{STAGE_LABEL[stage]}: {error}", finished=time.time())
        self.hub.push_job(job)
        shutil.rmtree(WORK / job_id, ignore_errors=True)
        self.log(f"Gave up on a clip for {job['streamer_name']} — {job['error']}", "error")

    # ---- supervision ---------------------------------------------------
    async def restart(self, name: str, manual: bool = False) -> None:
        agent = self.hub.agents.get(name)
        if not agent or agent is self:
            return
        restarts = 0 if manual or time.time() - agent.started > 600 else agent.restarts + 1
        await agent.stop()
        self.gave_up.discard(name)
        self.spawn(type(agent), agent.streamer_id, restarts)
        self.log(f"Restarted {name}" + (" on request" if manual else f" after a crash (attempt {restarts})"),
                 "info" if manual else "warn")

    async def supervise(self) -> None:
        hub = self.hub
        # 1. Restart anything that died.
        for name, agent in list(hub.agents.items()):
            if agent is self or not agent.dead or agent.stopping:
                continue
            if agent.restarts >= MAX_RESTARTS and time.time() - agent.started < 600:
                if name not in self.gave_up:
                    self.gave_up.add(name)
                    self.log(f"{name} keeps crashing — leaving it stopped. Use Restart on its card to retry.", "error")
                continue
            await self.restart(name)

        # 2. Core agents must always exist.
        for cls in CORE:
            if cls.role not in hub.agents:
                self.spawn(cls)

        # 3. Release held clip jobs once no follow-up moment arrived in time.
        now = time.time()
        for job in list(hub.jobs.values()):
            if job["status"] == "active" and job.get("hold_until") and now >= job["hold_until"]:
                job.pop("hold_until")
                self.dispatch(job, "cutter")

        # 4. A production step that hangs gets its worker replaced.
        for job in list(hub.jobs.values()):
            if job["status"] == "active" and job.get("stage_started") and now - job["stage_started"] > STAGE_TIMEOUT:
                stage = job["stage"]
                job["stage_started"] = None
                if stage in hub.agents:
                    await self.restart(stage)
                self.on_stage_failed(job["id"], stage, "step timed out")

        # 4. Housekeeping: forget old jobs, delete footage nobody needs any more.
        finished = sorted((j for j in hub.jobs.values() if j["status"] != "active"),
                          key=lambda j: j.get("finished", 0))
        for job in finished[:-15]:
            del hub.jobs[job["id"]]
            hub.emit("job_gone", {"id": job["id"]})
        for folder in BUFFER.iterdir():
            sid = folder.name
            cutting = any(j["streamer"] == sid and j["status"] == "active" and j["stage"] in ("queued", "cutter")
                          for j in hub.jobs.values())
            if folder.is_dir() and f"recorder:{sid}" not in hub.agents and not cutting:
                shutil.rmtree(folder, ignore_errors=True)

        # 5. Status line.
        live = sum(1 for sid in hub.streamers if hub.rt(sid).get("live"))
        active = sum(1 for j in hub.jobs.values() if j["status"] == "active")
        today = time.strftime("%Y%m%d")
        clips_today = sum(1 for c in hub.clips if time.strftime("%Y%m%d", time.localtime(c["created"])) == today)
        if active:
            task = f"Running {active} clip job(s) through production"
        elif live:
            task = f"Watching {live} live stream(s) for clippable moments"
        elif hub.streamers:
            task = "All quiet — waiting for a streamer to go live"
        else:
            task = "Add a streamer to get started"
        self.set("working" if (live or active) else "idle", task,
                 agents=len(hub.agents), live=live, jobs=active, clips_today=clips_today)
