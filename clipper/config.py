"""Paths, settings and small JSON persistence helpers."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DATA = ROOT / "data"
BUFFER = DATA / "buffer"   # rolling footage per live stream
WORK = DATA / "work"       # scratch space per clip job
CLIPS = DATA / "clips"     # finished clips
SOURCES = DATA / "sources" # clean footage + word timings per clip, for the vault editor
WEB = ROOT / "web"
FONTS = WEB / "fonts"

for _d in (CONFIG, BUFFER, WORK, CLIPS, SOURCES):
    _d.mkdir(parents=True, exist_ok=True)

DEFAULT_SETTINGS = {
    # --- hype detection ---
    "sensitivity": 3.0,        # how many "sigmas" above normal chat speed counts as a spike
    "spike_ratio": 2.2,        # chat must also be at least this many times faster than normal
    "min_msgs_per_10s": 8,     # ignore spikes in chats slower than this
    "chat_window_s": 10,       # sliding window used to measure chat speed
    "warmup_s": 60,            # seconds of chat to observe before trusting the baseline
    "cooldown_s": 75,          # quiet period after a moment before another can trigger
    "max_clips_per_hour": 6,   # per streamer
    # --- clip shape ---
    "clip_min_s": 45,
    "clip_max_s": 90,
    "pre_roll_s": 28,          # footage kept before chat reacted (the moment itself)
    "post_roll_s": 8,          # footage kept after chat calms down
    # --- recording ---
    "buffer_minutes": 12,
    "quality": "1080p60,1080p,720p60,720p,best",
    "skip_reruns": True,
    "poll_live_s": 60,         # live-check cadence around expected stream times
    "poll_idle_s": 240,        # live-check cadence when no stream is expected
    # --- production ---
    "whisper_model": "base",   # tiny | base | small | medium
    "caption_language": "",    # empty = auto-detect
    "captions_uppercase": True,
    "layout_zoom": 1.25,       # how much the gameplay is enlarged inside the 9:16 frame
    "facecam_layout": True,    # cut the streamer's webcam out and show it big in the bottom-right corner
    "film_grain": 5,           # subtle film grain strength (0 = off)
    "story_trim": True,        # trim each clip to one story: setup -> moment -> reaction
    "story_min_s": 30,         # shortest a story-trimmed clip may be
    "merge_window_s": 150,     # hold a new moment this long; a follow-up moment within it joins the same clip
    "fps": 30,
    "encoder": "libx264",
    "llm_titles": True,        # use Claude for titles when credentials are available
    "llm_model": "claude-opus-5-5",
}


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path, obj) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), "utf-8")
    os.replace(tmp, path)


def load_env() -> None:
    """Load KEY=VALUE pairs from .env without overriding the real environment."""
    try:
        lines = (ROOT / ".env").read_text("utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    saved = load_json(CONFIG / "settings.json", {})
    settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    return settings


def coerce_settings(patch: dict) -> dict:
    """Keep only known keys, cast to the type of the default value."""
    clean = {}
    for key, value in patch.items():
        if key not in DEFAULT_SETTINGS:
            continue
        default = DEFAULT_SETTINGS[key]
        try:
            if isinstance(default, bool):
                clean[key] = bool(value)
            elif isinstance(default, int):
                clean[key] = int(float(value))
            elif isinstance(default, float):
                clean[key] = float(value)
            else:
                clean[key] = str(value).strip()
        except (TypeError, ValueError):
            continue
    if "clip_min_s" in clean:
        clean["clip_min_s"] = max(15, min(clean["clip_min_s"], 170))
    if "clip_max_s" in clean:
        clean["clip_max_s"] = max(20, min(clean["clip_max_s"], 180))
    return clean
