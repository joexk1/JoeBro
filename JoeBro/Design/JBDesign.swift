// JBDesign.swift — the JoeBro design system. Shared verbatim between the
// macOS app (JoeBro/Design) and the iPhone app (JoeBroMobile/Design): edit one,
// copy to the other. Spec: "UI reckoning/DESIGN.md".
//
// Ink on paper, not glass: flat surfaces, 1pt hairlines, Geist + Geist Mono,
// one accent from the chosen palette, a phosphor "alive" colour for anything
// running, and ASCII/dither texture at the edges (see JBAscii.swift).

import SwiftUI
import CoreText
#if os(macOS)
import AppKit
typealias JBNativeColor = NSColor
#else
import UIKit
typealias JBNativeColor = UIColor
#endif

// MARK: - Colour

extension JBNativeColor {
    convenience init(jbHex hex: UInt32, alpha: CGFloat = 1) {
        self.init(red: CGFloat((hex >> 16) & 0xFF) / 255,
                  green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255,
                  alpha: alpha)
    }

    /// A colour that follows the light/dark appearance it is drawn in.
    static func jbDynamic(light: JBNativeColor, dark: JBNativeColor) -> JBNativeColor {
        #if os(macOS)
        return NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light
        }
        #else
        return UIColor { $0.userInterfaceStyle == .dark ? dark : light }
        #endif
    }
}

extension Color {
    /// Ink at an alpha (black in light mode, white in dark): composites over
    /// any ground — solid, photo or scene — without reading as a grey box.
    init(jbInkAlpha light: CGFloat, dark: CGFloat) {
        #if os(macOS)
        self.init(nsColor: .jbDynamic(light: NSColor(white: 0, alpha: light), dark: NSColor(white: 1, alpha: dark)))
        #else
        self.init(uiColor: .jbDynamic(light: UIColor(white: 0, alpha: light), dark: UIColor(white: 1, alpha: dark)))
        #endif
    }

    init(jbLight light: UInt32, dark: UInt32) {
        #if os(macOS)
        self.init(nsColor: .jbDynamic(light: .init(jbHex: light), dark: .init(jbHex: dark)))
        #else
        self.init(uiColor: .jbDynamic(light: .init(jbHex: light), dark: .init(jbHex: dark)))
        #endif
    }
}

/// The palette list shared by both apps. `mono` is the plain mode-aware
/// black/white; every other entry tints the accent and the backdrop glow.
enum JBPalette: String, CaseIterable, Identifiable, Codable {
    case auto, mono, ember, phosphor, cobalt, violet, rose, moss, sand

    var id: String { rawValue }
    var label: String { self == .mono ? "Mono" : self == .auto ? "From background" : rawValue.capitalized }

    /// Old preset names from 1.5 and earlier (mac `bgPreset`, iOS `wallpaperName`).
    init(legacy name: String?) {
        switch name ?? "" {
        case "purple": self = .violet
        case "ocean": self = .cobalt
        case "forest": self = .moss
        case "sunset": self = .ember
        case "graphite", "monochrome": self = .mono
        default: self = JBPalette(rawValue: name ?? "") ?? .ember
        }
    }

    /// (light-mode, dark-mode) accent. Light values are darkened for contrast on paper.
    var hex: (light: UInt32, dark: UInt32) {
        switch self {
        case .auto: return JBAppearance.shared.autoHex
        case .mono: return (0x151515, 0xEDEDEC)
        case .ember: return (0xD9501A, 0xFF6A2B)
        case .phosphor: return (0x0F8A69, 0x5FD1B0)
        case .cobalt: return (0x2F5BEA, 0x7397FF)
        case .violet: return (0x6A48DB, 0xA88EFF)
        case .rose: return (0xD23A68, 0xFF7099)
        case .moss: return (0x4C7A1E, 0xA6D35F)
        case .sand: return (0x94661C, 0xE3BD7E)
        }
    }

    var accent: Color { Color(jbLight: hex.light, dark: hex.dark) }
}

/// What sits behind the panels.
enum JBBackdropKind: String, Codable {
    case palette      // flat paper/ink + a dithered accent glow
    case photo        // a still image the user picked
    case scene        // an animated backdrop (bundled ASCII scene, user HTML, or video)
}

/// The one appearance store. Both apps read it; settings write it.
@Observable
final class JBAppearance: @unchecked Sendable {
    static let shared = JBAppearance()

