import hmac
import json
import mimetypes
import os
import re
import socket
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import directory, notify
from .session import Session

STATIC = Path(__file__).parent / "static"
CLIP_NAME = re.compile(r"[A-Za-z0-9_.-]+\.(mp4|vtt|srt)")
MAX_SESSIONS = 10
mimetypes.add_type("text/vtt", ".vtt")


class App:
    def __init__(self, out, cfg, dir_=None, session_cls=Session):
        self.out, self.cfg, self.session_cls = Path(out), cfg, session_cls
        self.dir = dir_ or directory.Directory()
        self.sessions, self.lock, self.fav_lock = {}, threading.Lock(), threading.Lock()
        self.fav_path = self.out / "favorites.json"
        self.following = {"category": "", "channels": []}  # served by /api/following before --follow runs

    def favorites(self):
        try:
            return json.loads(self.fav_path.read_text())
        except (OSError, ValueError):  # missing or corrupt
            return []

    def set_favorite(self, platform, login, on):
        with self.fav_lock:
            favs = [f for f in self.favorites() if f != f"{platform}:{login}"] + ([f"{platform}:{login}"] if on else [])
            self.out.mkdir(parents=True, exist_ok=True)
            tmp = self.fav_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(favs))
            os.replace(tmp, self.fav_path)
            return favs

    def start(self, platform, login):
        with self.lock:
            key = f"{platform}:{login}"
            if key not in self.sessions:
                if len(self.sessions) >= MAX_SESSIONS:
                    raise ValueError(f"at most {MAX_SESSIONS} channels at once")
                s = self.sessions[key] = self.session_cls(platform, login, self.out, self.cfg)
                s.on_clip = notify.hook(self.cfg.get("discord_webhook"), platform, login)
                s.start()
            return self.sessions[key].status()

    def stop(self, platform, login):
        with self.lock:
            s = self.sessions.pop(f"{platform}:{login}", None)
        if s:
            s.stop()
        return {"stopped": bool(s)}

    def follow(self, category, n=3, every=300, stop=None):
        """24/7 mode: keep clipping the top n live channels of a category; swap as rankings change."""
        stop = stop or threading.Event()
        followed = set()
        while True:
            top = {f"{s['platform']}:{s['login']}" for s in directory.category_top(category, n)}
            if top:  # empty = lookup failed or nobody live: keep what runs rather than thrash
                for key in followed - top:
                    self.stop(*key.split(":"))
                for key in top - followed:
                    self.start(*key.split(":"))
                followed = top
            self.following = {"category": category, "channels": sorted(followed)}
            if stop.wait(every):
                return

    def stop_all(self):
        for key in list(self.sessions):
            self.stop(*key.split(":"))

    def clips(self):
        d = self.out / "clips"
        files = sorted((p for p in d.glob("*.mp4") if not p.name.endswith((".tmp.mp4", ".short.mp4", ".part.mp4"))), key=lambda p: p.stat().st_mtime, reverse=True) if d.exists() else []
        return [{"name": p.name, "captions": p.with_suffix(".vtt").exists(), "short": p.with_suffix(".short.mp4").exists(), "platform": p.name.split("_")[0],
                 "login": "_".join(p.stem.split("_")[1:-1]), "at": p.stat().st_mtime} for p in files]


def channel(q):
    """Validate platform/login from untrusted input."""
    platform, login = q.get("platform", ""), q.get("login", "")
    if platform not in directory.PLATFORMS or not directory.NAME.fullmatch(login):
        raise ValueError("bad platform or channel name")
    return platform, login.lower()


class Handler(BaseHTTPRequestHandler):
    app: App
    hosts: frozenset
    loopback = ("127.0.0.1", "::1")  # peers that skip the --phone key
    key = None  # --phone: secret other devices must present once (then kept as a cookie)
    timeout = 10  # drop stalled clients

    def host_ok(self):
        host = self.headers.get("Host", "")
        if host not in self.hosts and self.hosts:  # empty hosts = check disabled (--no-host-check)
            self.send(403, {"error": "bad host"})  # DNS rebinding guard
            return False
        # "local" = the socket peer, never the client-supplied Host header (that was spoofable)
        if self.key is None or self.client_address[0] in self.loopback:
            return True
        jar = SimpleCookie()
        jar.load(self.headers.get("Cookie", ""))
        if "hc_key" in jar and hmac.compare_digest(jar["hc_key"].value, self.key):
            return True
        if hmac.compare_digest(parse_qs(urlparse(self.path).query).get("key", [""])[0], self.key):
            self.send_response(303)  # remember the key, drop it from the address bar
            self.send_header("Set-Cookie", f"hc_key={self.key}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict")
            self.send_header("Location", "/")
            self.end_headers()
            return False
        self.send(401, {"error": "open the link HyperCut printed (it contains the key)"})
        return False

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def file(self, path):
        self.send(200, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        app = self.app
        if not self.host_ok():
            return
        try:
            if u.path == "/":
                return self.file(STATIC / "index.html")
            if u.path in ("/logo.svg", "/favicon.svg"):
                return self.file(STATIC / "logo.svg")
            if u.path == "/api/live":
                return self.send(200, {"items": app.dir.live(), "errors": app.dir.errors, "favorites": app.favorites()})
            if u.path == "/api/lookup":
                item = directory.lookup(*channel(q))
                return self.send(200 if item else 404, item or {"error": "offline or not found"})
            if u.path == "/api/sessions":
                return self.send(200, [s.status() for s in list(app.sessions.values())])
            if u.path == "/api/following":
                return self.send(200, app.following)
            if u.path == "/api/clips":
                return self.send(200, app.clips())
            if u.path.startswith("/clips/"):
                name = u.path[len("/clips/"):]
                path = app.out / "clips" / name
                if CLIP_NAME.fullmatch(name) and path.is_file():
                    # shortcut: whole-file response, no Range; fine for 30s clips
                    return self.file(path)
            return self.send(404, {"error": "not found"})
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        except Exception as e:
            return self.send(502, {"error": str(e)})

    def do_POST(self):
        if not self.host_ok():
            return
        if self.headers.get_content_type() != "application/json":
            return self.send(415, {"error": "json only"})
        origin = self.headers.get("Origin")
        if self.hosts and origin is not None and origin not in {f"http://{h}" for h in self.hosts}:
            return self.send(403, {"error": "bad origin"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 <= n <= 4096:
                return self.send(413, {"error": "body too large"})
            body = json.loads(self.rfile.read(n) or b"{}")
            platform, login = channel(body)
            if self.path == "/api/start":
                return self.send(200, self.app.start(platform, login))
            if self.path == "/api/stop":
                return self.send(200, self.app.stop(platform, login))
            if self.path == "/api/favorite":
                return self.send(200, self.app.set_favorite(platform, login, bool(body.get("on"))))
            return self.send(404, {"error": "not found"})
        except (ValueError, AttributeError) as e:
            return self.send(400, {"error": str(e)})


def make_server(app, host="127.0.0.1", port=8787, lan_ip=None, key=None, host_check=True):
    handler = type("BoundHandler", (Handler,), {"app": app, "key": key})
    srv = ThreadingHTTPServer((host, port), handler)
    p = srv.server_address[1]
    handler.hosts = frozenset({f"127.0.0.1:{p}", f"localhost:{p}"} | ({f"{lan_ip}:{p}"} if lan_ip else set())) if host_check else frozenset()
    return srv


def lan_ip():
    """This machine's address on the local network (no packets are sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
