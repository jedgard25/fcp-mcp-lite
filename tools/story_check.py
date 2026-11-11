#!/usr/bin/env python3
"""Story check: read a cut back as text and flag lines that are said twice.

Frames and waveforms can't show a repeated sentence, so check the transcript of
what the cut actually plays. For every spine clip it reads the connected audio
child's source range (WAV time, so no offsets needed), pulls the words spoken in
that range, and joins them in timeline order. Then it flags:

  DUPLICATE  two sentences within --window sentences of each other that read
             nearly the same (a retake left in, or the end of one cut repeating
             the start of the next)
  STUTTER    the same word on both sides of a cut ("the | the")

usage: story_check.py export.fcpxml --words AUDIO.wav=words.json [--words …]
                      [--window 4] [--min-words 4] [--threshold 0.72] [--text]
words.json is parakeet-transcriber output for that audio file (WAV seconds).
--text also prints the whole kept transcript with cut markers.
Exit code 1 if anything is flagged.
"""
import json, os, re, sys, urllib.parse, xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from fractions import Fraction

def sec(s): return float(Fraction(s.rstrip("s"))) if s else 0.0
def norm(w): return re.sub(r"[^\w']", "", w.lower())

def kept_words(xml_path, words_by_audio):
    r = ET.parse(xml_path).getroot()
    assets = {}
    for a in r.iter("asset"):
        rep = a.find("media-rep")
        src = urllib.parse.unquote(urllib.parse.urlparse(rep.get("src")).path) if rep is not None else ""
        assets[a.get("id")] = (os.path.basename(src), sec(a.get("start")))
    out, cut = [], 0
    for ch in r.find(".//project/sequence/spine"):
        if ch.tag != "asset-clip":
            continue
        for au in ch.findall("asset-clip"):
            if not au.get("lane", "").startswith("-"):
                continue
            name, a0 = assets[au.get("ref")]
            if name not in words_by_audio:
                continue
            s = sec(au.get("start")) - a0
            e = s + sec(au.get("duration"))
            mid = lambda w: (w["startTime"] + w["endTime"]) / 2
            for w in words_by_audio[name]:
                if s <= mid(w) < e:
                    out.append(dict(word=w["word"], cut=cut, t=round(sec(ch.get("offset")) + mid(w) - s, 2)))
        cut += 1
    return out

def sentences(words):
    sents, cur = [], []
    for w in words:
        cur.append(w)
        if re.search(r"[.!?]$", w["word"]):
            sents.append(cur); cur = []
    if cur:
        sents.append(cur)
    return sents

def main():
    a = sys.argv
    xml = a[1]
    opt = lambda k, d: type(d)(a[a.index(k) + 1]) if k in a else d
    window, min_words, thr = opt("--window", 4), opt("--min-words", 4), opt("--threshold", 0.72)
    wb = {}
    for i, x in enumerate(a):
        if x == "--words":
            name, path = a[i + 1].split("=", 1)
            wb[os.path.basename(name)] = json.load(open(path))
    words = kept_words(xml, wb)
    sents = sentences(words)
    flags = []
    toks = [[norm(w["word"]) for w in s if norm(w["word"])] for s in sents]
    for i in range(len(sents)):
        if len(toks[i]) < min_words:
            continue
        for j in range(i + 1, min(len(sents), i + 1 + window)):
            if len(toks[j]) < min_words:
                continue
            # compare the shorter sentence against the same-length window of the longer
            x, y = sorted((toks[i], toks[j]), key=len)
            best = max(SequenceMatcher(None, x, y[k:k + len(x)]).ratio() for k in range(len(y) - len(x) + 1))
            if best >= thr:
                flags.append(dict(kind="DUPLICATE", score=round(best, 2),
                                  a=dict(t=sents[i][0]["t"], cut=sents[i][0]["cut"], text=" ".join(w["word"] for w in sents[i])),
                                  b=dict(t=sents[j][0]["t"], cut=sents[j][0]["cut"], text=" ".join(w["word"] for w in sents[j]))))
    for p, q in zip(words, words[1:]):
        if p["cut"] != q["cut"] and norm(p["word"]) and norm(p["word"]) == norm(q["word"]):
            flags.append(dict(kind="STUTTER", t=q["t"], cut=q["cut"], text=f"{p['word']} | {q['word']}"))
    if "--text" in a:
        last = None
        for w in words:
            if w["cut"] != last:
                print(f"\n[cut {w['cut']} @{w['t']:.1f}s] ", end="")
                last = w["cut"]
            print(w["word"], end=" ")
        print("\n")
    print(json.dumps({"cuts_with_words": len({w["cut"] for w in words}), "words": len(words),
                      "sentences": len(sents), "flags": flags}, indent=1))
    sys.exit(1 if flags else 0)

if __name__ == "__main__":
    main()
