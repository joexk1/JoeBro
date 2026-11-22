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

/// A three-cell block wave: ▂▄▆ → ▄▆█ … Replaces ProgressView spinners
/// for anything the agent is doing.
struct JBSpinner: View {
    var size: CGFloat = 12
    var tint: Color = JB.ink2
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private static let ramp = Array("▁▂▃▄▅▆▇█▇▆▅▄▃▂")

    var body: some View {
        TimelineView(.periodic(from: .now, by: reduceMotion ? 1 : 0.09)) { tl in
            let t = Int(tl.date.timeIntervalSinceReferenceDate / 0.09)
            let r = Self.ramp
            Text(String([r[t % r.count], r[(t + 3) % r.count], r[(t + 6) % r.count]]))
                .font(.jbMono(size))
                .foregroundStyle(tint)
                .fixedSize()
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
    var font: Font = .jbMono(12)
    var tint: Color = JB.ink2
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var start = Date()
    private static let noise = Array("░▒▓<>/\\|=+*#%@$&01")

    var body: some View {
        if reduceMotion {
            Text(text).font(font).foregroundStyle(tint)
        } else {
            TimelineView(.periodic(from: .now, by: 0.05)) { tl in
                Text(frame(at: tl.date)).font(font).foregroundStyle(tint)
            }
            .onAppear { start = Date() }
            .accessibilityLabel(text)
        }
    }

    private func frame(at date: Date) -> String {
        let chars = Array(text)
        let elapsed = date.timeIntervalSince(start)
        let tick = Int(date.timeIntervalSinceReferenceDate * 20)
        var out = ""
        for (i, c) in chars.enumerated() {
            if c == " " { out.append(" "); continue }
            let revealAt = Double(i) * 0.035 + 0.12
            var scrambled = elapsed < revealAt
            if active && !scrambled {
                // A soft travelling band of noise keeps "thinking" alive.
                let band = (tick / 2) % (chars.count + 6)
                scrambled = abs(band - i) <= 0 && (tick + i) % 3 == 0
            }
            out.append(scrambled ? Self.noise[(tick &+ i &* 7) % Self.noise.count] : c)
        }
        return out
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
    var text = "JOEBRO"
    var dot: CGFloat = 3
    var gap: CGFloat = 1.6
    var tint: Color = JB.ink
    var animated = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var start = Date()

    private static let glyphs: [Character: [String]] = [
        "J": ["..###", "....#", "....#", "....#", "#...#", "#...#", ".###."],
        "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
        "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
        "B": ["####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."],
        "R": ["####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"],
        "N": ["#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"],
        "G": [".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".###."],
        "H": ["#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
        "T": ["#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."],
        "I": [".###.", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."],
        "S": [".####", "#....", "#....", ".###.", "....#", "....#", "####."],
        "A": [".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
        "W": ["#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"],
        "K": ["#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"],
        " ": [".....", ".....", ".....", ".....", ".....", ".....", "....."],
    ]

    private var cells: [(x: Int, y: Int, on: Bool)] {
        var out: [(Int, Int, Bool)] = []
        for (ci, ch) in text.uppercased().enumerated() {
            let g = Self.glyphs[ch] ?? Self.glyphs[" "]!
            for (y, row) in g.enumerated() {
                for (x, c) in row.enumerated() { out.append((ci * 6 + x, y, c == "#")) }
            }
        }
        return out
    }

    var body: some View {
        let cols = text.count * 6 - 1
        let w = CGFloat(cols) * (dot + gap) - gap
        let h = 7 * (dot + gap) - gap
        let still = reduceMotion || !animated
        TimelineView(.animation(minimumInterval: 1 / 30, paused: still)) { tl in
            let t = still ? 99 : tl.date.timeIntervalSince(start)
            Canvas { ctx, _ in
                for c in cells {
                    let seed = Double((c.x &* 73 &+ c.y &* 151) % 97) / 97
                    let rect = CGRect(x: CGFloat(c.x) * (dot + gap), y: CGFloat(c.y) * (dot + gap), width: dot, height: dot)
                    let path = Path(roundedRect: rect, cornerRadius: dot * 0.28)
                    if c.on {
                        let appear = min(1, max(0, (t - seed * 0.6) / 0.18))
                        // A slow sweep brightens a column band every ~6s.
                        let sweep = (t.truncatingRemainder(dividingBy: 6.5)) * 14 - 8
                        let near = max(0, 1 - abs(Double(c.x) - sweep) / 3)
                        ctx.fill(path, with: .color(tint.opacity(appear * (0.86 + 0.14 * near))))
                    } else {
                        ctx.fill(path, with: .color(tint.opacity(0.06)))
                    }
                }
            }
            .frame(width: w, height: h)
        }
        .frame(width: w, height: h)
        .onAppear { start = Date() }
        .accessibilityLabel(text)
    }
}

// MARK: - Dot-matrix number

/// Small dot-matrix numerals for counters (agents running, unread, time).
struct JBDotNumber: View {
    let value: String
    var dot: CGFloat = 2
    var tint: Color = JB.ink
    private static let digits: [Character: [String]] = [
        "0": ["###", "#.#", "#.#", "#.#", "###"], "1": [".#.", "##.", ".#.", ".#.", "###"],
        "2": ["###", "..#", "###", "#..", "###"], "3": ["###", "..#", ".##", "..#", "###"],
        "4": ["#.#", "#.#", "###", "..#", "..#"], "5": ["###", "#..", "###", "..#", "###"],
        "6": ["###", "#..", "###", "#.#", "###"], "7": ["###", "..#", ".#.", ".#.", ".#."],
        "8": ["###", "#.#", "###", "#.#", "###"], "9": ["###", "#.#", "###", "..#", "###"],
        ":": ["...", ".#.", "...", ".#.", "..."], "+": ["...", ".#.", "###", ".#.", "..."],
    ]
    var body: some View {
        let chars = Array(value)
        let step = dot * 1.5
        Canvas { ctx, _ in
            for (i, ch) in chars.enumerated() {
                guard let g = Self.digits[ch] else { continue }
                for (y, row) in g.enumerated() {
                    for (x, c) in row.enumerated() where c == "#" {
                        let r = CGRect(x: CGFloat(i * 4 + x) * step, y: CGFloat(y) * step, width: dot, height: dot)
                        ctx.fill(Path(ellipseIn: r), with: .color(tint))
                    }
                }
            }
        }
        .frame(width: CGFloat(chars.count * 4 - 1) * step, height: 5 * step)
        .accessibilityLabel(value)
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
    var body: some View {
        VStack(spacing: 14) {
            Text(Self.drawing(art))
                .font(.jbMono(12))
                .foregroundStyle(JB.ink3)
                .multilineTextAlignment(.leading)
                .fixedSize()
            Text(text).font(.jb(13)).foregroundStyle(JB.ink2)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(24)
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
