"""Web server: dashboard, REST controls and the live event stream."""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import unquote
import hmac
import os
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import shutil

from .agents.manager import Manager
from .clip_editor import ClipEditor, remove_files, remove_later
from .config import CLIPS, DATA, ROOT, SOURCES, WEB, load_env
from .hub import Hub
from .manager_chat import ManagerChat

load_env()
hub = Hub()
hub.chat = ManagerChat(hub)
editor = ClipEditor(hub)


async def watchdog() -> None:
    """Keeps the Manager itself alive and sends a heartbeat to the dashboard."""
    while True:
        await asyncio.sleep(5)
        if hub.manager.dead and not hub.manager.stopping:
            crashes = hub.manager.restarts + 1
            await hub.manager.stop()
            hub.manager = Manager(hub)
            hub.manager.restarts = crashes
            hub.manager.start()
            hub.log("system", "The Manager stopped unexpectedly and was restarted", "warn")
        hub.emit("tick", {"now": time.time()})


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub.loop = asyncio.get_running_loop()
    hub.manager = Manager(hub)
    hub.manager.start()
    dog = asyncio.create_task(watchdog())
    yield
    dog.cancel()
    await hub.manager.stop()
    for agent in list(hub.agents.values()):
        await agent.stop()


app = FastAPI(title="ClipCrew", lifespan=lifespan)

# ── sharing: visitors coming through the public tunnel need the secret key from the share link ──
ACCESS_COOKIE = "clipcrew_access"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
DENIED_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ClipCrew</title><body style="margin:0;height:100vh;display:grid;place-items:center;background:#1F3635;color:#FCFBF4;
font:16px/1.5 system-ui,sans-serif;text-align:center"><div><div style="font-size:42px;font-weight:800;color:#CACE00">ClipCrew</div>
<p id="m">This crew is private.<br>Ask the person who shared it for the full link.</p></div>
<script>
// came here through the share link before but the cookie didn't stick? retry once with the saved key
var k = null; try { k = localStorage.getItem("clipcrew_key"); } catch (e) {}
var tried = false; try { tried = sessionStorage.getItem("clipcrew_retry") === "1"; } catch (e) {}
if (k && !tried) { try { sessionStorage.setItem("clipcrew_retry", "1"); } catch (e) {} location.replace("/k/" + encodeURIComponent(k)); }
else if (k) { document.getElementById("m").innerHTML = "Your browser is blocking cookies for this site.<br>Open the link in Safari or Chrome instead of inside the app."; }
</script></body>"""

# Returned for /k/<key>: sets the cookie on a normal page (iPhone browsers can drop cookies set on a redirect
# when the link was opened from another app such as WhatsApp), keeps a backup copy of the key, then moves on.
WELCOME_PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="1;url=/"><title>ClipCrew</title>
<body style="margin:0;height:100vh;display:grid;place-items:center;background:#1F3635;color:#FCFBF4;font:16px system-ui,sans-serif">
<div style="font-size:42px;font-weight:800;color:#CACE00">ClipCrew</div>
<script>try { localStorage.setItem("clipcrew_key", __KEY__); sessionStorage.removeItem("clipcrew_retry"); } catch (e) {}
setTimeout(function () { location.replace("/"); }, 150);</script></body>"""


def access_key() -> str:
    """The secret in the share link. Created on first run and kept in .env."""
    key = os.environ.get("CLIPCREW_ACCESS_KEY", "").strip()
    if not key:
        key = secrets.token_urlsafe(12)
        with open(ROOT / ".env", "a", encoding="utf-8") as fh:
            fh.write(f"\n# secret part of the link you share with friends (change it to revoke old links)\nCLIPCREW_ACCESS_KEY={key}\n")
        os.environ["CLIPCREW_ACCESS_KEY"] = key
    return key


ACCESS_KEY = access_key()


TUNNEL_HEADERS = ("cf-connecting-ip", "cf-ray", "x-forwarded-for", "x-forwarded-host", "forwarded")


