import argparse
import os
import secrets
import signal
import threading
import webbrowser

from .notify import PREFIXES
from .web import App, lan_ip, make_server


def parse(argv=None):
    p = argparse.ArgumentParser(prog="hypercut", description="Browse live Twitch/Kick streams and auto-clip chat spikes.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--out", default=".", help="folder for recordings/ and clips/")
    p.add_argument("--bucket", type=int, default=10, help="seconds per chat-count bucket")
    p.add_argument("--clip-length", type=int, default=30)
    p.add_argument("--pre", type=int, default=20, help="seconds of the clip before the spike")
    p.add_argument("--multiplier", type=float, default=2.0, help="spike = bucket >= multiplier x baseline")
    p.add_argument("--min-msgs", type=int, default=10, help="spike needs at least this many msgs per bucket")
    p.add_argument("--cooldown", type=int, default=30, help="min seconds between spikes")
    p.add_argument("--quality", default="best")
    p.add_argument("--whisper-model", default="base")
    p.add_argument("--follow", metavar="CATEGORY", help='24/7: auto-clip the top channels of a category, e.g. "Grand Theft Auto VI"')
    p.add_argument("--top", type=int, default=3, help="how many top channels --follow clips at once")
    p.add_argument("--phone", action="store_true", help="also serve on your Wi-Fi with a secret link for your phone")
    p.add_argument("--no-host-check", action="store_true", help="behind a trusted preview proxy/tunnel only")
    p.add_argument("--discord-webhook", metavar="URL", default=os.environ.get("HYPERCUT_DISCORD_WEBHOOK"), help="post each finished short to this Discord webhook")
    p.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    a = p.parse_args(argv)
    if a.discord_webhook and not a.discord_webhook.startswith(PREFIXES):
        p.error("--discord-webhook must start with https://discord.com/api/webhooks/")
    cfg = {k: getattr(a, k) for k in ("bucket", "clip_length", "pre", "multiplier", "min_msgs", "cooldown", "quality", "whisper_model", "discord_webhook")}
    cfg.update(restart_delay=30, segment=10, retention=600)
    return a, cfg


def _term(*_):
    raise KeyboardInterrupt


def main(argv=None, serve=True):
    a, cfg = parse(argv)
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
        pass
    finally:
        app.stop_all()
        srv.server_close()
    return app


def cli():
    """Console-script entry: exit status 0, don't print the App."""
    main()


if __name__ == "__main__":
    main()
