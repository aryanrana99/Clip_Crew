"""Helper for the hourly Instagram posting run (driven by Claude).

  python autopost.py review       new clips since the last run: chat signal, transcript, contact sheet
  python autopost.py open         temporary public access to /media/ videos (token + Cloudflare tunnel)
  python autopost.py url <id>     the public link for one clip's video (after `open`)
  python autopost.py close        remove the token and stop the tunnel
  python autopost.py mark <id> posted|rejected [permalink]
  python autopost.py delete-rejected   delete clips marked rejected (only ones that were reviewed)
"""
import json
import re
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CLIPS = DATA / "clips"
LEDGER = DATA / "autopost.json"
TOKEN = DATA / "publish_token.txt"
TUNNEL_LOG = DATA / "autopost-tunnel.log"
TUNNEL_URL = DATA / "autopost-tunnel-url.txt"
SHEETS = DATA / "autopost-sheets"
API = "http://127.0.0.1:8765"
sys.stdout.reconfigure(encoding="utf-8")


def ledger() -> dict:
    if LEDGER.exists():
        return json.loads(LEDGER.read_text("utf-8"))
    return {"posted": {}, "rejected": [], "deleted": []}


def save(led: dict) -> None:
    LEDGER.write_text(json.dumps(led, indent=1), "utf-8")


def clips() -> list:
    with urllib.request.urlopen(f"{API}/api/state", timeout=10) as r:
        return json.load(r)["clips"]


def review() -> None:
    led = ledger()
    seen = set(led["posted"]) | set(led["rejected"])
    SHEETS.mkdir(exist_ok=True)
    new = [c for c in clips() if c["id"] not in seen]
    print(f"{len(new)} new clip(s) since the last run; posted so far: {len(led['posted'])}")
    for c in sorted(new, key=lambda c: -(c.get("signal") or {}).get("z", 0)):
        s = c.get("signal") or {}
        meta = CLIPS / (c["file"].rsplit(".", 1)[0] + ".json")
        tr = json.loads(meta.read_text("utf-8")).get("transcript", "") if meta.exists() else ""
        sheet = SHEETS / f"{c['id']}.jpg"
        if not sheet.exists():
            d = max(float(c.get("duration") or 60), 8)
            # keyframes only: ~10x faster than decoding every frame, plenty for a contact sheet
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-skip_frame", "nokey", "-i", str(CLIPS / c["file"]), "-vf",
                            f"fps=8/{d},scale=270:-1,tile=8x1", "-frames:v", "1", str(sheet)])
        print(f"\n## {c['id']} | {c['streamer_name']} | {c['duration']}s | z={s.get('z')} "
              f"peak={s.get('peak_rate')}/min base={s.get('base')} {s.get('reaction')} | title: {c['title']}")
        print(f"sheet: {sheet}")
        print(tr[:600])


def open_access() -> None:
    TOKEN.write_text(secrets.token_urlsafe(24), "utf-8")
    TUNNEL_URL.unlink(missing_ok=True)
    log = open(TUNNEL_LOG, "w", encoding="utf-8")
    subprocess.Popen([str(ROOT / "tools" / "cloudflared.exe"), "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:8765"],
                     stdout=log, stderr=subprocess.STDOUT, creationflags=0x08000000)
    for _ in range(60):
        time.sleep(1)
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", TUNNEL_LOG.read_text("utf-8", "ignore"))
        if m:
            TUNNEL_URL.write_text(m.group(0), "utf-8")
            time.sleep(8)  # let the edge register the new name
            print("open:", m.group(0))
            return
    print("tunnel did not come up; see", TUNNEL_LOG)
    close_access()
    sys.exit(1)


def url(clip_id: str) -> None:
    clip = next(c for c in clips() if c["id"] == clip_id)
    print(f"{TUNNEL_URL.read_text('utf-8').strip()}/media/{clip['file']}?t={TOKEN.read_text('utf-8').strip()}")


def close_access() -> None:
    TOKEN.unlink(missing_ok=True)
    TUNNEL_URL.unlink(missing_ok=True)
    subprocess.run(["taskkill", "/F", "/IM", "cloudflared.exe"], capture_output=True)
    print("closed")


def mark(clip_id: str, what: str, link: str = "") -> None:
    led = ledger()
    if what == "posted":
        led["posted"][clip_id] = link or True
    else:
        led["rejected"].append(clip_id)
    save(led)
    print("marked", clip_id, what)


def delete_rejected() -> None:
    led = ledger()
    live = {c["id"] for c in clips()}
    for cid in led["rejected"]:
        if cid in live and cid not in led["posted"]:
            req = urllib.request.Request(f"{API}/api/clips/{cid}", method="DELETE")
            urllib.request.urlopen(req, timeout=60).read()
            (SHEETS / f"{cid}.jpg").unlink(missing_ok=True)
            led["deleted"].append(cid)
            print("deleted", cid)
    save(led)


if __name__ == "__main__":
    cmd, *args = sys.argv[1:] or ["review"]
    {"review": review, "open": open_access, "url": url, "close": close_access, "mark": mark,
     "delete-rejected": delete_rejected}[cmd](*args)
