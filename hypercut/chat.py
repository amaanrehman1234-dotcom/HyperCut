"""Anonymous chat readers for Twitch (IRC over WebSocket) and Kick (Pusher WebSocket)."""
import json
import random
import re
from websockets.sync.client import connect

from .directory import KICK
from .net import get_json

KICK_EMOTE = re.compile(r"\[emote:\d+:([^\]]+)\]")
TWITCH_IRC = "wss://irc-ws.chat.twitch.tv:443"
# Kick rotates this public key now and then (32cbd69e... died 2026); 4001 "not in this cluster" = rotated
KICK_PUSHER = "wss://ws-us2.pusher.com/app/34bf7a0ff419a2a775b9?protocol=7&client=js&version=8.4.0&flash=false"


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


def kick_chat(slug, on_msg, stop, url=KICK_PUSHER):
    """Kick chat live over its public Pusher socket (no account); calls on_msg(sender id, text)."""
    room = get_json(f"{KICK}/api/v2/channels/{slug}")["chatroom"]["id"]
    with connect(url, open_timeout=15) as ws:
        ws.send(json.dumps({"event": "pusher:subscribe", "data": {"auth": "", "channel": f"chatrooms.{room}.v2"}}))
        while not stop.is_set():
            try:
                m = json.loads(ws.recv(timeout=1))
            except TimeoutError:
                continue
            if m["event"] == "pusher:ping":
                ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
            elif m["event"].endswith("ChatMessageEvent"):
                d = json.loads(m["data"])
                on_msg(d["sender"]["id"], KICK_EMOTE.sub(r"\1", d["content"]))


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
