import io
import json
import threading

import pytest
from websockets.sync.server import serve

from hypercut import chat, directory, hype, net
from hypercut.spikes import SpikeCounter


def test_net_get_json(monkeypatch):
    seen = {}

    def fake(req, timeout):
        seen.update(body=req.data, h=dict(req.header_items()), t=timeout)
        return io.BytesIO(b'{"a": 1}')

    monkeypatch.setattr("hypercut.net.urllib.request.urlopen", fake)
    assert net.get_json("http://x", {"q": 1}, {"X-A": "b"}) == {"a": 1}
    assert seen["body"] == b'{"q": 1}' and seen["h"]["X-a"] == "b" and seen["t"] == 15
    net.get_json("http://x")
    assert seen["body"] is None


def test_spikes():
    c = SpikeCounter(multiplier=2, min_msgs=5, cooldown=10, warmup=2)
    for _ in range(3):  # warmup + baseline of 1
        c.add()
        assert not c.roll(0)
    c.add(20)
    assert c.roll(100) and c.last == 20 and c.baseline == 1.0
    c.add(20)
    assert not c.roll(101)  # cooldown
    c.add(50)
    assert c.roll(200)


def twitch_node(login, viewers=1, game=True):
    return {"title": "t", "viewersCount": viewers, "game": {"name": "G"} if game else None,
            "previewImageURL": "p", "broadcaster": {"login": login, "displayName": login.upper()}}


def test_directory(monkeypatch):
    def gql_resp(q, *a):
        edge = lambda n: {"node": n}
        game = {"node": {"streams": {"edges": [edge(twitch_node("b", 9)), edge(twitch_node("a", 5, False))]}}}
        return {"data": {"top": {"edges": [edge(twitch_node("a", 5, False))]}, "games": {"edges": [game]}}}

    monkeypatch.setattr(directory, "get_json", lambda url, *a: gql_resp(url))
    tl = directory.twitch_live()
    assert [t["login"] for t in tl] == ["a", "b"] and tl[0]["category"] == "" and tl[1]["category"] == "G"

    monkeypatch.setattr(directory, "get_json", lambda *a: {"errors": [{"message": "boom"}]})
    with pytest.raises(RuntimeError, match="boom"):
        directory.gql("x")

    def ev(slug, thumb, cats):
        return {"channel": {"slug": slug, "user": {"username": slug}}, "session_title": "s", "categories": cats,
                "viewer_count": 3, "thumbnail": thumb}

    pages = {1: {"data": [ev("k1", {"src": "s"}, [{"name": "C"}]), ev("k2", {"srcset": "u 1x, v 2x"}, None)],
                 "next_page_url": "n"},
             2: {"data": [ev("k3", None, [])]}}
    monkeypatch.setattr(directory, "get_json", lambda url, *a: pages[int(url.split("page=")[1].split("&")[0])])
    kl = directory.kick_live()
    assert [k["login"] for k in kl] == ["k1", "k2", "k3"]
    assert [k["thumb"] for k in kl] == ["s", "u", ""] and kl[0]["category"] == "C" and kl[1]["category"] == ""

    # lookup
    stream = {"title": "t", "viewersCount": 1, "game": None, "previewImageURL": "p"}
    users = iter([{"data": {"user": {"login": "a", "displayName": "A", "stream": stream}}},
                  {"data": {"user": None}}, {"data": {"user": {"login": "a", "displayName": "A", "stream": None}}}])
    monkeypatch.setattr(directory, "get_json", lambda *a: next(users))
    assert directory.lookup("twitch", "a")["name"] == "A"
    assert directory.lookup("twitch", "a") is None and directory.lookup("twitch", "a") is None
    ls = {"session_title": "s", "viewer_count": 2, "categories": [{"name": "C"}], "thumbnail": {"url": "u"}}
    ch = {"slug": "k", "user": {"username": "K"}, "livestream": ls}
    chans = iter([ch, {**ch, "livestream": {**ls, "categories": None, "thumbnail": None}}, {"livestream": None}])
    monkeypatch.setattr(directory, "get_json", lambda *a: next(chans))
    r = directory.lookup("kick", "k")
    assert r["category"] == "C" and r["thumb"] == "u"
    r = directory.lookup("kick", "k")
    assert r["category"] == "" and r["thumb"] == ""
    assert directory.lookup("kick", "k") is None


