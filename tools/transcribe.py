#!/usr/bin/env python3
"""Reliable transcription: split at silences, transcribe chunks in parallel, merge.

Transcribing a whole long file in one pass drops speech (measured: ~2.5% of words
on a 16-minute WAV). Splitting at silence midpoints means no word straddles a
chunk edge, so nothing is lost or duplicated at the seams.

Disfluent fragments (false starts: "The artwork looks like a…") are what ASR drops
most, and which ones it drops depends on where a chunk starts. So by default it
runs THREE passes with different chunk sizes (45, 28, 104 s) and takes the union by time,
which recovers most of them (single pass missed 55 fragment words on a 17-min WAV; 3 passes miss 8, in ~11 s). Fragments matter: take selection needs to see them.

usage: transcribe.py AUDIO [-o words.json] [--max-chunk 45] [--passes 3] [--jobs 4] [--model v3]
output: JSON list [{word, startTime, endTime, confidence}] in seconds of AUDIO
        (same format as parakeet-transcriber). Cached next to -o by size+mtime.
"""
import concurrent.futures as cf, hashlib, json, os, re, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PARAKEET = os.path.join(HERE, "parakeet-transcriber", ".build", "release", "parakeet-transcriber")

def duration(path):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                                capture_output=True, text=True, check=True).stdout.strip())

def silences(path, noise="-35dB", min_d=0.35):
    err = subprocess.run(["ffmpeg", "-nostats", "-i", path, "-af", f"silencedetect=noise={noise}:d={min_d}", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    return [(s + e) / 2 for s, e in zip(starts, ends)]

def plan_chunks(total, mids, max_chunk):
    """Greedy: cut at the last silence midpoint before max_chunk; hard cut if none."""
    cuts, cur = [0.0], 0.0
    while total - cur > max_chunk:
        cand = [m for m in mids if cur + 10 < m <= cur + max_chunk]
        nxt = cand[-1] if cand else cur + max_chunk
        cuts.append(nxt); cur = nxt
    cuts.append(total)
    return list(zip(cuts, cuts[1:]))

def run_chunk(path, a, b, model, tmp):
    wav = os.path.join(tmp, f"c_{a:09.3f}.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", path,
                    "-ac", "1", "-ar", "16000", wav], check=True)
    out = subprocess.run([PARAKEET, wav, "--model", model], capture_output=True, text=True, timeout=1800)
    if out.returncode:
        raise RuntimeError(f"parakeet failed on {a:.1f}-{b:.1f}: {out.stderr[-300:]}")
    words = json.loads(out.stdout)
    for w in words:
        w["startTime"] = round(w["startTime"] + a, 3)
        w["endTime"] = round(w["endTime"] + a, 3)
    return words

def main():
    a = sys.argv[1:]
    if not a or a[0].startswith("-"):
        sys.exit(__doc__)
    path = a[0]
    opt = lambda k, d: type(d)(a[a.index(k) + 1]) if k in a else d
    out = opt("-o", os.path.splitext(os.path.basename(path))[0] + ".words.json")
    max_chunk, jobs, model = opt("--max-chunk", 45.0), opt("--jobs", 4), opt("--model", "v3")
    passes = opt("--passes", 3)
    st = os.stat(path)
    key = hashlib.sha1(f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime}|{model}|{max_chunk}|{passes}".encode()).hexdigest()[:16]
    cache = out + ".key"
    if os.path.exists(out) and os.path.exists(cache) and open(cache).read() == key:
        print(json.dumps({"out": out, "cached": True, "words": len(json.load(open(out)))})); return
    total = duration(path)
    mids = silences(path)
    sizes = [max_chunk, max_chunk * 0.62, max_chunk * 2.3][:passes]  # different sizes => different seams
    runs = []
    with tempfile.TemporaryDirectory() as tmp, cf.ThreadPoolExecutor(jobs) as ex:
        for i, size in enumerate(sizes):
            chunks = plan_chunks(total, mids, size)
            sub = os.path.join(tmp, f"p{i}"); os.makedirs(sub)
            parts = list(ex.map(lambda c: run_chunk(path, c[0], c[1], model, sub), chunks))
            runs.append(sorted((w for p in parts for w in p), key=lambda w: w["startTime"]))
    words = list(runs[0]); added = 0
    for extra in runs[1:]:
        occupied = [(w["startTime"] - 0.05, w["endTime"] + 0.05) for w in words]
        import bisect
        starts = [o[0] for o in occupied]
        for w in extra:
            m = (w["startTime"] + w["endTime"]) / 2
            i = bisect.bisect(starts, m) - 1
            if i >= 0 and occupied[i][0] <= m <= occupied[i][1]:
                continue
            if i + 1 < len(occupied) and occupied[i + 1][0] <= m <= occupied[i + 1][1]:
                continue
            w["pass"] = 1 + runs.index(extra); words.append(w); added += 1
        words.sort(key=lambda w: w["startTime"])
    chunks = plan_chunks(total, mids, max_chunk)
    json.dump(words, open(out, "w"))
    open(cache, "w").write(key)
    print(json.dumps({"out": out, "cached": False, "duration_s": round(total, 1), "chunks": len(chunks), "passes": passes, "words": len(words), "recovered_by_extra_passes": added}))

if __name__ == "__main__":
    main()