    var palette: JBPalette {
        didSet { UserDefaults.standard.set(palette.rawValue, forKey: Keys.palette) }
    }
    /// Panel opacity over the backdrop, 0.35 (see-through) … 1 (solid).
    var solidity: Double {
        didSet { UserDefaults.standard.set(solidity, forKey: Keys.solidity) }
    }
    var backdrop: JBBackdropKind {
        didSet { UserDefaults.standard.set(backdrop.rawValue, forKey: Keys.backdrop) }
    }
    /// For `.scene`: a bundled scene id, or a file URL string for a user backdrop.
    var sceneID: String {
        didSet { UserDefaults.standard.set(sceneID, forKey: Keys.scene) }
    }

    /// Accent taken from the current background (photo, scene, video) for `.auto`.
    var autoHex: (light: UInt32, dark: UInt32) {
        didSet {
            UserDefaults.standard.set(Int(autoHex.light), forKey: Keys.autoLight)
            UserDefaults.standard.set(Int(autoHex.dark), forKey: Keys.autoDark)
        }
    }

    /// Derive the `.auto` accent from a picture of the background: the dominant
    /// vivid hue, re-lit for light and dark mode so it always reads as an accent.
    func adoptAccent(from image: CGImage) {
        let n = 32
        var px = [UInt8](repeating: 0, count: n * n * 4)
        guard let ctx = CGContext(data: &px, width: n, height: n, bitsPerComponent: 8, bytesPerRow: n * 4,
                                  space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return }
        ctx.interpolationQuality = .medium
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: n, height: n))
        var x = 0.0, y = 0.0, sat = 0.0, wsum = 0.0
        for i in stride(from: 0, to: px.count, by: 4) {
            let r = Double(px[i]) / 255, g = Double(px[i + 1]) / 255, b = Double(px[i + 2]) / 255
            let mx = max(r, g, b), mn = min(r, g, b), d = mx - mn
            guard mx > 0.12, d > 0.06 else { continue }
            let s = d / mx
            var h: Double
            if mx == r { h = (g - b) / d } else if mx == g { h = (b - r) / d + 2 } else { h = (r - g) / d + 4 }
            h = (h / 6).truncatingRemainder(dividingBy: 1); if h < 0 { h += 1 }
            let w = s * s * (1 - abs(mx - 0.65))
            x += cos(h * 2 * .pi) * w; y += sin(h * 2 * .pi) * w; sat += s * w; wsum += w
        }
        guard wsum > 0.4 else { return }   // a grey picture keeps the previous accent
        var hue = atan2(y, x) / (2 * .pi); if hue < 0 { hue += 1 }
        let s = min(0.78, max(0.42, sat / wsum))
        func hex(_ h: Double, _ s: Double, _ v: Double) -> UInt32 {
            let i = Int(h * 6) % 6, f = h * 6 - Double(Int(h * 6))
            let p = v * (1 - s), q = v * (1 - f * s), t = v * (1 - (1 - f) * s)
            let (r, g, b): (Double, Double, Double) = [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)][i]
            return UInt32(r * 255) << 16 | UInt32(g * 255) << 8 | UInt32(b * 255)
        }
        let next = (light: hex(hue, min(0.85, s + 0.1), 0.62), dark: hex(hue, s, 0.95))
        DispatchQueue.main.async { self.autoHex = next }
    }

    private enum Keys {
        static let autoLight = "jbAutoLight", autoDark = "jbAutoDark"
        static let palette = "jbPalette", solidity = "jbSolidity"
        static let backdrop = "jbBackdrop", scene = "jbScene"
    }

    private init() {
        let d = UserDefaults.standard
        palette = d.string(forKey: Keys.palette).flatMap(JBPalette.init(rawValue:))
            ?? JBPalette(legacy: d.string(forKey: "bgPreset") ?? d.string(forKey: "wallpaperName"))
        solidity = d.object(forKey: Keys.solidity) == nil ? 0.88 : d.double(forKey: Keys.solidity)
        backdrop = d.string(forKey: Keys.backdrop).flatMap(JBBackdropKind.init(rawValue:)) ?? .palette
        sceneID = d.string(forKey: Keys.scene) ?? ""
        autoHex = d.object(forKey: Keys.autoDark) == nil ? (0xD9501A, 0xFF6A2B)
            : (UInt32(d.integer(forKey: Keys.autoLight)), UInt32(d.integer(forKey: Keys.autoDark)))
    }

    var panelOpacity: Double { min(1, max(0.35, solidity)) }
}

