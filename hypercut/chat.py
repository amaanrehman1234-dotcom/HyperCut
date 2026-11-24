"""Anonymous chat readers for Twitch (IRC over WebSocket) and Kick (polling)."""
import random
import re
from collections import deque

from websockets.sync.client import connect

from .directory import KICK
from .net import get_json

KICK_EMOTE = re.compile(r"\[emote:\d+:([^\]]+)\]")
TWITCH_IRC = "wss://irc-ws.chat.twitch.tv:443"


def parse_privmsg(frame, send):
    """Twitch IRC frame -> [(nick, text)] chat messages; answers PING."""
    out = []
    for line in frame.split("\r\n"):
        if line.startswith("PING"):
            send("PONG" + line[4:])
        elif " PRIVMSG #" in line:
            head, _, text = line.partition(" PRIVMSG #")
            out.append((head.split("!")[0].lstrip(":"), text.partition(" :")[2]))
    return out


def twitch_chat(login, on_msg, stop, url=TWITCH_IRC):
    """Read chat anonymously (justinfan nick, no token) until stop is set; calls on_msg(nick, text)."""
    with connect(url, open_timeout=15) as ws:
        ws.send("PASS SCHMOOPIIE")
        ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
        ws.send(f"JOIN #{login}")
        while not stop.is_set():
            try:
                frame = ws.recv(timeout=1)
            except TimeoutError:
                continue
            for who, text in parse_privmsg(frame, ws.send):
                on_msg(who, text)


def kick_chat(slug, on_msg, stop, interval=1.0):
    """Poll Kick's public recent-messages endpoint; calls on_msg(sender id, text) for unseen ids."""
    # shortcut: endpoint returns the latest 25 msgs, so >25 msgs/interval undercounts;
    # switch to websocket if Kick reopens it
    cid = get_json(f"{KICK}/api/v2/channels/{slug}")["id"]
    seen = deque(maxlen=500)
    first = True
    while not stop.is_set():
        msgs = get_json(f"{KICK}/api/v2/channels/{cid}/messages")["data"]["messages"]
        new = [m for m in msgs if m["id"] not in seen]
        seen.extend(m["id"] for m in new)
        if not first:
            for m in new:
                on_msg(m["sender"]["id"], KICK_EMOTE.sub(r"\1", m["content"]))
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
