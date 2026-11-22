#if DEBUG
import SwiftUI
import AppKit

/// Marketing screenshots without Screen Recording or an unlocked screen.
/// `-JoeBroDemo -JoeBroShots <dir>` opens the main window at 1440x900, walks the
/// key panels and writes PNGs of the window content (NSView.cacheDisplay), then quits.
/// Optional: `-JoeBroShotsPalettes ember,cobalt` (default: the launch palette).
/// WKWebView content (some email bodies) may come out blank.
@MainActor enum DemoShots {
    static var dir: String? {
        let a = ProcessInfo.processInfo.arguments
        guard DemoMode.active, let i = a.firstIndex(of: "-JoeBroShots"), a.indices.contains(i + 1) else { return nil }
        return a[i + 1]
    }

    private static func option(_ name: String) -> String? {
        let a = ProcessInfo.processInfo.arguments
        return a.firstIndex(of: name).flatMap { a.indices.contains($0 + 1) ? a[$0 + 1] : nil }
    }

    private static func wait(_ s: Double) async { try? await Task.sleep(for: .seconds(s)) }

    private static func mainWindow() -> NSWindow? {
        NSApp.windows.first { $0.isVisible && $0.contentView != nil && $0.canBecomeMain }
    }

    private static func write(_ window: NSWindow, to path: String) {
        guard let view = window.contentView?.superview ?? window.contentView else { return }
        view.layoutSubtreeIfNeeded()
        guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: rep)
        if let png = rep.representation(using: .png, properties: [:]) {
            try? png.write(to: URL(fileURLWithPath: path))
        }
    }

    static func run(store: AppStore) async {
        guard let dir else { return }
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        NSApp.activate(ignoringOtherApps: true)
        await wait(3)
        guard let window = mainWindow() else { print("DemoShots: no window"); exit(1) }
        window.aspectRatio = NSSize(width: 1440, height: 900)
        window.contentMinSize = NSSize(width: 100, height: 100)
        window.setContentSize(NSSize(width: 1440, height: 900))
        window.center()
        await wait(1.5)
        let palettes = (option("-JoeBroShotsPalettes") ?? JBAppearance.shared.palette.rawValue).split(separator: ",").map(String.init)
        for (pi, palette) in palettes.enumerated() {
            if let p = JBPalette(rawValue: palette) { JBAppearance.shared.palette = p }
            for mode in ["dark", "light"] {
                NSApp.appearance = NSAppearance(named: mode == "dark" ? .darkAqua : .aqua)
                await wait(1.2)
                let full = pi == 0
                func snap(_ name: String) async {
                    await wait(2.2)
                    write(window, to: "\(dir)/mac_\(name)_\(mode)_\(palette).png")
                }
                // Empty chat with the wordmark.
                store.newChat(); await snap("chat-empty")
                // Chat with messages.
                if let first = store.sessions.first { store.select(first.id); await wait(1.5); await snap("chat") }
                if full, let first = store.sessions.first {
                    store.select(first.id); await wait(1)
                    NotificationCenter.default.post(name: .chatFillComposer, object: "/")
                    await snap("slash")
                    NotificationCenter.default.post(name: .chatFillComposer, object: "")
                }
                let tabs: [(String, WorkspaceTab, Bool)] = [
                    ("calendar", .calendar, true), ("memory", .memory, true), ("agents", .agents, true),
                    ("night-shift", .nightShift, true), ("tasks", .tasks, true), ("skills", .skills, full),
                    ("tools", .tools, true), ("email", .email, true), ("inbox", .inbox, true), ("research", .research, true),
                ]
                for (name, tab, on) in tabs where on { store.activeTab = tab; await snap(name) }
                // Command palette.
                if full {
                    store.activeTab = .chat
                    await wait(1)
                    NotificationCenter.default.post(name: .jbOpenPalette, object: nil)
                    await snap("palette")
                    NotificationCenter.default.post(name: .jbOpenPalette, object: nil)
                }
            }
        }
        // Settings (separate window): appearance tab.
        NSApp.sendAction(Selector(("showSettingsWindow:")), to: nil, from: nil)
        await wait(2)
        if let settings = NSApp.windows.first(where: { $0.isVisible && $0 !== window && $0.contentView != nil && $0.title.lowercased().contains("setting") }) ?? NSApp.windows.last(where: { $0 !== window && $0.isVisible }) {
            for mode in ["dark", "light"] {
                NSApp.appearance = NSAppearance(named: mode == "dark" ? .darkAqua : .aqua)
                await wait(1.5)
                write(settings, to: "\(dir)/mac_settings_\(mode).png")
            }
        }
        print("DemoShots: done")
        exit(0)
    }
}
#endif