/// Design tokens. Values are identical on macOS and iOS.
enum JB {
    // Ground and surfaces
    static let bg = Color(jbLight: 0xF5F4F1, dark: 0x0A0A0B)
    static let surface = Color(jbLight: 0xFCFBF9, dark: 0x131314)
    static let raised = Color(jbInkAlpha: 0.035, dark: 0.045)
    static let hover = Color(jbInkAlpha: 0.06, dark: 0.07)
    static let pressed = Color(jbInkAlpha: 0.09, dark: 0.10)

    // Lines (translucent: the same hairline on paper, photo or scene)
    static let line = Color(jbInkAlpha: 0.08, dark: 0.075)
    static let lineStrong = Color(jbInkAlpha: 0.15, dark: 0.13)

    // Ink
    static let ink = Color(jbLight: 0x151515, dark: 0xEDEDEC)
    static let ink2 = Color(jbLight: 0x66655F, dark: 0x9A9A9F)
    static let ink3 = Color(jbLight: 0xA09E98, dark: 0x5E5E63)
    /// Ink on the inverse ground (text on an ink-filled control).
    static let inverse = Color(jbLight: 0xFCFBF9, dark: 0x0A0A0B)
    // Status
    static let live = Color(jbLight: 0x0F8A69, dark: 0x5FD1B0)
    static let warn = Color(jbLight: 0xB97A06, dark: 0xF5B83D)
    static let danger = Color(jbLight: 0xD13A2C, dark: 0xFF5A4A)

    /// The chosen palette's accent. Reading it inside `body` tracks changes.
    static var accent: Color { JBAppearance.shared.palette.accent }
    /// Text/icon colour on an accent fill.
    static var onAccent: Color {
        JBAppearance.shared.palette == .mono ? inverse : Color(jbLight: 0xFFFFFF, dark: 0x0A0A0B)
    }
    static var accentSoft: Color { accent.opacity(0.14) }

    /// Panel fill at the user's transparency.
    static var panelFill: Color { surface.opacity(JBAppearance.shared.panelOpacity) }
    /// Fill for things you read or type into (composer, replies): follows the
    /// slider on the plain ground, but goes near-solid over a photo or scene.
    static var readFill: Color {
        JBAppearance.shared.backdrop == .palette ? panelFill : surface.opacity(0.96)
    }

    // Radii
    static let rControl: CGFloat = 7
    static let rCard: CGFloat = 10
    static let rPanel: CGFloat = 14

    // Spacing (4pt grid)
    static let s1: CGFloat = 4, s2: CGFloat = 8, s3: CGFloat = 12, s4: CGFloat = 16, s5: CGFloat = 24, s6: CGFloat = 32

    // Motion
    static let snap = Animation.spring(response: 0.26, dampingFraction: 0.9)
    static let smooth = Animation.spring(response: 0.42, dampingFraction: 0.88)
    static let gentle = Animation.easeOut(duration: 0.22)
    static let fade = Animation.easeInOut(duration: 0.16)
}

// MARK: - Type

enum JBFonts {
    /// Registers the bundled Geist faces. Call once at launch, before any view.
    static func register() {
        let names = ["Geist-Light", "Geist-Regular", "Geist-Medium", "Geist-SemiBold", "Geist-Bold",
                     "GeistMono-Regular", "GeistMono-Medium", "GeistMono-SemiBold", "GeistMono-Bold"]
        let urls = names.compactMap {
            Bundle.main.url(forResource: $0, withExtension: "ttf")
                ?? Bundle.main.url(forResource: $0, withExtension: "ttf", subdirectory: "Fonts")
        }
        guard !urls.isEmpty else { return }
        CTFontManagerRegisterFontURLs(urls as CFArray, .process, true, nil)
    }

    static func sans(_ weight: Font.Weight) -> String {
        switch weight {
        case .ultraLight, .thin, .light: return "Geist-Light"
        case .medium: return "Geist-Medium"
        case .semibold: return "Geist-SemiBold"
        case .bold, .heavy, .black: return "Geist-Bold"
        default: return "Geist-Regular"
        }
    }

    static func mono(_ weight: Font.Weight) -> String {
        switch weight {
        case .medium: return "GeistMono-Medium"
        case .semibold: return "GeistMono-SemiBold"
        case .bold, .heavy, .black: return "GeistMono-Bold"
        default: return "GeistMono-Regular"
        }
    }

