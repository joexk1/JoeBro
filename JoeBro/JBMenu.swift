import SwiftUI
import AppKit

/// JoeBro's dropdown. A drop-in for SwiftUI `Menu`, which macOS 26 always draws
/// as Liquid Glass. Opens a borderless floating window styled like the slash
/// menu (panel fill that follows the opacity slider, hairline, soft shadow):
/// below the control, or above it when there isn't room. Closes on a pick, a
/// click outside, or esc. Buttons, Labels, Dividers, Toggles and nested
/// JBMenus all work as content.
struct JBMenu<Content: View, MenuLabel: View>: View {
    private let content: () -> Content
    private let label: () -> MenuLabel
    var openOn: Notification.Name? = nil      // e.g. /model opens the model picker
    var closesOnPick = true                   // false for panels with sliders (appearance)
    @State private var anchor = JBMenuAnchor()

    init(openOn: Notification.Name? = nil, closesOnPick: Bool = true, @ViewBuilder content: @escaping () -> Content, @ViewBuilder label: @escaping () -> MenuLabel) {
        self.openOn = openOn
        self.closesOnPick = closesOnPick
        self.content = content
        self.label = label
    }

    var body: some View {
        Button { anchor.closesOnPick = closesOnPick; anchor.toggle { panel } } label: { label().contentShape(Rectangle()) }
            .buttonStyle(.plain)
            .background(JBMenuAnchorView(anchor: anchor))
            .onReceive(NotificationCenter.default.publisher(for: openOn ?? Notification.Name("JoeBro.never"))) { _ in
                anchor.open(at: nil) { panel }
            }
    }

    private var panel: some View {
        VStack(alignment: .leading, spacing: 1) { content() }
            .buttonStyle(JBMenuRowStyle())
            .labelStyle(JBMenuLabelStyle())
            .toggleStyle(JBMenuToggleStyle())
            .font(.jb(13))
            .foregroundStyle(JB.ink)
            .padding(5)
            .frame(minWidth: 190, alignment: .leading)
            .fixedSize()
            .jbFloating()
    }
}

extension JBMenu where MenuLabel == JBMenuTextLabel {
    /// `JBMenu("Title") { … }` — a text row that opens a (sub)menu.
    init(_ title: String, @ViewBuilder content: @escaping () -> Content) {
        self.init(content: content) { JBMenuTextLabel(title: title) }
    }
}

struct JBMenuTextLabel: View {
    let title: String
    var body: some View {
        HStack {
            Text(title).font(.jb(13)).foregroundStyle(JB.ink)
            Spacer(minLength: 12)
            Image(systemName: "chevron.right").font(.jb(9, .semibold)).foregroundStyle(JB.ink3)
        }
        .padding(.horizontal, 8).frame(height: 26)
    }
}

/// A row in a JBMenu: hover fill, accent check via Label("…", systemImage: "checkmark").
struct JBMenuRowStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View { Row(configuration: configuration) }
    private struct Row: View {
        let configuration: ButtonStyleConfiguration
        @Environment(\.isEnabled) private var enabled
        @State private var hover = false
        var body: some View {
            configuration.label
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 8)
                .frame(minHeight: 26)
                .background(RoundedRectangle(cornerRadius: JB.rControl - 2, style: .continuous)
                    .fill(configuration.isPressed ? JB.pressed : hover && enabled ? JB.hover : .clear))
                .opacity(enabled ? 1 : 0.4)
                .contentShape(Rectangle())
                .onHover { hover = $0 }
        }
    }
}

struct JBMenuLabelStyle: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 8) {
            configuration.icon.font(.jb(12)).foregroundStyle(JB.ink2).frame(width: 16)
            configuration.title
        }
    }
}

struct JBMenuToggleStyle: ToggleStyle {
    func makeBody(configuration: Configuration) -> some View {
        Button { configuration.isOn.toggle() } label: {
            HStack(spacing: 8) {
                Image(systemName: "checkmark").font(.jb(11, .semibold)).foregroundStyle(JB.accent)
                    .opacity(configuration.isOn ? 1 : 0).frame(width: 16)
                configuration.label
            }
        }
    }
}

// MARK: - Floating window plumbing

@MainActor
final class JBMenuAnchor {
    weak var view: NSView?
    var closesOnPick = true
    private var panel: NSPanel?
    private var monitor: Any?
    private var globalMonitor: Any?
    /// Open menus, outermost first (nested menus stay open while you use them).
    private static var stack: [JBMenuAnchor] = []
    private static let margin: CGFloat = 28   // room for the shadow

    func toggle<V: View>(@ViewBuilder _ content: () -> V) {
        if panel != nil { close(); return }
        open(at: nil, content)
    }

