#!/usr/bin/env python3
"""Find which external (processed) audio file belongs to which camera file, and the
offset between them, by cross-correlating loudness envelopes.

usage: align_audio.py CAMERA [CAMERA ...] --audio WAV [WAV ...]
prints JSON: for every camera×audio pair {camera, audio, offset_s, score}, plus the
best match per camera. offset_s is defined as:

    audio_time = camera_time + offset_s

so a camera frame at t seconds into the video is heard at t + offset_s in the WAV.
score is the normalized correlation peak (≳0.5 = confident; <0.2 = no match).
"""
import json, subprocess, sys
import numpy as np

RATE = 200  # envelope samples per second (5 ms resolution)

def envelope(path):
    """Mono 8 kHz PCM via ffmpeg -> RMS envelope at RATE Hz, log-compressed, z-scored."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "8000",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    hop = 8000 // RATE
    n = len(x) // hop
    e = np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-6)
    e = np.log(e)
    return (e - e.mean()) / (e.std() + 1e-9)

def xcorr(a, b):
    """Full normalized cross-correlation via FFT; returns (lag_samples, score)
    where b[i + lag] ≈ a[i]."""
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    c = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)
    c = np.concatenate([c[-(len(a) - 1):], c[: len(b)]])  # lags -(len(a)-1) .. len(b)-1
    lags = np.arange(-(len(a) - 1), len(b))
    # normalize by overlap length so long overlaps aren't favored unfairly
    overlap = np.minimum(len(a), len(b) - lags) - np.maximum(0, -lags)
    overlap = np.clip(overlap, 1, None)
    score = c / overlap
    score[overlap < RATE * 30] = -np.inf  # require ≥30 s of overlap
    i = int(np.argmax(score))
    return int(lags[i]), float(score[i])

def main():
    args = sys.argv[1:]
    if "--audio" not in args:
        sys.exit(__doc__)
    k = args.index("--audio")
    cams, auds = args[:k], args[k + 1:]
    env = {p: envelope(p) for p in cams + auds}
    pairs = []
    for c in cams:
        for au in auds:
            lag, score = xcorr(env[c], env[au])
            pairs.append(dict(camera=c, audio=au, offset_s=round(lag / RATE, 3), score=round(score, 3)))
    best = {}
    for p in pairs:
        if p["camera"] not in best or p["score"] > best[p["camera"]]["score"]:
            best[p["camera"]] = p
    print(json.dumps({"pairs": pairs, "best": list(best.values()),
                      "convention": "audio_time = camera_time + offset_s"}, indent=1))

if __name__ == "__main__":
    main()
