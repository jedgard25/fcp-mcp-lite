#!/usr/bin/env python3
"""Cut sheet: one row per cut of a storyline (frames at head/tail + waveform across
the edit with handles), built from an FCPXML export + ffmpeg on the SOURCE files.

This is the "refine cuts on audio / spot wrong clips" view. It reads only the
FCPXML (identity, source path, source in/out), so it never touches FCP.

usage: cut_sheet.py export.fcpxml out.jpg [--compound NAME] [--from N] [--count N] [--only 3,11,44] [--handle S] [--width 900]
  --only  just these cut indices (e.g. the cuts story_check flagged); cheaper to look at
"""
import sys, os, subprocess, tempfile, urllib.parse, xml.etree.ElementTree as ET
from fractions import Fraction
from PIL import Image, ImageDraw, ImageFont

def sec(s): return Fraction(s.rstrip("s")) if s else Fraction(0)

def load(path, compound=None):
    r = ET.parse(path).getroot()
    assets = {}
    for a in r.iter("asset"):
        rep = a.find("media-rep")
        src = urllib.parse.unquote(urllib.parse.urlparse(rep.get("src")).path) if rep is not None else None
        assets[a.get("id")] = dict(name=a.get("name"), src=src, start=sec(a.get("start")))
    if compound:
        seq = next(m for m in r.iter("media") if m.get("name") == compound).find("sequence")
    else:
        seq = r.find(".//project/sequence")
    cuts = []
    for ch in seq.find("spine"):
        if ch.tag != "asset-clip":
            continue
        a = assets[ch.get("ref")]
        audio = next((c for c in ch if c.tag == "asset-clip" and c.get("lane") == "-1"), None)
        aud = None
        if audio is not None:
            aa = assets[audio.get("ref")]
            # connected child: offset in parent time; its own source start
            aud = dict(src=aa["src"], start=sec(audio.get("start")) - aa["start"])
        cuts.append(dict(name=ch.get("name") or a["name"], off=sec(ch.get("offset")), dur=sec(ch.get("duration")),
                         src=a["src"], src_in=sec(ch.get("start")) - a["start"], audio=aud))
    return cuts

def frame(src, t, w, out):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{float(t):.3f}", "-i", src, "-frames:v", "1",
                    "-vf", f"scale={w}:-2", out], check=True)
    return Image.open(out).convert("RGB")

def wave(src, t0, dur, w, h, out):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0, float(t0)):.3f}", "-t", f"{float(dur):.3f}", "-i", src,
                    "-filter_complex", f"aformat=channel_layouts=mono,showwavespic=s={w}x{h}:colors=#7fd1ff:scale=sqrt",
                    "-frames:v", "1", out], check=True)
    return Image.open(out).convert("RGB")

def main():
    a = sys.argv
    src, out = a[1], a[2]
    opt = lambda k, d: type(d)(a[a.index(k) + 1]) if k in a else d
    cuts = load(src, opt("--compound", ""))
    start, count, handle = opt("--from", 0), opt("--count", 12), Fraction(opt("--handle", 0.5))
    if "--only" in a:
        want = [int(x) for x in a[a.index("--only") + 1].split(",") if x]
        sel = [cuts[i] for i in want if i < len(cuts)]
        labels = want
    else:
        sel = cuts[start:start + count]
        labels = list(range(start, start + len(sel)))
    W = opt("--width", 900)
    TW = W * 240 // 1140; WW = W * 640 // 1140; RH = TW * 9 // 16
    sheet = Image.new("RGB", (TW * 2 + WW + 20, RH * len(sel) + 10), (18, 18, 20))
    d = ImageDraw.Draw(sheet)
    tmp = tempfile.mkdtemp()
    for i, c in enumerate(sel):
        y = 5 + i * RH
        head = frame(c["src"], c["src_in"], TW, f"{tmp}/h{i}.jpg")
        tail = frame(c["src"], c["src_in"] + c["dur"] - Fraction(1001, 30000), TW, f"{tmp}/t{i}.jpg")
        sheet.paste(head, (5, y)); sheet.paste(tail, (TW + 10, y))
        if c["audio"]:
            t0 = c["audio"]["start"] - handle
            wv = wave(c["audio"]["src"], t0, c["dur"] + 2 * handle, WW, RH - 30, f"{tmp}/w{i}.png")
            x0 = TW * 2 + 15
            sheet.paste(wv, (x0, y + 18))
            px = lambda s: x0 + int(float((s - t0) / (c["dur"] + 2 * handle)) * WW)
            for s in (c["audio"]["start"], c["audio"]["start"] + c["dur"]):  # the edit points
                d.line([(px(s), y + 18), (px(s), y + RH - 12)], fill=(255, 90, 90), width=2)
        d.text((TW * 2 + 15, y + 2), f"#{labels[i]}  {c['name']}  @{float(c['off']):.2f}s  len {float(c['dur']):.2f}s  src {float(c['src_in']):.2f}s",
               fill=(230, 230, 230))
    sheet.save(out, quality=88)
    print(out, f"{len(sel)} cuts")

if __name__ == "__main__":
    main()
