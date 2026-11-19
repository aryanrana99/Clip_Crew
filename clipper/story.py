"""Story tools for the production line.

- find_facecam: where the streamer's webcam sits in the stream picture (numpy only, no extra installs)
- scene_cuts:   hard cuts in the footage, used to start/end a clip on a clean shot
- plan_story:   pick the part of the raw footage that tells one story (setup -> moment -> reaction)
- local_title:  a title that names what happened in the game, built from the transcript and chat
"""
from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path

import numpy as np

from .config import DATA, load_json, save_json
from .util import ffmpeg_bin

FACECAMS = DATA / "facecams.json"   # last good webcam box per streamer (fractions of the frame)
GW, GH = 192, 108                   # analysis resolution


# ───────────────────────────── facecam ─────────────────────────────
def _frames(video: Path, duration: float, count: int = 28, size: tuple[int, int] = (GW, GH)) -> np.ndarray:
    w, h = size
    rate = max(0.05, count / max(duration, 1.0))
    proc = subprocess.run([ffmpeg_bin(), "-v", "error", "-skip_frame", "nokey", "-i", str(video),
                           "-vf", f"fps={rate:.4f},scale={w}:{h}:flags=area", "-frames:v", str(count),
                           "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                          capture_output=True, timeout=120,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    data = np.frombuffer(proc.stdout, dtype=np.uint8)
    n = len(data) // (w * h * 3)
    return data[: n * w * h * 3].reshape(n, h, w, 3).astype(np.float32)


def _skin(frames: np.ndarray) -> np.ndarray:
    r, g, b = frames[..., 0], frames[..., 1], frames[..., 2]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cr = (r - y) * 0.713 + 128
    cb = (b - y) * 0.564 + 128
    return (y > 50) & (cr > 136) & (cr < 175) & (cb > 80) & (cb < 128) & (r > g) & (r > b)


def _blobs(mask: np.ndarray) -> list[tuple[int, int, int, int, int]]:
    """Connected components (4-way) of a small boolean mask -> (x0, y0, x1, y1, area)."""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    out = []
    for y in range(h):
        for x in range(w):
            if mask[y, x] and not seen[y, x]:
                stack, xs, ys = [(y, x)], [], []
                seen[y, x] = True
                while stack:
                    cy, cx = stack.pop()
                    xs.append(cx)
                    ys.append(cy)
                    for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                        if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            stack.append((ny, nx))
                out.append((min(xs), min(ys), max(xs) + 1, max(ys) + 1, len(xs)))
    return out


def detect_facecam(video: Path, duration: float) -> tuple[float, float, float, float] | None:
    """Box (x, y, w, h as fractions of the frame) around the streamer's webcam, or None if there isn't one.

    A webcam face stays in the same spot for the whole clip and sits near the left or right edge,
    while faces in the game move around. Around the face, a webcam's room barely changes from frame to
    frame while gameplay does, so the box is grown over the still area until it meets moving gameplay."""
    frames = _frames(video, duration)
    if len(frames) < 8:
        return None
    skin_rate = _skin(frames).mean(axis=0)
    motion = np.abs(np.diff(frames.mean(axis=3), axis=0)).mean(axis=0)   # per-pixel change between samples
    best = None
    for x0, y0, x1, y1, area in _blobs(skin_rate > 0.55):
        cx = (x0 + x1) / 2 / GW
        if area < 12 or area > GW * GH * 0.06 or 0.28 < cx < 0.72:
            continue
        bw, bh = x1 - x0, y1 - y0
        if bh > bw * 3.2 or bw > bh * 3.2:
            continue
        score = area * skin_rate[y0:y1, x0:x1].mean()
        if not best or score > best[0]:
            best = (score, x0, y0, x1, y1)
    if not best:
        return None
    _, x0, y0, x1, y1 = best
    gray = frames.mean(axis=3)
    # edges that stay put through the whole clip: an overlay's border, the frame edge, HUD lines
    pdx = np.abs(np.diff(gray, axis=2)).mean(axis=0)     # vertical lines   (GH, GW-1)
    pdy = np.abs(np.diff(gray, axis=1)).mean(axis=0)     # horizontal lines (GH-1, GW)
    thr = max(10.0, 4 * float(np.median(pdx)))
    fw, fh = x1 - x0, y1 - y0

    def line(profile: np.ndarray, lo: int, hi: int, span: slice, axis: str, toward_low: bool):
        """Strongest straight edge between lo..hi along the given span; None if nothing clear."""
        best_i, best_s = None, 0.0
        for i in range(max(lo, 0), min(hi, profile.shape[1 if axis == "x" else 0])):
            vals = profile[span, i] if axis == "x" else profile[i, span]
            if len(vals) < 3:
                continue
            s = float(np.percentile(vals, 30))   # must run along the whole side, not one bright spot
            if s > best_s:
                best_i, best_s = i, s
        return best_i if best_s >= thr else None

    rows = slice(y0, min(GH, int(y1 + fh * 1.5)))
    left = line(pdx, int(x0 - fw * 3.5), x0 - 1, rows, "x", True)
    right = line(pdx, x1, int(x1 + fw * 3.5), rows, "x", False)
    cam_x0 = 0 if x0 < fw * 2.5 and left is None else (left + 1 if left is not None else x0 - fw * 0.9)
    cam_x1 = GW if GW - x1 < fw * 2.5 and right is None else (right + 1 if right is not None else x1 + fw * 0.9)
    cols = slice(int(max(0, cam_x0)), int(min(GW, cam_x1)))
    top = line(pdy, int(y0 - fh * 2.5), y0 - 1, cols, "y", True)
    bottom = line(pdy, y1, int(y1 + fh * 3), cols, "y", False)
    cam_y0 = top + 1 if top is not None else (0 if y0 < fh * 1.5 else y0 - fh * 0.8)
    cam_y1 = bottom + 1 if bottom is not None else (GH if GH - y1 < fh * 2 else y1 + fh * 1.6)
    x0, x1, y0, y1 = max(0, cam_x0), min(GW, cam_x1), max(0, cam_y0), min(GH, cam_y1)
    w, h = x1 - x0, y1 - y0
    if w < fw * 1.4 or h < fh * 1.3 or w > GW * 0.45 or h > GH * 0.6:
        # borders didn't make sense: fall back to a head-and-shoulders box around the face
        cx = (best[1] + best[3]) / 2
        w = min(max(fw * 2.6, GW * 0.15), GW * 0.4)
        h = min(w * 0.75, GH * 0.55)
        x0 = max(0.0, min(cx - w / 2, GW - w))
        y0 = max(0.0, min(best[2] - h * 0.25, GH - h))
    return round(x0 / GW, 4), round(y0 / GH, 4), round(w / GW, 4), round(h / GH, 4)


# Measured from real stream frames. Used when a streamer has no entry in data/facecams.json yet;
# edit that file ("manual": true) to change one.
SEED = {
    "tw_lacy": [0.0, 0.0, 0.257, 0.256],
    "tw_jasontheween": [0.777, 0.444, 0.192, 0.263],
    "tw_jynxzi": [0.75, 0.64, 0.25, 0.36],
    "tw_kaicenat": [0.0, 0.4, 0.204, 0.204],
    "yt_ishowspeed": [0.0, 0.426, 0.202, 0.222],
    "tw_theburntpeanut": [0.13, 0.73, 0.115, 0.27],
}


def cam_present(video: Path, duration: float, box) -> bool:
    """Is there really a steady face (or avatar) in that box in this clip? IRL footage has no overlay."""
    frames = _frames(video, duration, count=20)
    if len(frames) < 6:
        return False
    x, y, w, h = box
    crop = frames[:, int(y * GH):int((y + h) * GH) + 1, int(x * GW):int((x + w) * GW) + 1]
    rate = _skin(crop).mean(axis=0)
    # a webcam face keeps the same pixels skin-coloured clip-long; moving footage doesn't
    return rate.size > 0 and float((rate > 0.6).mean()) > 0.03


def facecam_for(streamer: str, video: Path, duration: float) -> tuple[float, float, float, float] | None:
    """The streamer's webcam box for this clip, or None (no cam visible, e.g. IRL footage)."""
    known = load_json(FACECAMS, {})
    entry = known.get(streamer) or ({"box": SEED[streamer], "manual": True} if streamer in SEED else {})
    if entry.get("manual"):
        try:
            if cam_present(video, duration, entry["box"]):
                return tuple(entry["box"])
            # the streamer may have moved their cam: look for it, but only trust a box with a steady face
            box = detect_facecam(video, duration)
            return box if box and cam_present(video, duration, box) else None
        except Exception:
            return None
    try:
        box = detect_facecam(video, duration)
        if box and not cam_present(video, duration, box):
            box = None
    except Exception:
        box = None
    if box:
        hist = (entry.get("history") or [])[-5:] + [list(box)]
        known[streamer] = {"box": [round(float(np.median([b[i] for b in hist])), 4) for i in range(4)],
                           "history": hist}
        save_json(FACECAMS, known)
        # trust this clip's own detection when it agrees with history, else the steadier median
        med = known[streamer]["box"]
        return box if abs(box[0] - med[0]) < 0.08 and abs(box[1] - med[1]) < 0.08 else tuple(med)
    return tuple(entry["box"]) if entry.get("box") and len(entry.get("history", [])) >= 2 else None


# ───────────────────────────── layout ─────────────────────────────
def layout_graph(src_w: int, src_h: int, zoom: float, cam, fps: int, grain: int,
                 background: str = "blur", bg_color: str = "#1F3635") -> dict:
    """ffmpeg filter graph that turns [0:v] into the 1080x1920 frame (no text yet), ending in [base].

    With a webcam: gameplay sits high, cropped away from the side the webcam is on, and the webcam is cut
    out of the stream and shown big in the bottom-right corner. Without one: gameplay centred, as before.
    Also returns where the gameplay band and captions sit, for the subtitle file."""
    if background == "solid":
        graph = f"color=c=0x{bg_color[1:]}:s=1080x1920:r={fps}[bg];[0:v]fps={fps},split=2[g][c];"
    else:
        dim = "-0.35" if background == "blur-dark" else "-0.2"
        graph = (f"[0:v]fps={fps},split=3[a][g][c];"
                 "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=6:2,"
                 f"scale=1080:1920:flags=bilinear,eq=brightness={dim}:saturation=1.15[bg];")
    if cam:
        zoom = max(zoom, 1.35)
    fg_w = int(1080 * zoom / 2) * 2
    fg_h = min(int(src_h * fg_w / src_w / 2) * 2, 1300)
    grain_f = f",noise=c0s={grain}:c0f=t" if grain > 0 else ""
    if not cam:
        top = (1920 - fg_h) // 2
        graph += (f"[g]scale={fg_w}:-2:flags=bicubic,crop='min(iw,1080)':'min(ih,{fg_h})'[fg];[c]nullsink;"
                  f"[bg][fg]overlay=(W-w)/2:{top}:shortest=1{grain_f}[base]")
        return {"graph": graph, "fg_top": top, "fg_bottom": top + fg_h, "cap_y": top + fg_h + 70,
                "credit_left": False}
    x, y, w, h = cam
    crop_x = fg_w - 1080 if x + w / 2 < 0.5 else 0    # push the crop away from the webcam's side
    top = 300
    cw_src, ch_src = max(2, int(w * src_w)) // 2 * 2, max(2, int(h * src_h)) // 2 * 2
    aspect = cw_src / ch_src
    cw, ch = 620, int(620 / aspect)
    if ch > 500:
        ch, cw = 500, int(500 * aspect)
    cw, ch = cw // 2 * 2, ch // 2 * 2
    cx, cy = 1080 - cw - 12 - 34, 1920 - ch - 12 - 140
    graph += (f"[g]scale={fg_w}:-2:flags=bicubic,crop=1080:'min(ih,{fg_h})':{crop_x}:0[fg];"
              f"[c]crop={cw_src}:{ch_src}:{int(x * src_w)}:{int(y * src_h)},scale={cw}:{ch}:flags=bicubic,"
              f"pad={cw + 12}:{ch + 12}:6:6:color=white[cam];"
              f"[bg][fg]overlay=0:{top}:shortest=1[l1];[l1][cam]overlay={cx}:{cy}{grain_f}[base]")
    return {"graph": graph, "fg_top": top, "fg_bottom": top + fg_h, "cap_y": top + fg_h + 50,
            "credit_left": True, "cam_top": cy}


# ───────────────────────────── story ─────────────────────────────
def scene_cuts(video: Path) -> list[float]:
    """Times (s) of hard cuts in the footage."""
    proc = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", str(video), "-vf",
                           "fps=6,scale=160:90,select='gt(scene,0.38)',showinfo", "-an", "-f", "null", "-"],
                          capture_output=True, timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return [float(m) for m in re.findall(r"pts_time:([\d.]+)", proc.stderr.decode("utf-8", "ignore"))]


def _sentence_starts(words: list[dict]) -> list[float]:
    return [max(0.0, w["s"] - 0.25) for i, w in enumerate(words)
            if i == 0 or w["s"] - words[i - 1]["e"] > 0.7 or words[i - 1]["w"][-1:] in ".?!"]


def _sentence_ends(words: list[dict]) -> list[float]:
    return [w["e"] + 0.35 for i, w in enumerate(words)
            if i == len(words) - 1 or words[i + 1]["s"] - w["e"] > 0.7 or w["w"][-1:] in ".?!"]


def plan_story(words: list[dict], duration: float, moment: float, moment_end: float, cuts: list[float],
               min_len: float, max_len: float) -> dict:
    """One clean story out of the raw footage: start on the setup, keep the moment, end after the reaction.

    `moment` / `moment_end` are when chat started and stopped exploding (seconds into the footage). Chat
    reacts a few seconds after something happens, so the payoff itself is just before `moment`."""
    payoff = max(0.0, moment - 4)
    starts = sorted(set(_sentence_starts(words) + list(cuts)))
    ends = sorted(set(_sentence_ends(words) + list(cuts)))
    # setup: begin on a sentence start or a cut 10-30 s before the payoff, nearest to ~18 s
    cand = [s for s in starts if payoff - 30 <= s <= payoff - 10]
    start = min(cand, key=lambda s: abs(payoff - 18 - s)) if cand else max(0.0, payoff - 18)
    # reaction: end on the first finished sentence (or cut) 4-14 s after chat calmed down
    cand = [e for e in ends if moment_end + 4 <= e <= moment_end + 14]
    end = min(cand[0] if cand else moment_end + 7, duration)
    start = max(0.0, start)
    if end - start < min_len:      # too short: reach further back for more setup, at a clean boundary
        earlier = [s for s in starts if s <= end - min_len]
        start = max(earlier) if earlier else max(0.0, end - min_len)
        end = min(duration, max(end, start + min_len))
    if end - start > max_len:      # too long: drop setup first, the payoff and reaction matter most
        later = [s for s in starts if end - max_len <= s <= payoff - 6]
        start = min(later) if later else end - max_len
    return {"start": round(start, 2), "end": round(end, 2), "payoff": round(payoff - start, 2)}


# ───────────────────────────── titles ─────────────────────────────
GAMES = {"grand theft auto vi": "GTA 6", "grand theft auto v": "GTA 5", "fortnite": "Fortnite",
         "minecraft": "Minecraft", "valorant": "Valorant", "just chatting": "", "irl": "", "gaming": ""}

# (event, keywords, templates), most specific first. {n} = streamer, {g} = " in GTA 6" etc.
EVENTS = [
    ("plane", r"plane|landing|runway|pilot|airport|cockpit", ["{n} tried to land a plane{g}", "{n} should not be flying planes{g}"]),
    ("heli", r"helicopter|\bheli\b|chopper", ["{n}'s helicopter mission went wrong{g}", "{n} took a helicopter{g}"]),
    ("shark", r"shark", ["{n} ran into a shark{g}"]),
    ("gator", r"alligator|\bgator|\bcroc", ["{n} ran into alligators{g}"]),
    ("police", r"\bcops?\b|police|wanted level|arrest|\bjail|swat", ["{n} vs the cops{g}", "{n} has every cop after him{g}"]),
    ("heist", r"\brob(?:bery|bing|bed)?\b|heist|\bbank\b|\bsteal|\bstole", ["{n} pulled off a robbery{g}", "{n}'s robbery went sideways{g}"]),
    ("shootout", r"\bshoot|\bgun\b|sniper|\bshot him|bullet", ["{n} got into a shootout{g}", "{n} came in shooting{g}"]),
    ("fight", r"\bfight|\bpunch|knock(?:ed)? (?:him )?out", ["{n} started a fight{g}"]),
    ("crash", r"\bcrash(?:ed)?|\bwreck(?:ed)?|totaled|flipped", ["{n} crashed out{g}", "{n} wrecked everything{g}"]),
    ("chase", r"\bchase|run from|get away|getaway|\bescape", ["{n}'s getaway went crazy{g}", "{n} tried to escape{g}"]),
    ("water", r"\bboat|yacht|jet ?ski|\bswim|\bdive|scuba", ["{n} took to the water{g}", "{n}'s boat trip went wrong{g}"]),
    ("bike", r"dirt ?bike|motorcycle|\bbike\b|sent it|\bramp", ["{n} sent it on a bike{g}", "{n} hit a crazy jump{g}"]),
    ("car", r"lambo|ferrari|porsche|supercar|bugatti", ["{n} found a supercar{g}", "{n} took a supercar out{g}"]),
    ("dog", r"\bdog\b|puppy|doggy", ["{n} met a dog{g}"]),
    ("death", r"\bwasted\b|i'?m dead|i died|killed me", ["{n} got wasted{g}", "It all went wrong for {n}{g}"]),
]
NSFW = re.compile(r"(?i)strip ?club|lap ?dance|\bnaked|\bnude|\bshower|jiggle|\bdick\b|\bpussy|\bboobs?\b|\bsex\b")


def game_label(category: str) -> str:
    c = (category or "").strip().lower()
    if c in GAMES:
        return GAMES[c]
    return category.strip() if category and len(category) <= 16 else ""


def local_title(name: str, category: str, transcript: str, chat: list[str], seed: str) -> dict:
    """A title naming what happened in the game, from what the streamer said and what chat spammed."""
    text = " ".join([transcript or ""] + list(chat or []) * 2).lower()
    scores = Counter({event: len(re.findall(pattern, text)) for event, pattern, _ in EVENTS})
    game = game_label(category)
    g = f" in {game}" if game else ""
    event, hits = scores.most_common(1)[0]
    title = None
    if hits >= 1:
        templates = next(t for e, _, t in EVENTS if e == event)
        shown = name[:1].upper() + name[1:] if name.islower() else name
        title = templates[sum(map(ord, seed)) % len(templates)].format(n=shown, g=g)
    return {"title": title, "event": event if title else "", "nsfw": bool(NSFW.search(text))}
