import subprocess
import sys
import types
from pathlib import Path

import pytest

from hypercut import clipper


def test_write_captions(tmp_path):
    clipper.write_captions([(0, 1.5, " hi "), (3661.007, 3662, "yo")], tmp_path / "c.mp4")
    assert (tmp_path / "c.srt").read_text() == (
        "1\n00:00:00,000 --> 00:00:01,500\nhi\n\n2\n01:01:01,007 --> 01:01:02,000\nyo\n\n")
    assert (tmp_path / "c.vtt").read_text() == (
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.500\nhi\n\n01:01:01.007 --> 01:01:02.000\nyo\n\n")


def test_transcribe_caches_model(monkeypatch):
    made, tasks = [], []

    class FakeModel:
        def __init__(self, name, compute_type):
            made.append((name, compute_type))

        def transcribe(self, path, **kw):
            assert kw["word_timestamps"]
            tasks.append(kw["task"])
            w = types.SimpleNamespace(start=0, end=0.5, word=" a")
            return iter([types.SimpleNamespace(start=0, end=1, text="a", words=[w])]), None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setattr(clipper, "_models", {})
    assert clipper.transcribe(Path("x.mp4"), "m") == ([(0, 1, "a")], [(0, 0.5, " a")])
    clipper.transcribe("x.mp4", "m")
    clipper.transcribe("x.mp4", "m", translate=True)
    assert made == [("m", "int8")]
    assert tasks == ["transcribe", "transcribe", "translate"]


def test_cut_real(tmp_path):
    src, dst = tmp_path / "x.ts", tmp_path / "o.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=6:size=160x90:rate=10",
                    "-f", "lavfi", "-i", "sine=duration=6", "-shortest", str(src)], check=True)
    clipper.cut(src, -5, 2, dst)
    assert dst.stat().st_size > 0
    with pytest.raises(subprocess.CalledProcessError):
        clipper.cut(tmp_path / "missing.ts", 0, 1, dst)
    assert not dst.with_suffix(".tmp.mp4").exists()
    with pytest.raises(RuntimeError, match="empty clip"):  # seek past the end: ffmpeg exits 0 with nothing
        clipper.cut(src, 60, 2, tmp_path / "late.mp4")
    assert not (tmp_path / "late.mp4").exists() and not (tmp_path / "late.tmp.mp4").exists()


def test_loudness(tmp_path):
    tone, mute = tmp_path / "t.mp4", tmp_path / "m.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1", str(tone)], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64", str(mute)], check=True)
    assert 0 < clipper.loudness(tone) < 10  # sine: peak a few dB over the mean
    assert clipper.loudness(mute) == 0.0 and clipper.loudness(tmp_path / "missing") == 0.0