    /// Point size for each text style, matched to the platform's system sizes
    /// so layouts keep their proportions.
    static func size(_ style: Font.TextStyle) -> CGFloat {
        #if os(macOS)
        switch style {
        case .largeTitle: return 26
        case .title: return 22
        case .title2: return 17
        case .title3: return 15
        case .headline, .body: return 13
        case .callout: return 12
        case .subheadline: return 11
        case .footnote, .caption: return 10.5
        case .caption2: return 10
        @unknown default: return 13
        }
        #else
        switch style {
        case .largeTitle: return 34
        case .title: return 28
        case .title2: return 22
        case .title3: return 20
        case .headline, .body: return 17
        case .callout: return 16
        case .subheadline: return 15
        case .footnote: return 13
        case .caption: return 12
        case .caption2: return 11
        @unknown default: return 17
        }
        #endif
    }

    #if os(macOS)
    static func nsFont(_ size: CGFloat, _ weight: Font.Weight = .regular, mono: Bool = false) -> NSFont {
        NSFont(name: mono ? Self.mono(weight) : sans(weight), size: size)
            ?? (mono ? .monospacedSystemFont(ofSize: size, weight: .regular) : .systemFont(ofSize: size))
    }
    #else
    static func uiFont(_ size: CGFloat, _ weight: Font.Weight = .regular, mono: Bool = false) -> UIFont {
        UIFont(name: mono ? Self.mono(weight) : sans(weight), size: size)
            ?? (mono ? .monospacedSystemFont(ofSize: size, weight: .regular) : .systemFont(ofSize: size))
    }
    #endif
}

extension Font {
    /// Geist at an explicit size. Scales with Dynamic Type relative to `style`.
    static func jb(_ size: CGFloat, _ weight: Font.Weight = .regular, relativeTo style: Font.TextStyle = .body) -> Font {
        .custom(JBFonts.sans(weight), size: size, relativeTo: style)
    }
    /// Geist Mono at an explicit size: labels, meta, numbers, code, ASCII.
    static func jbMono(_ size: CGFloat, _ weight: Font.Weight = .regular, relativeTo style: Font.TextStyle = .body) -> Font {
        .custom(JBFonts.mono(weight), size: size, relativeTo: style)
    }
    static func jb(_ style: Font.TextStyle, _ weight: Font.Weight? = nil) -> Font {
        .custom(JBFonts.sans(weight ?? (style == .headline ? .semibold : .regular)),
                size: JBFonts.size(style), relativeTo: style)
    }
    static func jbMono(_ style: Font.TextStyle, _ weight: Font.Weight = .regular) -> Font {
        .custom(JBFonts.mono(weight), size: JBFonts.size(style) * 0.94, relativeTo: style)
    }

    static var jbLargeTitle: Font { .jb(.largeTitle, .semibold) }
    static var jbTitle: Font { .jb(.title, .semibold) }
    static var jbTitle2: Font { .jb(.title2, .semibold) }
    static var jbTitle3: Font { .jb(.title3, .medium) }
    static var jbHeadline: Font { .jb(.headline) }
    static var jbBody: Font { .jb(.body) }
    static var jbCallout: Font { .jb(.callout) }
    static var jbSubheadline: Font { .jb(.subheadline) }
    static var jbFootnote: Font { .jb(.footnote) }
    static var jbCaption: Font { .jb(.caption) }
    static var jbCaption2: Font { .jb(.caption2) }
}

// MARK: - Surfaces

enum JBSurfaceLevel { case panel, raised, chrome }

private struct JBSurfaceModifier: ViewModifier {
    var level: JBSurfaceLevel
    var radius: CGFloat?   // nil = capsule
    var stroke = true

    func body(content: Content) -> some View {
        let fill: Color = {
            switch level {
            case .panel, .chrome: return JB.panelFill
            case .raised: return JB.raised
            }
        }()
        return content.background {
            if let radius {
                RoundedRectangle(cornerRadius: radius, style: .continuous).fill(fill)
                    .overlay {
                        if stroke {
                            RoundedRectangle(cornerRadius: radius, style: .continuous)
                                .strokeBorder(JB.line, lineWidth: 1)
                        }
                    }
            } else {
                Capsule().fill(fill)
                    .overlay { if stroke { Capsule().strokeBorder(JB.line, lineWidth: 1) } }
            }
        }
    }
}

