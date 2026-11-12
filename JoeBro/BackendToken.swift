import Foundation
import Security

/// Bearer token for a remote backend (the bundled local one needs none). Lives in the Keychain; the first launch
/// seeds it from the `JoeBroBackendToken` Info.plist key (fed by the gitignored
/// Secrets.xcconfig) when the Keychain entry is empty.
enum BackendToken {
    private static var service: String {  (Bundle.main.bundleIdentifier ?? "app.joebro") + ".backend" }
    private static let account = "backendBearerToken"

    private static var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }

    static func load() -> String? {
        var q = query
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data, let s = String(data: data, encoding: .utf8), !s.isEmpty
        else { return nil }
        return s
    }

    static func save(_ token: String) {
        SecItemDelete(query as CFDictionary)
        let t = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        var attrs = query
        attrs[kSecValueData as String] = Data(t.utf8)
        attrs[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(attrs as CFDictionary, nil)
    }

    /// Call once at launch. Only builds whose Info.plist defines JoeBroBackendToken (the personal build) seed anything.
    static func seedIfEmpty() {
        guard load() == nil,
              let raw = Bundle.main.object(forInfoDictionaryKey: "JoeBroBackendToken") as? String
        else { return }
        let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        // An unexpanded "$(JOEBRO_BACKEND_TOKEN)" means no Secrets.xcconfig at build time.
        if !t.isEmpty, !t.hasPrefix("$(") { save(t) }
    }

    static func authorize(_ req: inout URLRequest) {
        if let t = load() { req.setValue("Bearer \(t)", forHTTPHeaderField: "Authorization") }
    }
}
