"""Who is live on Twitch and Kick, from their public web endpoints (no account)."""
import json
import re
import time

from .net import get_json

TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # public web client id, anonymous
KICK = "https://kick.com"
NAME = re.compile(r"[A-Za-z0-9_-]{1,40}")
PLATFORMS = ("twitch", "kick")

STREAM_FIELDS = "title viewersCount game{name} previewImageURL(width:440,height:248)"
NODE = f"node{{{STREAM_FIELDS} broadcaster{{login displayName}}}}"


def gql(query):
    """Run an anonymous Twitch GraphQL query and return its data; raises on a GQL error."""
    d = get_json(TWITCH_GQL, {"query": query}, {"Client-ID": TWITCH_CLIENT_ID})
    if d.get("errors"):
        raise RuntimeError(d["errors"][0]["message"])
    return d["data"]


def _twitch(login, name, s):
    return {"platform": "twitch", "login": login, "name": name, "title": s["title"],
            "category": (s.get("game") or {}).get("name", ""), "viewers": s["viewersCount"],
            "thumb": s["previewImageURL"]}


def _node(n):
    return _twitch(n["broadcaster"]["login"], n["broadcaster"]["displayName"], n)


def _kick(e):
    ch = e["channel"]
    thumb = e.get("thumbnail") or {}
    cats = e.get("categories") or [{}]
    return {"platform": "kick", "login": ch["slug"], "name": ch["user"]["username"], "title": e["session_title"],
            "category": cats[0].get("name", ""), "viewers": e["viewer_count"],
            "thumb": thumb.get("src") or thumb.get("srcset", "").split(" ")[0]}


def twitch_live():
    """Live Twitch channels: the top streams plus the top 30 streams of the top 30 categories."""
    # Twitch's anonymous GQL refuses cursor paging, so widen instead of paging
    streams = f"streams(first:30){{edges{{{NODE}}}}}"
    d = gql(f"{{top:{streams} games(first:30){{edges{{node{{{streams}}}}}}}}}")
    edges = d["top"]["edges"] + [e for g in d["games"]["edges"] for e in g["node"]["streams"]["edges"]]
    out = {e["node"]["broadcaster"]["login"]: e["node"] for e in edges}
    return [_node(n) for n in out.values()]


def kick_live(pages=10):
    """Live Kick channels, up to `pages` pages of 100."""
    out = []
    for p in range(1, pages + 1):
        d = get_json(f"{KICK}/stream/livestreams/en?page={p}&limit=100&sort=desc")
        out += [_kick(e) for e in d["data"]]
        if not d.get("next_page_url"):
            break
    return out


def lookup(platform, login):
    """Live info for one channel, or None if offline/unknown."""
    if platform == "twitch":
        u = gql(f'{{user(login:"{login}"){{login displayName stream{{{STREAM_FIELDS}}}}}}}')["user"]
        return _twitch(u["login"], u["displayName"], u["stream"]) if u and u["stream"] else None
    c = get_json(f"{KICK}/api/v2/channels/{login}")
    ls = c.get("livestream")
    if not ls:
        return None
    return {"platform": "kick", "login": c["slug"], "name": c["user"]["username"], "title": ls["session_title"],
            "category": ((ls.get("categories") or [{}])[0]).get("name", ""), "viewers": ls["viewer_count"],
            "thumb": (ls.get("thumbnail") or {}).get("url", "")}


class Directory:
    """Everyone live on Twitch + Kick (top pages of each), cached."""

    def __init__(self, ttl=60, sources=None):
        self.ttl, self.at, self.items, self.errors = ttl, 0.0, [], {}
        self.sources = sources or {"twitch": twitch_live, "kick": kick_live}

    def live(self):
        """All live channels, biggest first; refetched when the cache is older than ttl."""
        if time.time() - self.at > self.ttl:
            items, self.errors = [], {}
            for name, fn in self.sources.items():
                try:
                    items += fn()
                except Exception as e:  # one platform down must not blank the other
                    self.errors[name] = str(e)
            self.items, self.at = sorted(items, key=lambda s: -s["viewers"]), time.time()
        return self.items


def category_top(name, n=3):
    """Top n live channels in a category across Twitch + Kick (Kick slug = name lowercased, dashed)."""
    out = []
    try:
        g = gql(f'{{game(name:{json.dumps(name)}){{streams(first:{n}){{edges{{{NODE}}}}}}}}}')["game"]
        out += [_node(e["node"]) for e in (g["streams"]["edges"] if g else [])]
    except Exception:  # one platform down must not stop the other
        pass
    try:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        url = f"{KICK}/stream/livestreams/en?page=1&limit={n}&sort=desc&subcategory={slug}"
        out += [_kick(e) for e in get_json(url)["data"]]
    except Exception:
        pass
    return sorted(out, key=lambda s: -s["viewers"])[:n]
