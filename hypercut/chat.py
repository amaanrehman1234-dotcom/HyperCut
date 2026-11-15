import random
from collections import deque

from websockets.sync.client import connect

from .net import get_json

TWITCH_IRC = "wss://irc-ws.chat.twitch.tv:443"
KICK = "https://kick.com"


def count_privmsg(frame, send):
    """Twitch IRC frame -> number of chat messages; answers PING."""
    n = 0
    for line in frame.split("\r\n"):
        if line.startswith("PING"):
            send("PONG" + line[4:])
        elif " PRIVMSG #" in line:
            n += 1
    return n


def twitch_chat(login, on_msgs, stop, url=TWITCH_IRC):
    """Read chat anonymously (justinfan nick, no token) until stop is set."""
    with connect(url, open_timeout=15) as ws:
        ws.send("PASS SCHMOOPIIE")
        ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
        ws.send(f"JOIN #{login}")
        while not stop.is_set():
            try:
                frame = ws.recv(timeout=1)
            except TimeoutError:
                continue
            if n := count_privmsg(frame, ws.send):
                on_msgs(n)


def kick_chat(slug, on_msgs, stop, interval=1.0):
    """Poll Kick's public recent-messages endpoint; count unseen ids."""
    # shortcut: endpoint returns the latest 25 msgs, so >25 msgs/interval undercounts; switch to websocket if Kick reopens it
    cid = get_json(f"{KICK}/api/v2/channels/{slug}")["id"]
    seen = deque(maxlen=500)
    first = True
    while not stop.is_set():
        msgs = get_json(f"{KICK}/api/v2/channels/{cid}/messages")["data"]["messages"]
        new = [m["id"] for m in msgs if m["id"] not in seen]
        seen.extend(new)
        if new and not first:
            on_msgs(len(new))
        first = False
        stop.wait(interval)


def run_forever(fn, part, stop, backoff=5):
    """Keep a chat reader alive with reconnects; reflects state on part."""
    while not stop.is_set():
        try:
            part.set("ok", "connected")
            fn()
        except Exception as e:
            part.set("error", f"reconnecting: {e}"[:120])
            stop.wait(backoff)
        else:
            return
