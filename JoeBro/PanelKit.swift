import SwiftUI
import WebKit

/// Shared chrome for workspace panels: glass header capsule + content area.
struct PanelChrome<Actions: View, Content: View>: View {
    let title: String
    let icon: String
    @ViewBuilder var actions: Actions
    @ViewBuilder var content: Content

    var body: some View {
        VStack(spacing: 12) {
            HStack(spacing: 10) {
                Image(systemName: icon)
                    .foregroundStyle(Color.accentColor)
                Text(title)
                    .font(.system(size: 14, weight: .semibold, design: .rounded))
                Spacer()
                actions
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .joeGlassCapsule()

            content
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

/// Renders an HTML email body using the system web engine.
struct HTMLView: NSViewRepresentable {
    let html: String

    func makeNSView(context: Context) -> WKWebView {
        let cfg = WKWebViewConfiguration()
        let view = WKWebView(frame: .zero, configuration: cfg)
        view.setValue(false, forKey: "drawsBackground")
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {
        let wrapped = """
        <html><head><meta charset="utf-8">
        <style>
          :root { color-scheme: light dark; }
          body { font-family: -apple-system, sans-serif; font-size: 14px;
                 margin: 12px; word-wrap: break-word; }
          img { max-width: 100%; height: auto; }
          pre { white-space: pre-wrap; }
        </style></head><body>\(html)</body></html>
        """
        view.loadHTMLString(wrapped, baseURL: nil)
    }
}

/// Async-loading panel state wrapper.
enum Loadable<T> {
    case loading
    case loaded(T)
    case failed(String)
}

extension View {
    /// Standard glass card used across panels.
    func panelCard() -> some View {
        self
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .joeGlassRect(14)
    }
}

func relativeDate(_ iso: String?) -> String {
    guard let iso, let d = parseISO(iso) else { return "" }
    let fmt = RelativeDateTimeFormatter()
    fmt.unitsStyle = .abbreviated
    return fmt.localizedString(for: d, relativeTo: Date())
}


/// The signature hover feel: soft spring scale + optional highlight.
struct HoverBounce: ViewModifier {
    var scale: CGFloat = 1.02
    var highlightRadius: CGFloat? = nil
    @State private var hovering = false

    func body(content: Content) -> some View {
        content
            .background(
                Group {
                    if let r = highlightRadius {
                        RoundedRectangle(cornerRadius: r)
                            .fill(hovering ? AnyShapeStyle(.quaternary.opacity(0.5)) : AnyShapeStyle(.clear))
                    }
                }
            )
            .scaleEffect(hovering ? scale : 1)
            .onHover { h in
                withAnimation(.spring(duration: 0.22)) { hovering = h }
            }
    }
}

extension View {
    func hoverBounce(_ scale: CGFloat = 1.02) -> some View {
        modifier(HoverBounce(scale: scale))
    }

    /// Bounce + hover highlight behind the row.
    func hoverBounceHighlight(_ cornerRadius: CGFloat = 9, scale: CGFloat = 1.02) -> some View {
        modifier(HoverBounce(scale: scale, highlightRadius: cornerRadius))
    }
}


// MARK: - Adjustable glass

/// User-tunable panel clarity. 0 = clearest glass, 0.5 = the stock look
/// (default), 1 = nearly solid. One global scale, fixed surface ratios.
@Observable
final class GlassSettings {
    static let shared = GlassSettings()
    var solidity: Double = UserDefaults.standard.object(forKey: "glassSolidity2") == nil
        ? 0.5 : UserDefaults.standard.double(forKey: "glassSolidity2") {
        didSet { UserDefaults.standard.set(solidity, forKey: "glassSolidity2") }
    }
    /// Backdrop when no wallpaper is set — see BackgroundPreset.
    var backgroundPreset: String = UserDefaults.standard.string(forKey: "bgPreset") ?? "purple" {
        didSet { UserDefaults.standard.set(backgroundPreset, forKey: "bgPreset") }
    }
}

private struct JoeGlass: ViewModifier {
    var cornerRadius: CGFloat?    // nil = capsule
    var interactive = false
    var backingRatio = 0.85       // chrome panels take the full effect
    @Environment(GlassSettings.self) private var settings

    func body(content: Content) -> some View {
        let s = settings.solidity
        // The glass itself lives in a background layer so it can FADE:
        // left of centre the frost dissolves toward bare wallpaper;
        // right of centre a solid backing grows underneath it.
        let glassOpacity = s < 0.5 ? max(0.1, s * 2) : 1.0
        let backing = Color(nsColor: .windowBackgroundColor)
            .opacity(s > 0.5 ? (s - 0.5) * 2 * backingRatio : 0)
        let g: Glass = interactive ? Glass.regular.interactive() : .regular
        return content.background {
            if let r = cornerRadius {
                ZStack {
                    RoundedRectangle(cornerRadius: r).fill(backing)
                    Color.clear
                        .glassEffect(g, in: .rect(cornerRadius: r))
                        .opacity(glassOpacity)
                    // Control-Center depth: a soft specular sheen down from the
                    // top edge, and a faint darkening toward the bottom — this is
                    // what gives that glass its rich, lit-from-above look.
                    RoundedRectangle(cornerRadius: r)
                        .fill(LinearGradient(colors: [.white.opacity(0.18), .white.opacity(0.02), .clear],
                                             startPoint: .top, endPoint: .center))
                        .blendMode(.plusLighter)
                        .allowsHitTesting(false)
                    RoundedRectangle(cornerRadius: r)
                        .fill(LinearGradient(colors: [.clear, .black.opacity(0.10)],
                                             startPoint: .center, endPoint: .bottom))
                        .allowsHitTesting(false)
                    RoundedRectangle(cornerRadius: r)
                        .strokeBorder(.white.opacity(0.28 - min(s, 1) * 0.10), lineWidth: 0.9)
                        .blendMode(.plusLighter)
                    RoundedRectangle(cornerRadius: r)
                        .strokeBorder(.black.opacity(0.18), lineWidth: 1)
                        .mask(
                            RoundedRectangle(cornerRadius: r)
                                .fill(LinearGradient(colors: [.clear, .black.opacity(0.9)],
                                                     startPoint: .top, endPoint: .bottom))
                        )
                    RoundedRectangle(cornerRadius: max(0, r - 1))
                        .strokeBorder(.white.opacity(0.14), lineWidth: 0.7)
                        .padding(1)
                        .mask(
                            RoundedRectangle(cornerRadius: r)
                                .fill(LinearGradient(colors: [.white, .clear],
                                                     startPoint: .topLeading, endPoint: .bottomTrailing))
                        )
                }
            } else {
                ZStack {
                    Capsule().fill(backing)
                    Color.clear
                        .glassEffect(g, in: .capsule)
                        .opacity(glassOpacity)
                    Capsule()
                        .strokeBorder(.white.opacity(0.28 - min(s, 1) * 0.10), lineWidth: 0.9)
                        .blendMode(.plusLighter)
                    Capsule()
                        .strokeBorder(.black.opacity(0.18), lineWidth: 1)
                        .mask(
                            Capsule()
                                .fill(LinearGradient(colors: [.clear, .black.opacity(0.9)],
                                                     startPoint: .top, endPoint: .bottom))
                        )
                    Capsule()
                        .strokeBorder(.white.opacity(0.14), lineWidth: 0.7)
                        .padding(1)
                        .mask(
                            Capsule()
                                .fill(LinearGradient(colors: [.white, .clear],
                                                     startPoint: .topLeading, endPoint: .bottomTrailing))
                        )
                }
            }
        }
    }
}

extension View {
    func joeGlassRect(_ cornerRadius: CGFloat) -> some View {
        modifier(JoeGlass(cornerRadius: cornerRadius))
    }
    func joeGlassRectInteractive(_ cornerRadius: CGFloat) -> some View {
        modifier(JoeGlass(cornerRadius: cornerRadius, interactive: true))
    }
    func joeGlassCapsule() -> some View {
        modifier(JoeGlass(cornerRadius: nil))
    }
    func joeGlassCapsuleInteractive() -> some View {
        modifier(JoeGlass(cornerRadius: nil, interactive: true))
    }
    /// Content cards (bubbles etc) scale at a fraction of the chrome
    /// effect — preserves the chrome/content contrast ratio.
    func joeCardBacking(_ cornerRadius: CGFloat) -> some View {
        modifier(JoeCardBacking(cornerRadius: cornerRadius))
    }
}


/// Content-card surface (bubbles, tool rows): the material
/// fades toward bare on the clear side and solidifies on the right —
/// tracking the chrome at the same ratio.
private struct JoeCard: ViewModifier {
    var cornerRadius: CGFloat
    @Environment(GlassSettings.self) private var settings

    func body(content: Content) -> some View {
        let s = settings.solidity
        let materialOpacity = s < 0.5 ? max(0.12, s * 2) : 1.0
        let backing = max(0, s - 0.5) * 2 * 0.5
        return content.background {
            ZStack {
                RoundedRectangle(cornerRadius: cornerRadius)
                    .fill(Color(nsColor: .windowBackgroundColor).opacity(backing))
                RoundedRectangle(cornerRadius: cornerRadius)
                    .fill(.ultraThinMaterial)
                    .opacity(materialOpacity)
                RoundedRectangle(cornerRadius: cornerRadius)
                    .strokeBorder(.white.opacity(0.18), lineWidth: 0.7)
                    .blendMode(.plusLighter)
                RoundedRectangle(cornerRadius: cornerRadius)
                    .strokeBorder(.black.opacity(0.12), lineWidth: 0.8)
                    .mask(
                        RoundedRectangle(cornerRadius: cornerRadius)
                            .fill(LinearGradient(colors: [.clear, .black.opacity(0.8)],
                                                 startPoint: .top, endPoint: .bottom))
                    )
            }
        }
    }
}

extension View {
    func joeCard(_ cornerRadius: CGFloat) -> some View {
        modifier(JoeCard(cornerRadius: cornerRadius))
    }
}

private struct JoeCardBacking: ViewModifier {
    var cornerRadius: CGFloat
    @Environment(GlassSettings.self) private var settings

    func body(content: Content) -> some View {
        content.background(
            Color(nsColor: .windowBackgroundColor)
                .opacity(max(0, settings.solidity - 0.5) * 2 * 0.5),
            in: RoundedRectangle(cornerRadius: cornerRadius))
    }
}

/// In-window glass tuner — lives in the main window because macOS renders
/// glass differently while the window is inactive (i.e. whenever the
/// Settings window is focused), making remote tuning misleading.
struct GlassTunerPopover: View {
    @Environment(GlassSettings.self) private var settings

    var body: some View {
        @Bindable var settings = settings
        VStack(alignment: .leading, spacing: 10) {
            Text("Glass")
                .font(.system(size: 12, weight: .semibold))
            HStack(spacing: 8) {
                Image(systemName: "circle.dotted")
                    .foregroundStyle(.secondary)
                Slider(value: $settings.solidity, in: 0...1)
                    .frame(width: 180)
                Image(systemName: "circle.fill")
                    .foregroundStyle(.secondary)
            }
            HStack {
                Spacer()
                Button("Restore Default") {
                    withAnimation(.spring(duration: 0.3)) { settings.solidity = 0.5 }
                }
                .font(.system(size: 11))
                .disabled(abs(settings.solidity - 0.5) < 0.01)
            }
        }
        .padding(14)
    }
}

/// A "describe a new X…" input capsule that owns its OWN text state, so typing
/// re-renders only this little bar — not the parent panel and its (glassy) list,
/// which was causing keystroke lag in Tasks / Skills / Calendar / Brain.
struct QuickAddBar: View {
    let placeholder: String
    var actionIcon = "sparkles"
    var busy = false
    var errorText: String? = nil
    let onSubmit: (String) -> Void
    @State private var text = ""

    var body: some View {
        HStack(spacing: 8) {
            TextField(placeholder, text: $text)
                .textFieldStyle(.plain)
                .font(.system(size: 13))
                .onSubmit(fire)
                .disabled(busy)
            if busy {
                ProgressView().controlSize(.small)
            } else if let errorText {
                Text(errorText).font(.caption).foregroundStyle(.red).lineLimit(1)
            } else {
                Button(action: fire) {
                    Image(systemName: actionIcon).foregroundStyle(Color.accentColor)
                }
                .buttonStyle(.borderless)
                .disabled(text.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .joeGlassCapsuleInteractive()
    }

    private func fire() {
        let t = text.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty, !busy else { return }
        onSubmit(t)
        text = ""
    }
}
