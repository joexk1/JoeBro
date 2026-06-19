#!/bin/bash
# JoeBro release builder — builds a signed, updatable release in one command.
#
#   ./scripts/release.sh <marketing-version> <build-number>
#   e.g.  ./scripts/release.sh 1.1 2
#
# It will:
#   1. Build a Release JoeBro.app at the given version (ad-hoc signed — no Apple
#      Developer ID needed).
#   2. Package it into dist/JoeBro-<version>.dmg.
#   3. Run Sparkle's generate_appcast over dist/, which EdDSA-signs every DMG
#      with the private key in your login Keychain and writes/updates appcast.xml.
#
# After it finishes, follow the printed steps to publish (create a GitHub
# Release, upload the DMG, commit appcast.xml). See RELEASING.md for the full
# runbook.
set -euo pipefail

VERSION="${1:?usage: release.sh <marketing-version> <build-number>}"
BUILD="${2:?usage: release.sh <marketing-version> <build-number>}"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$REPO_DIR/dist"
DOWNLOAD_PREFIX="https://github.com/joexk1/joebro/releases/download/v$VERSION/"

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
XCB="$DEVELOPER_DIR/usr/bin/xcodebuild"

# --- locate Sparkle tools (build them from the resolved SPM checkout if needed)
TOOLS="$REPO_DIR/.sparkle-tools"
if [[ ! -x "$TOOLS/generate_appcast" || ! -x "$TOOLS/sign_update" ]]; then
  echo "==> Building Sparkle CLI tools (one-time)…"
  SPK="$(find "$HOME/Library/Developer/Xcode/DerivedData" -type d -path '*/SourcePackages/checkouts/Sparkle' 2>/dev/null | head -1)"
  [[ -n "$SPK" ]] || { echo "Sparkle checkout not found — open the project in Xcode once to resolve packages."; exit 1; }
  mkdir -p "$TOOLS"
  for t in generate_appcast sign_update generate_keys; do
    "$XCB" -project "$SPK/Sparkle.xcodeproj" -scheme "$t" -configuration Release \
      -derivedDataPath "$TOOLS/dd" CODE_SIGNING_ALLOWED=NO build >/dev/null 2>&1
    cp "$TOOLS/dd/Build/Products/Release/$t" "$TOOLS/"
  done
  rm -rf "$TOOLS/dd"
fi

# --- 1. build Release app at the requested version
echo "==> Building JoeBro $VERSION ($BUILD)…"
BUILD_DD="$REPO_DIR/.build-dd"
rm -rf "$BUILD_DD"
"$XCB" -project "$REPO_DIR/JoeBro.xcodeproj" -scheme JoeBro -configuration Release \
  -derivedDataPath "$BUILD_DD" -destination 'platform=macOS' \
  MARKETING_VERSION="$VERSION" CURRENT_PROJECT_VERSION="$BUILD" \
  CODE_SIGN_IDENTITY="-" CODE_SIGN_STYLE=Manual \
  build >/dev/null
APP="$BUILD_DD/Build/Products/Release/JoeBro.app"
[[ -d "$APP" ]] || { echo "Build produced no app"; exit 1; }

# --- 2. package into a DMG (drag-to-Applications layout)
echo "==> Packaging DMG…"
mkdir -p "$DIST"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
DMG="$DIST/JoeBro-$VERSION.dmg"
rm -f "$DMG"
hdiutil create -volname "JoeBro $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

# --- 3. sign + (re)generate the appcast over everything in dist/
echo "==> Signing + generating appcast…"
"$TOOLS/generate_appcast" --download-url-prefix "$DOWNLOAD_PREFIX" "$DIST"
cp "$DIST/appcast.xml" "$REPO_DIR/appcast.xml"

echo
echo "✅ Built: $DMG"
echo "✅ Updated: $REPO_DIR/appcast.xml"
echo
echo "Next (publish so users get the update):"
echo "  1. gh release create v$VERSION \"$DMG\" --title \"JoeBro $VERSION\" --notes \"...\""
echo "     (or upload the DMG to a GitHub Release tagged v$VERSION via the web UI)"
echo "  2. git add appcast.xml && git commit -m \"Release $VERSION\" && git push"
echo
echo "Installed copies will pick it up within a day, or via Settings → Check for Updates."
