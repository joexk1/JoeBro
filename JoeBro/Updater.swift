import Foundation
import Sparkle

/// Thin wrapper around Sparkle's standard updater so SwiftUI can drive it.
///
/// `startingUpdater: true` begins Sparkle's scheduled background checks the
/// moment the app launches (governed by the SU* keys in JoeBro-Info.plist), so
/// updates are found even if Settings is never opened. The Settings → General
/// "Check for Updates…" button calls `checkForUpdates()` for an on-demand check.
///
/// No Apple Developer ID is required: Sparkle verifies each update with its own
/// EdDSA signature (SUPublicEDKey) and strips the macOS quarantine flag from the
/// installed update, so after the one-time first-open Gatekeeper bypass, updates
/// apply seamlessly.
final class UpdaterManager: ObservableObject {
    static let shared = UpdaterManager()

    private let controller: SPUStandardUpdaterController

    /// Sparkle disables the check button mid-check; mirror that into SwiftUI.
    @Published var canCheckForUpdates = false

    private init() {
        controller = SPUStandardUpdaterController(
            startingUpdater: true, updaterDelegate: nil, userDriverDelegate: nil)
        controller.updater.publisher(for: \.canCheckForUpdates)
            .assign(to: &$canCheckForUpdates)
    }

    func checkForUpdates() {
        controller.updater.checkForUpdates()
    }

    /// User-facing toggle for Sparkle's scheduled background checks.
    var automaticallyChecksForUpdates: Bool {
        get { controller.updater.automaticallyChecksForUpdates }
        set { controller.updater.automaticallyChecksForUpdates = newValue }
    }
}