def is_local(headers) -> bool:
    """A visit typed on this PC (http://127.0.0.1:8765). Anything that came through a tunnel is not local,
    even if the tunnel rewrote the Host header."""
    if any(h in headers for h in TUNNEL_HEADERS):
        return False
    host_header = headers.get("host", "")
    host = host_header.rsplit(":", 1)[0] if not host_header.startswith("[") else host_header.split("]")[0] + "]"
    return host in LOCAL_HOSTS


def has_access(cookie: str | None) -> bool:
    return bool(cookie) and hmac.compare_digest(cookie, ACCESS_KEY)


@app.middleware("http")
async def require_share_key(request: Request, call_next):
    if is_local(request.headers):
        return await call_next(request)
    # temporary publishing access: while data/publish_token.txt exists, /media/ files open with ?t=<that token>
    # (lets Instagram fetch a finished video; delete the file to close it again)
    if request.url.path.startswith("/media/") and request.method in ("GET", "HEAD"):
        token_file = DATA / "publish_token.txt"
        given = request.query_params.get("t") or ""
        if given and token_file.exists():
            token = token_file.read_text("utf-8").strip()
            if token and hmac.compare_digest(given, token):
                return await call_next(request)
    # the key arrives in the path (/k/<key>, survives Serveo's warning page, which drops query strings) or as ?key=
    path = request.url.path
    raw = unquote(path[3:]) if path.startswith("/k/") else (request.query_params.get("key") or "")
    # keep only the key's own characters: chat apps sometimes glue on "**", ")", "." or a space
    m = re.match(r"[A-Za-z0-9_-]+", raw.strip())
    key = m.group(0) if m else ""
    if key and hmac.compare_digest(key, ACCESS_KEY):
        # remember this browser on a normal 200 page (not a redirect), then drop the key from the address bar
        log_visit(request, "key link")
        response = HTMLResponse(WELCOME_PAGE.replace("__KEY__", json.dumps(ACCESS_KEY)),
                                headers={"Cache-Control": "no-store"})
        response.set_cookie(ACCESS_COOKIE, ACCESS_KEY, max_age=60 * 60 * 24 * 365, httponly=True,
                            secure=True, samesite="lax")
        return response
    if has_access(request.cookies.get(ACCESS_COOKIE)):
        return await call_next(request)
    if request.url.path.startswith(("/api/", "/media/", "/sources/")):
        return JSONResponse({"detail": "This ClipCrew is private."}, status_code=403)
    if not request.url.path.startswith(("/static/", "/favicon")):
        log_visit(request, "denied" + (f" (wrong key: {raw[:6]}...)" if raw else ""))
    return HTMLResponse(DENIED_PAGE, status_code=403, headers={"Cache-Control": "no-store"})


