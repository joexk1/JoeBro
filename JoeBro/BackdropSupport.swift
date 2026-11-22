import SwiftUI
import WebKit
import AVFoundation
import AppKit

/// Animated backdrops: bundled ASCII scenes, user HTML files and video loops.
enum JBBackdropLibrary {
    static let bundledScenes: [String] = [
        "alpine-dawn", "amer-fort", "aurora-fjord", "black-prism", "cloud-sea", "command-city",
        "deep-reef", "desert-night", "earthrise", "edge-orbit", "gangaur-ghat", "green-storm",
        "jal-mahal", "jungle-falls", "kite-festival", "kyoto-dusk", "lake-pichola", "lantern-lake",
        "lava-wall", "marine-drive", "misty-forest", "night-coast", "night-highway", "ocean-sunset",
        "planet-scale", "prism-city", "salt-flat", "saturn-rise", "snow-train", "storm-plains",
        "taj-dawn", "tokyo-rain", "varanasi-ghats", "volcano-night",
    ]
    static let userExtensions: Set<String> = ["html", "htm", "mp4", "mov", "m4v"]

    static var folder: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let dir = base.appendingPathComponent("JoeBro/Backdrops", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    static func userBackdrops() -> [URL] {
        let files = (try? FileManager.default.contentsOfDirectory(
            at: folder, includingPropertiesForKeys: nil, options: [.skipsHiddenFiles])) ?? []
        return files.filter { userExtensions.contains($0.pathExtension.lowercased()) }
            .sorted { $0.lastPathComponent.localizedCaseInsensitiveCompare($1.lastPathComponent) == .orderedAscending }
    }

    static func displayName(_ id: String) -> String {
        if let u = url(for: id) { return u.deletingPathExtension().lastPathComponent }
        return id.replacingOccurrences(of: "-", with: " ")
    }

    /// A user backdrop is stored as a file URL string; bundled scenes as their plain name.
    static func url(for id: String) -> URL? {
        id.hasPrefix("file://") ? URL(string: id) : nil
    }

    static func isVideo(_ url: URL) -> Bool {
        ["mp4", "mov", "m4v"].contains(url.pathExtension.lowercased())
    }

    /// Copies a video into the Backdrops folder and returns its scene id.
    static func importVideo(_ src: URL) throws -> String {
        let access = src.startAccessingSecurityScopedResource()
        defer { if access { src.stopAccessingSecurityScopedResource() } }
        var dest = folder.appendingPathComponent(src.lastPathComponent)
        var n = 2
        while FileManager.default.fileExists(atPath: dest.path) {
            let base = src.deletingPathExtension().lastPathComponent
            dest = folder.appendingPathComponent("\(base) \(n).\(src.pathExtension)")
            n += 1
        }
        try FileManager.default.copyItem(at: src, to: dest)
        return dest.absoluteString
    }

    static var bundledScenesIndex: URL? {
        Bundle.main.url(forResource: "index", withExtension: "html", subdirectory: "AsciiScenes")
    }

    /// Copies the bundled backdrop skill into the Claude Code and Codex skill folders.
    /// Returns the destinations written.
    static func installSkill() throws -> [String] {
        guard let src = Bundle.main.url(forResource: "BackdropSkill", withExtension: nil) else {
            throw CocoaError(.fileNoSuchFile)
        }
        let fm = FileManager.default
        let home = fm.homeDirectoryForCurrentUser
        var done: [String] = []
        for root in [".claude/skills/joebro-backdrop", ".codex/skills/joebro-backdrop"] {
            let dest = home.appendingPathComponent(root, isDirectory: true)
            try fm.createDirectory(at: dest, withIntermediateDirectories: true)
            for name in try fm.contentsOfDirectory(atPath: src.path) {
                let to = dest.appendingPathComponent(name)
                if fm.fileExists(atPath: to.path) { try fm.removeItem(at: to) }
                try fm.copyItem(at: src.appendingPathComponent(name), to: to)
            }
            done.append("~/" + root)
        }
        return done
    }
}

// MARK: - Window activity

/// Reports whether the hosting window is visible (not occluded/miniaturised) and the app is active.
struct JBWindowActivityProbe: NSViewRepresentable {
    var onChange: (Bool) -> Void

    func makeNSView(context: Context) -> ProbeView {
        let v = ProbeView()
        v.onChange = onChange
        return v
    }
    func updateNSView(_ v: ProbeView, context: Context) { v.onChange = onChange }

    final class ProbeView: NSView {
        var onChange: ((Bool) -> Void)?
        private var observers: [NSObjectProtocol] = []

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            observers.forEach { NotificationCenter.default.removeObserver($0) }
            observers = []
            guard let w = window else { return }
            let nc = NotificationCenter.default
            let fire: (Notification) -> Void = { [weak self] _ in self?.report() }
            observers.append(nc.addObserver(forName: NSWindow.didChangeOcclusionStateNotification, object: w, queue: .main, using: fire))
            observers.append(nc.addObserver(forName: NSWindow.didMiniaturizeNotification, object: w, queue: .main, using: fire))
            observers.append(nc.addObserver(forName: NSWindow.didDeminiaturizeNotification, object: w, queue: .main, using: fire))
            observers.append(nc.addObserver(forName: NSApplication.didBecomeActiveNotification, object: nil, queue: .main, using: fire))
            observers.append(nc.addObserver(forName: NSApplication.didResignActiveNotification, object: nil, queue: .main, using: fire))
            report()
        }
        private func report() {
            guard let w = window else { return }
            onChange?(w.occlusionState.contains(.visible) && !w.isMiniaturized && NSApp.isActive)
        }
        deinit { observers.forEach { NotificationCenter.default.removeObserver($0) } }
    }
}

