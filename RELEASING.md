# Releasing JoeBro (and how auto-update works)

JoeBro updates itself with [Sparkle](https://sparkle-project.org): Sparkle
verifies each update with our own EdDSA signature, and the appcast + DMGs are
hosted free on GitHub. Releases are signed with an Apple **Developer ID** and
notarized, so downloads open on a plain double-click.

## One-time setup 

- ✅ A Sparkle EdDSA **signing key** was generated. The **private key lives in
  your login Keychain** (item: "Private key for signing Sparkle updates"). The
  **public key** is baked into `JoeBro-Info.plist` (`SUPublicEDKey`).
- ⬜ A **Developer ID Application** certificate in the login Keychain.
  Xcode → Settings → Accounts → your Apple ID → *Manage Certificates* → **+** →
  *Developer ID Application*. (Team `D9GFBLXV7L`, set as `DEVELOPMENT_TEAM` in
  the project.)
- ⬜ **notarytool credentials** stored under the profile `joebro`:
  ```bash
  xcrun notarytool store-credentials joebro \
      --apple-id <your-apple-id> --team-id D9GFBLXV7L \
      --password <app-specific password from appleid.apple.com>
  ```
  `release.sh` refuses to build without both, rather than quietly shipping an
  ad-hoc DMG that puts every user through the Gatekeeper dance.
- ✅ `SUFeedURL` points at `https://raw.githubusercontent.com/joexk1/joebro/main/appcast.xml`.
- ✅ The app checks for updates on launch (daily) and via **Settings → General →
  Check for Updates…**.

> ⚠️ **Back up the private key.** If you lose it (wipe the Mac, lose the
> Keychain), you can no longer sign updates that existing installs will accept,
> and everyone has to re-download manually. Export it once and keep it safe:
> ```
> security find-generic-password -s "https://sparkle-project.org" -w
> ```
> Store that string somewhere private (a password manager) — **never commit it.**

## Cutting a release

From the repo root:

```bash
./scripts/release.sh <marketing-version> <build-number>
# e.g. a new release after 1.0:
./scripts/release.sh 1.1 2
```

`<build-number>` **must increase every release** — Sparkle compares it to decide
"is there a newer version". (Keep it simple: 1, 2, 3, …)

The script builds the app at that version, notarizes and staples it, makes
`dist/JoeBro-<version>.dmg`, notarizes and staples *that*, EdDSA-signs it, and
regenerates `appcast.xml`. The two notarization round-trips take a few minutes
each. Then publish:

```bash
# 1. create the GitHub Release and upload the DMG as an asset
gh release create v1.1 dist/JoeBro-1.1.dmg --title "JoeBro 1.1" --notes "What changed…"

# 2. commit the updated appcast so installed copies can see it
git add appcast.xml && git commit -m "Release 1.1" && git push
```

That's it. Installed copies pick up the update within a day, or immediately if
the user hits **Check for Updates**. The download is verified against your
public key and installed in place.

> ⚠️ **The version never lands in the project file.** `release.sh` passes
> `MARKETING_VERSION` and `CURRENT_PROJECT_VERSION` to `xcodebuild` as
> command-line overrides for that one build, and nothing writes them back, so
> the committed `JoeBro.xcodeproj/project.pbxproj` stays at whatever it was
> (currently **1.1.1 / build 3**, while the appcast advertises **1.2.1 / 5**).
> A build straight from Xcode — which is what CONTRIBUTING.md tells
> contributors to do — therefore identifies itself as the old version and is
> immediately offered an "update" to the shipped one. Only the DMG the script
> produces carries the real version. If that bothers you, bump
> `MARKETING_VERSION` and `CURRENT_PROJECT_VERSION` in the project's build
> settings and commit them alongside the appcast.

> The DMG download URL in the appcast is
> `https://github.com/joexk1/joebro/releases/download/v<version>/JoeBro-<version>.dmg`,
> so the Release **tag must be `v<version>`** and the asset filename must match
> what the script produced. The script prints the exact commands.

## First launch

Notarized builds just open — drag JoeBro into Applications and double-click.

⚠️ **Everything released up to and including 1.2.1 was ad-hoc signed**, so users
on those builds still hit Gatekeeper on first open, and README.md /
GETTING_STARTED.md still walk them through right-click → Open. **Delete those
two sections once the first notarized DMG is live**, not before — until then
they're still accurate for whatever is on the Releases page.

To confirm a build really is notarized:

```bash
spctl -a -vv /Applications/JoeBro.app     # → "accepted, source=Notarized Developer ID"
xcrun stapler validate dist/JoeBro-<version>.dmg
```
