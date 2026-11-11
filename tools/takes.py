#!/usr/bin/env python3
"""Takes: turn a talking-head transcript into a reviewable take sheet.

Splits words into sentences, matches each to the script, groups retakes of the
same line, classifies false starts / off-script chatter, and suggests one pick
per script line, so the agent reviews a compact table instead of raw transcript.

usage: takes.py --source /path/CAMERA.MOV=words.json:OFFSET [--source …] [--script script.md]
                [--gap 0.6] [-o takes.json] [--text]

  words.json  transcript of the audio file (seconds in that file)
  OFFSET      audio_time = camera_time + OFFSET (from align_audio.py)
  CAMERA      give the real path: speech outside the camera's duration is dropped
Everything in the output is in CAMERA seconds.

output JSON:
  sentences[]  {id, camera, start, end, text, script_line, score, coverage, kind, flags}
               kind: take | fragment | offscript ; flags: filler, repeat, no_end
  lines[]      {script_line, section, script_text, takes:[ids], pick, why}
  unmatched_script_lines[]   script lines no take covers (missed content!)
  picks[]      ordered {id, camera, start, end, script_line}: a suggested cut list
The suggestion is a default (the last complete, clean take per line, in script
order); the agent's job is to review the flagged rows, not to rubber-stamp it.
"""
import json, os, re, subprocess, sys
from difflib import SequenceMatcher

FILLERS = {"um", "uh", "erm", "hmm", "uhm", "ah"}
END = re.compile(r"[.!?][\"')\]]*$")

def norm(w):
    return re.sub(r"[^\w']", "", w.lower()).replace("é", "e")

def sentences_from_words(words, gap):
    out, cur = [], []
    for w in words:
        if cur and (w["startTime"] - cur[-1]["endTime"] > gap or END.search(cur[-1]["word"])):
            out.append(cur); cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out

def parse_script(path):
    lines, section = [], ""
    for raw in open(path, encoding="utf-8"):
        s = raw.strip()
        if not s:
            continue
        if s.startswith("#"):
            section = re.sub(r"[#*\\]", "", s).strip(); continue
        s = re.sub(r"\\?\[[^\]]*\\?\]", "", s)           # [B-ROLL: …]
        s = re.sub(r"\*\*[A-Z ]+:\*\*", "", s)           # **VOICEOVER:**
        s = re.sub(r"[*_\\]", "", s).strip()
        if not s:
            continue
        for sent in re.split(r"(?<=[.!?])\s+", s):
            toks = [norm(t) for t in sent.split() if norm(t)]
            if len(toks) >= 2:
                lines.append(dict(section=section, text=sent, toks=toks))
    return lines

def match(toks, script, hint):
    """Best script line for a token list; search near the previous match first."""
    best = (0.0, None, 0.0)
    order = sorted(range(len(script)), key=lambda i: abs(i - hint))
    for i in order:
        st = script[i]["toks"]
        sm = SequenceMatcher(None, toks, st, autojunk=False)
        m = sum(b.size for b in sm.get_matching_blocks())
        score = 2 * m / (len(toks) + len(st))
        cov_utt = m / len(toks)
        if cov_utt >= 0.6 and len(toks) < len(st):      # fragment of a longer line
            score = max(score, cov_utt * 0.9)
        if score > best[0]:
            best = (score, i, m / len(st))
        if best[0] > 0.9:
            break
    return best

