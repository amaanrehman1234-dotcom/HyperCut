import http.client
import json
import os
import runpy
import signal
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from hypercut import __main__ as cli
from hypercut import directory, web


class FakeSession:
    def __init__(self, platform, login, out, cfg):
        self.k, self.stopped = f"{platform}:{login}", False

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def wait(self, timeout):
        self.waited = timeout

    def status(self):
        return {"key": self.k}


@pytest.fixture(autouse=True)
def tools_on_path(monkeypatch):
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))  # check_tools edits PATH; put it back after each test
    monkeypatch.setattr(cli.shutil, "which", lambda tool: f"/bin/{tool}")


class FakeDir:
    errors = {"x": "y"}

    def live(self):
        return [{"login": "a"}]


@pytest.fixture
def srv(tmp_path):
    app = web.App(tmp_path, {}, FakeDir(), FakeSession)
    s = web.make_server(app, port=0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield app, f"http://127.0.0.1:{s.server_address[1]}"
    s.shutdown()
    s.server_close()


def req(base, path, body=None, raw=None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json", **(headers or {})}
    try:
        r = urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=h), timeout=5)
    except urllib.error.HTTPError as e:
        r = e
    b = r.read()
    return r.status, (json.loads(b) if r.headers["Content-Type"] == "application/json" else b)


def test_routes(srv, tmp_path, monkeypatch):
    app, b = srv
    assert req(b, "/")[0] == 200 and req(b, "/logo.svg")[0] == 200 and req(b, "/favicon.svg")[0] == 200
    assert req(b, "/api/live") == (200, {"items": [{"login": "a"}], "errors": {"x": "y"}, "favorites": []})
    assert req(b, "/api/clips") == (200, [])  # no clips dir
    assert req(b, "/api/sessions") == (200, [])
    assert req(b, "/api/following") == (200, {"category": "", "channels": []})
    app.following = {"category": "G", "channels": ["kick:a"]}
    assert req(b, "/api/following") == (200, app.following)

    monkeypatch.setattr(directory, "lookup", lambda p, l: {"login": l} if l == "on" else None)
    assert req(b, "/api/lookup?platform=twitch&login=ON") == (200, {"login": "on"})
    assert req(b, "/api/lookup?platform=twitch&login=off")[0] == 404
    assert req(b, "/api/lookup?platform=nope&login=x")[0] == 400

    def boom(*a):
        raise OSError("down")

    monkeypatch.setattr(directory, "lookup", boom)
    assert req(b, "/api/lookup?platform=kick&login=x") == (502, {"error": "down"})

    clips = tmp_path / "clips"
    clips.mkdir()
    (clips / "twitch_a_b_20240101-000000.mp4").write_bytes(b"v")
    (clips / "twitch_a_b_20240101-000000.vtt").write_text("WEBVTT")
    (tmp_path / "secret.mp4").write_bytes(b"s")
    got = req(b, "/api/clips")[1]
    assert got[0]["captions"] and got[0]["login"] == "a_b" and got[0]["platform"] == "twitch"
    assert req(b, "/clips/twitch_a_b_20240101-000000.mp4") == (200, b"v")
    assert req(b, "/clips/twitch_a_b_20240101-000000.vtt")[0] == 200
    assert req(b, "/clips/..%2Fsecret.mp4")[0] == 404
    assert req(b, "/clips/missing.mp4")[0] == 404 and req(b, "/nope")[0] == 404

    assert req(b, "/api/start", {"platform": "twitch", "login": "Foo"}) == (200, {"key": "twitch:foo"})
    req(b, "/api/start", {"platform": "twitch", "login": "foo"})
    assert req(b, "/api/sessions") == (200, [{"key": "twitch:foo"}])
    assert req(b, "/api/favorite", {"platform": "twitch", "login": "foo", "on": 1}) == (200, ["twitch:foo"])
    assert req(b, "/api/favorite", {"platform": "twitch", "login": "foo"}) == (200, [])
    assert req(b, "/api/live")[1]["favorites"] == []
    assert req(b, "/api/stop", {"platform": "twitch", "login": "foo"}) == (200, {"stopped": True})
    assert req(b, "/api/stop", {"platform": "twitch", "login": "foo"}) == (200, {"stopped": False})
    assert req(b, "/api/other", {"platform": "twitch", "login": "foo"})[0] == 404
    assert req(b, "/api/start", {"platform": "x", "login": "foo"})[0] == 400
    assert req(b, "/api/start", raw=b"{bad")[0] == 400
    assert req(b, "/api/start", raw=b"[]")[0] == 400  # AttributeError
    assert req(b, "/api/start", raw=b"")[0] == 400  # empty -> {} -> bad channel
    assert req(b, "/clips/x.mp4\n")[0] == 404  # fullmatch: no trailing-newline slip


