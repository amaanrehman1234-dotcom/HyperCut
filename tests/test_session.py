import itertools
import subprocess
import threading
import time
import types


from hypercut import chat, clipper, directory, hype, session
from hypercut.session import Session

CFG = dict(bucket=0.05, clip_length=0, pre=0, multiplier=2, min_msgs=3, cooldown=0, quality="best",
           whisper_model="tiny", restart_delay=0.01, segment=1, retention=1, clip_votes=3)

# a fake streamlink: ignores its args and writes live-ish MPEG-TS to stdout (5 fps-paced keyframes every 5 frames)
FFMPEG_TS = ("ffmpeg -loglevel error -re -f lavfi -i testsrc=size=64x64:rate=10 -t {t} "
             "-c:v mpeg2video -g 5 -f mpegts pipe:1")


def script(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(0o755)
    return str(p)


def live_script(tmp_path, secs=30):
    return script(tmp_path, "sl", "exec " + FFMPEG_TS.format(t=secs))


def make_ts(path, secs=2):
    src = f"testsrc=size=64x64:rate=10:duration={secs}"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", src,
                    "-c:v", "mpeg2video", "-f", "mpegts", str(path)], check=True)


def wait_for(cond, t=10):
    end = time.time() + t
    while time.time() < end and not cond():
        time.sleep(0.02)
    assert cond()


def test_part_key_status(tmp_path):
    s = Session("kick", "Foo", tmp_path, CFG)
    assert s.key == "kick:foo" and (tmp_path / "clips").is_dir()
    s.parts["chat"].set("ok", "x")
    st = s.status()
    assert st["running"] == 1 and st["parts"]["chat"] == {"state": "ok", "detail": "x"} and st["baseline"] == 0


def test_record_segments_retention_stop(tmp_path):
    s = Session("twitch", "foo", tmp_path, CFG, streamlink=live_script(tmp_path))
    t = threading.Thread(target=s._record)
    t.start()
    seen = set()

    def rotated():
        seen.update(p for _, p in s.segments())
        return any(not p.exists() for p in seen) and s.segments()  # an old one was deleted, new ones keep coming

    wait_for(rotated, 30)
    assert s.parts["streamlink"].state == "ok" and "segments buffered" in s.parts["streamlink"].detail
    assert all(p.suffix == ".ts" and abs(int(p.stem) - time.time()) < 60 for p in seen)
    (s.seg_dir / "notes.ts").write_text("x")  # non-epoch names are ignored
    assert all(p.stem.isdigit() for _, p in s.segments())
    s.threads.append(t)
    s.stop()
    assert not t.is_alive() and s.parts["streamlink"].state == "idle"
    assert s.proc.poll() is not None


def test_record_fast_exit_restarts(tmp_path):
    s = Session("kick", "foo", tmp_path, CFG, streamlink=script(tmp_path, "sl", "exit 3"))
    t = threading.Thread(target=s._record)
    t.start()
    wait_for(lambda: s.parts["streamlink"].state == "error")
    assert "exited (3)" in s.parts["streamlink"].detail
    first = s.proc
    wait_for(lambda: s.proc is not first)  # restarted
    s.stop_ev.set()
    t.join(5)
    assert not t.is_alive()


def test_record_stream_ends_waits(tmp_path, monkeypatch):
    # stream that ran >5s then ended is "waiting" for the next live, not an error
    s = Session("kick", "foo", tmp_path, CFG, streamlink=script(tmp_path, "sl", "exit 0"))
    ticks = itertools.count(0, 10)  # every clock read is 10s later
    monkeypatch.setattr(session, "time", types.SimpleNamespace(time=lambda: next(ticks)))
    t = threading.Thread(target=s._record)
    t.start()
    part = s.parts["streamlink"]
    wait_for(lambda: part.detail == "streamlink exited (0)" and part.state == "waiting")
    s.stop_ev.set()
    t.join(5)


