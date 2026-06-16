import SwiftUI

/// Window background: the user's wallpaper if set, otherwise the chosen
/// colour preset — a vivid gradient so the glass has depth to refract.
struct WallpaperBackground: View {
    @Environment(AppStore.self) private var store
    @Environment(GlassSettings.self) private var glass
    @Environment(\.colorScheme) private var scheme
    @State private var cachedImage: NSImage?
    @State private var cachedPath: String?

    private func image(for url: URL) -> NSImage? {
        // Decode once, not on every body evaluation — re-reading a 4K JPEG
        // from disk per render was a constant CPU tax.
        if cachedPath == url.path, let cachedImage { return cachedImage }
        let img = NSImage(contentsOf: url)
        Task { @MainActor in
            cachedImage = img
            cachedPath = url.path
        }
        return img
    }

    var body: some View {
        GeometryReader { geo in
            Group {
                if let url = store.wallpaperURL, let img = image(for: url) {
                    Image(nsImage: img)
                        .resizable()
                        .scaledToFill()
                        .frame(width: geo.size.width, height: geo.size.height)
                        .clipped()
                        .overlay(Color.black.opacity(scheme == .dark ? 0.18 : 0.04))
                } else {
                    let preset = BackgroundPreset(rawValue: glass.backgroundPreset) ?? .purple
                    LinearGradient(colors: preset.colors(dark: scheme == .dark),
                                   startPoint: .topLeading,
                                   endPoint: .bottomTrailing)
                }
            }
        }
        .ignoresSafeArea()
    }
}

/// No-wallpaper backdrops. Deliberately saturated — liquid glass goes flat
/// over near-black, so even the dark variants keep visible colour depth.
enum BackgroundPreset: String, CaseIterable, Identifiable {
    case purple, ocean, forest, sunset, graphite

    var id: String { rawValue }

    var label: String {
        switch self {
        case .purple: return "Purple"
        case .ocean: return "Ocean"
        case .forest: return "Forest"
        case .sunset: return "Sunset"
        case .graphite: return "Graphite"
        }
    }

    func colors(dark: Bool) -> [Color] {
        switch (self, dark) {
        case (.purple, true):
            return [Color(red: 0.18, green: 0.10, blue: 0.34),
                    Color(red: 0.30, green: 0.13, blue: 0.44),
                    Color(red: 0.11, green: 0.13, blue: 0.36)]
        case (.purple, false):
            return [Color(red: 0.88, green: 0.83, blue: 0.99),
                    Color(red: 0.93, green: 0.85, blue: 0.99),
                    Color(red: 0.83, green: 0.86, blue: 0.99)]
        case (.ocean, true):
            return [Color(red: 0.05, green: 0.16, blue: 0.34),
                    Color(red: 0.07, green: 0.26, blue: 0.44),
                    Color(red: 0.04, green: 0.12, blue: 0.28)]
        case (.ocean, false):
            return [Color(red: 0.82, green: 0.91, blue: 0.99),
                    Color(red: 0.87, green: 0.95, blue: 0.99),
                    Color(red: 0.80, green: 0.88, blue: 0.97)]
        case (.forest, true):
            return [Color(red: 0.06, green: 0.20, blue: 0.14),
                    Color(red: 0.09, green: 0.28, blue: 0.18),
                    Color(red: 0.05, green: 0.16, blue: 0.13)]
        case (.forest, false):
            return [Color(red: 0.85, green: 0.95, blue: 0.88),
                    Color(red: 0.90, green: 0.97, blue: 0.90),
                    Color(red: 0.82, green: 0.93, blue: 0.86)]
        case (.sunset, true):
            return [Color(red: 0.30, green: 0.12, blue: 0.16),
                    Color(red: 0.40, green: 0.18, blue: 0.12),
                    Color(red: 0.24, green: 0.10, blue: 0.22)]
        case (.sunset, false):
            return [Color(red: 0.99, green: 0.89, blue: 0.83),
                    Color(red: 0.99, green: 0.92, blue: 0.85),
                    Color(red: 0.98, green: 0.86, blue: 0.88)]
        case (.graphite, true):
            return [Color(red: 0.12, green: 0.13, blue: 0.15),
                    Color(red: 0.17, green: 0.18, blue: 0.21),
                    Color(red: 0.10, green: 0.11, blue: 0.13)]
        case (.graphite, false):
            return [Color(red: 0.90, green: 0.91, blue: 0.93),
                    Color(red: 0.94, green: 0.95, blue: 0.96),
                    Color(red: 0.88, green: 0.89, blue: 0.91)]
        }
    }
}