def test_directory_cache():
    calls = []

    def ok():
        calls.append(1)
        return [{"viewers": 1}, {"viewers": 7}]

    def bad():
        raise RuntimeError("down")

    d = directory.Directory(ttl=1000, sources={"a": ok, "b": bad})
    assert [i["viewers"] for i in d.live()] == [7, 1] and d.errors == {"b": "down"}
    d.live()
    assert len(calls) == 1
    assert directory.Directory().sources.keys() == {"twitch", "kick"}


def test_parse_privmsg():
    sent = []
    frame = "PING :tmi\r\n:u!u@u PRIVMSG #c :hi : there\r\n:x NOTICE\r\n:v!v@v PRIVMSG #c :yo"
    assert chat.parse_privmsg(frame, sent.append) == [("u", "hi : there"), ("v", "yo")]
    assert sent == ["PONG :tmi"]


def test_twitch_chat_ws():
    got, stop, frames, done = [], threading.Event(), [], threading.Event()

    def handler(ws):
        for _ in range(3):
            frames.append(ws.recv())
        ws.send("PING :tmi.twitch.tv\r\n")
        ws.send(":a!a@a PRIVMSG #c :1\r\n:b!b@b PRIVMSG #c :2\r\n")
        ws.send("PING :x")  # no msgs -> falsy n
        frames.append(ws.recv())
        frames.append(ws.recv())
        done.wait(5)

    with serve(handler, "127.0.0.1", 0) as srv:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"ws://127.0.0.1:{srv.socket.getsockname()[1]}"
        t = threading.Thread(target=chat.twitch_chat, args=("c", lambda *m: got.append(m), stop, url))
        t.start()
        for _ in range(100):
            if got and len(frames) >= 5:
                break
            stop.wait(0.05)
        stop.set()
        t.join(5)
        done.set()
        srv.shutdown()
    assert frames[0] == "PASS SCHMOOPIIE" and frames[1].startswith("NICK justinfan") and frames[2] == "JOIN #c"
    assert frames[3:] == ["PONG :tmi.twitch.tv", "PONG :x"] and got == [("a", "1"), ("b", "2")]


def test_twitch_chat_recv_timeout():
    stop, done = threading.Event(), threading.Event()

    def handler(ws):
        done.wait(5)

    with serve(handler, "127.0.0.1", 0) as srv:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"ws://127.0.0.1:{srv.socket.getsockname()[1]}"
        t = threading.Thread(target=chat.twitch_chat, args=("c", print, stop, url))
        t.start()
        stop.wait(1.3)  # one recv timeout (1s) hits `continue`
        stop.set()
        t.join(5)
        done.set()
        srv.shutdown()
    assert not t.is_alive()


def kick_server(frames_out, sent, done):
    def handler(ws):
        frames_out.append(json.loads(ws.recv()))
        for f in sent:
            ws.send(json.dumps(f))
        frames_out.append(json.loads(ws.recv()))  # pong
        done.wait(5)
    return handler


def run_kick(monkeypatch, sent):
    monkeypatch.setattr(chat, "get_json", lambda url: {"chatroom": {"id": 42}})
    frames, got, stop, done = [], [], threading.Event(), threading.Event()
    with serve(kick_server(frames, sent, done), "127.0.0.1", 0) as srv:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"ws://127.0.0.1:{srv.socket.getsockname()[1]}"
        t = threading.Thread(target=chat.kick_chat, args=("s", lambda *m: got.append(m), stop, url))
        t.start()
        for _ in range(100):
            if len(frames) >= 2:
                break
            stop.wait(0.05)
        stop.wait(1.3)  # one recv timeout hits `continue`
        stop.set()
        t.join(5)
        done.set()
        srv.shutdown()
    return frames, got


def msg(i, content):
    return {"event": "App\\Events\\ChatMessageEvent", "data": json.dumps({"content": content, "sender": {"id": i}})}


