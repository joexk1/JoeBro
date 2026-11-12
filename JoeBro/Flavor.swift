import Foundation

/// The two builds share every source file. `PERSONAL` (set by the Personal build
/// configurations) switches on the owner's add-ons in `Personal/`.
enum Flavor {
    #if PERSONAL
    static let isPersonal = true
    /// Name this Mac registers as with a remote backend, so device tools and
    /// computer use run here instead of on the backend host.
    static let deviceName = "macbook"
    #else
    static let isPersonal = false
    static let deviceName = ""
    #endif
}