def test_security_headers(srv):
    _, b = srv
    for path in ("/", "/api/live", "/nope"):
        try:
            r = urllib.request.urlopen(b + path)
        except urllib.error.HTTPError as e:
            r = e
        assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_layout_and_frame(srv, tmp_path):
    app, b = srv
    ch = {"platform": "twitch", "login": "Foo"}
    assert req(b, "/api/layout?platform=twitch&login=foo") == (200, {"crop": None})
    assert req(b, "/api/layout", {**ch, "crop": {"x": 0.9, "y": 0, "w": 0.3, "h": 0.2}}) == (
        200, {"crop": {"x": 0.7, "y": 0.0, "w": 0.3, "h": 0.2}})
    assert req(b, "/api/layout?platform=twitch&login=foo")[1]["crop"]["x"] == 0.7
    assert req(b, "/api/layout", {**ch, "crop": {"x": "a"}})[0] == 400
    assert req(b, "/api/layout", ch)[0] == 400  # crop missing
    assert req(b, "/api/layout", {"platform": "x", "login": "foo", "crop": None})[0] == 400
    assert req(b, "/api/layout", {**ch, "crop": None}) == (200, {"crop": None})
    assert (tmp_path / "layouts.json").read_text() == "{}"
    (tmp_path / "layouts.json").write_text("{corrupt")
    assert req(b, "/api/layout?platform=twitch&login=foo") == (200, {"crop": None})
    assert req(b, "/api/layout", {**ch, "crop": None}, headers={"Origin": "http://evil.com"})[0] == 403

    assert req(b, "/api/frame?platform=twitch&login=foo")[0] == 404  # no footage
    assert req(b, "/api/frame?platform=nope&login=foo")[0] == 400
    seg = tmp_path / "recordings" / "twitch_foo"
    seg.mkdir(parents=True)
    (seg / "1.ts").write_bytes(b"junk")
    assert req(b, "/api/frame?platform=twitch&login=foo")[0] == 404  # undecodable
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=10",
                    "-c:v", "mpeg2video", str(seg / "2.ts")], check=True)
    code, jpg = req(b, "/api/frame?platform=twitch&login=foo")
    assert code == 200 and jpg[:2] == b"\xff\xd8"


def test_guards(srv):
    app, b = srv
    port = b.rsplit(":", 1)[1]
    ok = {"platform": "twitch", "login": "foo"}
    assert req(b, "/api/live", headers={"Host": "evil.com"})[0] == 403
    assert req(b, "/api/start", ok, headers={"Host": "evil.com"})[0] == 403
    assert req(b, "/api/live", headers={"Host": f"localhost:{port}"})[0] == 200
    assert req(b, "/api/start", ok, headers={"Content-Type": "text/plain"})[0] == 415
    assert req(b, "/api/start", ok, headers={"Origin": "http://evil.com"})[0] == 403
    assert req(b, "/api/start", ok, headers={"Origin": f"http://localhost:{port}"})[0] == 200
    assert req(b, "/api/start", raw=b" " * 4097)[0] == 413
    assert req(b, "/api/start", headers={"Content-Length": "-1"}, raw=b"")[0] == 413