extension View {
    /// Flat panel: surface at the user's opacity, hairline border. No blur.
    func jbSurface(_ level: JBSurfaceLevel = .panel, radius: CGFloat? = JB.rPanel, stroke: Bool = true) -> some View {
        modifier(JBSurfaceModifier(level: level, radius: radius, stroke: stroke))
    }
    /// Card inside a panel.
    func jbCard(padding: CGFloat = 14) -> some View {
        self.padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .jbSurface(.raised, radius: JB.rCard)
    }
    /// Popovers, menus, palettes: the only surfaces that cast a shadow.
    func jbFloating(radius: CGFloat = JB.rCard) -> some View {
        self.background {
            RoundedRectangle(cornerRadius: radius, style: .continuous).fill(JB.surface)
                .overlay(RoundedRectangle(cornerRadius: radius, style: .continuous).strokeBorder(JB.lineStrong, lineWidth: 1))
                .shadow(color: .black.opacity(0.28), radius: 24, y: 12)
        }
    }
    /// Uppercase mono eyebrow used for section headers and meta labels.
    func jbEyebrow() -> some View {
        self.font(.jb(12, .semibold)).foregroundStyle(JB.ink2)
    }
}

// MARK: - Controls

/// Primary = accent fill. Secondary = raised + hairline. Ghost = text only, hover fill.
struct JBButtonStyle: ButtonStyle {
    enum Kind { case primary, secondary, ghost, danger }
    var kind: Kind = .secondary
    var compact = false
    @Environment(\.isEnabled) private var enabled
    #if os(macOS)
    @State private var hovering = false
    #endif

    func makeBody(configuration: Configuration) -> some View {
        let pressed = configuration.isPressed
        #if os(macOS)
        let hot = hovering
        #else
        let hot = false
        #endif
        return configuration.label
            .font(.jb(compact ? 12 : 13, .medium))
            .padding(.horizontal, compact ? 9 : 12)
            .padding(.vertical, compact ? 4 : 7)
            .foregroundStyle(foreground)
            .background {
                RoundedRectangle(cornerRadius: JB.rControl, style: .continuous)
                    .fill(background(pressed: pressed, hot: hot))
            }
            .overlay {
                if kind == .secondary {
                    RoundedRectangle(cornerRadius: JB.rControl, style: .continuous)
                        .strokeBorder(JB.line, lineWidth: 1)
                }
            }
            .scaleEffect(pressed ? 0.97 : 1)
            .opacity(enabled ? 1 : 0.4)
            .animation(JB.snap, value: pressed)
            .contentShape(RoundedRectangle(cornerRadius: JB.rControl))
            #if os(macOS)
            .onHover { h in withAnimation(JB.fade) { hovering = h } }
            #endif
    }

    private var foreground: Color {
        switch kind {
        case .primary: return JB.onAccent
        case .danger: return JB.danger
        case .secondary, .ghost: return JB.ink
        }
    }

    private func background(pressed: Bool, hot: Bool) -> Color {
        switch kind {
        case .primary: return JB.accent.opacity(pressed ? 0.82 : hot ? 0.92 : 1)
        case .secondary: return pressed ? JB.pressed : hot ? JB.hover : JB.raised
        case .ghost, .danger: return pressed ? JB.pressed : hot ? JB.hover : .clear
        }
    }
}

extension ButtonStyle where Self == JBButtonStyle {
    static var jbPrimary: JBButtonStyle { JBButtonStyle(kind: .primary) }
    static var jbSecondary: JBButtonStyle { JBButtonStyle(kind: .secondary) }
    static var jbGhost: JBButtonStyle { JBButtonStyle(kind: .ghost) }
    static var jbDanger: JBButtonStyle { JBButtonStyle(kind: .danger) }
}

/// Square icon button: hover fill on mac, press scale everywhere.
struct JBIconButtonStyle: ButtonStyle {
    var size: CGFloat = 28
    var active = false
    #if os(macOS)
    @State private var hovering = false
    #endif

