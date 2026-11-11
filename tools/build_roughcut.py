#!/usr/bin/env python3
"""Build an A-roll rough cut FCPXML from takes.json picks (or a segment list).

Writes the project as FCP needs it, so the agent never hand-writes FCPXML:
  - camera clip on the storyline, its own audio muted (audio-channel-source)
  - synced processed WAV connected underneath, sample-accurate (N/48000s)
  - camera timecode handled (asset start = embedded TC)
  - each cut padded into silence without clipping neighbouring speech
  - contiguous picks from one take merged into a single cut
  - every clip noted `fcpmcp:id=<id> fcpmcp:group=<Section>:<palette>` (IDs for
    reconcile.py; groups for section colors via bridge/hooks/group_colors.m)
  - a chapter marker (with posterOffset, which FCP needs) at each section start
  - validated with tools/validate_fcpxml.sh before it reports success

usage:
  build_roughcut.py --takes takes.json \\
      --source /abs/CAM.MOV=/abs/processed.wav:OFFSET [--source …] \\
      --project NAME --event NAME -o out.fcpxml \\
      [--drop S012,S044] [--add S013] [--pad-in 0.12] [--pad-out 0.2] [--no-sections]
  or  --segments segs.json  (list of {camera, start, end, id?, section?} in camera seconds)

--drop/--add edit the suggested picks by sentence id (see takes.py --text).
"""
import json, os, subprocess, sys, urllib.parse
from fractions import Fraction

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
from weftcut_to_fcpxml import tc_start_frames  # noqa: E402

PALETTE = [9, 3, 13, 6, 1, 2, 4, 5, 10, 14]   # FCP role-palette indices, visually distinct

