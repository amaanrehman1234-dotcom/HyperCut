"""One channel's pipeline: record, read chat, count spikes, cut, caption, render."""
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from functools import partial

from . import chat, clipper, directory, hype, shorts
from .spikes import SpikeCounter

URLS = {"twitch": "https://twitch.tv/{}", "kick": "https://kick.com/{}"}
PARTS = ("streamlink", "chat", "spikes", "ffmpeg", "whisper")
VOTE_WINDOW = 15  # seconds in which N distinct chatters must ask for a clip


class Part:
    """One pipeline stage as the dashboard shows it: a state plus a line of detail."""

    def __init__(self):
        self.state, self.detail = "idle", ""

    def set(self, state, detail=""):
        """Update what the dashboard shows for this stage."""
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
        login = login.lower()
        self.platform, self.login, self.cfg, self.streamlink = platform, login, cfg, streamlink
        self.rec_dir, self.clip_dir = out / "recordings", out / "clips"
        self.rec_dir.mkdir(parents=True, exist_ok=True)
        self.clip_dir.mkdir(parents=True, exist_ok=True)
        self.parts = {p: Part() for p in PARTS}
        self.counter = SpikeCounter(cfg["multiplier"], cfg["min_msgs"], cfg["cooldown"])
        self.stop_ev = threading.Event()
        self.jobs = queue.Queue()
        self.layouts = out / "layouts.json"
        self.seg_dir = self.rec_dir / f"{platform}_{login}"
        self.proc = None
        self.on_clip = None  # hook for uploaders: called with each finished clip path
        self.clips = self.spikes = 0
        self.series = []  # last 30 bucket counts for the sparkline
        self.msgs = deque()  # (time, weight, who) of recent chat messages, for the hype score
        self.kept = []  # (time, score, dst) of clips kept in the last hour, for --top-per-hour
        self.votes = {}  # chatter -> time of their latest clip request
        self.category = ""
        self.threads = []

    @property
    def key(self):
        """'platform:login', the id sessions go by everywhere."""
        return f"{self.platform}:{self.login}"

    def start(self):
        """Start the four worker threads: record, chat, count, clip."""
        reader = chat.twitch_chat if self.platform == "twitch" else chat.kick_chat
        read = partial(reader, self.login, self.on_msg, self.stop_ev)
        run_chat = partial(chat.run_forever, read, self.parts["chat"], self.stop_ev)
        for fn in (self._record, run_chat, self._count, self._clip):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self.threads.append(t)
        threading.Thread(target=self._lookup, daemon=True).start()
        self.parts["ffmpeg"].set("ok", "ready")
        self.parts["whisper"].set("ok", f"model {self.cfg['whisper_model']}")

    def _lookup(self):
        try:
            self.category = directory.lookup(self.platform, self.login)["category"]
        except Exception:  # offline or directory down: the category stays empty
            pass

    def on_msg(self, who, text):
        """Chat thread: count the message, weigh it for the hype score, and queue a clip on enough !clip votes."""
        now = time.time()
        self.counter.add()
        self.msgs.append((now, hype.weight(text), who))
        votes = self.cfg["clip_votes"]
        if votes and hype.is_clip(text):
            self.votes[who] = now
            self.votes = {w: t for w, t in self.votes.items() if now - t <= VOTE_WINDOW}
            # a vote clip and a spike share one cooldown, so a moment is never clipped twice
            if len(self.votes) >= votes and self.counter.claim(now, self.cfg["cooldown"]):
                self.votes = {}
                self.jobs.put((now, "votes", self.counter.baseline))

    def stop(self):
        """Stop recording and chat. Returns before queued spikes are done; wait() is for that."""
        self.stop_ev.set()
        self.jobs.put(None)
        if self.proc:
            reap(self.proc)
        for t in self.threads[:3]:  # clip worker keeps draining queued spikes in the background
            t.join(timeout=10)
        for p in self.parts.values():
            p.set("idle", "stopped")

    def wait(self, timeout):
        """After stop(): give the clip worker up to timeout seconds to finish queued spikes."""
        for t in self.threads[3:]:
            t.join(max(0.0, timeout))

    def status(self):
        """Snapshot for /api/sessions."""
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
            sl = self.proc = subprocess.Popen(
                [self.streamlink, "--retry-streams", "30", "--retry-max", "0", "-O",
                 URLS[self.platform].format(self.login), self.cfg["quality"]],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            ff = subprocess.Popen(
                ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-map", "0", "-c", "copy", "-f", "segment",
                 "-segment_time", str(seg), "-reset_timestamps", "1", "-strftime", "1", str(self.seg_dir / "%s.ts")],
                stdin=sl.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
            while self.msgs and self.msgs[0][0] < now - self.cfg["retention"]:
                self.msgs.popleft()
            if hit:
                self.spikes += 1
                self.jobs.put((now, "spike", self.counter.baseline))

    def _hype(self, spike, baseline, loud):
        """Hype of the clip window: each chatter counts at most 3, loud audio boosts up to 1.5x."""
        t0, n = spike - self.cfg["pre"], self.cfg["clip_length"]
        win = [(w, who) for t, w, who in list(self.msgs) if t0 <= t <= t0 + n]
        per = {}
        for w, who in win:
            per[who] = min(per.get(who, 0) + w, 3)  # 10 spammers can't fake a hype moment
        boost = 1 + min(max(loud - 15, 0), 15) / 30
        return {"score": round(hype.score(sum(per.values()), baseline, n / self.cfg["bucket"]) * boost, 2),
                "msgs": len(win), "chatters": len(per)}

    def _sidecar(self, dst, lines, spike, trigger, baseline, hit, loud):
        """Write <clip>.json: hype score of the clip window, title, hashtags, review status."""
        path = dst.with_suffix(".json")
        with hype.lock:  # a review status set while whisper ran wins over "pending"
            hype.write(path, {
                **hit, "loudness": round(loud, 1),
                "baseline": round(baseline, 1), "platform": self.platform, "login": self.login,
                "category": self.category, "spike_at": round(spike, 1), "title": hype.title(lines, f"{self.login} · {self.category}" if self.category else self.login),
                "hashtags": hype.hashtags(self.platform, self.login, self.category), "status": "pending",
                "trigger": trigger, **hype.read(path)})

    def _weak(self, dst, spike, score):
        """Apply --min-score / --top-per-hour to a fresh cut: True (and its file deleted) if it is not worth keeping."""
        top = self.cfg.get("top_per_hour", 0)
        self.kept = [k for k in self.kept if k[0] > spike - 3600]
        low = min(self.kept, key=lambda k: k[1]) if top and len(self.kept) >= top else None
        if score < self.cfg.get("min_score", 0) or (low and score <= low[1]):
            dst.unlink(missing_ok=True)
            return True
        if low:  # full hour and this one beats the weakest: drop the weakest
            for ext in (".mp4", ".srt", ".vtt", ".json", ".short.mp4"):
                low[2].with_suffix(ext).unlink(missing_ok=True)
            self.kept.remove(low)
            self.clips -= 1
        if top:
            self.kept.append((spike, score, dst))
        self.clips += 1
        return False

    def _clip(self):
        """Cut, caption, render each spike. On stop, still finishes queued spikes with the footage there is."""
        cfg, ff, wh = self.cfg, self.parts["ffmpeg"], self.parts["whisper"]
        while (job := self.jobs.get()) is not None:
            spike, trigger, baseline = job
            # chat reacts after the moment, so most of the window is before the spike
            t0 = spike - cfg["pre"]
            self.stop_ev.wait(max(0.0, t0 + cfg["clip_length"] + cfg["segment"] - time.time()))
            best = self._hype(spike, baseline, 30)["score"]  # chat score with the max loudness boost
            if best < cfg.get("min_score", 0):  # can't reach --min-score even if loud: don't even cut
                ff.set("ok", f"skipped spike: hype at most {best:.2f} < min")
                continue
            segs = [(t, p) for t, p in self.segments() if t0 - cfg["segment"] <= t <= t0 + cfg["clip_length"]]
            if not segs:
                ff.set("ok", "spike skipped: no footage recorded")
                continue
            src = "concat:" + "|".join(str(p) for _, p in segs)
            dst = self.clip_dir / f"{self.platform}_{self.login}_{datetime.fromtimestamp(spike):%Y%m%d-%H%M%S}.mp4"
            try:
                ff.set("busy", f"cutting {dst.name}")
                clipper.cut(src, t0 - segs[0][0], cfg["clip_length"], dst)
                loud = clipper.loudness(dst)
                hit = self._hype(spike, baseline, loud)
                if self._weak(dst, spike, hit["score"]):
                    ff.set("ok", f"skipped {dst.name}: hype {hit['score']:.2f} < min")
                    continue
                ff.set("ok", f"{self.clips} clips")
                wh.set("busy", f"captioning {dst.name}")
                lines, words = clipper.transcribe(dst, cfg["whisper_model"], cfg.get("translate", False))
                clipper.write_captions(lines, dst)
                self._sidecar(dst, lines, spike, trigger, baseline, hit, loud)
                wh.set("busy", f"rendering vertical short {dst.name}")
                shorts.render(dst, words, URLS[self.platform].format(self.login)[8:], dst.with_suffix(".short.mp4"),
                              crop=hype.read(self.layouts).get(self.key))  # read now: edits apply to the next short
                wh.set("ok", f"captioned {dst.name}")
                if self.on_clip:
                    self.on_clip(dst)
            except Exception as e:
                (wh if ff.state == "ok" else ff).set("error", str(e)[:120])
        if self.stop_ev.is_set():
            shutil.rmtree(self.seg_dir, ignore_errors=True)  # stopped: footage no longer needed