    func makeBody(configuration: Configuration) -> some View {
        #if os(macOS)
        let hot = hovering
        #else
        let hot = false
        #endif
        return configuration.label
            .font(.system(size: size * 0.48, weight: .regular))
            .foregroundStyle(active ? JB.accent : (hot ? JB.ink : JB.ink2))
            .frame(width: size, height: size)
            .background {
                RoundedRectangle(cornerRadius: JB.rControl, style: .continuous)
                    .fill(configuration.isPressed ? JB.pressed : (hot || active) ? JB.hover : .clear)
            }
            .scaleEffect(configuration.isPressed ? 0.92 : 1)
            .animation(JB.snap, value: configuration.isPressed)
            .contentShape(Rectangle())
            #if os(macOS)
            .onHover { h in withAnimation(JB.fade) { hovering = h } }
            #endif
    }
}

/// Press feedback for custom tappable rows/cards (no hover scale — scale-on-hover reads cheap).
struct JBPressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.985 : 1)
            .opacity(configuration.isPressed ? 0.85 : 1)
            .animation(JB.snap, value: configuration.isPressed)
    }
}

/// Hover highlight for list rows on mac (no-op on iOS).
private struct JBHoverRow: ViewModifier {
    var radius: CGFloat
    var selected: Bool
    @State private var hovering = false
    func body(content: Content) -> some View {
        content
            .background {
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .fill(selected ? JB.pressed : hovering ? JB.hover : .clear)
            }
            .overlay(alignment: .leading) {
                // 2pt accent tick marks the selected row (inspo: quiet nav with a tick).
                if selected {
                    Capsule().fill(JB.accent).frame(width: 2).padding(.vertical, 7).offset(x: -1)
                        .transition(.opacity)
                }
            }
            #if os(macOS)
            .onHover { h in withAnimation(JB.fade) { hovering = h } }
            #endif
    }
}

extension View {
    func jbHoverRow(radius: CGFloat = JB.rControl, selected: Bool = false) -> some View {
        modifier(JBHoverRow(radius: radius, selected: selected))
    }
}

/// Toggles: one tint everywhere, readable in every palette.
struct JBSwitchStyle: ToggleStyle {
    func makeBody(configuration: Configuration) -> some View {
        Toggle(configuration).toggleStyle(.switch).tint(JB.live)
    }
}

/// Mono chip: tags, slash commands, shortcuts, statuses.
struct JBChip: View {
    let text: String
    var tint: Color? = nil
    var body: some View {
        Text(text)
            .font(.jb(12, .medium))
            .foregroundStyle(tint ?? JB.ink2)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(RoundedRectangle(cornerRadius: 5, style: .continuous).fill((tint ?? JB.ink).opacity(0.08)))
            .overlay(RoundedRectangle(cornerRadius: 5, style: .continuous).strokeBorder((tint ?? JB.ink).opacity(0.12), lineWidth: 1))
    }
}

/// Keyboard shortcut hint, e.g. JBKbd("⌘K").
struct JBKbd: View {
    let keys: String
    init(_ keys: String) { self.keys = keys }
    var body: some View {
        Text(keys)
            .font(.jb(10.5, .medium))
            .foregroundStyle(JB.ink3)
            .padding(.horizontal, 5)
            .padding(.vertical, 1.5)
            .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(JB.line, lineWidth: 1))
    }
}

/// Section header: mono eyebrow + optional count + trailing accessory.
struct JBSectionHeader<Trailing: View>: View {
    let title: String
    var count: Int? = nil
    @ViewBuilder var trailing: Trailing
    var body: some View {
        HStack(spacing: 6) {
            Text(title).jbEyebrow()
            if let count { Text("\(count)").font(.jb(12)).foregroundStyle(JB.ink3) }
            Spacer(minLength: 0)
            trailing
        }
    }
}

extension JBSectionHeader where Trailing == EmptyView {
    init(_ title: String, count: Int? = nil) {
        self.title = title
        self.count = count
        self.trailing = EmptyView()
    }
}

/// Status dot; `live` breathes.
struct JBStatusDot: View {
    var color: Color = JB.live
    var live = false
    var size: CGFloat = 7
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var on = false
    var body: some View {
        Circle().fill(color).frame(width: size, height: size)
            .background {
                if live && !reduceMotion {
                    Circle().fill(color.opacity(0.35))
                        .scaleEffect(on ? 2.4 : 1).opacity(on ? 0 : 1)
                }
            }
            .onAppear {
                guard live, !reduceMotion else { return }
                withAnimation(.easeOut(duration: 1.6).repeatForever(autoreverses: false)) { on = true }
            }
    }
}