def test_session_cap(tmp_path):
    app = web.App(tmp_path, {}, FakeDir(), FakeSession)
    for i in range(web.MAX_SESSIONS):
        app.start("kick", f"a{i}")
    app.start("kick", "a0")  # existing is fine
    with pytest.raises(ValueError):
        app.start("kick", "extra")


def test_favorites_robust(tmp_path):
    app = web.App(tmp_path, {}, FakeDir(), FakeSession)
    app.out.mkdir(exist_ok=True)
    app.fav_path.write_text("{corrupt")
    assert app.favorites() == []
    assert app.set_favorite("kick", "a", True) == ["kick:a"] and not app.fav_path.with_suffix(".tmp").exists()


def test_clips_skip_tmp(tmp_path):
    d = tmp_path / "clips"
    app = web.App(tmp_path, {})
    assert app.clips() == []  # no clips/ folder yet
    d.mkdir()
    junk = ("kick_a_1.tmp.mp4", "kick_a_1.short.part.mp4", "kick_a_1.short.ass", "kick_a_9.short.mp4")
    for n in junk:
        (d / n).write_bytes(b"x")
    assert app.clips() == []  # half-written files are never listed, even mid-render
    (d / "kick_a_2.mp4").write_bytes(b"x")
    (d / "kick_a_3.mp4").write_bytes(b"x")
    (d / "kick_a_3.short.mp4").write_bytes(b"x")
    flags = {c["name"]: c["short"] for c in web.App(tmp_path, {}).clips()}
    assert flags == {"kick_a_2.mp4": False, "kick_a_3.mp4": True}


def test_stop_all(tmp_path):
    app = web.App(tmp_path, {}, FakeDir(), FakeSession)
    app.start("kick", "a")
    app.start("kick", "b")
    a, b = app.sessions["kick:a"], app.sessions["kick:b"]
    app.stop_all(60)
    assert a.stopped and b.stopped and not app.sessions
    assert 59 < b.waited <= a.waited <= 60  # one shared deadline, not 60s each


def test_temp_files_swept_on_start_and_exit(tmp_path):
    d = tmp_path / "clips"
    d.mkdir()
    junk = [d / n for n in ("kick_a_1.tmp.mp4", "kick_a_1.short.part.mp4", "kick_a_1.short.ass")]
    keep = [d / n for n in ("kick_a_1.mp4", "kick_a_1.short.mp4", "kick_a_1.srt", "kick_a_1.vtt")]
    for p in junk + keep:
        p.write_bytes(b"x")
    app = web.App(tmp_path, {}, FakeDir(), FakeSession)  # startup: a killed run left these behind
    assert not any(p.exists() for p in junk) and all(p.exists() for p in keep)
    for p in junk:
        p.write_bytes(b"x")
    app.stop_all()
    assert not any(p.exists() for p in junk) and all(p.exists() for p in keep)
    assert isinstance(web.App(tmp_path, {}).dir, directory.Directory)


def test_cli(tmp_path, monkeypatch):
    a, cfg = cli.parse(["--port", "1", "--bucket", "5"])
    assert a.port == 1 and a.multiplier == 2.0 and cfg["bucket"] == 5 and cfg["restart_delay"] == 30
    assert cfg["clip_votes"] == 3 and cli.parse(["--clip-votes", "0"])[1]["clip_votes"] == 0
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    app = cli.main(["--port", "0", "--out", str(tmp_path), "--open"], serve=False)
    assert opened[0].startswith("http://127.0.0.1:") and isinstance(app, web.App)
    cli.main(["--port", "0", "--out", str(tmp_path)], serve=False)
    assert len(opened) == 1

    class Srv:
        server_address = ("h", 9)
        closed = False

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            self.closed = True

    s = Srv()
    monkeypatch.setattr(cli, "make_server", lambda *a, **k: s)
    cli.main(["--out", str(tmp_path)])
    assert s.closed

    monkeypatch.setattr(web, "make_server", lambda *a, **k: s)
    monkeypatch.setattr("sys.argv", ["hypercut", "--out", str(tmp_path)])
    runpy.run_module("hypercut", run_name="__main__")

    old = signal.getsignal(signal.SIGTERM)
    s.closed = False
    s.serve_forever = lambda: os.kill(os.getpid(), signal.SIGTERM)
    try:
        cli.main(["--out", str(tmp_path)])  # SIGTERM acts like Ctrl+C
    finally:
        signal.signal(signal.SIGTERM, old)
    assert s.closed


