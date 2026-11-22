import SwiftUI
import WebKit

/// Renders an SF symbol name, swapping Apple-identifiable ones for JoeBro glyphs.
struct JBSymbol: View {
    let name: String
    var size: CGFloat = 14
    private static let glyphs: [String: JBGlyph] = [
        "sparkles": .spark, "wand.and.stars": .spark, "sparkle": .spark,
        "brain": .memory, "brain.head.profile": .memory,
        "moon.zzz": .nightShift, "moon.stars": .nightShift,
        "graduationcap": .skills, "hammer": .tools, "wrench.and.screwdriver": .tools,
        "terminal": .coding, "puzzlepiece": .plugin, "puzzlepiece.extension": .plugin,
        "checklist": .tasks, "clock.badge.checkmark": .tasks,
        "person.3.sequence": .agent, "person.crop.circle": .agent,
        "antenna.radiowaves.left.and.right": .research, "sparkle.magnifyingglass": .research,
    ]
    var body: some View {
        if let g = Self.glyphs[name] {
            JBIcon(g, size: size)
        } else {
            Image(systemName: name).font(.jb(size - 1))
        }
    }
}

/// Shared chrome for workspace panels: flat header (icon, title, actions), a hairline, then content.
struct PanelChrome<Actions: View, Content: View>: View {
    let title: String
    let icon: String
    @ViewBuilder var actions: Actions
    @ViewBuilder var content: Content

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                JBSymbol(name: icon, size: 15)
                    .foregroundStyle(JB.ink2)
                Text(title)
                    .font(.jb(15, .semibold))
                    .tracking(-0.2)
                    .foregroundStyle(JB.ink)
                Spacer(minLength: 8)
                actions.tint(JB.ink2)
            }
            .padding(.horizontal, 16)
            .frame(height: 44)
            Rectangle().fill(JB.line).frame(height: 1)
            content
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .jbSurface(.panel, radius: JB.rPanel)
    }
}

private func jbHex(_ c: Color) -> String {
    let n = NSColor(c).usingColorSpace(.sRGB) ?? .gray
    return String(format: "#%02X%02X%02X", Int(n.redComponent * 255), Int(n.greenComponent * 255), Int(n.blueComponent * 255))
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
          :root { color-scheme: light dark; --ink:#151515; --ink2:#66655F; --line:#E3E1DC; }
          @media (prefers-color-scheme: dark) { :root { --ink:#EDEDEC; --ink2:#9A9A9F; --line:#252528; } }
          body { font-family: -apple-system, "Helvetica Neue", sans-serif; font-size: 14px; line-height: 1.5;
                 color: var(--ink); margin: 12px; word-wrap: break-word; }
          a { color: \(jbHex(JB.accent)); }
          blockquote { border-left: 2px solid var(--line); margin-left: 0; padding-left: 12px; color: var(--ink2); }
          img { max-width: 100%; height: auto; }
          pre, code { font-family: ui-monospace, Menlo, monospace; font-size: 12.5px; }
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
            .jbSurface(.raised, radius: JB.rCard)
    }
}

// Built once per email row and per memory card otherwise.
private let relativeFmt: RelativeDateTimeFormatter = {
    let f = RelativeDateTimeFormatter()
    f.unitsStyle = .abbreviated
    return f
}()

func relativeDate(_ iso: String?) -> String {
    guard let iso, let d = parseISO(iso) else { return "" }
    return relativeFmt.localizedString(for: d, relativeTo: Date())
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
            .onHover { h in
                withAnimation(JB.fade) { hovering = h }
            }
    }
}

extension View {
    func hoverBounce(_ scale: CGFloat = 1.02) -> some View {
        modifier(HoverBounce(scale: scale))
    }
}


// MARK: - Surfaces

/// The environment handle older views read. All state lives in JBAppearance
/// (shared with iOS): palette, panel opacity, backdrop.
@Observable
final class GlassSettings {
    static let shared = GlassSettings()
    var solidity: Double {
        get { JBAppearance.shared.solidity }
        set { JBAppearance.shared.solidity = newValue }
    }
    var backgroundPreset: String {
        get { JBAppearance.shared.palette.rawValue }
        set { JBAppearance.shared.palette = JBPalette(legacy: newValue) }
    }
}

extension View {
    func joeGlassRect(_ cornerRadius: CGFloat) -> some View { jbSurface(.panel, radius: cornerRadius) }
    func joeGlassRectInteractive(_ cornerRadius: CGFloat) -> some View { jbSurface(.panel, radius: cornerRadius) }
    func joeGlassCapsule() -> some View { jbSurface(.chrome, radius: nil) }
    func joeGlassCapsuleInteractive() -> some View { jbSurface(.chrome, radius: nil) }
    func joeCard(_ cornerRadius: CGFloat) -> some View { jbSurface(.raised, radius: cornerRadius) }
}

/// In-window appearance tuner: palette + panel opacity, live over the backdrop.
struct GlassTunerPopover: View {
    @Bindable private var look = JBAppearance.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Palette").jbEyebrow()
            JBPaletteRow(selection: $look.palette)
            Text("Opacity").jbEyebrow()
            Slider(value: $look.solidity, in: 0.35...1)
                .frame(width: 208)
                .tint(JB.accent)
        }
        .padding(14)
    }
}