/// Text field chrome: raised fill, hairline, accent ring on focus.
struct JBFieldChrome: ViewModifier {
    var focused: Bool
    var radius: CGFloat = JB.rControl
    func body(content: Content) -> some View {
        content
            .textFieldStyle(.plain)
            .font(.jbBody)
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: radius, style: .continuous).fill(JB.raised))
            .overlay(RoundedRectangle(cornerRadius: radius, style: .continuous)
                .strokeBorder(focused ? JB.accent.opacity(0.7) : JB.line, lineWidth: 1))
            .animation(JB.fade, value: focused)
    }
}

extension View {
    func jbField(focused: Bool = false, radius: CGFloat = JB.rControl) -> some View {
        modifier(JBFieldChrome(focused: focused, radius: radius))
    }
}

// MARK: - Icons

/// Concepts that get a JoeBro glyph instead of an Apple-identifiable SF Symbol.
/// Plain utility symbols (chevrons, plus, xmark, gear, folder, envelope, trash,
/// magnifyingglass, arrow.*) stay as SF Symbols, outline weight.
enum JBGlyph: String, CaseIterable {
    case agent      // the personal agent JoeBro (was person/face/sparkles)
    case spark      // AI / generate / magic (was sparkles, wand)
    case memory     // shared memory / brain (was brain, brain.head.profile)
    case nightShift // was moon.zzz
    case skills     // was graduationcap
    case tools      // was hammer
    case coding     // coding agents (was terminal, chevron.left.forwardslash.chevron.right)
    case research   // was sparkle.magnifyingglass, doc.text.magnifyingglass
    case pulse      // heartbeat / proactive / autonomy
    case plugin     // was puzzlepiece.extension
    case tasks      // was checklist
}

/// Draws a JBGlyph on a 16-unit grid with a 1.5-unit stroke, scaled to `size`.
struct JBIcon: View {
    let glyph: JBGlyph
    var size: CGFloat = 16
    var lineWidth: CGFloat? = nil
    init(_ glyph: JBGlyph, size: CGFloat = 16, lineWidth: CGFloat? = nil) {
        self.glyph = glyph
        self.size = size
        self.lineWidth = lineWidth
    }

