#!/usr/bin/env python3
"""Conform an FCPXML project from one asset+frame-rate to a higher-rate source.

Given an exported FCPXML and a mapping (old asset id -> new asset id, old
format id -> new format id), rewrite one named project in place:

  * rename the project (optional)
  * repoint every <* ref="OLD_ASSET"> in the project subtree to the new asset
  * repoint the sequence <format> to the new format
  * drop everything not reachable from the project (event browser clips,
    other projects) so an import can't clobber the rest of the library

Time values (durations, offsets, <timeMap> points) are left byte-for-byte
alone: they are all seconds, so an identical-rate swap preserves the cut and
every speed ramp exactly. Output is a minimal <fcpxml> ready to import.
"""
import argparse
import copy
import xml.etree.ElementTree as ET


def collect_needed(project, resmap):
    needed = set()
    for el in project.iter():
        for attr in ("ref", "format"):
            v = el.get(attr)
            if v:
                needed.add(v)
    # assets pull in their format resource (and effects have no deps)
    changed = True
    while changed:
        changed = False
        for rid in list(needed):
            r = resmap.get(rid)
            if r is None:
                continue
            fmt = r.get("format")
            if fmt and fmt not in needed:
                needed.add(fmt)
                changed = True
    return needed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--project", required=True, help="project name to conform")
    ap.add_argument("--new-name", default=None, help="rename the project")
    ap.add_argument("--asset-map", required=True,
                    help="comma list old=new asset ids, e.g. r4=r6")
    ap.add_argument("--format-map", default=None,
                    help="comma list old=new format ids, e.g. r5=r7")
    args = ap.parse_args()

    asset_map = dict(p.split("=") for p in args.asset_map.split(",") if p)
    fmt_map = dict(p.split("=") for p in args.format_map.split(",")) if args.format_map else {}

    tree = ET.parse(args.src)
    root = tree.getroot()
    resources = root.find("resources")
    resmap = {r.get("id"): r for r in resources}
    library = root.find("library")

    project = None
    for p in root.iter("project"):
        if p.get("name") == args.project:
            project = p
            break
    if project is None:
        raise SystemExit(f"project {args.project!r} not found")

    if args.new_name:
        project.set("name", args.new_name)

    seq = project.find("sequence")
    if seq is not None:
        old = seq.get("format")
        if old in fmt_map:
            seq.set("format", fmt_map[old])

    swaps = 0
    for el in project.iter():
        for attr in ("ref", "format"):
            v = el.get(attr)
            if v is None:
                continue
            if attr == "ref" and v in asset_map:
                el.set(attr, asset_map[v]); swaps += 1
            elif attr == "format" and v in fmt_map:
                el.set(attr, fmt_map[v])

    needed = collect_needed(project, resmap)

    new_res = ET.Element("resources")
    for r in resources:
        if r.get("id") in needed:
            new_res.append(copy.deepcopy(r))

    new_root = ET.Element("fcpxml", {"version": root.get("version", "1.13")})
    new_root.append(new_res)
    loc = library.get("location") if library is not None else None
    new_lib = ET.SubElement(new_root, "library", {"location": loc} if loc else {})
    event = None
    if library is not None:
        event = library.find("event")
    ev_attrs = {}
    if event is not None:
        for a in ("name", "uid"):
            if event.get(a):
                ev_attrs[a] = event.get(a)
    new_ev = ET.SubElement(new_lib, "event", ev_attrs)
    new_ev.append(copy.deepcopy(project))

    ET.indent(new_root, space="  ")
    ET.ElementTree(new_root).write(args.dst, encoding="UTF-8", xml_declaration=True)

    print(f"wrote {args.dst}: {swaps} clip refs repointed, "
          f"{len(needed)} resources kept ({sorted(needed)})")


if __name__ == "__main__":
    main()
