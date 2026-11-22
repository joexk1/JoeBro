#!/bin/bash
# JoeBro release builder — builds a signed, updatable release in one command.
#
#   ./scripts/release.sh <marketing-version> <build-number>
#   e.g.  ./scripts/release.sh 1.1 2
#
# It will:
#   1. Build a Release JoeBro.app at the given version, signed with your
#      Developer ID, then notarize and staple it.
#   2. Package it into dist/JoeBro-<version>.dmg, then notarize and staple that
#      too — so the first download opens on a plain double-click.
#   3. Run Sparkle's generate_appcast over dist/, which EdDSA-signs every DMG
#      with the private key in your login Keychain and writes/updates appcast.xml.
#
# Signs with a local "Developer ID Application" cert when there is one, otherwise
# with your team's cloud-managed one through Xcode (no certificate to install,
# no notarytool password). Run it from a GUI Terminal on the Mac, not over SSH:
# Xcode's account and the keychain are only unlocked in the logged-in session.
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

TEAM_ID="${JOEBRO_TEAM_ID:-D9GFBLXV7L}"
NOTARY_PROFILE="${JOEBRO_NOTARY_PROFILE:-joebro}"

ADHOC="${JOEBRO_ADHOC:-0}"   # 1 = ad-hoc signed, not notarized (first open: right-click → Open)

# --- 0. pick how to sign. Three ways, best available first:
#   local   a "Developer ID Application" cert in this keychain + notarytool credentials
#   cloud   no local cert: Xcode signs with your team's cloud-managed Developer ID
#           certificate and notarizes through the Apple ID signed in to Xcode
#           (Xcode → Settings → Accounts). Needs the Account Holder or Admin role.
#   adhoc   JOEBRO_ADHOC=1: ad-hoc signed, not notarized (first open: right-click → Open)
if [[ "$ADHOC" == 1 ]]; then
  MODE=adhoc
elif security find-identity -v -p codesigning | grep -q "Developer ID Application" \
     && xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null 2>&1; then
  MODE=local
else
  MODE=cloud
fi
echo "==> Signing: $MODE"

# --- locate Sparkle tools (build them from the resolved SPM checkout if needed)
TOOLS="$REPO_DIR/.sparkle-tools"
if [[ ! -x "$TOOLS/generate_appcast" || ! -x "$TOOLS/sign_update" ]]; then
  echo "==> Building Sparkle CLI tools (one-time)…"
  SPK="$(find "$HOME/Library/Developer/Xcode/DerivedData" -type d -path '*/SourcePackages/checkouts/Sparkle' 2>/dev/null | head -1)"
  [[ -n "$SPK" ]] || { echo "Sparkle checkout not found — open the project in Xcode once to resolve packages."; exit 1; }
  mkdir -p "$TOOLS"
  for t in generate_appcast sign_update generate_keys; do
    "$XCB" -project "$SPK/Sparkle.xcodeproj" -scheme "$t" -configuration Release \
      -derivedDataPath "$TOOLS/dd" CODE_SIGNING_ALLOWED=NO MACOSX_DEPLOYMENT_TARGET=12.0 build >/dev/null 2>&1
    cp "$TOOLS/dd/Build/Products/Release/$t" "$TOOLS/"
  done
  rm -rf "$TOOLS/dd"
fi

# --- 1. build the Release app at the requested version, signed and notarized
BUILD_DD="$REPO_DIR/.build-dd"
rm -rf "$BUILD_DD"
VERS=(MARKETING_VERSION="$VERSION" CURRENT_PROJECT_VERSION="$BUILD")
echo "==> Building JoeBro $VERSION ($BUILD)…"
if [[ "$MODE" == cloud ]]; then
  ARCHIVE="$BUILD_DD/JoeBro.xcarchive"; EXPORT="$BUILD_DD/export"
  "$XCB" -project "$REPO_DIR/JoeBro.xcodeproj" -scheme JoeBro -configuration Release \
    -derivedDataPath "$BUILD_DD" -destination 'generic/platform=macOS' -archivePath "$ARCHIVE" \
    -allowProvisioningUpdates "${VERS[@]}" CODE_SIGN_STYLE=Automatic "DEVELOPMENT_TEAM=$TEAM_ID" \
    CODE_SIGN_IDENTITY="Apple Development" archive | grep -E "error:|ARCHIVE" || true
  [[ -d "$ARCHIVE" ]] || { echo "Archive failed"; exit 1; }
  cat > "$BUILD_DD/ExportOptions.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>method</key><string>developer-id</string>
  <key>signingStyle</key><string>automatic</string>
  <key>teamID</key><string>$TEAM_ID</string>
  <key>destination</key><string>upload</string>