def log_visit(request: Request, what: str) -> None:
    """One line per share-link visit in data/access.log, to see why a friend's browser didn't get in."""
    try:
        line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {what:<20} path={request.url.path[:3]}...  "
                f"cookie={'yes' if request.cookies.get(ACCESS_COOKIE) else 'no'}  "
                f"ua={request.headers.get('user-agent', '')[:120]}\n")
        with open(DATA / "access.log", "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
app.mount("/media", StaticFiles(directory=CLIPS), name="media")
app.mount("/sources", StaticFiles(directory=SOURCES), name="sources")
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/state")
async def state():
    return hub.snapshot()


@app.post("/api/streamers")
async def add_streamer(body: dict = Body(...)):
    try:
        return hub.add_streamer(body.get("channel", ""), body.get("platform", ""))
    except LookupError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/streamers/{sid}")
async def remove_streamer(sid: str):
    try:
        hub.remove_streamer(sid)
    except KeyError:
        raise HTTPException(404, "Unknown streamer")
    return {"ok": True}


@app.post("/api/streamers/{sid}/toggle")
async def toggle_streamer(sid: str):
    if sid not in hub.streamers:
        raise HTTPException(404, "Unknown streamer")
    s = hub.streamers[sid]
    s["enabled"] = not s.get("enabled", True)
    hub.save_streamers()
    hub.rt(sid)["next_check"] = 0
    hub.manager.send({"type": "streamer_added", "streamer": sid})
    hub.push_streamer(sid)
    hub.log("system", f"{'Resumed' if s['enabled'] else 'Paused'} watching {s['name']}")
    return {"enabled": s["enabled"]}


@app.post("/api/streamers/{sid}/check")
async def check_streamer(sid: str):
    if sid not in hub.streamers:
        raise HTTPException(404, "Unknown streamer")
    hub.rt(sid)["next_check"] = 0
    hub.manager.send({"type": "streamer_added", "streamer": sid})
    return {"ok": True}


@app.post("/api/streamers/{sid}/clip")
async def clip_now(sid: str):
    if f"recorder:{sid}" not in hub.agents:
        raise HTTPException(409, "That streamer isn't being recorded right now.")
    hub.manager.send({"type": "clip_now", "streamer": sid})
    return {"ok": True}


@app.post("/api/agents/{name}/restart")
async def restart_agent(name: str):
    if name not in hub.agents or name == "manager":
        raise HTTPException(404, "Unknown agent")
    hub.manager.send({"type": "restart_agent", "name": name})
    return {"ok": True}


@app.put("/api/settings")
async def update_settings(body: dict = Body(...)):
    hub.apply_settings(body)
    return hub.settings


# ── requirements chat with the Manager ──
@app.get("/api/manager/chat")
async def chat_view():
    return hub.chat.view()


@app.post("/api/manager/chat/start")
async def chat_start():
    return hub.chat.start()


@app.post("/api/manager/chat/message")
async def chat_message(body: dict = Body(...)):
    try:
        return await hub.chat.message(str(body.get("text", "")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/manager/chat/end")
async def chat_end():
    return hub.chat.end()


@app.delete("/api/manager/requirements/{rid}")
async def drop_requirement(rid: str):
    before = len(hub.chat.requirements)
    hub.chat.requirements = [r for r in hub.chat.requirements if r["id"] != rid]
    if len(hub.chat.requirements) == before:
        raise HTTPException(404, "Unknown requirement")
    hub.chat.save_requirements()
    return {"ok": True}


@app.delete("/api/clips/{clip_id}")
async def delete_clip(clip_id: str):
    clip = next((c for c in hub.clips if c["id"] == clip_id), None)
    if not clip:
        raise HTTPException(404, "Unknown clip")
    hub.clips.remove(clip)
    hub.save_clips()
    hub.emit("clip_gone", {"id": clip_id})
    stem = clip["file"].rsplit(".", 1)[0]
    targets = [(CLIPS / f"{stem}.{ext}").resolve() for ext in ("mp4", "jpg", "json")]
    src = (SOURCES / clip_id).resolve()
    if src.parent == SOURCES.resolve() and src.is_dir():
        targets += [p for p in src.rglob("*") if p.is_file()]
    leftover = await remove_files([t for t in targets if t.parent in (CLIPS.resolve(), src) or src in t.parents])
    if leftover:
        # Something (often a video player) still has the file open. Keep trying in the background.
        asyncio.create_task(remove_later(hub, leftover))
    shutil.rmtree(src, ignore_errors=True)
    return {"ok": True, "pending": [p.name for p in leftover]}


# ── vault editor ──
@app.get("/api/clips/{clip_id}/editor")
async def clip_editor_open(clip_id: str):
    try:
        return await editor.prepare(clip_id)
    except KeyError:
        raise HTTPException(404, "Unknown clip")
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/clips/{clip_id}/render")
async def clip_editor_render(clip_id: str, body: dict = Body(...)):
    try:
        style = await editor.render(clip_id, body)
    except KeyError:
        raise HTTPException(404, "Unknown clip")
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True, "style": style}


@app.websocket("/ws")
async def events(sock: WebSocket):
    if not is_local(sock.headers) and not has_access(sock.cookies.get(ACCESS_COOKIE)):
        await sock.close(code=1008)
        return
    await sock.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=3000)
    hub.clients.add(queue)
    try:
        await sock.send_json({"type": "snapshot", "data": hub.snapshot()})
        while True:
            await sock.send_json(await queue.get())
    except (WebSocketDisconnect, RuntimeError, ConnectionError):
        pass
    finally:
        hub.clients.discard(queue)
