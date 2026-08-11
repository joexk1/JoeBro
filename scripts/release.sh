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
# Needs a "Developer ID Application" certificate and stored notarytool
# credentials; the script checks for both up front and tells you how to make
# them if they're missing.
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

# --- 0. refuse to build a release we can't notarize, rather than quietly
# shipping an ad-hoc DMG that makes every user do the right-click → Open dance.
if ! security find-identity -v -p codesigning | grep -q "Developer ID Application"; then
  cat <<'MSG' >&2
No "Developer ID Application" certificate in the keychain.

  Xcode → Settings → Accounts → (your Apple ID) → Manage Certificates
  → + → Developer ID Application

Then re-run this script.
MSG
  exit 1
fi
if ! xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null 2>&1; then
  cat <<MSG >&2
No notarytool credentials stored under the profile "$NOTARY_PROFILE".

  xcrun notarytool store-credentials $NOTARY_PROFILE \\
      --apple-id <your-apple-id> --team-id $TEAM_ID \\
      --password <app-specific-password from appleid.apple.com>

Then re-run this script.
MSG
  exit 1
fi

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
  CODE_SIGN_IDENTITY="Developer ID Application" CODE_SIGN_STYLE=Manual \
  DEVELOPMENT_TEAM="$TEAM_ID" OTHER_CODE_SIGN_FLAGS="--timestamp" \
  build >/dev/null
APP="$BUILD_DD/Build/Products/Release/JoeBro.app"
[[ -d "$APP" ]] || { echo "Build produced no app"; exit 1; }

# --- 1b. notarize the .app and staple the ticket INTO the bundle, so a copy
# that never came from the DMG (a Sparkle update, an unzipped app) is trusted
# offline too. Apple only accepts archives, hence the throwaway zip.
echo "==> Notarizing JoeBro.app (this takes a few minutes)…"
APPZIP="$(mktemp -d)/JoeBro.zip"
ditto -c -k --keepParent "$APP" "$APPZIP"
xcrun notarytool submit "$APPZIP" --keychain-profile "$NOTARY_PROFILE" --wait
xcrun stapler staple "$APP"
rm -rf "$(dirname "$APPZIP")"

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
echo "==> Notarizing the DMG…"
codesign --force --sign "Developer ID Application" --timestamp "$DMG"
xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
xcrun stapler staple "$DMG"
spctl -a -t open --context context:primary-signature -v "$DMG"

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
