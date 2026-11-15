# Changelog

## 0.1.0 (2026-11-15)

First release.

- Dashboard that lists who is live on Twitch and Kick, with search, platform and category filters, favourites and an in-page player.
- Auto-clipping: chat is read anonymously, a spike in messages cuts a 30-second clip, whisper captions it, and a 1080×1920 short is rendered with word-by-word captions.
- 24/7 mode (`--follow`, `--top`) that keeps clipping the biggest live channels of one category.
- Phone mode (`--phone`) with a secret link for devices on the same Wi-Fi.
- Discord webhook notifications for finished shorts (`--discord-webhook`).
- `hypercut --help` groups flags into Dashboard, Clipping, 24/7 and Notifications, and `hypercut --version` prints the version.
- HyperCut exits with a plain message if `ffmpeg` or `streamlink` is missing.
- The Live tab shows a message instead of an empty page when neither Twitch nor Kick can be reached, and the browser tab title shows how many channels are being clipped.

### Fixed

- Stopping HyperCut (Ctrl+C or `kill`) while a clip or short was rendering left `.tmp.mp4`, `.short.part.mp4` and `.short.ass` files in `clips/`. Shutdown now waits up to 60 seconds for clip work in progress, and leftover temp files are removed on exit and on the next start.
