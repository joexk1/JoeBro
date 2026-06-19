# Releasing JoeBro (and how auto-update works)

JoeBro updates itself with [Sparkle](https://sparkle-project.org). No Apple
Developer account is required — Sparkle verifies each update with our own EdDSA
signature, and the appcast + DMGs are hosted free on GitHub.

## One-time setup — already done

- ✅ A Sparkle EdDSA **signing key** was generated. The **private key lives in
  your login Keychain** (item: "Private key for signing Sparkle updates"). The
  **public key** is baked into `JoeBro-Info.plist` (`SUPublicEDKey`).
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

The script builds the app at that version, makes `dist/JoeBro-<version>.dmg`,
EdDSA-signs it, and regenerates `appcast.xml`. Then publish:

```bash
# 1. create the GitHub Release and upload the DMG as an asset
gh release create v1.1 dist/JoeBro-1.1.dmg --title "JoeBro 1.1" --notes "What changed…"

# 2. commit the updated appcast so installed copies can see it
git add appcast.xml && git commit -m "Release 1.1" && git push
```

That's it. Installed copies pick up the update within a day, or immediately if
the user hits **Check for Updates**. The download is verified against your
public key and installed in place.

> The DMG download URL in the appcast is
> `https://github.com/joexk1/joebro/releases/download/v<version>/JoeBro-<version>.dmg`,
> so the Release **tag must be `v<version>`** and the asset filename must match
> what the script produced. The script prints the exact commands.

## First launch (every user, once)

Because the app is ad-hoc signed (not notarized by Apple), Gatekeeper blocks the
**first** open. This is normal for indie Mac apps. Tell users:

1. Drag **JoeBro** into Applications (from the DMG).
2. **Right-click** JoeBro → **Open** → in the dialog, click **Open** again.
   *(Plain double-click won't show the Open button — it must be right-click → Open.)*
3. If macOS still refuses: **System Settings → Privacy & Security**, scroll to
   the bottom, and click **Open Anyway** next to the JoeBro message.

After that one time, JoeBro opens normally — and Sparkle updates apply without
repeating this, because it removes the quarantine flag on installed updates.

The README's install section spells this out for end users too.