    /// Opens the menu under the control, or at a screen point (right-click).
    func open<V: View>(at point: NSPoint?, @ViewBuilder _ content: () -> V) {
        if panel != nil { close() }
        guard let view, let window = view.window else { return }
        let close = { [weak self] in Self.closeAll(); _ = self }
        let picks = closesOnPick
        let root = content()
            .simultaneousGesture(TapGesture().onEnded { if picks { DispatchQueue.main.async { close() } } })
            .onExitCommand { close() }
            .padding(Self.margin)
        let host = NSHostingView(rootView: root)
        host.layoutSubtreeIfNeeded()
        let size = host.fittingSize
        let p = NSPanel(contentRect: NSRect(origin: .zero, size: size),
                        styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = false
        p.level = .popUpMenu
        p.appearance = window.effectiveAppearance
        p.contentView = host

        let r = point.map { NSRect(x: $0.x, y: $0.y, width: 0, height: 0) }
            ?? window.convertToScreen(view.convert(view.bounds, to: nil))
        let screen = (window.screen ?? NSScreen.main)?.visibleFrame ?? .infinite
        let m = Self.margin
        let nested = window is NSPanel && Self.stack.contains { $0.panel === window }
        var origin: NSPoint
        if nested {   // submenu: to the right of its row
            origin = NSPoint(x: r.maxX - 4 - m, y: r.maxY + 5 + m - size.height)
        } else {
            origin = NSPoint(x: r.minX - m, y: r.minY - 6 + m - size.height)
            if origin.y + m < screen.minY { origin.y = r.maxY + 6 - m }   // no room below: open upward
        }
        origin.x = min(max(origin.x, screen.minX - m), screen.maxX - size.width + m)
        p.setFrameOrigin(origin)
        window.addChildWindow(p, ordered: .above)
        p.orderFront(nil)
        panel = p
        Self.stack.append(self)

        monitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown, .keyDown]) { [weak self] e in
            guard let self, Self.stack.last === self else { return e }
            if e.type == .keyDown {
                if e.keyCode == 53 { self.close(); return nil }
                return e
            }
            if let w = e.window, Self.stack.contains(where: { $0.panel === w }) {
                // Clicked inside an outer menu: close the ones above it.
                while let top = Self.stack.last, top.panel !== w { top.close() }
                return e
            }
            if point == nil, e.window === self.view?.window, let v = self.view,
               v.bounds.contains(v.convert(e.locationInWindow, from: nil)) {
                // Clicking the control again: let its button toggle us closed.
                return e
            }
            Self.closeAll()
            return e
        }
        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { _ in
            Task { @MainActor in Self.closeAll() }
        }
    }

    func close() {
        if let monitor { NSEvent.removeMonitor(monitor) }
        if let globalMonitor { NSEvent.removeMonitor(globalMonitor) }
        monitor = nil; globalMonitor = nil
        if let panel {
            panel.parent?.removeChildWindow(panel)
            panel.orderOut(nil)
        }
        panel = nil
        Self.stack.removeAll { $0 === self }
    }

    static func closeAll() {
        while let top = stack.last { top.close() }
    }
}

private struct JBMenuAnchorView: NSViewRepresentable {
    let anchor: JBMenuAnchor
    func makeNSView(context: Context) -> NSView {
        let v = NSView()
        anchor.view = v
        return v
    }
    func updateNSView(_ nsView: NSView, context: Context) { anchor.view = nsView }
    static func dismantleNSView(_ nsView: NSView, coordinator: ()) {}
}

/// "Are you sure?" before anything is deleted for good.
@MainActor
enum JBConfirm {
    static func delete(_ what: String, _ name: String) -> Bool {
        let a = NSAlert()
        a.messageText = name.isEmpty ? "Delete this \(what)?" : "Delete \(what) \u{201C}\(name)\u{201D}?"
        a.informativeText = "This can't be undone."
        a.alertStyle = .warning
        a.addButton(withTitle: "Delete").hasDestructiveAction = true
        a.addButton(withTitle: "Cancel").keyEquivalent = "\u{1b}"
        return a.runModal() == .alertFirstButtonReturn
    }
}


// MARK: - Right-click menus

extension View {
    /// JoeBro's right-click menu: the same dropdown as JBMenu, opened at the cursor
    /// (SwiftUI's .contextMenu is a system menu drawn in Liquid Glass).
    func jbContextMenu<C: View>(@ViewBuilder _ content: @escaping () -> C) -> some View {
        modifier(JBContextMenuModifier(content: content))
    }
}

private struct JBContextMenuModifier<C: View>: ViewModifier {
    let content: () -> C
    @State private var anchor = JBMenuAnchor()
    @State private var monitor = JBRightClickMonitor()

    func body(content view: Content) -> some View {
        view
            .background(JBMenuAnchorView(anchor: anchor))
            .onAppear {
                monitor.start(anchor: anchor) {
                    VStack(alignment: .leading, spacing: 1) { content() }
                        .buttonStyle(JBMenuRowStyle())
                        .labelStyle(JBMenuLabelStyle())
                        .toggleStyle(JBMenuToggleStyle())
                        .font(.jb(13))
                        .foregroundStyle(JB.ink)
                        .padding(5)
                        .frame(minWidth: 190, alignment: .leading)
                        .fixedSize()
                        .jbFloating()
                }
            }
            .onDisappear { monitor.stop() }
    }
}

@MainActor
private final class JBRightClickMonitor {
    private var token: Any?
    func start<V: View>(anchor: JBMenuAnchor, content: @escaping () -> V) {
        stop()
        token = NSEvent.addLocalMonitorForEvents(matching: .rightMouseDown) { [weak anchor] e in
            guard let anchor, let v = anchor.view, let w = v.window, e.window === w,
                  !v.isHiddenOrHasHiddenAncestor else { return e }
            let p = v.convert(e.locationInWindow, from: nil)
            guard v.visibleRect.contains(p) else { return e }
            // Innermost row wins when rows are nested: skip if a smaller registered view also contains the click.
            if JBRightClickMonitor.claimed == e.timestamp { return nil }
            JBRightClickMonitor.claimed = e.timestamp
            let screen = w.convertPoint(toScreen: e.locationInWindow)
            anchor.open(at: screen, content)
            return nil
        }
    }
    func stop() { if let token { NSEvent.removeMonitor(token) }; token = nil }
    static var claimed: TimeInterval = 0
}