def test_follow_swaps_channels(tmp_path, monkeypatch):
    log = []

    class Sess(FakeSession):
        def __init__(self, platform, login, out, cfg):
            super().__init__(platform, login, out, cfg)
            log.append(("start", self.k))

        def stop(self):
            log.append(("stop", self.k))

    stop = threading.Event()
    rounds = iter([("a", "b"), ("b", "c"), ()])

    def top(cat, n):
        r = next(rounds)
        if not r:
            stop.set()  # last lookup: nobody live, loop ends after it
        assert (cat, n) == ("G", 2)
        return [{"platform": "twitch", "login": x} for x in r]

    monkeypatch.setattr(directory, "category_top", top)
    app = web.App(tmp_path, {}, FakeDir(), Sess)
    app.follow("G", 2, every=0.01, stop=stop)
    assert sorted(log[:2]) == [("start", "twitch:a"), ("start", "twitch:b")]
    assert sorted(log[2:]) == [("start", "twitch:c"), ("stop", "twitch:a")]  # b kept, empty round changes nothing
    assert app.following == {"category": "G", "channels": ["twitch:b", "twitch:c"]}
    assert set(app.sessions) == {"twitch:b", "twitch:c"}


def test_follow_default_stop_event(tmp_path, monkeypatch):
    monkeypatch.setattr(directory, "category_top", lambda c, n: [])

    class Ev(threading.Event):
        def wait(self, t=None):
            return True  # returns after one round

    monkeypatch.setattr(web.threading, "Event", Ev)
    app = web.App(tmp_path, {}, FakeDir(), FakeSession)
    app.follow("G", every=0)
    assert app.following == {"category": "G", "channels": []}


def test_cli_follow(tmp_path, monkeypatch):
    got, ev = [], threading.Event()
    monkeypatch.setattr(cli.App, "follow", lambda self, *a: (got.append(a), ev.set()))
    cli.main(["--port", "0", "--out", str(tmp_path), "--follow", "G", "--top", "2"], serve=False)
    assert ev.wait(5) and got == [("G", 2)]


