import os
import subprocess
import threading

_models, _lock = {}, threading.Lock()


def cut(src, start, length, dst):
    """Frame-accurate re-encode (-ss after -i: input seeking on a concat: source drops everything past the first file) of [start, start+length) from a (possibly growing) recording."""
    tmp = dst.with_suffix(".tmp.mp4")  # never expose a half-written clip under its final name
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                        "-ss", f"{max(0.0, start):.2f}", "-t", str(length), "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", str(tmp)],
                       check=True, stdin=subprocess.DEVNULL)
        probe = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(tmp)]
        got = subprocess.run(probe, capture_output=True, text=True).stdout.strip()
        if not got.replace(".", "", 1).isdigit() or float(got) < length / 2:  # ffmpeg exits 0 on a seek past the end
            raise RuntimeError("empty clip: spike is outside the recording")
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def _ts(t, sep):
    h, r = divmod(int(t * 1000), 3_600_000)
    m, r = divmod(r, 60_000)
    return f"{h:02}:{m:02}:{r // 1000:02}{sep}{r % 1000:03}"


def write_captions(segments, base):
    """segments: [(start, end, text)] -> base.srt + base.vtt (vtt is what the browser player reads)."""
    srt = "".join(f"{i}\n{_ts(a, ',')} --> {_ts(b, ',')}\n{t.strip()}\n\n" for i, (a, b, t) in enumerate(segments, 1))
    vtt = "WEBVTT\n\n" + "".join(f"{_ts(a, '.')} --> {_ts(b, '.')}\n{t.strip()}\n\n" for a, b, t in segments)
    base.with_suffix(".srt").write_text(srt, encoding="utf-8")
    base.with_suffix(".vtt").write_text(vtt, encoding="utf-8")


def transcribe(path, model_name):
    from faster_whisper import WhisperModel  # heavy import, only when first clip is captioned

    with _lock:
        if model_name not in _models:
            _models[model_name] = WhisperModel(model_name, compute_type="int8")
        # VAD: no hallucinated text over music/silence; word timestamps drive the word-by-word short captions
        segs = list(_models[model_name].transcribe(str(path), vad_filter=True, word_timestamps=True)[0])
        return [(s.start, s.end, s.text) for s in segs], [(w.start, w.end, w.word) for s in segs for w in s.words]
