#if DEBUG
import SwiftUI
import AppKit
import WebKit

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

    private static func webViews(in v: NSView) -> [WKWebView] {
        (v as? WKWebView).map { [$0] } ?? v.subviews.flatMap { webViews(in: $0) }
    }

    /// The window as a PNG. cacheDisplay can't see WKWebView content (animated
    /// backdrops), so each web view is snapshotted and drawn underneath.
    private static func write(_ window: NSWindow, to path: String) async {
        guard let view = window.contentView?.superview ?? window.contentView else { return }
        view.layoutSubtreeIfNeeded()
        guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: rep)
        var layers: [(NSImage, NSRect)] = []
        for wv in webViews(in: view) where !wv.isHidden && wv.bounds.width > 10 {
            let cfg = WKSnapshotConfiguration()
            if let img = try? await wv.takeSnapshot(configuration: cfg) {
                layers.append((img, wv.convert(wv.bounds, to: view)))
            }
        }
        var out = rep
        if !layers.isEmpty, let full = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: rep.pixelsWide, pixelsHigh: rep.pixelsHigh,
                                                        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                                        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) {
            full.size = view.bounds.size
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: full)
            for (img, r) in layers {
                let flipped = view.isFlipped ? NSRect(x: r.minX, y: view.bounds.height - r.maxY, width: r.width, height: r.height) : r
                img.draw(in: flipped)
            }
            rep.draw(in: NSRect(origin: .zero, size: view.bounds.size), from: .zero, operation: .sourceOver, fraction: 1, respectFlipped: false, hints: nil)
            NSGraphicsContext.restoreGraphicsState()
            out = full
        }
        if let png = out.representation(using: .png, properties: [:]) {
            try? png.write(to: URL(fileURLWithPath: path))
        }
    }

    /// `-JoeBroShotsExternal`: instead of drawing the window into a bitmap (which
    /// misses WKWebView/animated backdrops), each shot writes "<name> <windowNumber>"
    /// to `<dir>/.pending` and waits until a capturer (screencapture -l) deletes it.
    private static var external: Bool { ProcessInfo.processInfo.arguments.contains("-JoeBroShotsExternal") }

    private static func shoot(_ window: NSWindow, _ path: String) async {
        guard external else { await write(window, to: path); return }
        let pending = (dir ?? "/tmp") + "/.pending"
        try? "\((path as NSString).lastPathComponent) \(window.windowNumber)".write(toFile: pending, atomically: true, encoding: .utf8)
        for _ in 0..<150 where FileManager.default.fileExists(atPath: pending) { await wait(0.2) }
    }

    /// A look: appearance + accent + background. Default set covers every kind.
    private struct Look { let mode: String, palette: String, backdrop: String, scene: String
        var name: String { "\(mode)-\(backdrop == "scene" ? scene : backdrop)-\(palette)" } }
    private static var looks: [Look] {
        let raw = option("-JoeBroShotsLooks")
            ?? "dark:auto:scene:misty-forest,light:auto:scene:alpine-dawn,dark:ember:palette:-,light:cobalt:palette:-,dark:violet:scene:tokyo-rain,light:moss:scene:cloud-sea,dark:mono:palette:-,dark:auto:scene:aurora-fjord"
        return raw.split(separator: ",").compactMap { l in
            let f = l.split(separator: ":").map(String.init)
            return f.count == 4 ? Look(mode: f[0], palette: f[1], backdrop: f[2], scene: f[3]) : nil
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
        let only = option("-JoeBroShotsOnly").map { Set($0.split(separator: ",").map(String.init)) }
        let ap = JBAppearance.shared
        for (li, look) in looks.enumerated() {
            NSApp.appearance = NSAppearance(named: look.mode == "dark" ? .darkAqua : .aqua)
            if look.backdrop == "scene" { ap.sceneID = look.scene; ap.backdrop = .scene; ap.solidity = 0.72 }
            else { ap.backdrop = .palette; ap.solidity = 1 }
            if let p = JBPalette(rawValue: look.palette) { ap.palette = p }
            await wait(look.backdrop == "scene" ? 4 : 1.5)
            let full = li == 0 || li == 1
            func snap(_ name: String, settle: Double = 2.2) async {
                if let only, !only.contains(name) { return }
                await wait(settle)
                await shoot(window, "\(dir)/mac_\(name)_\(look.name).png")
            }
            store.activeTab = .chat
            AgentNav.shared.close()
            if let first = store.sessions.first { store.select(first.id); await wait(1.5); await snap("chat") }
            // A coding agent as a chat (Claude Code / Codex / OpenCode under one roof).
            await AgentNav.shared.refresh()
            if let agent = AgentNav.shared.runs.first(where: { $0.live }) ?? AgentNav.shared.runs.first {
                AgentNav.shared.open(agent.id); await snap("coding-agent", settle: 3)
                AgentNav.shared.close()
            }
            if full {
                store.newChat(); await snap("chat-empty")
                if let first = store.sessions.first {
                    store.select(first.id); await wait(1)
                    NotificationCenter.default.post(name: .chatFillComposer, object: "/")
                    await snap("slash")
                    NotificationCenter.default.post(name: .chatFillComposer, object: "")
                }
                store.openDocumentByID("d1"); await snap("editor", settle: 3); store.editorVisible = false
            }
            let tabs: [(String, WorkspaceTab, Bool)] = [
                ("agents", .agents, true), ("memory", .memory, true), ("night-shift", .nightShift, full),
                ("calendar", .calendar, full), ("skills", .skills, full), ("tools", .tools, full),
                ("notifications", .inbox, full), ("tasks", .tasks, full), ("research", .research, full),
            ]
            for (name, tab, on) in tabs where on { store.activeTab = tab; await snap(name) }
            if full {
                store.activeTab = .chat
                await wait(1)
                NotificationCenter.default.post(name: .jbOpenPalette, object: nil)
                await snap("palette")
                NotificationCenter.default.post(name: .jbOpenPalette, object: nil)
            }
        }
        // Settings (separate window): appearance tab.
        NSApp.sendAction(Selector(("showSettingsWindow:")), to: nil, from: nil)
        await wait(2)
        if let settings = NSApp.windows.first(where: { $0.isVisible && $0 !== window && $0.contentView != nil && $0.title.lowercased().contains("setting") }) ?? NSApp.windows.last(where: { $0 !== window && $0.isVisible }) {
            for mode in ["dark", "light"] {
                NSApp.appearance = NSAppearance(named: mode == "dark" ? .darkAqua : .aqua)
                await wait(1.5)
                await shoot(settings, "\(dir)/mac_settings_\(mode).png")
            }
        }
        print("DemoShots: done")
        exit(0)
    }
}
#endif
