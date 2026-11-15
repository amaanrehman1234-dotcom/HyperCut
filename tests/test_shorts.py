import json
import subprocess

import pytest

from hypercut import shorts

Y = shorts.YELLOW


def test_build_ass_exact():
    words = [(0, 0.5, " hi"), (0.6, 1.0, "there"), (1.2, 2.0, "you"), (3661.5, 3662, "yo")]
    out = shorts.build_ass(words, "kick.com/x")
    head, events = out.split("[Events]\n")
    assert "PlayResX: 1080" in head and "PlayResY: 1920" in head
    assert events.splitlines() == [
        "Format: Layer, Start, End, Style, Text",
        "Dialogue: 0,0:00:00.00,2:01:02.00,Credit,kick.com/x",  # last word end + 1h
        rf"Dialogue: 1,0:00:00.00,0:00:00.60,Cap,{{\pos(540,1305)}}{{\c{Y}\fscx112\fscy112}}HI{{\r}} THERE YOU",
        rf"Dialogue: 1,0:00:00.60,0:00:01.20,Cap,{{\pos(540,1305)}}HI {{\c{Y}\fscx112\fscy112}}THERE{{\r}} YOU",
        rf"Dialogue: 1,0:00:01.20,0:00:02.00,Cap,{{\pos(540,1305)}}HI THERE {{\c{Y}\fscx112\fscy112}}YOU{{\r}}",
        rf"Dialogue: 1,1:01:01.50,1:01:02.00,Cap,{{\pos(540,1305)}}{{\c{Y}\fscx112\fscy112}}YO{{\r}}",
    ]


def test_build_ass_no_words():
    assert shorts.build_ass([], "c").endswith("Dialogue: 0,0:00:00.00,1:00:00.00,Credit,c\n")


def probe(p):
    r = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                 "stream=width,height", "-of", "json", str(p)], text=True)
    s = json.loads(r)["streams"][0]
    return s["width"], s["height"]


def test_render_real(tmp_path):
    src, dst = tmp_path / "in.mp4", tmp_path / "o.short.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x180:rate=10",
                    "-f", "lavfi", "-i", "sine=duration=2", "-shortest", str(src)], check=True)
    shorts.render(src, [(0, 0.5, "hi"), (0.5, 1, "there")], "kick.com/x", dst)
    assert probe(dst) == (1080, 1920)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in.mp4", "o.short.mp4"]


def test_render_failure_cleans_up(tmp_path):
    dst = tmp_path / "o.short.mp4"
    with pytest.raises(subprocess.CalledProcessError):
        shorts.render(tmp_path / "missing.mp4", [(0, 1, "a")], "c", dst)
    assert list(tmp_path.iterdir()) == []
