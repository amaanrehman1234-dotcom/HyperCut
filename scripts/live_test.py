"""Live end-to-end check: pick a random live Twitch and Kick streamer, clip via the real API, assert mp4 + captions.

Run: python scripts/live_test.py [--minutes 4]   (needs network, streamlink, ffmpeg)
"""
import argparse
import json
import random
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PORT = 8799


def call(path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--minutes", type=float, default=4)
    a = a.parse_args()
    out = Path(tempfile.mkdtemp(prefix="hypercut-live-"))
    srv = subprocess.Popen([sys.executable, "-m", "hypercut", "--port", str(PORT), "--out", str(out), "--quality", "worst",
                            "--min-msgs", "2", "--multiplier", "1.3", "--cooldown", "40", "--whisper-model", "tiny"])
    try:
        time.sleep(2)
        live = call("/api/live")["items"]
        picks = []
        for plat in ("twitch", "kick"):
            # busy-but-not-top chats spike reliably at low thresholds
            pool = [s for s in live if s["platform"] == plat][:60]
            picks.append(random.choice(pool))
        for s in picks:
            print("clipping", s["platform"], s["login"], s["viewers"], "viewers", flush=True)
            call("/api/start", {"platform": s["platform"], "login": s["login"]})
        end = time.time() + a.minutes * 60
        while time.time() < end:
            time.sleep(15)
            for st in call("/api/sessions"):
                print(st["key"], f'{st["running"]}/5', {k: v["state"] for k, v in st["parts"].items()}, "spikes", st["spikes"], "clips", st["clips"], flush=True)
            clips = call("/api/clips")
            done = {c["platform"] for c in clips if c["captions"]}
            if done >= {"twitch", "kick"}:
                break
        for s in picks:
            call("/api/stop", {"platform": s["platform"], "login": s["login"]})
        assert call("/api/sessions") == [], "stop did not clear sessions"
        clips = call("/api/clips")
        print("clips:", json.dumps(clips, indent=1))
        for plat in ("twitch", "kick"):
            ok = [c for c in clips if c["platform"] == plat and c["captions"]]
            assert ok, f"no captioned clip for {plat}"
            mp4 = out / "clips" / ok[0]["name"]
            dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp4)]))
            assert 25 <= dur <= 31, f"{plat} clip duration {dur}"
            print(plat, "OK", mp4.name, f"{dur:.1f}s", "\n", mp4.with_suffix(".srt").read_text()[:300])
        print("LIVE TEST PASSED", out)
    finally:
        srv.terminate()


if __name__ == "__main__":
    main()