def test_start_stop_both_platforms(tmp_path, monkeypatch):
    def reader(login, on, stop):
        for i in range(10):
            on(i, "hi")
        stop.wait(5)

    monkeypatch.setattr(directory, "lookup", lambda p, l: {"category": "Chess"})
    monkeypatch.setattr(chat, "twitch_chat", reader)
    monkeypatch.setattr(chat, "kick_chat", reader)
    for plat in ("twitch", "kick"):
        s = Session(plat, "foo", tmp_path / plat, CFG, streamlink=script(tmp_path, "sl" + plat, "sleep 30"))
        s.start()
        assert s.parts["whisper"].detail == "model tiny"
        wait_for(lambda: s.parts["chat"].state == "ok" and s.counter.count + s.counter.last >= 10)
        wait_for(lambda: s.category == "Chess")
        s.stop()
        wait_for(lambda: not any(t.is_alive() for t in s.threads))  # clip worker drains in the background
        assert not s.seg_dir.exists()


def test_count_spike(tmp_path):
    s = Session("twitch", "foo", tmp_path, CFG)
    s.msgs.append((time.time() - 100, 1))  # older than retention: pruned
    t = threading.Thread(target=s._count)
    t.start()
    for _ in range(5):
        time.sleep(0.12)
    s.counter.add(50)
    wait_for(lambda: s.spikes >= 1)
    s.stop_ev.set()
    t.join(5)
    at, trigger, base = s.jobs.get_nowait()
    assert not s.msgs and abs(at - time.time()) < 10 and trigger == "spike" and base >= 0 and len(s.series) <= 30 and "msgs/0.05s" in s.parts["spikes"].detail


def stub_pipeline(monkeypatch):
    cuts, caps, shorts_ = [], [], []
    monkeypatch.setattr(clipper, "cut", lambda src, st, ln, dst: cuts.append((src, st, ln, dst.name)))
    monkeypatch.setattr(clipper, "transcribe", lambda p, m: ([(0, 1, "a")], [(0, 1, "a")]))
    monkeypatch.setattr(session.shorts, "render", lambda *a, **k: shorts_.append((*a, k)))
    monkeypatch.setattr(clipper, "write_captions", lambda segs, dst: caps.append(segs))
    return cuts, caps, shorts_


def boom(*a):
    raise RuntimeError("cut fail")


def test_clip_stubbed(tmp_path, monkeypatch):
    cfg = {**CFG, "clip_length": 20, "pre": 5, "segment": 10}
    s = Session("twitch", "foo", tmp_path, cfg)
    cuts, caps, shorts_ = stub_pipeline(monkeypatch)
    hooked = []
    s.on_clip = hooked.append
    now = time.time() - 100

    s.seg_dir.mkdir(parents=True)
    s.jobs.put((now, "spike", 2.0))  # no footage at all
    s.jobs.put(None)
    s._clip()
    assert s.clips == 0 and not cuts and s.parts["ffmpeg"].detail == "spike skipped: no footage recorded"
    assert s.seg_dir.exists()  # not stopped: footage stays

    t0 = now - 5
    names = [int(t0) - 9, int(t0) + 3, int(t0) + 30, int(t0) - 50]  # last two fall outside [t0-10, t0+20]
    for n in names:
        (s.seg_dir / f"{n}.ts").write_bytes(b"x")
    s.jobs.put((now, "spike", 2.0))
    s.jobs.put(None)
    s._clip()
    src, off, length, name = cuts[0]
    assert src == f"concat:{s.seg_dir}/{names[0]}.ts|{s.seg_dir}/{names[1]}.ts" and length == 20
    assert abs(off - (t0 - names[0])) < 1e-6 and name.startswith("twitch_foo_") and name.endswith(".mp4")
    assert s.clips == 1 and caps == [[(0, 1, "a")]] and hooked and hooked[0].name == name
    words, credit, out, kw = shorts_[0][1:]
    assert kw == {"crop": None}
    assert words == [(0, 1, "a")] and credit == "twitch.tv/foo" and out.name.endswith(".short.mp4")
    assert s.parts["whisper"].detail.startswith("captioned twitch_foo_") and s.parts["ffmpeg"].detail == "1 clips"

    monkeypatch.setattr(clipper, "cut", boom)
    s.jobs.put((now, "spike", 2.0))
    s.jobs.put(None)
    s._clip()
    assert s.parts["ffmpeg"].state == "error" and s.parts["ffmpeg"].detail == "cut fail"

    monkeypatch.setattr(clipper, "cut", lambda *a: None)
    monkeypatch.setattr(clipper, "transcribe", boom)
    s.jobs.put((now, "spike", 2.0))
    s.jobs.put(None)
    s._clip()
    assert s.parts["whisper"].state == "error"


