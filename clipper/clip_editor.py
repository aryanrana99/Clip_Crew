"""Vault editor: restyle a finished clip (title, captions, fonts, colours, sizes, background, trim) and re-render it.

Every clip keeps its clean footage in data/sources/<id>/ (raw.mp4 + meta.json with word timings).
Clips made before that existed are prepared on first edit: the stream area is cut back out of the finished
video (it carries no burned-in text) and the audio is transcribed again for captions.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

from . import story
from .agents.production import EMOJI, FFMPEG_QUIET, ass_text, ass_time, caption_chunks, transcribe_wav
from .config import CLIPS, FONTS, SOURCES
from .util import ffmpeg_bin, ffprobe_bin, kill, run_cmd, slug, spawn

FONT_CHOICES = ["Arial Black", "Impact", "Gondens DEMO", "Bahnschrift", "Segoe UI Black", "Georgia",
                "Trebuchet MS", "Verdana", "Comic Sans MS", "Consolas"]
BACKGROUNDS = {"blur", "blur-dark", "solid"}
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")

DEFAULT_STYLE = {
    # sizes are the visible (em) size in px on the 1080x1920 frame, the same in every font;
    # 51 / 60 in Arial Black match the size the Video Editor uses for new clips
    "title": "", "title_font": "Arial Black", "title_size": 51, "title_color": "#141414",
    "title_box": True, "title_box_color": "#FFFFFF", "title_pos": "above",
    "captions": True, "cap_font": "Arial Black", "cap_size": 60, "cap_color": "#FFFFFF",
    "cap_highlight": "#FFE500", "cap_outline": "#000000", "cap_upper": True,
    "background": "blur", "bg_color": "#1F3635", "zoom": 1.0, "trim_start": 0.0, "trim_end": None,
}


_SCALE_CACHE: dict[str, float] = {}


def _font_file(name: str) -> Path | None:
    if name == "Gondens DEMO":
        return FONTS / "gondens.otf"
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
        i = 0
        while True:
            label, value, _ = winreg.EnumValue(key, i)
            i += 1
            base = re.sub(r"\s*\((TrueType|OpenType)\)$", "", label)
            if name in [b.strip() for b in base.split("&")]:
                p = Path(value)
                return p if p.is_absolute() else Path(r"C:\Windows\Fonts") / p
    except OSError:
        return None


def font_scale(name: str) -> float:
    """CSS em size / libass font size. libass sizes a font by its full win ascent+descent, CSS by its em."""
    if name in _SCALE_CACHE:
        return _SCALE_CACHE[name]
    scale = 0.75
    try:
        import struct
        data = _font_file(name).read_bytes()
        base = struct.unpack(">I", data[12:16])[0] if data[:4] == b"ttcf" else 0
        tables = {}
        for i in range(struct.unpack(">H", data[base + 4:base + 6])[0]):
            tag, _, off, _ = struct.unpack(">4sIII", data[base + 12 + 16 * i:base + 28 + 16 * i])
            tables[tag] = off
        upem = struct.unpack(">H", data[tables[b"head"] + 18:tables[b"head"] + 20])[0]
        win_asc, win_desc = struct.unpack(">HH", data[tables[b"OS/2"] + 74:tables[b"OS/2"] + 78])
        if win_asc + win_desc:
            scale = upem / (win_asc + win_desc)
    except Exception:
        pass
    _SCALE_CACHE[name] = round(scale, 4)
    return _SCALE_CACHE[name]


def ass_color(hex_color: str, alpha: int = 0) -> str:
    h = hex_color.lstrip("#")
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def clean_style(raw: dict, meta: dict, clip: dict) -> dict:
    st = dict(DEFAULT_STYLE)
    st["title"] = clip.get("title", "")
    st.update(clip.get("style") or {})
    for key, value in (raw or {}).items():
        if key not in DEFAULT_STYLE:
            continue
        default = DEFAULT_STYLE[key]
        try:
            if key.endswith("_font"):
                if value in FONT_CHOICES:
                    st[key] = value
            elif key.endswith("color") or key in ("cap_highlight", "cap_outline"):
                if isinstance(value, str) and HEX.match(value):
                    st[key] = value.upper()
            elif isinstance(default, bool):
                st[key] = bool(value)
            elif key == "background":
                if value in BACKGROUNDS:
                    st[key] = value
            elif key == "title_pos":
                if value in ("above", "top", "center"):
                    st[key] = value
            elif key == "title":
                st[key] = " ".join(str(value).split())[:90]
            elif key == "trim_end":
                st[key] = None if value in (None, "") else float(value)
            else:
                st[key] = float(value)
        except (TypeError, ValueError):
            continue
    full = float(meta["duration"])
    st["title_size"] = int(max(28, min(120, st["title_size"])))
    st["cap_size"] = int(max(30, min(110, st["cap_size"])))
    st["zoom"] = round(max(0.7, min(1.6, st["zoom"])), 2)
    st["trim_start"] = round(max(0.0, min(st["trim_start"], full - 5)), 2)
    end = full if st["trim_end"] is None else st["trim_end"]
    st["trim_end"] = round(max(st["trim_start"] + 5, min(end, full)), 2)
    return st


def title_outline(size: int, box: bool) -> int:
    """Box padding (box on) or stroke width (box off), in frame px. 14 at the default size, like new clips."""
    return max(8, round(size * 0.28)) if box else max(3, round(size * 0.1))


def cap_outline(size: int) -> int:
    return max(3, round(size * 0.12))


def layout(meta: dict, style: dict) -> tuple[int, int, int]:
    fg_w = int(1080 * meta.get("base_zoom", 1.0) * style["zoom"] / 2) * 2
    fg_h = min(int(meta["src_h"] * fg_w / meta["src_w"] / 2) * 2, 1300)
    return fg_w, fg_h, (1920 - fg_h) // 2


def build_styled_ass(meta: dict, style: dict, clip: dict, fg_top: int, fg_bottom: int,
                     cap_y: int | None = None, credit_left: bool = False) -> str:
    title = EMOJI.sub("", style["title"] or "").strip()
    size = style["title_size"]
    lines = [ass_text(title)] if title else []   # libass wraps it (WrapStyle 0), same as the browser preview
    t_primary = ass_color(style["title_color"])
    t_box = ass_color(style["title_box_color"])
    t_outline = title_outline(size, style["title_box"])
    t_border = 3 if style["title_box"] else 1
    if style["title_pos"] == "top":
        t_align, t_margin = 8, 150
    elif style["title_pos"] == "center":
        t_align, t_margin = 5, 0
    else:  # just above the stream
        t_align, t_margin = 2, 1920 - fg_top + 46
    c_size = style["cap_size"]
    # libass sizes fonts by line height, so convert the visible size into that font's ASS size
    t_ass = round(size / font_scale(style["title_font"]))
    c_ass = round(c_size / font_scale(style["cap_font"]))
    credit = ("twitch.tv/" + meta["handle"]) if meta["platform"] == "twitch" else "youtube.com/" + meta["handle"]
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title,{style['title_font']},{t_ass},{t_primary},{t_primary},{t_box},{t_box},0,0,0,0,100,100,0,0,{t_border},{t_outline},0,{t_align},60,60,{t_margin},1
Style: Cap,{style['cap_font']},{c_ass},{ass_color(style['cap_color'])},{ass_color(style['cap_color'])},{ass_color(style['cap_outline'])},&H96000000,0,0,0,0,100,100,0,0,1,{cap_outline(c_size)},3,8,60,60,{cap_y or fg_bottom + 70},1
Style: Credit,Arial,34,&H50FFFFFF,&H50FFFFFF,&H78000000,&H78000000,1,0,0,0,100,100,0,0,1,2,0,{3 if credit_left else 2},60,{50 if credit_left else 60},{68 if credit_left else 110},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    t0, length = style["trim_start"], style["trim_end"] - style["trim_start"]
    end = ass_time(length + 1)
    events = [f"Dialogue: 0,0:00:00.00,{end},Credit,,0,0,0,,{ass_text(credit)}"]
    if lines:
        events.append(f"Dialogue: 1,0:00:00.00,{end},Title,,0,0,0,," + r"\N".join(lines))
    if style["captions"]:
        hl = "{\\c" + ass_color(style["cap_highlight"]) + "&}"
        base = "{\\c" + ass_color(style["cap_color"]) + "&}"
        words = [{"w": w["w"], "s": w["s"] - t0, "e": w["e"] - t0} for w in meta.get("words") or []
                 if w["e"] > t0 and w["s"] < style["trim_end"]]
        chunks = caption_chunks(words)
        for ci, chunk in enumerate(chunks):
            nxt = chunks[ci + 1][0]["s"] if ci + 1 < len(chunks) else 1e9
            chunk_end = min(chunk[-1]["e"] + 0.3, nxt)
            for i, word in enumerate(chunk):
                start = max(0.0, word["s"])
                stop = chunk[i + 1]["s"] if i + 1 < len(chunk) else chunk_end
                if stop <= start:
                    stop = start + 0.08
                parts = []
                for k, w in enumerate(chunk):
                    txt = ass_text(w["w"].upper() if style["cap_upper"] else w["w"])
                    parts.append(f"{hl}{txt}{base}" if k == i else txt)
                pop = r"{\fscx86\fscy86\t(0,90,\fscx100\fscy100)}" if i == 0 else ""
                events.append(f"Dialogue: 2,{ass_time(start)},{ass_time(stop)},Cap,,0,0,0,,{pop}{' '.join(parts)}")
    return head + "\n".join(events) + "\n"


async def remove_files(paths: list, tries: int = 6) -> list:
    """Delete files, retrying briefly while Windows still has them locked. Returns what is left."""
    left = list(paths)
    for attempt in range(tries):
        still = []
        for p in left:
            try:
                p.unlink(missing_ok=True)
            except PermissionError:
                still.append(p)
        left = still
        if not left:
            break
        await asyncio.sleep(0.25 * (attempt + 1))
    return left


async def remove_later(hub, paths: list) -> None:
    for _ in range(60):
        await asyncio.sleep(10)
        done = list(paths)
        paths = await remove_files(paths, tries=1)
        for p in done:   # drop a clip's emptied source folder too
            if p.parent.parent == SOURCES:
                try:
                    p.parent.rmdir()
                except OSError:
                    pass
        if not paths:
            return
    hub.log("system", f"Could not delete {', '.join(p.name for p in paths)}: the file is still open in another program",
            "warn")


class ClipEditor:
    def __init__(self, hub) -> None:
        self.hub = hub
        self.busy: dict[str, dict] = {}   # clip id -> {"status", "progress"}
        self.locks: dict[str, asyncio.Lock] = {}

    def clip(self, clip_id: str) -> dict:
        clip = next((c for c in self.hub.clips if c["id"] == clip_id), None)
        if not clip:
            raise KeyError("Unknown clip")
        return clip

    def status(self, clip_id: str, status: str, progress: float | None = None, error: str = "") -> None:
        self.busy[clip_id] = {"status": status, "progress": progress, "error": error}
        self.hub.emit("clip_edit", {"id": clip_id, **self.busy[clip_id]})

    # --------------------------------------------------------- sources
    async def prepare(self, clip_id: str) -> dict:
        """Make sure the clean footage and word timings exist. Returns meta (+ style, fonts) for the editor."""
        clip = self.clip(clip_id)
        lock = self.locks.setdefault(clip_id, asyncio.Lock())
        async with lock:
            folder = SOURCES / clip_id
            meta_file = folder / "meta.json"
            if not (meta_file.exists() and (folder / "raw.mp4").exists()):
                await self.rebuild_source(clip, folder)
            meta = json.loads(meta_file.read_text("utf-8"))
            if "facecam" not in meta and not meta.get("legacy") and self.hub.settings.get("facecam_layout", True):
                # footage saved before the webcam layout existed: find the webcam once and remember it
                meta["facecam"] = await asyncio.to_thread(story.facecam_for, clip.get("streamer", ""),
                                                          folder / "raw.mp4", float(meta["duration"]))
                meta.setdefault("grain", int(self.hub.settings.get("film_grain", 0)))
                meta_file.write_text(json.dumps(meta), "utf-8")
        style = clean_style({}, meta, clip)
        return {"id": clip_id, "meta": meta, "style": style, "fonts": FONT_CHOICES,
                "font_scale": {f: font_scale(f) for f in FONT_CHOICES + ["Arial"]},
                "source": f"/sources/{clip_id}/raw.mp4?v={int(meta_file.stat().st_mtime)}",
                "legacy": meta.get("legacy", False)}

    async def rebuild_source(self, clip: dict, folder: Path) -> None:
        """Older clip without saved footage: cut the clean stream band back out and re-transcribe it."""
        video = CLIPS / clip["file"]
        if not video.exists():
            raise FileNotFoundError("The clip's video file is missing.")
        folder.mkdir(parents=True, exist_ok=True)
        self.status(clip["id"], "preparing", None)
        zoom = max(1.0, float(self.hub.settings.get("layout_zoom", 1.25)))
        # The original stream was 16:9; reproduce where the Video Editor placed it in the 1080x1920 frame.
        fg_w = int(1080 * zoom / 2) * 2
        fg_h = min(int(1080 * fg_w / 1920 / 2) * 2, 1300)
        fg_top = (1920 - fg_h) // 2
        rc, _, err = await run_cmd([ffmpeg_bin(), *FFMPEG_QUIET, "-i", str(video),
                                    "-vf", f"crop=1080:{fg_h}:0:{fg_top}", "-c:v", "libx264", "-preset", "veryfast",
                                    "-crf", "16", "-c:a", "copy", "-movflags", "+faststart", "raw.mp4"],
                                   timeout=300, cwd=folder)
        if rc != 0:
            raise RuntimeError(f"Could not extract the footage: {err.strip()[-200:]}")
        rc, out, _ = await run_cmd([ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
                                    "-of", "json", "raw.mp4"], timeout=30, cwd=folder)
        duration = float(json.loads(out)["format"]["duration"])
        await run_cmd([ffmpeg_bin(), *FFMPEG_QUIET, "-i", "raw.mp4", "-vn", "-ac", "1", "-ar", "16000",
                       "-c:a", "pcm_s16le", "audio.wav"], timeout=120, cwd=folder)
        words = []
        if (folder / "audio.wav").exists():
            self.status(clip["id"], "transcribing", None)
            try:
                words, _ = await asyncio.to_thread(transcribe_wav, str(folder / "audio.wav"),
                                                   self.hub.settings["whisper_model"],
                                                   self.hub.settings["caption_language"])
            except Exception as exc:
                self.hub.log("editor", f"Couldn't transcribe the clip for editing ({exc}); captions will be off", "warn")
            (folder / "audio.wav").unlink(missing_ok=True)
        handle = clip.get("url", "").rstrip("/").split("/")[-1] or clip.get("streamer_name", "")
        (folder / "meta.json").write_text(json.dumps({
            "words": words, "src_w": 1080, "src_h": fg_h, "duration": round(duration, 2), "base_zoom": 1.0,
            "handle": handle, "platform": clip.get("platform", "twitch"), "legacy": True,
        }), "utf-8")
        self.status(clip["id"], "ready", None)

    # ---------------------------------------------------------- render
    async def render(self, clip_id: str, raw_style: dict) -> None:
        clip = self.clip(clip_id)
        if self.busy.get(clip_id, {}).get("status") == "rendering":
            raise RuntimeError("This clip is already being rendered.")
        info = await self.prepare(clip_id)
        meta = info["meta"]
        style = clean_style(raw_style, meta, clip)
        self.status(clip_id, "rendering", 0.0)
        asyncio.create_task(self.run_render(clip, meta, style))
        return style

    async def run_render(self, clip: dict, meta: dict, style: dict) -> None:
        clip_id = clip["id"]
        folder = SOURCES / clip_id
        work = folder / "edit"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        try:
            fps = self.hub.settings["fps"]
            grain = int(meta.get("grain", self.hub.settings.get("film_grain", 0)))
            lay = story.layout_graph(meta["src_w"], meta["src_h"], meta.get("base_zoom", 1.0) * style["zoom"],
                                     meta.get("facecam"), fps, grain, style["background"], style["bg_color"])
            (work / "overlay.ass").write_text(build_styled_ass(meta, style, clip, lay["fg_top"], lay["fg_bottom"],
                                                               lay["cap_y"], lay["credit_left"]), "utf-8")
            fonts = work / "fonts"
            fonts.mkdir()
            for f in FONTS.glob("*.otf"):
                shutil.copy(f, fonts / f.name)
            graph = lay["graph"] + ";[base]ass=overlay.ass:fontsdir=fonts,format=yuv420p[v]"
            length = style["trim_end"] - style["trim_start"]
            args = [ffmpeg_bin(), *FFMPEG_QUIET, "-progress", "pipe:1", "-nostats",
                    "-ss", f"{style['trim_start']:.2f}", "-i", str(folder / "raw.mp4"),
                    "-filter_complex", graph, "-map", "[v]", "-map", "0:a:0?", "-t", f"{length:.2f}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-maxrate", "9M", "-bufsize", "18M",
                    "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart", "final.mp4"]
            rc = await asyncio.to_thread(self.ffmpeg, args, work, length, clip_id)
            if rc != 0:
                detail = (work / "render.log").read_text("utf-8", "replace").strip()[-300:]
                raise RuntimeError(f"ffmpeg failed: {detail or rc}")
            await run_cmd([ffmpeg_bin(), *FFMPEG_QUIET, "-ss", f"{min(3.0, length / 2):.1f}", "-i", "final.mp4",
                           "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4", "thumb.jpg"], timeout=60, cwd=work)

            # new file names so browsers don't show the cached old version
            old = [CLIPS / clip["file"], CLIPS / clip.get("thumb", ""), CLIPS / (clip["file"].rsplit(".", 1)[0] + ".json")]
            stem = re.sub(r"-e\d+$", "", clip["file"].rsplit(".", 1)[0])
            stem = f"{stem.split('_', 2)[0]}_{slug(clip.get('streamer_name', 'clip'), 24)}_{slug(style['title'] or 'clip', 40)}"
            base = f"{stem}-e{int(time.time()) % 100000}"
            shutil.move(str(work / "final.mp4"), CLIPS / f"{base}.mp4")
            if (work / "thumb.jpg").exists():
                shutil.move(str(work / "thumb.jpg"), CLIPS / f"{base}.jpg")
            clip.update(file=f"{base}.mp4", thumb=f"{base}.jpg", title=style["title"], style=style,
                        duration=round(length, 1), edited=time.time(), captions=bool(style["captions"] and meta["words"]),
                        size_mb=round((CLIPS / f"{base}.mp4").stat().st_size / 1e6, 1))
            (CLIPS / f"{base}.json").write_text(json.dumps(clip, indent=2, ensure_ascii=False), "utf-8")
            self.hub.save_clips()
            left = await remove_files([p for p in old if p.name and p.exists() and p.name != f"{base}.mp4"])
            if left:
                asyncio.create_task(remove_later(self.hub, left))
            self.hub.emit("clip_update", clip)
            self.status(clip_id, "done", 1.0)
            self.hub.log("editor", f"Re-rendered “{style['title']}” with your edits")
        except Exception as exc:
            self.status(clip_id, "failed", None, str(exc)[:300])
            self.hub.log("editor", f"Couldn't re-render the clip: {exc}", "error")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def ffmpeg(self, args: list[str], cwd: Path, duration: float, clip_id: str) -> int:
        with open(cwd / "render.log", "wb") as errlog:
            proc = spawn(args, cwd=cwd, stdout=subprocess.PIPE, stderr=errlog)
            last = 0.0
            try:
                for raw in proc.stdout:
                    line = raw.decode("ascii", "ignore").strip()
                    if line.startswith("out_time_us=") and time.time() - last > 0.6:
                        last = time.time()
                        try:
                            frac = min(0.99, int(line.split("=")[1]) / 1e6 / max(1.0, duration))
                        except ValueError:
                            continue
                        self.hub.loop.call_soon_threadsafe(self.status, clip_id, "rendering", round(frac, 2))
                return proc.wait()
            finally:
                kill(proc)
