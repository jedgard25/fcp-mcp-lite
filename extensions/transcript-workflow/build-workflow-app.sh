#!/bin/sh
# Build the FCP Workflow Extension container skeleton.
# Full .appex linking needs Apple's Workflow Extension SDK + Xcode target
# (see README); this script verifies the shared Swift package builds and
# stages a signed TranscriptPanel.app with the extension Info.plist +
# entitlements so FCP registration can be tested once the SDK target exists.
set -eu
cd "$(dirname "$0")"
swift build -c release
APP="$PWD/.build/TranscriptPanel.app"
APPEX="$APP/Contents/PlugIns/TranscriptWorkflow.appex"
mkdir -p "$APP/Contents/MacOS" "$APPEX/Contents/MacOS" "$APPEX/Contents/Resources"
# Container placeholder binary: re-use `true` unless an Xcode app target exists.
# The Swift package itself is the source of truth and is what `swift build` verifies.
cp Container/Info.plist "$APP/Contents/Info.plist"
cp Extension/Info.plist "$APPEX/Contents/Info.plist"
cp Container/TranscriptWorkflow.entitlements "$APPEX/Contents/Resources/TranscriptWorkflow.entitlements"
printf '#!/bin/sh\nexec /usr/bin/true "$@"\n' > "$APP/Contents/MacOS/TranscriptPanel"
chmod +x "$APP/Contents/MacOS/TranscriptPanel"
printf '#!/bin/sh\nexec /usr/bin/true "$@"\n' > "$APPEX/Contents/MacOS/TranscriptWorkflow"
chmod +x "$APPEX/Contents/MacOS/TranscriptWorkflow"
codesign --force --sign - "$APPEX" 2>/dev/null || true
codesign --force --sign - "$APP" 2>/dev/null || true
printf '%s\n' "$APP"
