---
name: hypercut
description: >
  Run, operate and troubleshoot HyperCut, the local tool that browses live Twitch/Kick streams,
  auto-clips chat spikes, captions them with whisper and renders vertical 9:16 shorts. Use when the
  user wants to start clipping a streamer or a category (e.g. "clip ohnepixel", "run GTA 6 mode 24/7"),
  open the dashboard on their phone, tune spike sensitivity, find or fix clips, or change HyperCut's code.
argument-hint: "[channel | category] e.g. twitch:ohnepixel or \"Grand Theft Auto VI\""
---

# HyperCut

Install once (needs Python 3.10+ and ffmpeg on PATH):

```bash
pipx install git+https://github.com/amaanrehman1234-dotcom/HyperCut
ffmpeg -version   # install with the OS package manager if missing
```

Source and docs: https://github.com/amaanrehman1234-dotcom/HyperCut (`docs/ARCHITECTURE.md` before changing code).

## Pick the command from what the user asked

| User wants | Command |
|---|---|
| Dashboard to browse / watch / clip by hand | `hypercut --open` |
| Dashboard on their phone | add `--phone`, give them the printed `http://<lan-ip>:8787/?key=…` link (same Wi-Fi only) |
| Clip a category around the clock | `hypercut --follow "Grand Theft Auto VI" --top 3` |
| Clip one channel without the UI | start the server, then `curl -XPOST -H 'Content-Type: application/json' 127.0.0.1:8787/api/start -d '{"platform":"twitch","login":"NAME"}'` |
| More / fewer clips | lower / raise `--multiplier` (default 2.0) and `--min-msgs` (default 10) |
| Clip on chat request | `--clip-votes N` (default 3 chatters saying `!clip` within 15 s; 0 = off) |
| Facecam on top of shorts | dashboard: watch the channel, Start clipping, wait ~20 s, **Set facecam**, drag a box. Or `curl -XPOST -H 'Content-Type: application/json' 127.0.0.1:8787/api/layout -d '{"platform":"twitch","login":"NAME","crop":{"x":0.7,"y":0.3,"w":0.3,"h":0.35}}'` (fractions 0..1; `"crop":null` clears). See the frame first: `curl 127.0.0.1:8787/api/frame?platform=twitch\&login=NAME -o f.jpg` |
| Discord ping per clip | add `--discord-webhook https://discord.com/api/webhooks/...` |
| Better captions | `--whisper-model small` (slower) |
| Behind a trusted preview proxy only | `--no-host-check` (never on an open network) |

Platforms are `twitch` or `kick`. Channel names: letters, digits, `_`, `-`.
Category names must match Twitch's exact name; the Kick slug is derived (lowercase, dashes).

## Where output goes

`<out>/clips/`: `<platform>_<login>_<YYYYmmdd-HHMMSS>.mp4` (30 s), `.srt`, `.vtt`, `.short.mp4` (1080×1920,
burned-in word-by-word captions), `.json` (hype score, title, hashtags, review `status`). `<out>/layouts.json` holds facecam crops per channel. `<out>/recordings/<platform>_<login>/` holds rolling 10 s segments
(deleted after 10 min and on stop).

## Checking it works

- `curl -s 127.0.0.1:8787/api/sessions` → each session should show `"running": 5`.
- Part states: `ok` / `busy` = fine, `waiting` = channel offline (streamlink waits for it to go live),
  `error` = read its `detail`.
- No clips? Look at `spikes.detail` ("N msgs/10s · baseline B"). If N never reaches 2× B, chat is steady:
  lower `--multiplier` (1.6 worked on ohnepixel; 2.5 produced nothing in 10 min).

## Changing code

- Work in a clone of the repo (`pip install -e . -r requirements-dev.txt`). Follow its `CLAUDE.md`: stdlib first, smallest change.
- Gate: `pytest --cov=hypercut --cov-fail-under=100` (100% line coverage, no network, no `pragma: no cover`).
- Real checks before claiming done: `python scripts/live_test.py` (random live Twitch + Kick streamer →
  captioned clip) and `python scripts/e2e.py` (browser: browse → watch → start → 5/5 → stop).
- Known traps, verified live:
  - ffprobe reports bogus durations for growing live `.ts` files; time clips by wall-clock segment names.
  - With a `concat:` input, `-ss` must come after `-i` or clips get truncated at segment boundaries.
  - ffmpeg exits 0 when seeking past the end; `clipper.cut` rejects clips shorter than half the length.
  - Twitch anonymous GQL allows `first` ≤ 30 and refuses cursor paging; Kick's Pusher chat key is dead, so
    Kick chat is polled (25 newest per second; very fast chats undercount).
  - faster-whisper 1.2 needs `av<16`.
  - `/api/frame` only works while the channel records (it reads the newest `.ts` segment); no session = 404.
  - The split layout reads `layouts.json` at render time, not at spike time. Gameplay is a center crop, so a corner
    facecam can leave a sliver in the bottom half.
  - Spikes and `!clip` votes must go through `SpikeCounter.claim` (locked); touching `last_spike` directly brings back a double-clip race.
  - The CSP in `web.SECURITY_HEADERS` blocks anything not listed (e.g. `blob:` images, other API hosts); new front-end
    features that load from elsewhere need it widened, and e2e fails on CSP console errors.

## Not supported, and why

Auto-posting to TikTok/Instagram needs the user's own developer apps (TikTok Content Posting API, Meta
Graph API). Browser-automating uploads breaks both platforms' terms. `Session.on_clip` is the hook to
attach an official uploader once the user has API access.