/// Swatch row for the shared palette list.
struct JBPaletteRow: View {
    @Binding var selection: JBPalette
    var body: some View {
        HStack(spacing: 8) {
            ForEach(JBPalette.allCases) { p in
                Button {
                    withAnimation(JB.snap) { selection = p }
                } label: {
                    Circle()
                        .fill(p == .mono ? JB.ink : p.accent)
                        .frame(width: 18, height: 18)
                        .overlay(Circle().strokeBorder(JB.line, lineWidth: 1))
                        .padding(3)
                        .overlay(Circle().strokeBorder(selection == p ? JB.ink : .clear, lineWidth: 1.5))
                }
                .buttonStyle(JBPressStyle())
                .help(p.label)
                .accessibilityLabel(p.label)
            }
        }
    }
}

/// A "describe a new X…" input that owns its OWN text state, so typing
/// re-renders only this bar and not the parent panel's list.
struct QuickAddBar: View {
    let placeholder: String
    var actionIcon = "sparkles"
    var busy = false
    var errorText: String? = nil
    let onSubmit: (String) -> Void
    @State private var text = ""
    @FocusState private var focused: Bool

    private var ready: Bool { !text.trimmingCharacters(in: .whitespaces).isEmpty }

    var body: some View {
        HStack(spacing: 8) {
            TextField(placeholder, text: $text)
                .textFieldStyle(.plain)
                .font(.jb(13))
                .focused($focused)
                .onSubmit(fire)
                .disabled(busy)
            if busy {
                JBSpinner()
            } else if let errorText {
                Text(errorText).font(.jbCaption).foregroundStyle(JB.danger).lineLimit(1)
            } else {
                if focused && ready { JBKbd("↩") }
                Button(action: fire) {
                    JBSymbol(name: actionIcon, size: 14)
                }
                .buttonStyle(JBIconButtonStyle(size: 24, active: ready))
                .disabled(!ready)
            }
        }
        .padding(.leading, 12)
        .padding(.trailing, 6)
        .padding(.vertical, 5)
        .background(RoundedRectangle(cornerRadius: JB.rControl, style: .continuous).fill(JB.raised))
        .overlay(RoundedRectangle(cornerRadius: JB.rControl, style: .continuous)
            .strokeBorder(focused ? JB.accent.opacity(0.7) : JB.line, lineWidth: 1))
        .animation(JB.fade, value: focused)
    }

    private func fire() {
        let t = text.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty, !busy else { return }
        onSubmit(t)
        text = ""
    }
}

/// Panel-header search: a magnifier button that expands into an inline field
/// in place, and collapses + clears on the x.
struct PanelSearchField: View {
    @Binding var text: String
    @Binding var open: Bool
    var placeholder = "Search"
    @FocusState private var focused: Bool

    var body: some View {
        HStack(spacing: 6) {
            if open {
                Image(systemName: "magnifyingglass")
                    .font(.jb(11))
                    .foregroundStyle(JB.ink3)
                TextField(placeholder, text: $text)
                    .textFieldStyle(.plain)
                    .font(.jb(12))
                    .frame(width: 150)
                    .focused($focused)
                    .onExitCommand { withAnimation(JB.snap) { text = ""; open = false } }
                Button {
                    withAnimation(JB.snap) { text = ""; open = false }
                } label: {
                    Image(systemName: "xmark")
                        .font(.jb(10, .medium))
                        .foregroundStyle(JB.ink3)
                }
                .buttonStyle(.plain)
            } else {
                Button {
                    withAnimation(JB.snap) { open = true }
                } label: {
                    Image(systemName: "magnifyingglass")
                }
                .buttonStyle(JBIconButtonStyle())
                .help("Search")
            }
        }
        .padding(.horizontal, open ? 10 : 0)
        .padding(.vertical, open ? 5 : 0)
        .background(open ? AnyShapeStyle(JB.raised) : AnyShapeStyle(.clear), in: RoundedRectangle(cornerRadius: JB.rControl, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: JB.rControl, style: .continuous)
            .strokeBorder(open ? (focused ? JB.accent.opacity(0.7) : JB.line) : .clear, lineWidth: 1))
        .onChange(of: open) { _, isOpen in if isOpen { focused = true } }
    }
}

/// The "N selected · All/None · delete · close" strip every list panel shows once you tick a row.
struct SelectionBar: View {
    let total: Int
    let allIDs: [String]
    @Binding var selection: Set<String>
    let onDelete: ([String]) -> Void

    var body: some View {
        HStack(spacing: 10) {
            Text("\(selection.count) selected")
                .font(.jbMono(11, .medium))
                .foregroundStyle(JB.ink)
            Button(selection.count == total ? "None" : "All") {
                withAnimation(JB.snap) {
                    selection = selection.count == total ? [] : Set(allIDs)
                }
            }
            .buttonStyle(.jbGhost)
            Spacer()
            Button {
                let ids = selection
                selection = []          // clear first: the rows are about to go
                onDelete(Array(ids))
            } label: {
                Image(systemName: "trash")
            }
            .buttonStyle(JBIconButtonStyle())
            .help("Delete selected")
            Button {
                withAnimation(JB.snap) { selection = [] }
            } label: {
                Image(systemName: "xmark")
            }
            .buttonStyle(JBIconButtonStyle())
            .help("Clear selection")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 5)
        .background(JB.accentSoft)
        .overlay(alignment: .bottom) { Rectangle().fill(JB.line).frame(height: 1) }
        .transition(.move(edge: .top).combined(with: .opacity))
    }
}

/// Tick/untick one row in a panel's selection set.
func toggleSelection(_ id: String, in selection: inout Set<String>) {
    withAnimation(JB.snap) {
        if selection.contains(id) { selection.remove(id) } else { selection.insert(id) }
    }
}
