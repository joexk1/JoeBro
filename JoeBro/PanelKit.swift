import SwiftUI
import WebKit

/// Shared chrome for workspace panels: glass header capsule + content area.
struct PanelChrome<Actions: View, Content: View>: View {
    let title: String
    let icon: String
    @ViewBuilder var actions: Actions
    @ViewBuilder var content: Content
    // Rebuild the header (icon + glass) when the system flips light/dark, so the
    // top-bar SF icon and glass don't keep the appearance they launched in.
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        VStack(spacing: 12) {
            HStack(spacing: 10) {
                Image(systemName: icon)
                    .foregroundStyle(JB.accent)
                Text(title)
                    .font(.jb(14, .semibold))
                Spacer()
                actions
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .joeGlassCapsule()
            .id(colorScheme)

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
                .font(.jb(13))
                .onSubmit(fire)
                .disabled(busy)
            if busy {
                ProgressView().controlSize(.small)
            } else if let errorText {
                Text(errorText).font(.jbCaption).foregroundStyle(.red).lineLimit(1)
            } else {
                Button(action: fire) {
                    Image(systemName: actionIcon).foregroundStyle(JB.accent)
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

/// Panel-header search: a magnifying-glass button that expands into an inline
/// search field in place, and collapses + clears on the x. Shared by Skills,
/// Brain, and Tasks so the affordance is identical everywhere.
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
                    .foregroundStyle(.secondary)
                TextField(placeholder, text: $text)
                    .textFieldStyle(.plain)
                    .font(.jb(12))
                    .frame(width: 150)
                    .focused($focused)
                Button {
                    withAnimation(.spring(duration: 0.25)) { text = ""; open = false }
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.jb(11))
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.borderless)
            } else {
                Button {
                    withAnimation(.spring(duration: 0.25)) { open = true }
                } label: {
                    Image(systemName: "magnifyingglass")
                }
                .buttonStyle(.borderless)
                .help("Search")
            }
        }
        .padding(.horizontal, open ? 10 : 0)
        .padding(.vertical, open ? 5 : 0)
        .background(open ? AnyShapeStyle(.quaternary.opacity(0.5)) : AnyShapeStyle(.clear), in: Capsule())
        .onChange(of: open) { _, isOpen in if isOpen { focused = true } }
    }
}


/// The "N selected · All/None · 🗑 · ✕" strip every list panel shows once you
/// tick a row. Was copy-pasted into four panels; only `onDelete` ever differed.
struct SelectionBar: View {
    let total: Int
    let allIDs: [String]
    @Binding var selection: Set<String>
    let onDelete: ([String]) -> Void

    var body: some View {
        HStack(spacing: 10) {
            Text("\(selection.count) selected")
                .font(.jb(11, .semibold))
            Button(selection.count == total ? "None" : "All") {
                withAnimation(.spring(duration: 0.25)) {
                    selection = selection.count == total ? [] : Set(allIDs)
                }
            }
            .buttonStyle(.borderless)
            .font(.jb(11))
            Spacer()
            Button {
                let ids = selection
                selection = []          // clear first: the rows are about to go
                onDelete(Array(ids))
            } label: {
                Image(systemName: "trash").font(.jb(12))
            }
            .buttonStyle(.borderless)
            .help("Delete selected")
            Button {
                withAnimation(.spring(duration: 0.25)) { selection = [] }
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .font(.jb(12))
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.borderless)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(JB.accent.opacity(0.10))
        .transition(.move(edge: .top).combined(with: .opacity))
    }
}

/// Tick/untick one row in a panel's selection set.
func toggleSelection(_ id: String, in selection: inout Set<String>) {
    withAnimation(.spring(duration: 0.2)) {
        if selection.contains(id) { selection.remove(id) } else { selection.insert(id) }
    }
}
