"""Discord webhook notifications for finished shorts."""
import logging
import urllib.request
import uuid
from pathlib import Path

PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
MAX_BYTES = 25 * 1024 * 1024


def hook(url, platform, login):
    """Return an on_clip callback posting each finished short to Discord, or None without a webhook."""
    if not url:
        return None

    def on_clip(clip):
        try:
            short = Path(clip).with_suffix(".short.mp4")
            b = uuid.uuid4().hex
            field = f'--{b}\r\nContent-Disposition: form-data; name='
            parts = [f'{field}"content"\r\n\r\n🔥 Clip from {platform}/{login}\r\n'.encode()]
            if short.stat().st_size <= MAX_BYTES:
                parts.append(f'{field}"files[0]"; filename="{short.name}"\r\nContent-Type: video/mp4\r\n\r\n'.encode())
                parts += [short.read_bytes(), b"\r\n"]
            parts.append(f"--{b}--\r\n".encode())
            headers = {"Content-Type": f"multipart/form-data; boundary={b}", "User-Agent": "HyperCut"}
            req = urllib.request.Request(url, b"".join(parts), headers)
            urllib.request.urlopen(req, timeout=60).close()
        except Exception as e:  # never break the clip worker
            logging.warning("discord notify failed: %s", e)

    return on_clip
