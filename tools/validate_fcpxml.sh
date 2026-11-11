#!/bin/bash
# Validate FCPXML against the DTD shipped inside the patched FCP, plus lint rules for
# XML that the DTD accepts but FCP mishandles. Exit 1 on any failure.
# (xmllint can't load a DTD path containing spaces, so the DTD is copied to a temp file.)
set -uo pipefail
DTD_SRC="$HOME/Applications/FCPBridge/Final Cut Pro.app/Contents/Frameworks/Interchange.framework/Versions/A/Resources/FCPXMLv1_13.dtd"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cp "$DTD_SRC" "$TMP/fcpxml.dtd"
rc=0
for f in "$@"; do
  xmllint --noout --dtdvalid "$TMP/fcpxml.dtd" "$f" || { rc=1; continue; }
  python3 -I - "$f" <<'PY' || rc=1
import sys, xml.etree.ElementTree as ET
f = sys.argv[1]; r = ET.parse(f).getroot(); bad = []
for m in r.iter("chapter-marker"):
    if not m.get("posterOffset"):
        bad.append(f"chapter-marker '{m.get('value')}' has no posterOffset: FCP stores an invalid time and CRASHES when the marker is drawn/clicked (use posterOffset=\"11/30s\")")
for c in r.iter("asset-clip"):
    if c.get("srcEnable"):
        bad.append(f"asset-clip '{c.get('name')}' uses srcEnable: silently dropped on import; use <audio-channel-source srcCh=\"1, 2\" enabled=\"0\"/>")
for md in r.iter("md"):
    if not md.get("key", "").startswith("com.apple."):
        bad.append(f"custom metadata key '{md.get('key')}' is dropped on import; put IDs in <note>fcpmcp:id=…</note>")
for c in r.iter():
    tm = c.find("timeMap")
    if tm is not None and c.get("start"):
        from fractions import Fraction as F
        last = max(F(p.get("time").rstrip("s")) for p in tm.findall("timept"))
        if F(c.get("start").rstrip("s")) > last:
            bad.append(f"{c.tag} '{c.get('name')}': start lies outside its timeMap; FCP silently drops the clip")
if bad:
    print(f"LINT FAIL {f}:"); [print("  - " + b) for b in bad]; sys.exit(1)
print(f"valid: {f}")
PY
done
exit $rc