def probe(path):
    j = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
                                  capture_output=True, text=True, check=True).stdout)
    v = next((s for s in j["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in j["streams"] if s["codec_type"] == "audio"), None)
    return dict(dur=float(j["format"]["duration"]), w=v and v["width"], h=v and v["height"],
                fps=v and Fraction(v["r_frame_rate"]), ach=a and int(a.get("channels", 2)), ar=a and int(a.get("sample_rate", 48000)))

def url(p): return "file://" + urllib.parse.quote(p)
def rt(x): x = Fraction(x).limit_denominator(10**7); return "0s" if x == 0 else f"{x.numerator}/{x.denominator}s"

def main():
    a = sys.argv
    opt = lambda k, d: type(d)(a[a.index(k) + 1]) if k in a else d
    pad_in, pad_out = opt("--pad-in", 0.12), opt("--pad-out", 0.2)
    sources = {}
    for i, x in enumerate(a):
        if x == "--source":
            cam, rest = a[i + 1].split("=", 1); wav, off = rest.rsplit(":", 1)
            sources[os.path.basename(cam)] = dict(cam=cam, wav=wav, off=Fraction(off))
    if not sources:
        sys.exit(__doc__)
    sents_by_cam = {}
    if "--takes" in a:
        T = json.load(open(opt("--takes", "")))
        by_id = {s["id"]: s for s in T["sentences"]}
        line_section = {l["script_line"]: l["section"] for l in T["lines"]}
        drop = set(filter(None, opt("--drop", "").split(","))); add = set(filter(None, opt("--add", "").split(",")))
        picks = [p["id"] for p in T["picks"] if p["id"] not in drop]
        for x in add:
            if x not in picks: picks.append(x)
        # keep script order; added extras sort in by camera time within their line
        picks.sort(key=lambda i: (by_id[i]["script_line"] if by_id[i]["script_line"] is not None else 10**6,
                                  by_id[i]["camera"], by_id[i]["start"]))
        segs = [dict(id=i, camera=by_id[i]["camera"], start=by_id[i]["start"], end=by_id[i]["end"],
                     section=line_section.get(by_id[i]["script_line"], "")) for i in picks]
        for s in T["sentences"]:
            sents_by_cam.setdefault(s["camera"], []).append((s["start"], s["end"]))
    else:
        segs = json.load(open(opt("--segments", "")))
    for v in sents_by_cam.values(): v.sort()
    # pad into silence without touching neighbouring speech, then merge contiguous
    def room(cam, t, direction):
        spans = sents_by_cam.get(cam, [])
        if direction < 0:
            prev = [e for s, e in spans if e <= t + 1e-3]
            return t - max(prev) if prev else 9
        nxt = [s for s, e in spans if s >= t - 1e-3]
        return min(nxt) - t if nxt else 9
    cuts = []
    for s in segs:
        st = s["start"] - min(pad_in, max(0.0, room(s["camera"], s["start"], -1) / 2))
        en = s["end"] + min(pad_out, max(0.0, room(s["camera"], s["end"], +1) / 2))
        if cuts and cuts[-1]["camera"] == s["camera"] and abs(st - cuts[-1]["end"]) < 0.35 and cuts[-1]["section"] == s.get("section", ""):
            cuts[-1]["end"] = max(cuts[-1]["end"], en); cuts[-1]["ids"].append(s.get("id", ""))
        else:
            cuts.append(dict(camera=s["camera"], start=max(0.0, st), end=en, ids=[s.get("id", f"seg{len(cuts)}")], section=s.get("section", "")))
    # media facts
    info = {}
    for cam, src in sources.items():
        info[cam] = dict(v=probe(src["cam"]), a=probe(src["wav"]), tc=tc_start_frames(src["cam"]))
    first = info[cuts[0]["camera"]]["v"]
    fps = first["fps"]; fd = 1 / fps
    q = lambda sec: round(Fraction(sec).limit_denominator(10**6) / fd) * fd        # quantize to frames
    names = {Fraction(30000, 1001): "2997", Fraction(30): "30", Fraction(24000, 1001): "2398", Fraction(24): "24",
             Fraction(25): "25", Fraction(60000, 1001): "5994", Fraction(60): "60", Fraction(50): "50"}
    fmt_name = f' name="FFVideoFormat{first["w"]}x{first["h"]}p{names[fps]}"' if fps in names else ""
    res = [f'<format id="r1"{fmt_name} frameDuration="{rt(fd)}" width="{first["w"]}" height="{first["h"]}" colorSpace="1-1-1 (Rec. 709)"/>']
    ids = {}
    for k, (cam, src) in enumerate(sources.items()):
        v, au, tc = info[cam]["v"], info[cam]["a"], info[cam]["tc"]
        ids[cam] = (f"v{k}", f"a{k}", tc * fd)
        res.append(f'<asset id="v{k}" name="{os.path.splitext(cam)[0]}" start="{rt(tc * fd)}" duration="{rt(q(v["dur"]))}" hasVideo="1" hasAudio="1" format="r1" '
                   f'videoSources="1" audioSources="1" audioChannels="{v["ach"] or 2}" audioRate="{v["ar"] or 48000}"><media-rep kind="original-media" src="{url(src["cam"])}"/></asset>')
        res.append(f'<asset id="a{k}" name="{os.path.splitext(os.path.basename(src["wav"]))[0]}" start="0s" duration="{rt(Fraction(round(au["dur"] * 48000), 48000))}" '
                   f'hasAudio="1" audioSources="1" audioChannels="{au["ach"] or 2}" audioRate="{au["ar"] or 48000}"><media-rep kind="original-media" src="{url(src["wav"])}"/></asset>')
    sections = []
    if "--no-sections" not in a:
        for c in cuts:
            if c["section"] and c["section"] not in sections: sections.append(c["section"])
    color = {s: PALETTE[i % len(PALETTE)] for i, s in enumerate(sections)}
    spine, t, last_section = [], Fraction(0), None
    for n, c in enumerate(cuts):
        v_id, a_id, tc0 = ids[c["camera"]]
        cin, cout = q(c["start"]), q(c["end"])
        if cout <= cin: continue
        d = cout - cin
        start = tc0 + cin
        wav_in = Fraction(round((cin + sources[c["camera"]]["off"]) * 48000), 48000)
        cid = f"C{n:03d}"
        tag = f"fcpmcp:id={cid} fcpmcp:src={'+'.join(c['ids'])}"
        if c["section"] in color:
            tag += f" fcpmcp:group={c['section'].split(' —')[0].split(' -')[0].replace(' ', '_')[:24]}:{color[c['section']]}"
        marker = ""
        if c["section"] in color and c["section"] != last_section:
            marker = f'<chapter-marker start="{rt(start)}" duration="{rt(fd)}" value="{c["section"].split(" —")[0].title()}" posterOffset="11/30s"/>'
        last_section = c["section"]
        label = f'{os.path.splitext(c["camera"])[0]} {float(c["start"]):.1f}s'
        spine.append(f'<asset-clip ref="{v_id}" offset="{rt(t)}" name="{label}" start="{rt(start)}" duration="{rt(d)}">'
                     f'<note>{tag}</note>'
                     f'<asset-clip ref="{a_id}" lane="-1" offset="{rt(start)}" name="VO {label}" start="{rt(wav_in)}" duration="{rt(d)}" audioRole="dialogue"><note>{tag.replace(cid, cid + "a", 1)}</note></asset-clip>'
                     f'{marker}<audio-channel-source srcCh="1, 2" enabled="0"/></asset-clip>')
        t += d
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n<fcpxml version="1.13"><resources>' + "".join(res) + "</resources>"
           f'<event name="{opt("--event", "Rough Cut")}"><project name="{opt("--project", "rough cut")}">'
           f'<sequence format="r1" duration="{rt(t)}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k"><spine>'
           + "".join(spine) + "</spine></sequence></project></event></fcpxml>\n")
    out = opt("-o", "roughcut.fcpxml")
    open(out, "w").write(xml)
    v = subprocess.run([os.path.join(HERE, "validate_fcpxml.sh"), out], capture_output=True, text=True)
    print(json.dumps({"out": out, "cuts": len(spine), "duration_s": round(float(t), 1), "sections": {s: color[s] for s in sections},
                      "valid": v.returncode == 0, "validator": (v.stdout + v.stderr).strip()[-400:]}, indent=1))
    sys.exit(0 if v.returncode == 0 else 1)

if __name__ == "__main__":
    main()
