#!/bin/bash
# Validate FCPXML against the DTD shipped inside the patched FCP.
# (xmllint can't load a DTD path containing spaces, so copy it to a temp file.)
set -euo pipefail
DTD_SRC="$HOME/Applications/FCPBridge/Final Cut Pro.app/Contents/Frameworks/Interchange.framework/Versions/A/Resources/FCPXMLv1_13.dtd"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cp "$DTD_SRC" "$TMP/fcpxml.dtd"
for f in "$@"; do xmllint --noout --dtdvalid "$TMP/fcpxml.dtd" "$f" && echo "valid: $f"; done