</dict></plist>
PLIST
  echo "==> Signing with the cloud Developer ID cert and sending to Apple's notary service…"
  "$XCB" -exportArchive -archivePath "$ARCHIVE" -exportOptionsPlist "$BUILD_DD/ExportOptions.plist" \
    -exportPath "$EXPORT" -allowProvisioningUpdates | grep -E "error:|EXPORT|Upload" || true
  echo "==> Waiting for notarization (usually 2–10 minutes)…"
  for _ in $(seq 1 120); do
    if "$XCB" -exportNotarizedApp -archivePath "$ARCHIVE" -exportPath "$EXPORT" >/dev/null 2>&1; then break; fi
    sleep 15
  done
  APP="$EXPORT/JoeBro.app"
  xcrun stapler validate "$APP" >/dev/null 2>&1 || { echo "Notarization didn't finish: see Xcode → Organizer → Archives"; exit 1; }
else
  if [[ "$MODE" == adhoc ]]; then
    SIGN_ARGS=(CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual DEVELOPMENT_TEAM=)
  else
    SIGN_ARGS=("CODE_SIGN_IDENTITY=Developer ID Application" CODE_SIGN_STYLE=Manual "DEVELOPMENT_TEAM=$TEAM_ID" OTHER_CODE_SIGN_FLAGS=--timestamp)
  fi
  "$XCB" -project "$REPO_DIR/JoeBro.xcodeproj" -scheme JoeBro -configuration Release \
    -derivedDataPath "$BUILD_DD" -destination 'platform=macOS' "${VERS[@]}" "${SIGN_ARGS[@]}" build >/dev/null
  APP="$BUILD_DD/Build/Products/Release/JoeBro.app"
  [[ -d "$APP" ]] || { echo "Build produced no app"; exit 1; }
  # Notarize the .app and staple the ticket INTO the bundle, so a copy that never came from
  # the DMG (a Sparkle update, an unzipped app) is trusted offline too.
  if [[ "$MODE" == local ]]; then
    echo "==> Notarizing JoeBro.app (this takes a few minutes)…"
    APPZIP="$(mktemp -d)/JoeBro.zip"
    ditto -c -k --keepParent "$APP" "$APPZIP"
    xcrun notarytool submit "$APPZIP" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$APP"
    rm -rf "$(dirname "$APPZIP")"
  fi
fi
[[ "$MODE" == adhoc ]] || spctl -a -t exec -vv "$APP"

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

# --- 2b. sign, notarize and staple the DMG so the very first download opens
# with a normal double-click. This MUST happen before generate_appcast:
# stapling rewrites the DMG, and Sparkle's EdDSA signature has to cover the
# final bytes or every update will fail its signature check.
# (Cloud mode leaves the DMG itself unsigned: the stapled app inside is what Gatekeeper checks.)
if [[ "$MODE" == local ]]; then
echo "==> Notarizing the DMG…"
codesign --force --sign "Developer ID Application" --timestamp "$DMG"
xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
xcrun stapler staple "$DMG"
spctl -a -t open --context context:primary-signature -v "$DMG"
fi

# --- 3. sign + (re)generate the appcast over everything in dist/
echo "==> Signing + generating appcast…"
[[ -f "$DIST/appcast.xml" ]] || cp "$REPO_DIR/appcast.xml" "$DIST/appcast.xml" 2>/dev/null || true   # keep earlier entries
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
