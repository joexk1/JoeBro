import SwiftUI

/// JoeBro's launch moment: a sphere of dots (a Fibonacci lattice) that breathes in
/// faint and small, swells wide and sparse, settles dense and bright, takes the
/// accent, and lifts away to reveal the app. Always on black (it follows the black
/// launch screen), in light and dark. Tap to skip. Shared verbatim by the Mac and
/// iOS apps; skipped under Reduce Motion.
struct JBIntro: View {
    var onFinish: () -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var start: Date?          // set on first appearance, so a slow launch never eats the animation
    @State private var done = false

    static let duration: Double = 3.0
    private static let points: [SIMD3<Double>] = {
        let n = 900, golden = Double.pi * (3 - sqrt(5))
        return (0..<n).map { i in
            let y = 1 - (Double(i) + 0.5) / Double(n) * 2
            let r = (1 - y * y).squareRoot(), th = golden * Double(i)
            return SIMD3(cos(th) * r, y, sin(th) * r)
        }
    }()

    var body: some View {
        GeometryReader { geo in
            let m = min(geo.size.width, geo.size.height)
            TimelineView(.animation(paused: done)) { tl in
                let t = start.map { tl.date.timeIntervalSince($0) } ?? 0
                ZStack {
                    Color.black
                        .opacity(1 - Self.ease(max(0, (t - 2.45) / 0.55)))
                    Canvas { ctx, size in draw(&ctx, size, t) }
                    Text("JoeBro")
                        .font(.jb(30, .semibold)).tracking(-0.6)
                        .foregroundStyle(.white)
                        .opacity(Self.window(t, 1.75, 2.15, 2.5, 2.85))
                        .offset(y: m * 0.30 + 30)      // just under the settled sphere
                }
                .onChange(of: t >= Self.duration) { _, over in if over { finish() } }
            }
        }
        .ignoresSafeArea()
        .contentShape(Rectangle())
        .onTapGesture { finish() }
        .onAppear { if reduceMotion { finish() } else if start == nil { start = Date() } }
        .allowsHitTesting(!done)
    }

    private func finish() {
        guard !done else { return }
        done = true
        onFinish()
    }

    private func draw(_ ctx: inout GraphicsContext, _ size: CGSize, _ t: Double) {
        let m = min(size.width, size.height)
        // Radius: faint seed → wide breath → dense settle → lift away.
        let k: Double
        switch t {
        case ..<0.55: k = 0.16 + 0.22 * Self.ease(t / 0.55)
        case ..<1.15: k = 0.38 + 0.12 * Self.ease((t - 0.55) / 0.6)
        case ..<1.75: k = 0.50 - 0.20 * Self.easeBack((t - 1.15) / 0.6)
        case ..<2.45: k = 0.30 - 0.015 * sin((t - 1.75) / 0.7 * .pi)
        default:      k = 0.30 + 0.9 * Self.easeIn((t - 2.45) / 0.55)
        }
        let radius = m * k
        let fadeIn = Self.ease(min(1, t / 0.6))
        let fadeOut = 1 - Self.ease(max(0, (t - 2.5) / 0.5))
        // Sparse when wide, dense when settled: dot size follows how packed the shell is.
        let dot = max(1.1, m * 0.0042 * (0.62 / max(0.3, k)).squareRoot())
        let spin = t * 0.9, tilt = 0.38
        let (cs, ss, ct, st) = (cos(spin), sin(spin), cos(tilt), sin(tilt))
        let centre = CGPoint(x: size.width / 2, y: size.height / 2 - (t > 1.75 ? m * 0.035 * Self.ease(min(1, (t - 1.75) / 0.4)) : 0))
        let accentMix = Self.ease(max(0, min(1, (t - 1.6) / 0.6)))
        let base = Color.white
        for p in Self.points {
            let x1 = p.x * cs + p.z * ss, z1 = -p.x * ss + p.z * cs
            let y2 = p.y * ct - z1 * st, z2 = p.y * st + z1 * ct
            let depth = (z2 + 1) / 2                     // 0 back … 1 front
            let a = (0.18 + 0.82 * depth) * fadeIn * fadeOut
            guard a > 0.02 else { continue }
            let s = dot * (0.55 + 0.6 * depth)
            let pt = CGPoint(x: centre.x + x1 * radius, y: centre.y + y2 * radius)
            let rect = CGRect(x: pt.x - s / 2, y: pt.y - s / 2, width: s, height: s)
            // Front of the sphere takes the accent; the rest stays white, so it never dims.
            let tint = depth > 0.6 && accentMix > 0.5 ? JB.accent.opacity(min(1, a * 1.15)) : base.opacity(a)
            ctx.fill(Path(ellipseIn: rect), with: .color(tint))
        }
    }

    private static func ease(_ x: Double) -> Double { let c = min(1, max(0, x)); return c * c * (3 - 2 * c) }
    private static func easeIn(_ x: Double) -> Double { let c = min(1, max(0, x)); return c * c * c }
    private static func easeBack(_ x: Double) -> Double {
        let c = min(1, max(0, x)), s = 1.4
        return 1 + (s + 1) * pow(c - 1, 3) + s * pow(c - 1, 2)
    }
    /// 0 → 1 between a and b, holds, 1 → 0 between c and d.
    private static func window(_ t: Double, _ a: Double, _ b: Double, _ c: Double, _ d: Double) -> Double {
        t < b ? ease((t - a) / (b - a)) : 1 - ease((t - c) / (d - c))
    }
}
