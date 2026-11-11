#!/usr/bin/env python3
"""Grade an A-roll rough cut (FCPXML exported from FCP) against a reference cut.

Reference = a WeftCut composition (e.g. the "Host (A-roll)" comp of
best-pokemon-artists) or another FCPXML. Compares the *source ranges kept* per
camera file, not timeline positions, so ordering differences don't hide
selection quality:

  recall    = reference-kept source seconds the candidate also kept
  precision = candidate-kept source seconds the reference also kept
  sync      = per connected-audio child: (audio_src - camera_src) vs the reference offset

usage: roughcut_grade.py candidate.fcpxml --weftcut project.json --comp-label "Host (A-roll)"
"""
import json, os, sys, urllib.parse, xml.etree.ElementTree as ET
from fractions import Fraction

def sec(s): return float(Fraction(s.rstrip("s"))) if s else 0.0

def merge(iv):
    out = []
    for a, b in sorted(iv):
        if out and a <= out[-1][1] + 1e-3:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out

def inter(x, y):
    i = j = 0; tot = 0.0
    while i < len(x) and j < len(y):
        a, b = max(x[i][0], y[j][0]), min(x[i][1], y[j][1])
        if b > a: tot += b - a
        if x[i][1] < y[j][1]: i += 1
        else: j += 1
    return tot

def length(iv): return sum(b - a for a, b in iv)

def candidate(path):
    r = ET.parse(path).getroot()
    assets = {}
    for a in r.iter("asset"):
        rep = a.find("media-rep")
        src = urllib.parse.unquote(urllib.parse.urlparse(rep.get("src")).path) if rep is not None else ""
        assets[a.get("id")] = (os.path.basename(src), sec(a.get("start")))
    seq = r.find(".//project/sequence")
    kept, sync, cuts, total = {}, [], 0, 0.0
    def walk(e):
        nonlocal cuts, total
        for ch in e:
            if ch.tag == "asset-clip" and ch.get("lane") is None:
                name, a0 = assets[ch.get("ref")]
                s, d = sec(ch.get("start")) - a0, sec(ch.get("duration"))
                kept.setdefault(name, []).append((s, s + d)); cuts += 1; total += d
                for au in ch.findall("asset-clip"):
                    if au.get("lane", "").startswith("-"):
                        an, aa0 = assets[au.get("ref")]
                        # child offset is in parent source time; child start is its own source time
                        cam_at_child = sec(au.get("offset")) - a0
                        sync.append((name, an, round(sec(au.get("start")) - aa0 - cam_at_child, 3)))
            elif ch.tag in ("spine", "gap"):
                walk(ch)
    walk(seq.find("spine"))
    return {k: merge(v) for k, v in kept.items()}, sync, cuts, total

def weftcut(path, label):
    p = json.load(open(path))
    pool = p["media_pool"]
    comp = next(c for c in p["compositions"].values() if c.get("label") == label)
    by = {l["id"]: l for t in comp["tracks"] for l in t["layers"]}
    kept, offs = {}, {}
    for ln in comp.get("links", []):
        mem = [by[m] for m in ln["members"]]
        v = next(l for l in mem if l["params"]["kind"] == "VideoClip")
        a = next(l for l in mem if l["params"]["kind"] == "Audio")
        vn = os.path.basename(pool[v["params"]["media"]]["path_abs"])
        an = os.path.basename(pool[a["params"]["media"]]["path_abs"])
        s, e = v["params"]["src_in_us"] / 1e6, v["params"]["src_out_us"] / 1e6
        kept.setdefault(vn, []).append((s, e))
        offs.setdefault((vn, an), []).append(round((a["params"]["src_in_us"] - v["params"]["src_in_us"]) / 1e6, 3))
    n = len(comp["links"])
    offs = {k: max(set(v), key=v.count) for k, v in offs.items()}  # modal offset per pair
    return {k: merge(v) for k, v in kept.items()}, offs, n

def main():
    a = sys.argv
    cand = a[1]
    ref, offs, ref_n = weftcut(a[a.index("--weftcut") + 1], a[a.index("--comp-label") + 1])
    ck, sync, cuts, total = candidate(cand)
    files = sorted(set(ref) | set(ck))
    rep = {"candidate": {"cuts": cuts, "duration_s": round(total, 1)},
           "reference": {"cuts": ref_n, "duration_s": round(sum(length(v) for v in ref.values()), 1)},
           "per_file": {}}
    R = P = RT = PT = 0.0
    for f in files:
        r, c = ref.get(f, []), ck.get(f, [])
        both = inter(r, c)
        rep["per_file"][f] = {"ref_s": round(length(r), 1), "cand_s": round(length(c), 1), "overlap_s": round(both, 1)}
        R += both; RT += length(r); P += both; PT += length(c)
    rep["recall"] = round(R / RT, 3) if RT else None
    rep["precision"] = round(P / PT, 3) if PT else None
    bad = [(cam, au, o, offs.get((cam, au))) for cam, au, o in sync
           if (cam, au) not in offs or abs(o - offs[(cam, au)]) > 0.021]
    rep["sync"] = {"children": len(sync), "pairs_used": sorted({(c, a_) for c, a_, _ in sync}),
                   "reference_offsets": {f"{k[0]}→{k[1]}": v for k, v in offs.items()},
                   "off_by_more_than_20ms": len(bad), "examples": bad[:5]}
    print(json.dumps(rep, indent=1, default=str))

if __name__ == "__main__":
    main()
