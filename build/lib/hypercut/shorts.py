"""Vertical 9:16 short with clipper-style burned-in captions (word-by-word, current word highlighted)."""
import subprocess
from pathlib import Path

FONTS = Path(__file__).parent / "fonts"
FONT = "Montserrat Thin Black"  # family name inside the bundled Montserrat-Black.ttf
W, H = 1080, 1920
WORDS_PER_LINE = 3
YELLOW, WHITE = r"&H003BD4FF&", r"&H00FFFFFF&"  # ASS colours are &HAABBGGRR

HEADER = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Alignment, MarginL, MarginR, MarginV, BorderStyle, Outline, Shadow
Style: Cap,{FONT},96,{WHITE},&H00000000,&H80000000,0,5,80,80,0,1,7,3
Style: Credit,{FONT},44,&H00FFFFFF,&H00000000,&H80000000,0,8,40,40,170,1,4,0

[Events]
Format: Layer, Start, End, Style, Text
"""


def _t(s):
    cs = int(round(s * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02}:{cs // 100 % 60:02}.{cs % 100:02}"


def build_ass(words, credit):
    """words: [(start, end, text)] -> ASS: lines of up to 3 words, the spoken word yellow + popped."""
    events = [f"Dialogue: 0,{_t(0)},{_t(max((w[1] for w in words), default=0) + 3600)},Credit,{credit}"]
    for i in range(0, len(words), WORDS_PER_LINE):
        line = words[i:i + WORDS_PER_LINE]
        for j, (a, b, _) in enumerate(line):
            end = line[j + 1][0] if j + 1 < len(line) else b  # no gaps between words inside a line
            text = " ".join(
                rf"{{\c{YELLOW}\fscx112\fscy112}}{w.strip().upper()}{{\r}}" if k == j else w.strip().upper()
                for k, (_, _, w) in enumerate(line))
            events.append(rf"Dialogue: 1,{_t(a)},{_t(end)},Cap,{{\pos({W // 2},{int(H * 0.68)})}}{text}")
    return HEADER + "\n".join(events) + "\n"


def render(src, words, credit, dst):
    """Stream full-width in the middle over a blurred zoomed copy, captions burned in."""
    dst = Path(dst)
    ass, tmp = dst.with_suffix(".ass"), dst.with_suffix(".part.mp4")
    ass.write_text(build_ass(words, credit), encoding="utf-8")
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
