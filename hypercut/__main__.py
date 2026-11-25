"""Command line entry: parse flags, start the dashboard, clean up on Ctrl+C or SIGTERM."""
import argparse
import os
import secrets
import shutil
import signal
import sys
import threading
import webbrowser

from . import __version__
from .notify import PREFIXES
from .web import App, lan_ip, make_server

TOOLS = {"ffmpeg": "install it from https://ffmpeg.org/download.html or your package manager",
         "streamlink": "install it with: pip install streamlink"}
CFG_KEYS = ("bucket", "clip_length", "pre", "multiplier", "min_msgs", "cooldown", "quality", "whisper_model",
            "discord_webhook", "clip_votes", "translate", "max_sessions", "min_score", "top_per_hour")
SHUTDOWN_WAIT = 60  # seconds clip workers get to finish on exit


def parse(argv=None):
    """Parse command line flags into (args, cfg dict for sessions)."""
    p = argparse.ArgumentParser(prog="hypercut",
                                description="Browse live Twitch/Kick streams and auto-clip chat spikes.")
    p.add_argument("--version", action="version", version=f"hypercut {__version__}")

    g = p.add_argument_group("Dashboard")
    g.add_argument("--host", default="127.0.0.1", help="address to listen on (default: %(default)s)")
    g.add_argument("--port", type=int, default=8787, help="port to listen on (default: %(default)s)")
    g.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    g.add_argument("--phone", action="store_true", help="also serve on your Wi-Fi with a secret link for your phone")
    g.add_argument("--no-host-check", action="store_true", help="behind a trusted preview proxy/tunnel only")

    g = p.add_argument_group("Clipping")
    g.add_argument("--out", default=".", metavar="DIR", help="folder for recordings/ and clips/ (default: here)")
    g.add_argument("--multiplier", metavar="X", type=float, default=2.0,
                   help="spike = bucket >= multiplier x baseline (default: %(default)s)")
    g.add_argument("--min-msgs", metavar="N", type=int, default=10,
                   help="spike needs at least this many msgs per bucket (default: %(default)s)")
    g.add_argument("--cooldown", metavar="SEC", type=int, default=30,
                   help="min seconds between spikes (default: %(default)s)")
    g.add_argument("--bucket", metavar="SEC", type=int, default=10,
                   help="seconds per chat-count bucket (default: %(default)s)")
    g.add_argument("--clip-votes", metavar="N", type=int, default=3,
                   help="clip at once when N chatters say !clip within 15 s; 0 = off (default: %(default)s)")
    g.add_argument("--min-score", metavar="X", type=float, default=0,
                   help="drop clips with a hype score below X before captioning; 0 = keep all (default: %(default)s)")
    g.add_argument("--top-per-hour", metavar="N", type=int, default=0,
                   help="keep only the N best clips per channel per hour; 0 = off (default: %(default)s)")
    g.add_argument("--clip-length", metavar="SEC", type=int, default=30, help="seconds per clip (default: %(default)s)")
    g.add_argument("--pre", metavar="SEC", type=int, default=20,
                   help="seconds of the clip before the spike (default: %(default)s)")
    g.add_argument("--quality", metavar="NAME", default="best", help="streamlink quality (default: %(default)s)")
    g.add_argument("--whisper-model", metavar="NAME", default="base",
                   help="caption model: tiny is fastest, small and up caption better (default: %(default)s)")
    g.add_argument("--translate", action="store_true", help="captions and titles in English, whatever language is spoken")

    g = p.add_argument_group("24/7")
    g.add_argument("--follow", metavar="CATEGORY",
                   help='auto-clip the top channels of a category, e.g. "Grand Theft Auto VI"')
    g.add_argument("--max-sessions", metavar="N", type=int, default=10,
                   help="most channels clipped at once; each needs ~125 MB RAM (default: %(default)s)")
    g.add_argument("--buffer", metavar="SEC", type=int, default=600,
                   help="seconds of recording kept per channel; >= 120 (default: %(default)s)")
    g.add_argument("--top", metavar="N", type=int, default=3,
                   help="how many top channels --follow clips at once (default: %(default)s)")

    g = p.add_argument_group("Notifications")
    g.add_argument("--discord-webhook", metavar="URL", default=os.environ.get("HYPERCUT_DISCORD_WEBHOOK"),
                   help="post each finished short to this Discord webhook (or set HYPERCUT_DISCORD_WEBHOOK)")
    a = p.parse_args(argv)
    if a.discord_webhook and not a.discord_webhook.startswith(PREFIXES):
        p.error("--discord-webhook must start with https://discord.com/api/webhooks/")
    cfg = {k: getattr(a, k) for k in CFG_KEYS}
    cfg.update(restart_delay=30, segment=10, retention=max(a.buffer, 120))
    return a, cfg


def check_tools():
    """Exit with a plain message if ffmpeg or streamlink can't be found."""
    # pipx/venv installs put streamlink next to our Python, which isn't always on PATH
    dirs = dict.fromkeys([os.path.dirname(sys.executable), *os.environ.get("PATH", "").split(os.pathsep)])
    os.environ["PATH"] = os.pathsep.join(dirs)
    missing = [f"hypercut: {tool} not found on PATH; {fix}" for tool, fix in TOOLS.items() if not shutil.which(tool)]
    if missing:
        sys.exit("\n".join(missing))


def _term(*_):
    raise KeyboardInterrupt


def main(argv=None, serve=True):
    """Run HyperCut until Ctrl+C or SIGTERM; returns the App (tests pass serve=False)."""
    a, cfg = parse(argv)
    check_tools()
    app = App(a.out, cfg)
    if a.phone:
        ip, key = lan_ip(), secrets.token_urlsafe(12)
        srv = make_server(app, "0.0.0.0", a.port, ip, key)
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        print(f"On your phone (same Wi-Fi), open:  http://{ip}:{srv.server_address[1]}/?key={key}")
    else:
        srv = make_server(app, a.host, a.port, host_check=not a.no_host_check)
        url = f"http://{a.host}:{srv.server_address[1]}"
    print(f"HyperCut running at {url}  (Ctrl+C to quit)")
    if a.follow:
        threading.Thread(target=app.follow, args=(a.follow, a.top), daemon=True).start()
        print(f"Following top {a.top} live in {a.follow!r}")
    if a.open:
        webbrowser.open(url)
    try:
        if serve:
            signal.signal(signal.SIGTERM, _term)  # so `kill` cleans up like Ctrl+C
            srv.serve_forever()
    except KeyboardInterrupt:
        print(f"\nStopping. Waiting up to {SHUTDOWN_WAIT}s for clips in progress, then tidying clips/.")
    finally:
        app.stop_all(SHUTDOWN_WAIT)
        srv.server_close()
    return app


def cli():
    """Console-script entry: exit status 0, don't print the App."""
    main()


if __name__ == "__main__":
    main()
