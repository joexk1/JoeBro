// PERSONAL add-on: the owner's build talks to one fixed remote backend instead of
// spawning the bundled one. Not part of the production app.
#if PERSONAL
import Foundation

enum PersonalSetup {
    /// Seed the Keychain token and the server address from the build settings
    /// (JOEBRO_BACKEND_TOKEN, JOEBRO_DEFAULT_SERVER in Config/Secrets.xcconfig),
    /// unless the user already set them in Settings.
    static func configureRemoteBackend() {
        BackendToken.seedIfEmpty()
        guard (UserDefaults.standard.string(forKey: "serverURL") ?? "").isEmpty
                || UserDefaults.standard.string(forKey: "serverURL") == "http://127.0.0.1:8765",
              let raw = Bundle.main.object(forInfoDictionaryKey: "JoeBroDefaultServer") as? String
        else { return }
        let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty, !value.hasPrefix("$(") else { return }
        UserDefaults.standard.set(value.contains("://") ? value : "http://" + value, forKey: "serverURL")
    }
}
#endif
