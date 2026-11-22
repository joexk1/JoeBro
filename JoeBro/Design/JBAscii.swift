// JBAscii.swift — JoeBro's ASCII / dither identity kit. Shared verbatim
// between macOS and iOS. Everything here respects Reduce Motion and stops
// ticking when off screen (TimelineView pauses with the view).
//
// Rules: texture at the edges, never behind dense text; one tint per piece;
// motion is slow and small. See "UI reckoning/DESIGN.md".

import SwiftUI
#if os(macOS)
import AppKit
#else
import UIKit
#endif

// MARK: - Spinner

/// A thin rotating arc for anything the agent is doing.
struct JBSpinner: View {
    var size: CGFloat = 12
    var tint: Color = JB.ink2
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var spin = false

    var body: some View {
        Circle()
            .trim(from: 0.12, to: 0.78)
            .stroke(tint, style: StrokeStyle(lineWidth: max(1.3, size * 0.13), lineCap: .round))
            .frame(width: size, height: size)
            .rotationEffect(.degrees(spin ? 360 : 0))
            .onAppear {
                guard !reduceMotion else { return }
                withAnimation(.linear(duration: 0.8).repeatForever(autoreverses: false)) { spin = true }
            }
            .accessibilityLabel("Working")
    }
}

// MARK: - Scramble

/// Text that resolves out of glyph noise. `active` keeps it shimmering
/// (thinking labels); otherwise it resolves once on appear (titles, banners).
struct JBScrambleText: View {
    let text: String
    var active = false
    var font: Font = .jb(13)
    var tint: Color = JB.ink2
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var label: String { text.prefix(1).uppercased() + text.dropFirst() }

    var body: some View {
        if active && !reduceMotion {
            TimelineView(.animation(minimumInterval: 1.0 / 30)) { tl in
                let t = tl.date.timeIntervalSinceReferenceDate
                HStack(spacing: 6) {
                    // A soft light sweeps across the label…
                    Text(label).font(font).foregroundStyle(tint.opacity(0.55))
                        .overlay {
                            GeometryReader { g in
                                let w = g.size.width, p = (t.truncatingRemainder(dividingBy: 1.8)) / 1.8
                                LinearGradient(colors: [.clear, tint, .clear], startPoint: .leading, endPoint: .trailing)
                                    .frame(width: max(40, w * 0.45))
                                    .offset(x: -w * 0.45 + p * (w * 1.45))
                            }
                            .mask(Text(label).font(font))
                        }
                    // …and a three-cell dither trail breathes after it.
                    HStack(spacing: 1.5) {
                        ForEach(0..<3, id: \.self) { i in
                            let v = 0.5 + 0.5 * sin(t * 5 - Double(i) * 0.9)
                            Text(["░", "▒", "▓"][min(2, Int(v * 3))])
                                .font(.jbMono(10))
                                .foregroundStyle(JB.accent.opacity(0.35 + 0.5 * v))
                        }
                    }
                }
            }
        } else {
            Text(label).font(font).foregroundStyle(tint)
        }
    }
}

// MARK: - Stream cursor

/// The block cursor at the tail of streaming text.
struct JBStreamCursor: View {
    var tint: Color = JB.accent
    var height: CGFloat = 14
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var on = true
    var body: some View {
        RoundedRectangle(cornerRadius: 1)
            .fill(tint)
            .frame(width: height * 0.5, height: height)
            .opacity(on ? 1 : 0.15)
            .onAppear {
                guard !reduceMotion else { return }
                withAnimation(.easeInOut(duration: 0.55).repeatForever(autoreverses: true)) { on = false }
            }
            .accessibilityHidden(true)
    }
}

// MARK: - JoeBro's face

