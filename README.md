# ClipCrew

A team of agents that watches Twitch and YouTube live streams, notices when chat explodes,
and turns that moment into a 45–90 second vertical clip with captions and a title —
ready for YouTube Shorts and Instagram Reels.

## Start it

Double-click `start.bat`, or run:

```
.venv\Scripts\python run.py
```

Then open http://127.0.0.1:8765, paste a channel link into **Streamers** and press **Add streamer**.
Finished clips are saved in `data/clips` (video, thumbnail, and a `.json` with the title, caption and hashtags).

Needs Python 3.10+ and `ffmpeg` on the PATH.

## The agents, in working order

| Step | Agent | What it does |
|---|---|---|
| — | **Manager** | Deploys a watch crew when a stream goes live, opens clip jobs, routes each job through production, retries failed steps, restarts crashed agents. |
| 1 | **Calendar Analyst** | Learns each streamer's weekly rhythm from past broadcasts (and official schedules where available) and predicts the next stream. |
| 2 | **Live Scout** | Checks who is live — every minute around expected stream times, every few minutes otherwise. |
| 3 | **Stream Recorder** | One per live stream. Keeps the last 12 minutes of footage on a rolling buffer. |
| 3 | **Chat Monitor** | One per live stream. Reads chat in real time. |
| 4 | **Hype Detector** | One per live stream. Compares chat speed with the channel's normal pace and flags spikes and "clip it" requests. |
| 5 | **Clip Cutter** | Pulls the flagged window out of the buffer. |
| 6 | **Caption Writer** | Transcribes speech with word-level timing (Whisper, runs locally). |
| 7 | **Title Writer** | Writes the title, post caption and hashtags — with Claude if an API key is set, otherwise from templates. |
| 8 | **Video Editor** | Renders the 1080×1920 video: blurred backdrop, title on top, animated captions, channel credit. |

## Talk to the Manager

Click the **Manager** in the middle of the crew to open a chat. Tell it what you want (streamers, the kind of
moments you care about, clip length, title tone, caption style…). It changes settings and the watch list
for you, saves the rest as standing requirements (the Title Writer follows them), and asks follow-up questions after
every message until you say the session is over. With `ANTHROPIC_API_KEY` set it's driven by Claude; without one
it uses a simpler built-in interviewer.

## Edit clips in the vault

Every clip in **Clips** has an **Edit** button: change the title text, font, size, position and colours, the
caption font, size, colours and case, the background (blur, dark blur or a solid colour), how big the stream sits in
the frame, and trim the start and end. The preview updates live; **Save & render** re-renders the video.
Clean footage for each clip is kept in `data/sources` so it can be restyled any number of times.

## Share it with friends

Double-click `share.bat`. It starts ClipCrew (if needed), opens a free Cloudflare Quick Tunnel (no account) at a
random `https://….trycloudflare.com` address — using `tools\cloudflared.exe` from
[Cloudflare's releases](https://github.com/cloudflare/cloudflared/releases) — and prints the link to send (also saved in `share-link.txt`) — it ends
in `/k/…` (the secret key). The address can change when the tunnel restarts, so re-send the new link if it does. Friends who open that link get full use of the site
(add streamers, chat with the Manager, make and edit clips); their browser remembers them afterwards. Anyone
without the key only sees a "private" page. Keep the window open and your PC awake while they use it.

- To cut off old links, change `CLIPCREW_ACCESS_KEY` in `.env` and restart.
- Everyone works on the same crew and vault, and Manager chats use your `ANTHROPIC_API_KEY` if one is set.

## Optional setup

Copy `.env.example` to `.env`:

- `ANTHROPIC_API_KEY` — AI-written titles and a virality score for each clip.
- `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` — read official Twitch schedules.

Detection sensitivity, clip length, caption style and more are under **Settings** in the dashboard.

## Good to know

- Clips are saved locally; nothing is uploaded for you.
- Only publish clips of streamers who allow it — check their clip policy or ask first. Each clip carries a channel credit.
- Recording uses roughly 45 MB per minute per live stream (1080p), capped by the rolling buffer.

## Title font

The header uses the free **Gondens DEMO** font, which is personal-use only and may not be republished, so it is not in this repo.
Download it from 1001fonts.com and save the font file as `web/fonts/gondens.otf`. Without it the page falls back to a system font.