def raw_get(srv, path, host, method="GET", headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    c.request(method, path, body=body, headers={"Host": host, **(headers or {})})
    r = c.getresponse()
    out = r.status, dict(r.getheaders()), r.read()
    c.close()
    return out


def serve_with(tmp_path, **kw):
    s = web.make_server(web.App(tmp_path, {}, FakeDir(), FakeSession), port=0, **kw)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def test_phone_key_flow(tmp_path):
    s = serve_with(tmp_path, lan_ip="192.168.1.5", key="sekret")
    try:
        p = s.server_address[1]
        assert raw_get(s, "/api/live", f"192.168.1.5:{p}")[0] == 200  # real loopback peer needs no key
        s.RequestHandlerClass.loopback = ()  # from here on, act as a LAN peer
        lan = f"192.168.1.5:{p}"
        assert raw_get(s, "/api/live", lan)[0] == 401
        assert raw_get(s, "/api/live?key=wrong", lan)[0] == 401
        code, h, _ = raw_get(s, "/api/live?key=sekret", lan)
        assert code == 303 and h["Location"] == "/"
        assert "hc_key=sekret" in h["Set-Cookie"] and "HttpOnly" in h["Set-Cookie"]
        assert raw_get(s, "/api/live", lan, headers={"Cookie": "a=b; hc_key=sekret"})[0] == 200
        # regression: a LAN peer spoofing a loopback Host header must still need the key
        assert raw_get(s, "/api/live", f"localhost:{p}")[0] == 401
        assert raw_get(s, "/api/live", f"127.0.0.1:{p}")[0] == 401
        assert raw_get(s, "/api/live", "evil.com")[0] == 403
    finally:
        s.shutdown()
        s.server_close()


def test_no_host_check(tmp_path):
    s = serve_with(tmp_path, host_check=False)
    try:
        h = {"Content-Type": "application/json", "Origin": "https://preview.example"}
        assert raw_get(s, "/api/live", "anything.example")[0] == 200
        start = json.dumps({"platform": "kick", "login": "a"})
        code, _, body = raw_get(s, "/api/start", "anything.example", "POST", h, start)
        assert code == 200 and json.loads(body) == {"key": "kick:a"}
    finally:
        s.shutdown()
        s.server_close()


def test_lan_ip(monkeypatch):
    class Sock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def connect(self, addr):
            self.addr = addr

        def getsockname(self):
            return ("192.168.9.9", 1234)

    monkeypatch.setattr(web.socket, "socket", lambda *a: Sock())
    assert web.lan_ip() == "192.168.9.9"


def test_cli_phone_and_no_host_check(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "lan_ip", lambda: "10.0.0.2")
    app = cli.main(["--port", "0", "--out", str(tmp_path), "--phone"], serve=False)
    out = capsys.readouterr().out
    assert "http://10.0.0.2:" in out and "/?key=" in out and isinstance(app, web.App)

    seen = {}
    monkeypatch.setattr(cli, "make_server", lambda *a, **k: seen.update(k=k) or type("S", (), {
        "server_address": ("h", 9), "server_close": lambda self: None})())
    cli.main(["--out", str(tmp_path), "--no-host-check"], serve=False)
    assert seen["k"] == {"host_check": False}


def test_discord_webhook(tmp_path, monkeypatch):
    from hypercut import notify
    from hypercut.web import App
    for bad in ("http://x", "https://evil.com/api/webhooks/1"):
        with pytest.raises(SystemExit):
            cli.parse(["--discord-webhook", bad])
    ok = "https://discord.com/api/webhooks/1/x"
    assert cli.parse(["--discord-webhook", ok])[1]["discord_webhook"] == ok
    monkeypatch.setenv("HYPERCUT_DISCORD_WEBHOOK", ok)
    assert cli.parse([])[1]["discord_webhook"] == ok
    assert notify.hook(None, "twitch", "a") is None
    sent = []
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda r, timeout: sent.append(r) or open(__file__, "rb"))
    clip = tmp_path / "c.mp4"
    short = tmp_path / "c.short.mp4"
    short.write_bytes(b"VIDEO")
    notify.hook(ok, "twitch", "a")(clip)
    body = sent[0].data
    assert sent[0].full_url == ok and b"\xf0\x9f\x94\xa5 Clip from twitch/a" in body
    assert b'name="files[0]"' in body and b"VIDEO" in body
    monkeypatch.setattr(notify, "MAX_BYTES", 1)
    notify.hook(ok, "twitch", "a")(clip)
    assert b"files[0]" not in sent[1].data
    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda r, timeout: 1 / 0)
    notify.hook(ok, "twitch", "a")(clip)  # swallowed
    created = []
    class S:
        def __init__(self, *a): created.append(self)
        def start(self): pass
        def status(self): return {}
    app = App(tmp_path, {"discord_webhook": ok}, dir_=object(), session_cls=S)
    app.start("twitch", "z")
    assert created[0].on_clip


