#!/bin/bash
#
# fcp-mcp-lite patcher: copy App Store FCP, inject FCPBridge.dylib, re-sign.
# Mechanism follows SpliceKit's patcher/patch_fcp.sh (MIT):
#   insert_dylib (LC_LOAD_DYLIB) + entitlements + codesign.
# Your App Store FCP is never touched.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SOURCE_APP="${SOURCE_APP:-/Applications/Final Cut Pro.app}"
DEST_DIR="${DEST_DIR:-$HOME/Applications/FCPBridge}"
DYLIB="${DYLIB:-$REPO_DIR/bridge/build/FCPBridge.dylib}"
BRIDGE_PORT="${BRIDGE_PORT:-9876}"

log()  { echo "[+] $*"; }
warn() { echo "[!] $*"; }
err()  { echo "[X] $*" >&2; }

[[ -d "$SOURCE_APP" ]] || { err "No FCP at $SOURCE_APP (try SOURCE_APP=... )"; exit 1; }
[[ -f "$DYLIB" ]] || { err "Build the bridge first: make -C $REPO_DIR/bridge"; exit 1; }

APP_NAME="$(basename "$SOURCE_APP")"
DEST_APP="$DEST_DIR/$APP_NAME"
BINARY="$DEST_APP/Contents/MacOS/Final Cut Pro"

if [[ ! -d "$DEST_APP" ]]; then
  log "Copying $SOURCE_APP -> $DEST_APP (a few minutes)..."
  mkdir -p "$DEST_DIR"
  cp -R "$SOURCE_APP" "$DEST_APP"
else
  log "Copy exists, reusing: $DEST_APP"
fi

log "Installing dylib..."
cp -f "$DYLIB" "$DEST_APP/Contents/MacOS/FCPBridge.dylib"

if otool -L "$BINARY" 2>/dev/null | grep -q "@executable_path/FCPBridge.dylib"; then
  log "Already injected (skipping insert_dylib)"
else
  INSERT_DYLIB="/tmp/fcpbridge_insert_dylib"
  if [[ ! -x "$INSERT_DYLIB" ]]; then
    log "Building insert_dylib..."
    TMPD="$(mktemp -d)"
    git clone --quiet https://github.com/tyilo/insert_dylib.git "$TMPD/insert_dylib"
    clang -o "$INSERT_DYLIB" "$TMPD/insert_dylib/insert_dylib/main.c" -framework Foundation
    rm -rf "$TMPD"
  fi
  "$INSERT_DYLIB" --inplace --all-yes "@executable_path/FCPBridge.dylib" "$BINARY"
  otool -L "$BINARY" | grep -q FCPBridge || { err "injection failed"; exit 1; }
  log "LC_LOAD_DYLIB injected"
fi

ENTITLEMENTS="$(mktemp -t fcpbridge-ent).plist"
cat > "$ENTITLEMENTS" << 'ENT'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "https://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.app-sandbox</key><false/>
    <key>com.apple.security.cs.disable-library-validation</key><true/>
    <key>com.apple.security.cs.allow-dyld-environment-variables</key><true/>
    <key>com.apple.security.get-task-allow</key><true/>
</dict>
</plist>
ENT

SIGN_IDENTITY="$(/usr/bin/security find-identity -v -p codesigning 2>/dev/null | awk '/"Apple Development:/ { print $2; exit } /"Developer ID Application:/ && developer == "" { developer = $2 } END { if (developer != "") print developer }')"
SIGN_IDENTITY="${SIGN_IDENTITY:-"-"}"
log "Signing with identity: $SIGN_IDENTITY"

# Sign our dylib first, then the app bundle. Apple's own nested frameworks
# keep their signatures (SpliceKit learned this the hard way).
codesign --force --sign "$SIGN_IDENTITY" "$DEST_APP/Contents/MacOS/FCPBridge.dylib"
codesign --force --deep --options runtime --entitlements "$ENTITLEMENTS" \
  --sign "$SIGN_IDENTITY" "$DEST_APP" || \
  codesign --force --options runtime --entitlements "$ENTITLEMENTS" \
  --sign "$SIGN_IDENTITY" "$DEST_APP"
rm -f "$ENTITLEMENTS"

codesign --verify --verbose "$DEST_APP" 2>&1 | tail -1 || true
log "Done. Launch with: open \"$DEST_APP\""
log "Bridge will listen on 127.0.0.1:$BRIDGE_PORT — check with: make mcp-doctor"