// MARK: - Web backdrops

private final class PassiveWebView: WKWebView {
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
    override var acceptsFirstResponder: Bool { false }
}

private let blockNetworkRules = """
[{"trigger":{"url-filter":"^https?://"},"action":{"type":"block"}}]
"""

/// Renders a bundled ASCII scene or a user HTML backdrop. `sceneName` is used when `fileURL` is nil.
struct WebBackdropView: NSViewRepresentable {
    var sceneName: String
    var fileURL: URL?
    var paused: Bool
    var dark: Bool

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> WKWebView {
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .nonPersistent()
        let view = PassiveWebView(frame: .zero, configuration: cfg)
        view.setValue(false, forKey: "drawsBackground")
        view.navigationDelegate = context.coordinator
        context.coordinator.view = view
        context.coordinator.state = self
        let target: URL? = fileURL ?? JBBackdropLibrary.bundledScenesIndex
        guard let target else { return view }
        let isUser = fileURL != nil
        Task { @MainActor in
            if isUser {
                // Fail closed: a user backdrop never loads without the network block in place.
                guard let list = try? await WKContentRuleListStore.default()
                    .compileContentRuleList(forIdentifier: "jbBackdropNoNet", encodedContentRuleList: blockNetworkRules) else { return }
                view.configuration.userContentController.add(list)
            }
            view.loadFileURL(target, allowingReadAccessTo: target.deletingLastPathComponent())
        }
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {
        context.coordinator.state = self
        context.coordinator.apply()
    }

    static func dismantleNSView(_ view: WKWebView, coordinator: Coordinator) {
        view.evaluateJavaScript("window.jbBackdrop&&window.jbBackdrop.pause&&window.jbBackdrop.pause()")
        view.stopLoading()
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        weak var view: WKWebView?
        var state: WebBackdropView?
        private var ready = false
        private var applied = ""

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            ready = true
            applied = ""
            apply()
        }

        func apply() {
            guard ready, let s = state, let view else { return }
            let scheme = s.dark ? "dark" : "light"
            let js: String
            if s.fileURL == nil {
                let name = (try? JSONEncoder().encode(s.sceneName)).flatMap { String(data: $0, encoding: .utf8) } ?? "\"\""
                js = "window.setScene(\(name), \(s.paused), \(!s.dark))"
            } else {
                js = """
                (function(){var b=window.jbBackdrop;if(!b)return;
                if(b.setScheme)b.setScheme('\(scheme)');
                if(\(s.paused)){b.pause&&b.pause()}else{b.resume&&b.resume()}})()
                """
            }
            guard js != applied else { return }
            applied = js
            view.evaluateJavaScript(js)
        }
    }
}

// MARK: - Video backdrop

struct VideoBackdropView: NSViewRepresentable {
    var url: URL
    var paused: Bool

    func makeNSView(context: Context) -> VideoLayerView {
        let v = VideoLayerView()
        v.load(url)
        v.setPaused(paused)
        return v
    }
    func updateNSView(_ v: VideoLayerView, context: Context) {
        if v.url != url { v.load(url) }
        v.setPaused(paused)
    }
    static func dismantleNSView(_ v: VideoLayerView, coordinator: ()) { v.teardown() }

    final class VideoLayerView: NSView {
        private(set) var url: URL?
        private var queue: AVQueuePlayer?
        private var looper: AVPlayerLooper?
        private let playerLayer = AVPlayerLayer()

        override init(frame: NSRect) {
            super.init(frame: frame)
            wantsLayer = true
            layer = CALayer()
            playerLayer.videoGravity = .resizeAspectFill
            layer?.addSublayer(playerLayer)
        }
        required init?(coder: NSCoder) { fatalError() }
        override func hitTest(_ point: NSPoint) -> NSView? { nil }
        override func layout() {
            super.layout()
            CATransaction.begin(); CATransaction.setDisableActions(true)
            playerLayer.frame = bounds
            CATransaction.commit()
        }

        func load(_ url: URL) {
            teardown()
            self.url = url
            let q = AVQueuePlayer()
            q.isMuted = true
            looper = AVPlayerLooper(player: q, templateItem: AVPlayerItem(url: url))
            playerLayer.player = q
            queue = q
        }
        func setPaused(_ p: Bool) {
            guard let queue else { return }
            if p { queue.pause() } else { queue.play() }
        }
        func teardown() {
            queue?.pause()
            looper?.disableLooping()
            looper = nil
            playerLayer.player = nil
            queue = nil
            url = nil
        }
    }
}

/// The animated layer behind the panels.
struct SceneBackdrop: View {
    let sceneID: String
    let paused: Bool
    let dark: Bool

    var body: some View {
        if let url = JBBackdropLibrary.url(for: sceneID) {
            if !FileManager.default.fileExists(atPath: url.path) {
                JBDitherBackdrop(palette: JBAppearance.shared.palette)
            } else if JBBackdropLibrary.isVideo(url) {
                VideoBackdropView(url: url, paused: paused)
            } else {
                WebBackdropView(sceneName: "", fileURL: url, paused: paused, dark: dark).id(url)
            }
        } else if JBBackdropLibrary.bundledScenes.contains(sceneID) {
            WebBackdropView(sceneName: sceneID, fileURL: nil, paused: paused, dark: dark)
        } else {
            JBDitherBackdrop(palette: JBAppearance.shared.palette)
        }
    }
}