def test_clip_drains_after_stop_and_cleans_up(tmp_path, monkeypatch):
    s = Session("twitch", "foo", tmp_path, {**CFG, "segment": 10})
    cuts, *_ = stub_pipeline(monkeypatch)
    now = time.time() - 100
    s.seg_dir.mkdir(parents=True)
    (s.seg_dir / f"{int(now) - 3}.ts").write_bytes(b"x")
    s.stop_ev.set()  # stopped with spikes still queued: they are cut with what exists, wait is skipped
    s.jobs.put((now, "spike", 2.0))
    s.jobs.put((now, "spike", 2.0))
    s.jobs.put(None)
    s._clip()
    assert len(cuts) == 2 and s.clips == 2
    assert not s.seg_dir.exists()


def test_wait_joins_clip_worker(tmp_path):
    s = Session("kick", "foo", tmp_path, CFG)
    s.wait(1)  # never started: nothing to wait for
    go = threading.Event()
    s.threads = [None, None, None, threading.Thread(target=go.wait, daemon=True)]
    s.threads[3].start()
    s.wait(-5)  # past the deadline: returns at once, worker still busy
    assert s.threads[3].is_alive()
    go.set()
    s.wait(10)
    assert not s.threads[3].is_alive()


def test_clip_real_concat_cut(tmp_path, monkeypatch):
    cfg = {**CFG, "clip_length": 4, "pre": 3, "segment": 6}
    s = Session("kick", "foo", tmp_path, cfg)
    base = int(time.time()) - 100
    s.seg_dir.mkdir(parents=True)
    make_ts(s.seg_dir / f"{base}.ts", 6)
    make_ts(s.seg_dir / f"{base + 6}.ts", 6)
    monkeypatch.setattr(clipper, "transcribe", lambda p, m: ([], []))
    monkeypatch.setattr(session.shorts, "render", lambda *a, **k: None)
    hooked = []
    s.on_clip = hooked.append
    s.jobs.put((base + 8, "spike", 2.0))  # t0 = base+5: 1s of seg 1 + 3s of seg 2 (input-side -ss would truncate to 1s)
    s.jobs.put(None)
    s._clip()
    assert s.clips == 1 and s.parts["ffmpeg"].detail == "1 clips"
    got = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", hooked[0]],
                         capture_output=True, text=True).stdout
    assert float(got) > 3.5


def test_reap_kills_stubborn_proc():
    class P:
        calls = []

        def poll(self):
            return None

        def terminate(self):
            self.calls.append("term")

        def wait(self, t):
            raise subprocess.TimeoutExpired("x", t)

        def kill(self):
            self.calls.append("kill")

    session.reap(P())
    assert P.calls == ["term", "kill"]


def test_record_stop_during_popen(tmp_path, monkeypatch):
    s = Session("kick", "foo", tmp_path, CFG, streamlink=script(tmp_path, "sl", "sleep 30"))
    real = subprocess.Popen

    def popen(*a, **k):
        p = real(*a, **k)
        s.stop_ev.set()  # stop() lands right as Popen returns
        return p

    monkeypatch.setattr(subprocess, "Popen", popen)
    s._record()
    assert s.proc.poll() is not None


