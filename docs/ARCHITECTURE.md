# HyperCut architecture

One Python process. Threads, no asyncio. Stdlib HTTP server. External tools: `streamlink`, `ffmpeg`/`ffprobe`.

```
browser ──HTTP──> web.py (ThreadingHTTPServer, 127.0.0.1)
                    │
                    ├─ directory.py  live lists (Twitch GQL, Kick livestreams), cached 60 s
                    ├─ App.follow    24/7: every 5 min, top-N of a category → start/stop sessions
                    └─ Session (one per channel, session.py), 4 threads:
                         _record  streamlink -O | ffmpeg -f segment → recordings/<ch>/<epoch>.ts
                         chat     chat.twitch_chat (IRC over WebSocket, justinfan) / chat.kick_chat (poll)
                         _count   spikes.SpikeCounter.roll() every 10 s → queue spike time
                         _clip    concat segments → clipper.cut → clipper.transcribe → write_captions
                                  → shorts.render (1080×1920) → on_clip hook
```

## Modules

| File | Job |
|---|---|
| `__main__.py` | CLI flags → `cfg` dict; checks `ffmpeg`/`streamlink` are on PATH; starts server, optional follow thread; SIGTERM = Ctrl+C (stops everything cleanly) |
| `web.py` | Routes, request validation, Host/Origin checks, phone key cookie, `App` (sessions, favorites, clips list, follow) |
| `directory.py` | `twitch_live`, `kick_live`, `lookup`, `category_top`, `Directory` cache; `NAME` regex for channel names |
| `net.py` | `get_json`: urllib + JSON |
| `chat.py` | Anonymous chat readers + `run_forever` reconnect loop |
| `spikes.py` | 10 s buckets; spike = bucket ≥ max(min_msgs, multiplier × median of last 30 buckets), after 3-bucket warm-up, 30 s cooldown |
| `session.py` | Per-channel pipeline and its 5 status parts (streamlink, chat, spikes, ffmpeg, whisper) |
| `clipper.py` | `cut` (temp file → rename, rejects empty output), `transcribe` (faster-whisper, VAD, word timestamps), `write_captions` (.srt + .vtt) |
| `shorts.py` | ASS subtitles (3 words per line, current word yellow and enlarged) + ffmpeg blurred-background vertical render |
| `static/index.html` | Whole dashboard (inline CSS/JS): Live grid, Watch (official embeds), Clipping cards, Clips gallery |

## Timing

Segment files are named by the wall-clock second they start. A spike at time `t` cuts
`[t − pre, t − pre + clip_length]` from the segments covering that window, with `-ss` measured from the first
segment's start. Timing errors reset every 10 s segment, so ads and stalls don't add up over a 24/7 run.
The clip worker waits until the window plus one segment has been recorded.

## Shutdown

`Session.stop()` returns as soon as recording and chat have stopped; the clip worker keeps going in the background
so the Stop button stays quick. On exit, `App.stop_all(60)` stops every session and then gives all clip workers
60 s in total to finish what is queued. Whatever is still half-written after that (`.tmp.mp4`, `.short.part.mp4`,
`.short.ass`) is deleted, and again on the next start in case the process was killed. `App.clips()` never lists
those names.

## Security model

- Binds `127.0.0.1` by default. Rejects requests whose `Host` isn't the bound address (DNS rebinding).
- POSTs need `Content-Type: application/json` and a matching `Origin`, so other sites can't trigger them (CSRF).
  Bodies are capped at 4 KB, there are at most 10 sessions, and the socket timeout is 10 s.
- `--phone` binds `0.0.0.0` and adds the LAN IP. Non-localhost clients must present a random key once
  (`?key=` → HttpOnly cookie).
- Subprocesses get list arguments (no shell). Channel names are regex-validated (`re.fullmatch`). Clip file
  serving only allows `[A-Za-z0-9_.-]+.(mp4|vtt|srt)`.

## Testing

- `tests/` covers every line (`--cov-fail-under=100`) with no network. It uses fake streamlink shell scripts,
  a local WebSocket IRC server, stubbed HTTP, and real ffmpeg on generated test media.
- `scripts/live_test.py` and `scripts/e2e.py` are the real-world checks; they need network access.