    var body: some View {
        Canvas { ctx, canvas in
            let k = canvas.width / 16
            let w = (lineWidth ?? 1.5) * k
            let style = StrokeStyle(lineWidth: w, lineCap: .round, lineJoin: .round)
            func p(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: x * k, y: y * k) }
            func rr(_ x: CGFloat, _ y: CGFloat, _ wd: CGFloat, _ ht: CGFloat, _ r: CGFloat) -> Path {
                Path(roundedRect: CGRect(x: x * k, y: y * k, width: wd * k, height: ht * k), cornerRadius: r * k)
            }
            func dot(_ x: CGFloat, _ y: CGFloat, _ r: CGFloat = 1.1) -> Path {
                Path(ellipseIn: CGRect(x: (x - r) * k, y: (y - r) * k, width: 2 * r * k, height: 2 * r * k))
            }
            let shade = GraphicsContext.Shading.foreground
            switch glyph {
            case .agent:
                // A square face with two eyes: JoeBro's ASCII head, iconised.
                ctx.stroke(rr(2.5, 3, 11, 10, 3), with: shade, style: style)
                ctx.fill(dot(6, 7.6), with: shade)
                ctx.fill(dot(10, 7.6), with: shade)
                var m = Path(); m.move(to: p(6.5, 10.4)); m.addLine(to: p(9.5, 10.4))
                ctx.stroke(m, with: shade, style: style)
            case .spark:
                // Asterisk: three crossing strokes.
                var a = Path()
                a.move(to: p(8, 2.5)); a.addLine(to: p(8, 13.5))
                a.move(to: p(3.2, 5.25)); a.addLine(to: p(12.8, 10.75))
                a.move(to: p(3.2, 10.75)); a.addLine(to: p(12.8, 5.25))
                ctx.stroke(a, with: shade, style: style)
            case .memory:
                // Three linked nodes.
                var l = Path()
                l.move(to: p(4, 11.5)); l.addLine(to: p(8, 4.5)); l.addLine(to: p(12, 11.5)); l.addLine(to: p(4, 11.5))
                ctx.stroke(l, with: shade, style: StrokeStyle(lineWidth: w * 0.8, lineCap: .round, lineJoin: .round))
                for (x, y) in [(4.0, 11.5), (8.0, 4.5), (12.0, 11.5)] { ctx.fill(dot(x, y, 2), with: shade) }
            case .nightShift:
                // Crescent: big circle minus an offset circle, outlined.
                let c1 = CGPoint(x: 7.6, y: 8.6), r1: CGFloat = 5.4
                let c2 = CGPoint(x: 10.4, y: 5.9), r2: CGFloat = 4.3
                let dx = c2.x - c1.x, dy = c2.y - c1.y, d = sqrt(dx * dx + dy * dy)
                let a = (r1 * r1 - r2 * r2 + d * d) / (2 * d), h = sqrt(max(0, r1 * r1 - a * a))
                let mx = c1.x + a * dx / d, my = c1.y + a * dy / d
                let i1 = CGPoint(x: mx + h * dy / d, y: my - h * dx / d)
                let i2 = CGPoint(x: mx - h * dy / d, y: my + h * dx / d)
                func ang(_ c: CGPoint, _ q: CGPoint) -> Angle { .radians(atan2(Double(q.y - c.y), Double(q.x - c.x))) }
                var c = Path()
                c.addArc(center: p(c1.x, c1.y), radius: r1 * k, startAngle: ang(c1, i1), endAngle: ang(c1, i2), clockwise: true)
                c.addArc(center: p(c2.x, c2.y), radius: r2 * k, startAngle: ang(c2, i2), endAngle: ang(c2, i1), clockwise: false)
                c.closeSubpath()
                ctx.stroke(c, with: shade, style: style)
                ctx.fill(dot(13.4, 2.8, 0.9), with: shade)
            case .skills:
                // Stacked layers.
                var s = Path()
                s.move(to: p(8, 2.8)); s.addLine(to: p(13.5, 5.8)); s.addLine(to: p(8, 8.8)); s.addLine(to: p(2.5, 5.8)); s.closeSubpath()
                s.move(to: p(2.5, 9)); s.addLine(to: p(8, 12)); s.addLine(to: p(13.5, 9))
                ctx.stroke(s, with: shade, style: style)
            case .tools:
                // Two crossed bars with square ends.
                var t = Path()
                t.move(to: p(3.5, 12.5)); t.addLine(to: p(10, 6))
                t.move(to: p(12.5, 12.5)); t.addLine(to: p(6, 6))
                ctx.stroke(t, with: shade, style: style)
                ctx.stroke(rr(9, 2.5, 4.5, 4.5, 1), with: shade, style: style)
                ctx.stroke(rr(2.5, 2.5, 4.5, 4.5, 1), with: shade, style: style)
            case .coding:
                // Prompt: > _
                var c = Path()
                c.move(to: p(3, 4.5)); c.addLine(to: p(6.5, 8)); c.addLine(to: p(3, 11.5))
                c.move(to: p(8.5, 11.5)); c.addLine(to: p(13, 11.5))
                ctx.stroke(c, with: shade, style: style)
            case .research:
                // Lens with a spark inside.
                ctx.stroke(Path(ellipseIn: CGRect(x: 2.5 * k, y: 2.5 * k, width: 8.5 * k, height: 8.5 * k)), with: shade, style: style)
                var h = Path(); h.move(to: p(10, 10)); h.addLine(to: p(13.5, 13.5))
                ctx.stroke(h, with: shade, style: style)
                ctx.fill(dot(6.75, 6.75, 1.2), with: shade)
            case .pulse:
                var l = Path()
                l.move(to: p(1.8, 8.5)); l.addLine(to: p(5, 8.5)); l.addLine(to: p(6.8, 4)); l.addLine(to: p(9.2, 12.5))
                l.addLine(to: p(11, 8.5)); l.addLine(to: p(14.2, 8.5))
                ctx.stroke(l, with: shade, style: style)
            case .plugin:
                // Socket: square with two prongs.
                ctx.stroke(rr(3, 6, 10, 7.5, 2), with: shade, style: style)
                var pr = Path(); pr.move(to: p(6, 2.5)); pr.addLine(to: p(6, 6)); pr.move(to: p(10, 2.5)); pr.addLine(to: p(10, 6))
                ctx.stroke(pr, with: shade, style: style)
            case .tasks:
                ctx.stroke(rr(2.5, 2.5, 11, 11, 2.5), with: shade, style: style)
                var ck = Path(); ck.move(to: p(5.5, 8.2)); ck.addLine(to: p(7.3, 10)); ck.addLine(to: p(10.8, 6.2))
                ctx.stroke(ck, with: shade, style: style)
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}
