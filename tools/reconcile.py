#!/usr/bin/env python3
"""Reconcile: what changed between the agent's last snapshot and the timeline now?

The agent stamps every clip it places with a note `fcpmcp:id=<ID>` (FCP keeps clip
notes through import, paste, blade and save; custom <metadata> keys are dropped).
After the user edits by hand, export the project again and diff the two exports
by ID, so the agent can pick up on the new shape instead of overwriting it.

  SPLIT     one ID now appears in several pieces (user bladed it)
  DELETED   an ID is gone
  TRIMMED   same ID, different source in/out
  MOVED     same ID and source range, different timeline position, beyond the
            ripple from earlier changes
  RELANED   connected item now on a different role/lane
  ADDED     a clip with no ID (the user brought it in)
  UNCHANGED everything else (counted only)

usage: reconcile.py before.fcpxml after.fcpxml [--project NAME]
Lane numbers aren't stable (FCP compacts empty lanes), so connected items are
compared by role and parent, not by lane number.
"""
import json, re, sys, xml.etree.ElementTree as ET
from fractions import Fraction

def sec(s): return float(Fraction(s.rstrip("s"))) if s else 0.0
ID = re.compile(r"fcpmcp:id=([\w.-]+)")
CLIPS = ("asset-clip", "clip", "video", "audio", "title", "ref-clip", "sync-clip", "mc-clip")

def items(path, project=None):
    r = ET.parse(path).getroot()
    proj = next(p for p in r.iter("project") if project is None or p.get("name") == project)
    out = []
    def walk(e, parent_id, parent_abs, parent_start):
        for ch in e:
            if ch.tag in CLIPS:
                note = ch.findtext("note") or ""
                m = ID.search(note)
                off = sec(ch.get("offset"))
                absoff = off if parent_abs is None else parent_abs + (off - parent_start)
                it = dict(id=m.group(1) if m else None, tag=ch.tag, name=ch.get("name"),
                          role=ch.get("role") or ch.get("audioRole"), connected=parent_abs is not None,
                          parent=parent_id, t=round(absoff, 3), dur=round(sec(ch.get("duration")), 3),
                          src=round(sec(ch.get("start")), 3), enabled=ch.get("enabled", "1"))
                out.append(it)
                walk(ch, it["id"], absoff, sec(ch.get("start")))
            elif ch.tag in ("spine", "gap"):
                if ch.tag == "gap":
                    walk(ch, parent_id, sec(ch.get("offset")) if parent_abs is None else parent_abs, sec(ch.get("start")))
                else:
                    walk(ch, parent_id, parent_abs, parent_start)
    walk(proj.find("sequence/spine"), None, None, 0.0)
    return out

def main():
    a = sys.argv
    project = a[a.index("--project") + 1] if "--project" in a else None
    before, after = items(a[1], project), items(a[2], project)
    bmap = {}
    for it in before:
        if it["id"]:
            bmap.setdefault(it["id"], []).append(it)
    amap = {}
    for it in after:
        if it["id"]:
            amap.setdefault(it["id"], []).append(it)
    changes = []
    for i, bs in bmap.items():
        b = bs[0]
        if i not in amap:
            changes.append(dict(kind="DELETED", id=i, name=b["name"], was_t=b["t"], was_dur=b["dur"]))
            continue
        aa = amap[i]
        if len(aa) > len(bs):
            changes.append(dict(kind="SPLIT", id=i, name=b["name"], pieces=[dict(t=x["t"], dur=x["dur"], src=x["src"]) for x in aa]))
            continue
        x = aa[0]
        if (x["src"], x["dur"]) != (b["src"], b["dur"]):
            changes.append(dict(kind="TRIMMED", id=i, name=b["name"], src=[b["src"], x["src"]], dur=[b["dur"], x["dur"]]))
        if x["connected"] and (x["role"] != b["role"] or x["parent"] != b["parent"]):
            changes.append(dict(kind="RELANED", id=i, name=b["name"], role=[b["role"], x["role"]], parent=[b["parent"], x["parent"]]))
        if x["enabled"] != b["enabled"]:
            changes.append(dict(kind="ENABLED" if x["enabled"] == "1" else "DISABLED", id=i, name=b["name"]))
    # moves: primary-storyline order of surviving IDs
    border = [it["id"] for it in before if it["id"] and not it["connected"] and it["id"] in amap]
    aorder = []
    for it in after:
        if it["id"] and not it["connected"] and it["id"] in bmap and it["id"] not in aorder:
            aorder.append(it["id"])
    if [i for i in border if i in aorder] != [i for i in aorder if i in border]:
        changes.append(dict(kind="MOVED", before=border, after=aorder))
    for it in after:
        if not it["id"]:
            changes.append(dict(kind="ADDED", name=it["name"], tag=it["tag"], t=it["t"], dur=it["dur"], role=it["role"], connected=it["connected"]))
    touched = {c.get("id") for c in changes}
    print(json.dumps({"before_items": len(before), "after_items": len(after),
                      "unchanged_ids": len([i for i in bmap if i not in touched and i in amap]),
                      "changes": changes}, indent=1))

if __name__ == "__main__":
    main()