def test_kick_chat(monkeypatch):
    frames, got = run_kick(monkeypatch, [{"event": "pusher_internal:subscription_succeeded"}, msg(1, "hi"),
                                         msg(2, "[emote:123:KEKW] [emote:4:W]"), {"event": "pusher:ping"}])
    assert frames == [{"event": "pusher:subscribe", "data": {"auth": "", "channel": "chatrooms.42.v2"}},
                      {"event": "pusher:pong", "data": {}}]
    assert got == [(1, "hi"), (2, "KEKW W")] and hype.weight(got[1][1]) == 2


def test_run_forever():
    class P:
        log = []

        def set(self, s, d=""):
            self.log.append((s, d))

    stop, calls = threading.Event(), []

    def fn():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("x" * 200)

    chat.run_forever(fn, P(), stop, backoff=0)
    assert len(calls) == 2 and P.log[1][0] == "error" and len(P.log[1][1]) == 120 and P.log[-1] == ("ok", "connected")
    stop.set()
    chat.run_forever(fn, P(), stop)
    assert len(calls) == 2


def test_category_top(monkeypatch):
    game = {"data": {"game": {"streams": {"edges": [{"node": twitch_node("t1", 5)}, {"node": twitch_node("t2", 50)}]}}}}
    kick = {"data": [{"channel": {"slug": "k1", "user": {"username": "K1"}}, "session_title": "s", "categories": None,
                      "viewer_count": 20, "thumbnail": None}]}
    urls = []

    def fake(url, *a):
        urls.append(url)
        return game if "gql" in url else kick

    monkeypatch.setattr(directory, "get_json", fake)
    top = directory.category_top("Grand Theft Auto: VI", 2)
    assert [s["login"] for s in top] == ["t2", "k1"]  # merged, by viewers, cut to n
    assert "subcategory=grand-theft-auto-vi" in urls[1]

    def unknown_twitch(url, *a):
        if "gql" in url:
            return {"data": {"game": None}}
        raise OSError("kick down")

    monkeypatch.setattr(directory, "get_json", unknown_twitch)
    assert directory.category_top("x") == []

    def down(url, *a):
        raise OSError("down")

    monkeypatch.setattr(directory, "get_json", down)
    assert directory.category_top("x") == []


def test_hype_weight_and_votes():
    for t in ("KEKW", "that was LUL", "😂", "NO WAY DUDE", "clip it", "!CLIP"):
        assert hype.weight(t) == 2, t
    for t in ("hello", "OK", "kekw", "WOW", "clipping this", "I clip it"):
        assert hype.weight(t) == 1, t
    assert hype.is_clip(" !clip ") and not hype.is_clip("clip it now")


def test_hype_score():
    assert hype.score(60, 10, 3) == 2.0 and hype.score(5, 0, 3) == 1.67 and hype.score(5, 2, 0) == 2.5


def test_hype_title_hashtags_sidecar(tmp_path):
    assert hype.title([]) == ""
    assert hype.title([], "ohnepixel · Just Chatting") == "ohnepixel · Just Chatting"  # silent clip
    assert hype.title([(0, 1, "  ")], "ohnepixel") == "ohnepixel"
    assert hype.title([(0, 1, " so anyway "), (1, 2, " no way! ")]) == "no way!"
    assert hype.title([(0, 1, "so anyway"), (1, 2, "  "), (2, 3, "then")]) == "so anyway"
    assert hype.title([(0, 1, "x" * 99 + "!"), (1, 2, "we GOT it")]) == "we GOT it"
    assert hype.title([(0, 1, "a" * 80)]) == "a" * 59 + "…"
    assert hype.title([(0, 1, "oh my god")]) == "oh my god"
    assert hype.hashtags("kick", "Foo", "Grand Theft Auto VI") == [
        "#foo", "#grandtheftautovi", "#kick", "#clips", "#shorts"]
    assert hype.hashtags("twitch", "foo", "") == ["#foo", "#twitch", "#clips", "#shorts"]
    p = tmp_path / "a.json"
    assert hype.read(p) == {}
    hype.write(p, {"a": 1})
    assert hype.read(p) == {"a": 1} and not (tmp_path / "a.json.tmp").exists()
    p.write_text("[1]")
    assert hype.read(p) == {}


