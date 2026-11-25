"""Hype score, clip votes, titles and the <clip>.json sidecar."""
import json
import os
import re
import threading

EMOTES = {"KEKW", "LUL", "LULW", "OMEGALUL", "POG", "POGGERS", "PogChamp", "Pog", "W", "monkaS", "EZ", "Clap"}
EMOJI = "😂🤣💀🔥😭😱"
REACT = re.compile(r"\b(?:" + "|".join(("lmao", "lmfao", "lol", "wtf", "holy", "no way", "omg", "bro")) + r")\b", re.I)
WORDS = ("insane", "crazy", "no way", "let's go", "oh my god", "omg", "wow")
CLIP = re.compile(r"!?clip( it)?")
TITLE_MAX = 60
lock = threading.Lock()  # guards sidecar read-modify-write (review status vs. whisper finishing)


def is_clip(text):
    """True for "clip", "!clip" or "clip it" (a vote to clip)."""
    return bool(CLIP.fullmatch(text.strip().lower()))


def weight(text):
    """1 per chat message, 2 if it is hype: a hype emote/emoji, a reaction word (LMAO, WTF, NO WAY...), ALL CAPS (>= 5 letters) or a clip request."""
    letters = "".join(c for c in text if c.isalpha())
    hype = (EMOTES.intersection(text.split()) or any(e in text for e in EMOJI)
            or REACT.search(text)
            or (len(letters) >= 5 and letters.isupper()) or is_clip(text))
    return 2 if hype else 1


def score(msgs, baseline, buckets):
    """Hype score = weighted messages in the clip window / (baseline msgs per bucket * buckets in the window).

    1.0 = chat at its usual pace; 3.0 = three times as busy, more if the messages are hype. Baseline floors at 1.
    """
    return round(msgs / (max(baseline, 1) * max(buckets, 1)), 2)


def title(lines, fallback=""):
    """Title from transcript [(start, end, text)]: first short punchy line (! ? CAPS hype word), else first line.

    No speech at all (muted mic, music) -> fallback, so a clip never ships with a blank title."""
    texts = [t.strip() for _, _, t in lines if t.strip()]

    def punchy(t):
        return len(t) <= TITLE_MAX and ("!" in t or "?" in t or any(w.isupper() and len(w) > 1 for w in t.split())
                                        or any(w in t.lower() for w in WORDS))

    pick = next((t for t in texts if punchy(t)), texts[0] if texts else fallback)
    return pick if len(pick) <= TITLE_MAX else pick[:TITLE_MAX - 1].rstrip() + "…"


def hashtags(platform, login, category):
    """#login #category #platform #clips #shorts (category squashed to letters and digits, skipped if empty)."""
    cat = re.sub(r"[^a-z0-9]", "", category.lower())
    return [f"#{login.lower()}", *([f"#{cat}"] if cat else []), f"#{platform}", "#clips", "#shorts"]


def read(path):
    """Sidecar as a dict; {} if missing or corrupt."""
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def write(path, data):
    """Atomic write: temp file then rename."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)