/// The personal agent's ASCII face. It blinks, glances, talks and sleeps.
struct JBFace: View {
    enum Mood: Equatable { case idle, thinking, talking, happy, sleeping, alert }
    var mood: Mood = .idle
    var size: CGFloat = 13
    var tint: Color = JB.ink
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.periodic(from: .now, by: reduceMotion ? 10 : 0.12)) { tl in
            let lines = Self.frame(mood: mood, t: reduceMotion ? 0 : tl.date.timeIntervalSinceReferenceDate)
            VStack(alignment: .leading, spacing: 0) {
                ForEach(0..<lines.count, id: \.self) { i in
                    Text(lines[i]).font(.jbMono(size, .medium)).foregroundStyle(tint)
                }
            }
            .fixedSize()
        }
        .accessibilityLabel("JoeBro")
    }

    /// Four-line frame. Pure function of mood and time so every face stays in sync.
    static func frame(mood: Mood, t: Double) -> [String] {
        let tick = Int(t / 0.12)
        // Blink ~ every 3.6s for 2 ticks; glance left/right on a slow cycle.
        let blink = tick % 30 >= 28
        let glance = (tick / 40) % 4      // 0 centre, 1 left, 2 centre, 3 right
        var eyeL = "o", eyeR = "o"
        var mouth = "-"
        var pad = (l: 2, r: 2)
        var extra = "   "
        switch mood {
        case .idle:
            if glance == 1 { pad = (1, 3) } else if glance == 3 { pad = (3, 1) }
        case .thinking:
            eyeL = "°"; eyeR = "°"; mouth = "~"; pad = (3, 1)
            extra = ["   ", ".  ", ".. ", "..."][(tick / 3) % 4]
        case .talking:
            mouth = ["-", "o", "O", "o", "-", "o"][tick % 6]
        case .happy:
            eyeL = "^"; eyeR = "^"; mouth = "w"
        case .sleeping:
            eyeL = "-"; eyeR = "-"; mouth = "_"
            extra = [" z ", " zZ", "zZ "][(tick / 6) % 3]
        case .alert:
            eyeL = "O"; eyeR = "O"; mouth = "o"
        }
        if blink && mood != .sleeping && mood != .happy { eyeL = "-"; eyeR = "-" }
        let eyes = String(repeating: " ", count: pad.l) + eyeL + " " + eyeR + String(repeating: " ", count: pad.r)
        return [
            "╭───────╮" + extra,
            "│" + eyes + "│",
            "│   " + mouth + "   │",
            "╰───────╯",
        ]
    }
}

// MARK: - Dot-matrix wordmark

/// "JOEBRO" in a 5×7 dot matrix. Dots switch on in a scatter when it
/// appears, then a faint sparkle travels across now and then.
struct JBWordmark: View {
    var text = "JoeBro"
    var dot: CGFloat = 3
    var gap: CGFloat = 1.6
    var tint: Color = JB.ink
    var animated = true
    var body: some View {
        Text(text).font(.jb(dot * 10, .semibold)).tracking(-dot * 0.35).foregroundStyle(tint)
            .accessibilityLabel("JoeBro")
    }
}

// MARK: - Dot-matrix number

/// Small dot-matrix numerals for counters (agents running, unread, time).
struct JBDotNumber: View {
    let value: String
    var dot: CGFloat = 2
    var tint: Color = JB.ink
    var body: some View {
        Text(value).font(.jb(max(12, dot * 6.5), .semibold)).monospacedDigit().foregroundStyle(tint)
            .contentTransition(.numericText())
    }
}

// MARK: - Dithered backdrop

/// The default backdrop: flat ground with a 1-bit ordered-dither glow of the
/// accent bleeding in from one corner. Rendered once per size bucket into a
/// tiny bitmap and scaled up with nearest-neighbour, so it costs nothing.
struct JBDitherBackdrop: View {
    var palette: JBPalette
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        GeometryReader { geo in
            ZStack {
                JB.bg
                if palette != .mono, let img = JBDitherCache.image(size: geo.size, palette: palette, dark: scheme == .dark) {
                    Image(decorative: img, scale: 1)
                        .interpolation(.none)
                        .resizable()
                        .frame(width: geo.size.width, height: geo.size.height)
                }
            }
        }
        .ignoresSafeArea()
        .accessibilityHidden(true)
    }
}

enum JBDitherCache {
    nonisolated(unsafe) private static var cache: [String: CGImage] = [:]
    static let cell: CGFloat = 3   // on-screen size of one dither pixel