def test_cli_version_help_and_missing_tools(tmp_path, monkeypatch, capsys):
    import hypercut
    with pytest.raises(SystemExit) as e:
        cli.parse(["--version"])
    assert e.value.code == 0 and capsys.readouterr().out == f"hypercut {hypercut.__version__}\n" == "hypercut 0.2.0\n"
    with pytest.raises(SystemExit):
        cli.parse(["--help"])
    out = capsys.readouterr().out
    assert all(f"\n{g}:\n" in out for g in ("Dashboard", "Clipping", "24/7", "Notifications"))

    monkeypatch.setenv("PATH", "/usr/bin" + os.pathsep + "/usr/bin")
    monkeypatch.setattr(cli.sys, "executable", "/venv/bin/python")
    cli.check_tools()  # both found: our Python's folder goes first on PATH, once
    cli.check_tools()
    assert os.environ["PATH"] == "/venv/bin" + os.pathsep + "/usr/bin"

    monkeypatch.setattr(cli.shutil, "which", lambda tool: None if tool == "ffmpeg" else "/bin/x")
    with pytest.raises(SystemExit) as e:
        cli.main(["--port", "0", "--out", str(tmp_path)], serve=False)
    assert "ffmpeg not found" in e.value.code and "streamlink" not in e.value.code  # a str code exits 1
    monkeypatch.setattr(cli.shutil, "which", lambda tool: None)
    with pytest.raises(SystemExit) as e:
        cli.check_tools()
    assert "ffmpeg not found" in e.value.code and "streamlink not found" in e.value.code


def test_cli_entry_returns_none(monkeypatch):
    import hypercut.__main__ as m
    called = []
    monkeypatch.setattr(m, "main", lambda: called.append(1) or "app")
    assert m.cli() is None and called == [1]  # sys.exit(None) -> status 0


def test_clip_status(srv):
    app, b = srv
    d = app.out / "clips"
    d.mkdir()
    (d / "kick_a_b_1.mp4").write_bytes(b"v")
    (d / "kick_a_b_2.mp4").write_bytes(b"v")
    (d / "kick_a_b_2.json").write_text('{"score": 3.5, "status": "pending", "title": "hi"}')
    got = {c["name"]: c for c in req(b, "/api/clips")[1]}
    assert got["kick_a_b_1.mp4"]["status"] == "pending" and "score" not in got["kick_a_b_1.mp4"]
    assert got["kick_a_b_2.mp4"]["score"] == 3.5 and got["kick_a_b_2.mp4"]["login"] == "a_b"
    assert req(b, "/api/clip-status", {"name": "kick_a_b_2.mp4", "status": "approved"}) == (
        200, {"name": "kick_a_b_2.mp4", "status": "approved"})
    assert req(b, "/api/clip-status", {"name": "kick_a_b_1.mp4", "status": "rejected"})[0] == 200  # no sidecar yet
    got = {c["name"]: c for c in req(b, "/api/clips")[1]}
    assert got["kick_a_b_2.mp4"]["status"] == "approved" and got["kick_a_b_2.mp4"]["title"] == "hi"
    assert got["kick_a_b_1.mp4"]["status"] == "rejected" and not list(d.glob("*.tmp"))
    for body in ({"name": "kick_a_b_2.mp4", "status": "bogus"}, {"name": "kick_a_b_2.mp4", "status": ["x"]},
                 {"name": "missing.mp4", "status": "approved"}, {"name": "../x.mp4", "status": "approved"},
                 {"name": "kick_a_b_2.json", "status": "approved"}, {"name": 5, "status": "approved"}, [1]):
        assert req(b, "/api/clip-status", body)[0] == 400, body
    assert req(b, "/api/clip-status", {"name": "kick_a_b_2.mp4", "status": "pending"},
               headers={"Origin": "http://evil.com"})[0] == 403
    assert req(b, "/api/clip-status", {"name": "kick_a_b_2.mp4", "status": "pending"},
               headers={"Content-Type": "text/plain"})[0] == 415
    (d / "x.json.tmp").write_text("{}")
    app.sweep()
    assert not (d / "x.json.tmp").exists()
