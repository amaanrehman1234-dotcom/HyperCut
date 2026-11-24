"""Vertical 9:16 short with clipper-style burned-in captions (word-by-word, current word highlighted)."""
import math
import subprocess
from pathlib import Path

FONTS = Path(__file__).parent / "fonts"
FONT = "Montserrat Thin Black"  # family name inside the bundled Montserrat-Black.ttf
W, H = 1080, 1920
WORDS_PER_LINE = 3
TOP = 672  # split layout: facecam section height (35% of the short)
MIN_CROP = 0.05
YELLOW, WHITE = r"&H003BD4FF&", r"&H00FFFFFF&"  # ASS colours are &HAABBGGRR

HEADER = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Alignment, MarginL, MarginR, \
MarginV, BorderStyle, Outline, Shadow
Style: Cap,{FONT},96,{WHITE},&H00000000,&H80000000,0,5,80,80,0,1,7,3
Style: Credit,{FONT},44,&H00FFFFFF,&H00000000,&H80000000,0,8,40,40,170,1,4,0

[Events]
Format: Layer, Start, End, Style, Text
"""


def _t(s):
    cs = int(round(s * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02}:{cs // 100 % 60:02}.{cs % 100:02}"


def build_ass(words, credit, y=int(H * 0.68)):
    """words: [(start, end, text)] -> ASS: lines of up to 3 words, the spoken word yellow + popped."""
    events = [f"Dialogue: 0,{_t(0)},{_t(max((w[1] for w in words), default=0) + 3600)},Credit,{credit}"]
    for i in range(0, len(words), WORDS_PER_LINE):
        line = words[i:i + WORDS_PER_LINE]
        for j, (a, b, _) in enumerate(line):
            end = line[j + 1][0] if j + 1 < len(line) else b  # no gaps between words inside a line
            text = " ".join(
                rf"{{\c{YELLOW}\fscx112\fscy112}}{w.strip().upper()}{{\r}}" if k == j else w.strip().upper()
                for k, (_, _, w) in enumerate(line))
            events.append(rf"Dialogue: 1,{_t(a)},{_t(end)},Cap,{{\pos({W // 2},{y})}}{text}")
    return HEADER + "\n".join(events) + "\n"


def clamp(c):
    """Validate a facecam crop {x, y, w, h} (fractions of the frame) and clamp it inside the frame, w and h >= 0.05."""
    try:
        x, y, w, h = (float(c[k]) for k in "xywh")
    except (TypeError, KeyError, ValueError):
        raise ValueError("crop needs numbers x, y, w, h") from None
    if not all(map(math.isfinite, (x, y, w, h))):
        raise ValueError("crop needs numbers x, y, w, h")
    w, h = min(max(w, MIN_CROP), 1), min(max(h, MIN_CROP), 1)
    return {"x": round(min(max(x, 0), 1 - w), 4), "y": round(min(max(y, 0), 1 - h), 4), "w": round(w, 4), "h": round(h, 4)}


def render(src, words, credit, dst, crop=None):
    """Vertical short with captions burned in. No crop: stream full-width over a blurred zoomed copy.

    With a facecam crop (fractions of the frame): the crop fills the top 35%, gameplay fills the rest, captions sit at the seam.
    """
    dst, c = Path(dst), clamp(crop) if crop else None  # a bad crop fails before any file is written
    ass, tmp = dst.with_suffix(".ass"), dst.with_suffix(".part.mp4")
    ass.write_text(build_ass(words, credit, **({"y": TOP + 160} if c else {})), encoding="utf-8")
    if c:
        graph = (f"[0:v]split[a][b];[a]crop=iw*{c['w']}:ih*{c['h']}:iw*{c['x']}:ih*{c['y']},"
                 f"scale={W}:{TOP}:force_original_aspect_ratio=increase,crop={W}:{TOP}[top];"
                 f"[b]scale={W}:{H - TOP}:force_original_aspect_ratio=increase,crop={W}:{H - TOP}[game];"
                 f"[top][game]vstack,ass={ass.name}:fontsdir={FONTS}[v]")
    else:
        graph = (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:5,"
                 f"eq=brightness=-0.15[bg];[b]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,"
                 f"ass={ass.name}:fontsdir={FONTS}[v]")
    try:
        # cwd=ass dir so the filter path needs no escaping
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(Path(src).resolve()), "-filter_complex", graph,
                        "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-c:a", "aac", str(tmp.resolve())], check=True, cwd=ass.parent, stdin=subprocess.DEVNULL)
        tmp.replace(dst)
    finally:
        ass.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