    static func image(size: CGSize, palette: JBPalette, dark: Bool) -> CGImage? {
        guard size.width > 1, size.height > 1 else { return nil }
        // Bucket to 64pt so live window resizing reuses bitmaps.
        let bw = Int((size.width / 64).rounded(.up)) * 64, bh = Int((size.height / 64).rounded(.up)) * 64
        let key = "\(palette.rawValue)-\(dark)-\(bw)x\(bh)"
        if let hit = cache[key] { return hit }
        let w = max(1, Int(CGFloat(bw) / cell)), h = max(1, Int(CGFloat(bh) / cell))
        let hex = dark ? palette.hex.dark : palette.hex.light
        let r = UInt8((hex >> 16) & 0xFF), g = UInt8((hex >> 8) & 0xFF), b = UInt8(hex & 0xFF)
        let peak: Double = dark ? 0.30 : 0.20
        let bayer: [Double] = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5].map { ($0 + 0.5) / 16 }
        var px = [UInt8](repeating: 0, count: w * h * 4)
        let cx = Double(w) * 1.02, cy = Double(h) * 1.04
        let radius = Double(max(w, h)) * 0.78
        for y in 0..<h {
            for x in 0..<w {
                let dx = (Double(x) - cx) / radius, dy = (Double(y) - cy) / (radius * 0.82)
                let d = sqrt(dx * dx + dy * dy)
                let v = max(0, 1 - d)
                let intensity = v * v * (3 - 2 * v)          // smoothstep falloff
                guard intensity > bayer[(y % 4) * 4 + (x % 4)] * 0.98 else { continue }
                let i = (y * w + x) * 4
                let a = peak * (0.35 + 0.65 * intensity)
                px[i] = UInt8(Double(r) * a); px[i + 1] = UInt8(Double(g) * a)
                px[i + 2] = UInt8(Double(b) * a); px[i + 3] = UInt8(255 * a)
            }
        }
        let space = CGColorSpaceCreateDeviceRGB()
        guard let provider = CGDataProvider(data: Data(px) as CFData),
              let img = CGImage(width: w, height: h, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: w * 4,
                                space: space, bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
                                provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)
        else { return nil }
        if cache.count > 12 { cache.removeAll() }
        cache[key] = img
        return img
    }
}

// MARK: - Empty state

/// One small ASCII drawing + one line. No subtitles.
struct JBEmptyState: View {
    enum Art { case inbox, search, done, sleep, plug, files }
    var art: Art = .inbox
    let text: String

    private var symbol: String {
        switch art {
        case .inbox: return "tray"
        case .search: return "magnifyingglass"
        case .done: return "checkmark"
        case .sleep: return "moon"
        case .plug: return "link"
        case .files: return "doc.text"
        }
    }

    /// Just the circled icon, for empty states that lay out their own text.
    static func icon(_ art: Art) -> some View {
        Image(systemName: JBEmptyState(art: art, text: "").symbol)
            .font(.jb(17, .regular))
            .foregroundStyle(JB.ink3)
            .frame(width: 44, height: 44)
            .background(Circle().fill(JB.raised))
            .overlay(Circle().strokeBorder(JB.line, lineWidth: 1))
    }

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: symbol)
                .font(.jb(17, .regular))
                .foregroundStyle(JB.ink3)
                .frame(width: 44, height: 44)
                .background(Circle().fill(JB.raised))
                .overlay(Circle().strokeBorder(JB.line, lineWidth: 1))
            Text(text)
                .font(.jb(14))
                .foregroundStyle(JB.ink2)
                .multilineTextAlignment(.center)
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    static func drawing(_ art: Art) -> String {
        switch art {
        case .inbox:
            return "╭────────────╮\n│            │\n├──╮      ╭──┤\n│  ╰──────╯  │\n╰────────────╯"
        case .search:
            return "  ╭────╮\n  │ ·· │\n  ╰────╯\\\n         \\\n          ·"
        case .done:
            return "╭───╮\n│ • │\n╰───╯"
        case .sleep:
            return "      z Z\n╭───────╮\n│ -   - │\n│   _   │\n╰───────╯"
        case .plug:
            return "  │   │\n╭─┴───┴─╮\n│       │\n╰───┬───╯\n    │"
        case .files:
            return "╭──────╮──╮\n│ ──── │  │\n│ ──── │  │\n│ ──   │──╯\n╰──────╯"
        }
    }
}

// MARK: - Appear motion

private struct JBAppear: ViewModifier {
    var delay: Double
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var shown = false
    func body(content: Content) -> some View {
        content
            .opacity(shown ? 1 : 0)
            .offset(y: shown || reduceMotion ? 0 : 6)
            .onAppear { withAnimation(JB.smooth.delay(delay)) { shown = true } }
    }
}

extension View {
    /// Fade-and-rise on first appear. Stagger lists with `delay: Double(index) * 0.03` (cap ~0.3s).
    func jbAppear(delay: Double = 0) -> some View { modifier(JBAppear(delay: delay)) }
}