def main():
    a = sys.argv
    opt = lambda k, d: type(d)(a[a.index(k) + 1]) if k in a else d
    gap = opt("--gap", 0.6)
    srcs = [a[i + 1] for i, x in enumerate(a) if x == "--source"]
    if not srcs:
        sys.exit(__doc__)
    script = parse_script(opt("--script", "")) if "--script" in a else []
    sents = []
    for s in srcs:
        cam, rest = s.split("=", 1)
        path, off = rest.rsplit(":", 1)
        off = float(off)
        cam_dur = None
        if os.path.exists(cam):
            cam_dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", cam],
                                           capture_output=True, text=True).stdout.strip() or 0) or None
        for ws in sentences_from_words(json.load(open(path)), gap):
            if ws[0]["startTime"] - off < 0 or (cam_dur and ws[-1]["endTime"] - off > cam_dur):
                continue  # spoken while the camera wasn't rolling
            toks = [norm(w["word"]) for w in ws if norm(w["word"])]
            if not toks:
                continue
            flags = []
            if any(t in FILLERS for t in toks): flags.append("filler")
            if any(x == y for x, y in zip(toks, toks[1:])): flags.append("repeat")
            if not END.search(ws[-1]["word"]): flags.append("no_end")
            sents.append(dict(camera=os.path.basename(cam), start=round(ws[0]["startTime"] - off, 3),
                              end=round(ws[-1]["endTime"] - off, 3), text=" ".join(w["word"] for w in ws),
                              toks=toks, flags=flags))
    sents.sort(key=lambda x: (x["camera"], x["start"]))
    # camera files are assumed to be recorded in order (DSCF9371 before DSCF9372)
    for i, s in enumerate(sents):
        s["id"] = f"S{i:03d}"
    hint = 0
    for s in sents:
        if script:
            score, li, cov = match(s["toks"], script, hint)
            s["script_line"], s["score"], s["coverage"] = (li if score >= 0.45 else None), round(score, 2), round(cov, 2)
            if s["script_line"] is not None:
                hint = li
        else:
            s["script_line"], s["score"], s["coverage"] = None, 0, 0
        s["kind"] = "offscript" if (script and s["script_line"] is None) else "take"
    # merge back-to-back sentences matching the same script line into one take run
    runs = []
    for x in sents:
        prev = runs[-1] if runs else None
        if (prev and x["script_line"] is not None and x["script_line"] == prev["script_line"] and x["camera"] == prev["camera"]
                and x["start"] - prev["end"] < 1.0 and prev["kind"] == "take"
                and x["toks"][:2] != prev["toks"][:2]):   # same opening words = a restart, not a continuation
            prev["end"] = x["end"]; prev["text"] += " " + x["text"]; prev["toks"] += x["toks"]
            prev["flags"] = sorted(set(prev["flags"]) | set(x["flags"]) - {"no_end"} | ({"no_end"} if "no_end" in x["flags"] else set()))
            prev["parts"].append(x["id"])
            st = script[x["script_line"]]["toks"]
            sm = SequenceMatcher(None, prev["toks"], st, autojunk=False)
            prev["coverage"] = round(sum(b.size for b in sm.get_matching_blocks()) / len(st), 2)
        else:
            x["parts"] = [x["id"]]; runs.append(x)
    sents = runs
    # group by script line; fragments = takes that cover clearly less than the best take
    lines = {}
    for s in sents:
        if s["script_line"] is not None:
            lines.setdefault(s["script_line"], []).append(s)
    out_lines = []
    for li in sorted(lines):
        takes = lines[li]
        best_cov = max(t["coverage"] for t in takes)
        for t in takes:
            if t["coverage"] < best_cov - 0.25 or (t["coverage"] < 0.6 and "no_end" in t["flags"]):
                t["kind"] = "fragment"
        full = [t for t in takes if t["kind"] == "take"]
        clean = [t for t in full if not ({"filler", "repeat"} & set(t["flags"]))]
        pool = clean or full or takes
        pick = pool[-1]
        why = ("last clean complete take" if clean else "last complete take (has filler/repeat)" if full
               else "only partial takes exist")
        if len(takes) > 1:
            why += f" of {len(takes)}"
        out_lines.append(dict(script_line=li, section=script[li]["section"], script_text=script[li]["text"],
                              takes=[t["id"] for t in takes], pick=pick["id"], why=why))
    covered = set(lines)
    unmatched = [dict(script_line=i, section=l["section"], text=l["text"]) for i, l in enumerate(script) if i not in covered]
    by_id = {s["id"]: s for s in sents}
    picks = [dict(id=l["pick"], camera=by_id[l["pick"]]["camera"], start=by_id[l["pick"]]["start"],
                  end=by_id[l["pick"]]["end"], script_line=l["script_line"]) for l in out_lines]
    for s in sents:
        s.pop("toks")
    res = dict(sentences=sents, lines=out_lines, unmatched_script_lines=unmatched, picks=picks,
               stats=dict(sentences=len(sents), script_lines=len(script), lines_covered=len(covered),
                          offscript=sum(s["kind"] == "offscript" for s in sents),
                          fragments=sum(s["kind"] == "fragment" for s in sents)))
    json.dump(res, open(opt("-o", "takes.json"), "w"), indent=1)
    if "--text" in a:
        for l in out_lines:
            print(f"\n[{l['script_line']:3d}] {l['script_text'][:90]}")
            for tid in l["takes"]:
                t = by_id[tid]
                mark = "»" if tid == l["pick"] else " "
                print(f"  {mark} {tid} {t['camera'][:8]} {t['start']:7.2f}-{t['end']:7.2f} {t['kind']:9s} "
                      f"cov={t['coverage']:.2f} {','.join(t['flags']):14s} {t['text'][:80]}")
        print("\nOFF-SCRIPT:", "; ".join(f"{s['id']}@{s['start']:.1f} {s['text'][:40]}" for s in sents if s["kind"] == "offscript")[:3000])
        print("\nUNMATCHED SCRIPT LINES:", [u["script_line"] for u in unmatched])
    print(json.dumps(res["stats"]))

if __name__ == "__main__":
    main()