def test_on_msg_votes_and_cooldown(tmp_path):
    s = Session("twitch", "foo", tmp_path, {**CFG, "cooldown": 60})
    s.on_msg("a", "KEKW")
    for who in ("a", "a", "b"):  # a repeat voter counts once
        s.on_msg(who, "!clip")
    assert s.jobs.empty() and s.counter.count == 4 and s.msgs[0][1] == 2
    s.votes["old"] = time.time() - 100  # a stale vote has expired
    s.on_msg("c", "clip it")
    at, trigger, base = s.jobs.get_nowait()
    assert trigger == "votes" and s.votes == {} and s.counter.last_spike == at
    for who in "abc":  # same cooldown as spikes: no second clip
        s.on_msg(who, "clip")
    assert s.jobs.empty()
    off = Session("twitch", "bar", tmp_path, {**CFG, "clip_votes": 0})
    for who in "abc":
        off.on_msg(who, "clip")
    assert off.jobs.empty()


def test_lookup_category(tmp_path, monkeypatch):
    s = Session("kick", "foo", tmp_path, CFG)
    monkeypatch.setattr(directory, "lookup", lambda p, l: None)  # offline
    s._lookup()
    monkeypatch.setattr(directory, "lookup", lambda p, l: {"category": "Chess"})
    s._lookup()
    assert s.category == "Chess"


def test_clip_sidecar(tmp_path, monkeypatch):
    cfg = {**CFG, "clip_length": 20, "pre": 5, "segment": 10, "bucket": 10}
    s = Session("twitch", "Foo", tmp_path, cfg)
    s.category = "Just Chatting"
    stub_pipeline(monkeypatch)
    monkeypatch.setattr(clipper, "transcribe", lambda p, m: ([(0, 1, "no way!")], []))
    now = time.time() - 100
    s.seg_dir.mkdir(parents=True)
    (s.seg_dir / f"{int(now) - 10}.ts").write_bytes(b"x")
    s.msgs.extend([(now - 5, 2), (now + 10, 1), (now - 50, 1), (now + 40, 1)])  # last two outside the window
    s.jobs.put((now, "votes", 1.5))
    s.jobs.put(None)
    s._clip()
    (side,) = (tmp_path / "clips").glob("*.json")
    assert hype.read(side) == {
        "score": 1.0, "msgs": 2, "baseline": 1.5, "platform": "twitch", "login": "foo", "category": "Just Chatting",
        "spike_at": round(now, 1), "title": "no way!", "status": "pending", "trigger": "votes",
        "hashtags": ["#foo", "#justchatting", "#twitch", "#clips", "#shorts"]}


def test_clip_reads_layout_at_render_time(tmp_path, monkeypatch):
    s = Session("twitch", "foo", tmp_path, {**CFG, "segment": 10})
    *_, shorts_ = stub_pipeline(monkeypatch)
    now = time.time() - 100
    s.seg_dir.mkdir(parents=True)
    (s.seg_dir / f"{int(now) - 3}.ts").write_bytes(b"x")
    crop = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
    hype.write(s.layouts, {"twitch:foo": crop, "kick:foo": {}})
    s.jobs.put((now, "spike", 2.0))
    s.jobs.put(None)
    s._clip()
    assert shorts_[0][-1] == {"crop": crop}


def test_claim_is_atomic(tmp_path):
    s = Session("twitch", "foo", tmp_path, {**CFG, "cooldown": 60})
    wins = []
    ts = [threading.Thread(target=lambda: wins.append(s.counter.claim(time.time(), 60))) for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert wins.count(True) == 1


def test_sidecar_keeps_status_set_while_whisper_ran(tmp_path):
    s = Session("twitch", "foo", tmp_path, CFG)
    dst = tmp_path / "clips" / "twitch_foo_1.mp4"
    hype.write(dst.with_suffix(".json"), {"status": "approved"})  # user approved mid-whisper
    s._sidecar(dst, [(0, 1, "hi")], time.time(), "spike", 1.0)
    d = hype.read(dst.with_suffix(".json"))
    assert d["status"] == "approved" and d["title"] == "hi"

