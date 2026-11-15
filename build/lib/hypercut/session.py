import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime
from functools import partial

from . import chat, clipper, shorts
from .spikes import SpikeCounter

URLS = {"twitch": "https://twitch.tv/{}", "kick": "https://kick.com/{}"}
PARTS = ("streamlink", "chat", "spikes", "ffmpeg", "whisper")


class Part:
    def __init__(self):
        self.state, self.detail = "idle", ""

    def set(self, state, detail=""):
        self.state, self.detail = state, detail


def reap(proc):
    """Stop a child process: terminate, then kill if it ignores us for 5s."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


class Session:
    """One channel being watched: record + read chat + count + cut + caption."""

    def __init__(self, platform, login, out, cfg, streamlink="streamlink"):
        self.platform, self.login, self.cfg, self.streamlink = platform, login, cfg, streamlink
        self.rec_dir, self.clip_dir = out / "recordings", out / "clips"
        self.rec_dir.mkdir(parents=True, exist_ok=True)
        self.clip_dir.mkdir(parents=True, exist_ok=True)
        self.parts = {p: Part() for p in PARTS}
        self.counter = SpikeCounter(cfg["multiplier"], cfg["min_msgs"], cfg["cooldown"])
        self.stop_ev = threading.Event()
        self.jobs = queue.Queue()
        self.seg_dir = self.rec_dir / f"{platform}_{login}"
        self.proc = None
        self.on_clip = None  # hook for uploaders: called with each finished clip path
        self.clips = self.spikes = 0
        self.series = []  # last 30 bucket counts for the sparkline
        self.threads = []

    @property
    def key(self):
        return f"{self.platform}:{self.login}"

    def start(self):
        reader = chat.twitch_chat if self.platform == "twitch" else chat.kick_chat
        read = partial(reader, self.login, self.counter.add, self.stop_ev)
        for fn in (self._record, partial(chat.run_forever, read, self.parts["chat"], self.stop_ev), self._count, self._clip):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self.threads.append(t)
        self.parts["ffmpeg"].set("ok", "ready")
        self.parts["whisper"].set("ok", f"model {self.cfg['whisper_model']}")

    def stop(self):
        self.stop_ev.set()
        self.jobs.put(None)
        if self.proc:
            reap(self.proc)
        for t in self.threads[:3]:  # clip worker keeps draining queued spikes in the background
            t.join(timeout=10)
        for p in self.parts.values():
            p.set("idle", "stopped")

    def status(self):
        return {"key": self.key, "platform": self.platform, "login": self.login,
                "running": sum(p.state in ("ok", "busy") for p in self.parts.values()),
                "parts": {n: vars(p) for n, p in self.parts.items()},
                "clips": self.clips, "spikes": self.spikes, "series": self.series,
                "last": self.counter.last, "baseline": round(self.counter.baseline, 1)}

    def segments(self):
        """[(wall-clock start, path)] of recorded 10s segments, oldest first."""
        return sorted((int(p.stem), p) for p in self.seg_dir.glob("*.ts") if p.stem.isdigit())

    def _record(self):
        # streamlink -> ffmpeg segmenter: 10s files named by wall-clock epoch, so ad gaps/stalls can't
        # drift clip timing past one segment, and old segments are deleted (bounded disk, 24/7 safe)
        part, seg = self.parts["streamlink"], self.cfg["segment"]
        self.seg_dir.mkdir(parents=True, exist_ok=True)
        while not self.stop_ev.is_set():
            part.set("waiting", "waiting for stream to go live")
            began = time.time()
            sl = self.proc = subprocess.Popen([self.streamlink, "--retry-streams", "30", "--retry-max", "0", "-O",
                                               URLS[self.platform].format(self.login), self.cfg["quality"]],
                                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            ff = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-map", "0", "-c", "copy", "-f", "segment",
                                   "-segment_time", str(seg), "-reset_timestamps", "1", "-strftime", "1",
                                   str(self.seg_dir / "%s.ts")], stdin=sl.stdout, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
            sl.stdout.close()  # ffmpeg owns the pipe now
            while sl.poll() is None and not self.stop_ev.is_set():
                segs = self.segments()
                if segs:
                    part.set("ok", f"recording · {len(segs)} segments buffered")
                for t, path in segs:
                    if t < time.time() - self.cfg["retention"]:
                        path.unlink(missing_ok=True)
                self.stop_ev.wait(1)
            reap(sl)
            reap(ff)
            if self.stop_ev.is_set():
                return
            # stream ended (or streamlink failed fast): back off briefly, then wait for the next live session
            part.set("error" if time.time() - began < 5 else "waiting", f"streamlink exited ({sl.returncode})")
            self.stop_ev.wait(self.cfg["restart_delay"])

    def _count(self):
        part, bucket = self.parts["spikes"], self.cfg["bucket"]
        part.set("ok", "counting")
        while not self.stop_ev.wait(bucket):
            now = time.time()
            hit = self.counter.roll(now)
            self.series = (self.series + [self.counter.last])[-30:]
            part.set("ok", f"{self.counter.last} msgs/{bucket}s · baseline {self.counter.baseline:.1f}")
            if hit:
                self.spikes += 1
                self.jobs.put(now)

    def _clip(self):
        """Cut, caption, render each spike. On stop, still finishes queued spikes with the footage there is."""
        cfg, ff, wh = self.cfg, self.parts["ffmpeg"], self.parts["whisper"]
        while (spike := self.jobs.get()) is not None:
            # chat reacts after the moment, so most of the window is before the spike
            t0 = spike - cfg["pre"]
            self.stop_ev.wait(max(0.0, t0 + cfg["clip_length"] + cfg["segment"] - time.time()))
            segs = [(t, p) for t, p in self.segments() if t0 - cfg["segment"] <= t <= t0 + cfg["clip_length"]]
            if not segs:
                ff.set("ok", "spike skipped: no footage recorded")
                continue
            src = "concat:" + "|".join(str(p) for _, p in segs)
            dst = self.clip_dir / f"{self.platform}_{self.login}_{datetime.fromtimestamp(spike):%Y%m%d-%H%M%S}.mp4"
            try:
                ff.set("busy", f"cutting {dst.name}")
                clipper.cut(src, t0 - segs[0][0], cfg["clip_length"], dst)
                self.clips += 1
                ff.set("ok", f"{self.clips} clips")
                wh.set("busy", f"captioning {dst.name}")
                segs_, words = clipper.transcribe(dst, cfg["whisper_model"])
                clipper.write_captions(segs_, dst)
                wh.set("busy", f"rendering vertical short {dst.name}")
                shorts.render(dst, words, URLS[self.platform].format(self.login)[8:], dst.with_suffix(".short.mp4"))
                wh.set("ok", f"captioned {dst.name}")
                if self.on_clip:
                    self.on_clip(dst)
            except Exception as e:
                (wh if ff.state == "ok" else ff).set("error", str(e)[:120])
        if self.stop_ev.is_set():
            shutil.rmtree(self.seg_dir, ignore_errors=True)  # stopped: footage no longer needed
