import SwiftUI
import Foundation

final class LocalBackend {
    static let shared = LocalBackend()
    private var process: Process?
    private let port = 8765
    private let logURL = FileManager.default.temporaryDirectory.appendingPathComponent("joebro-backend-launch.log")

    var url: URL { URL(string: "http://127.0.0.1:\(port)")! }

    func start() {
        UserDefaults.standard.set(url.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/")), forKey: "serverURL")
        guard process?.isRunning != true else { return }
        guard let script = backendScriptURL() else {
            log("JoeBro backend script not found")
            return
        }

        guard let python = pythonURL() else {
            log("No usable python3 interpreter found")
            return
        }
        let p = Process()
        p.executableURL = python
        p.arguments = [script.path, "--host", "127.0.0.1", "--port", String(port)]
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        p.environment = env
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        p.standardOutput = try? FileHandle(forWritingTo: logURL)
        p.standardError = p.standardOutput
        do {
            try p.run()
            process = p
            log("Started backend pid \(p.processIdentifier) with \(python.path) from \(script.path)")
        } catch {
            log("Could not start JoeBro backend: \(error.localizedDescription)")
        }
        // The backend dies with the app — orphaned children outlive their
        // parent on macOS unless terminated explicitly.
        NotificationCenter.default.addObserver(
            forName: NSApplication.willTerminateNotification, object: nil, queue: .main
        ) { _ in
            LocalBackend.shared.stop()
        }
    }

    func stop() {
        process?.terminate()
        process = nil
    }

    private func backendScriptURL() -> URL? {
        let fm = FileManager.default
        let candidates: [URL?] = [
            Bundle.main.url(forResource: "joebro_backend", withExtension: "py"),
            Bundle.main.resourceURL?.appendingPathComponent("Backend/joebro_backend.py"),
            URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent()
                .appendingPathComponent("Backend/joebro_backend.py"),
        ]
        return candidates.compactMap { $0 }.first { fm.fileExists(atPath: $0.path) }
    }

    private func pythonURL() -> URL? {
        let fm = FileManager.default
        let candidates = [
            "/Library/Developer/CommandLineTools/usr/bin/python3",
            "/Applications/Xcode.app/Contents/Developer/usr/bin/python3",
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ]
        return candidates.first { fm.isExecutableFile(atPath: $0) }.map(URL.init(fileURLWithPath:))
    }

    private func log(_ message: String) {
        let line = "[\(Date())] \(message)\n"
        if let data = line.data(using: .utf8) {
            if FileManager.default.fileExists(atPath: logURL.path),
               let fh = try? FileHandle(forWritingTo: logURL) {
                _ = try? fh.seekToEnd()
                try? fh.write(contentsOf: data)
                try? fh.close()
            } else {
                try? data.write(to: logURL)
            }
        }
    }
}

@main
struct JoeBroApp: App {
    @State private var store = AppStore()

    init() {
        LocalBackend.shared.start()
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(store)
                .environment(GlassSettings.shared)
                .frame(minWidth: 880, minHeight: 560)
                .task { await store.bootstrap() }
        }
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(after: .newItem) {
                Button("New Chat") { store.newChat() }
                    .keyboardShortcut("n", modifiers: .command)
            }
        }

        Settings {
            SettingsView()
                .environment(store)
                .environment(GlassSettings.shared)
        }

        // Pop-out editor — same surface, own window. Shares the store, so
        // tabs/autosave/AI co-editing all behave identically.
        Window("JoeBro Editor", id: "editor") {
            ZStack {
                WallpaperBackground()
                EditorPane()
                    .padding(12)
                    .padding(.top, 26)   // traffic lights
            }
            .ignoresSafeArea()
            .frame(minWidth: 520, minHeight: 480)
            .environment(store)
            .environment(GlassSettings.shared)
            .onAppear { store.editorDetached = true }
            .onDisappear { store.editorDetached = false }
        }
        .windowStyle(.hiddenTitleBar)
    }
}

/// Locks the window to the screen's aspect ratio + a sane minimum size,
/// so every layout scales proportionally instead of spilling.
struct WindowConfigurator: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView {
        let v = NSView()
        DispatchQueue.main.async {
            guard let window = v.window, let screen = window.screen ?? NSScreen.main else { return }
            let s = screen.visibleFrame.size
            window.aspectRatio = s
            window.minSize = NSSize(width: s.width * 0.55, height: s.height * 0.55)
        }
        return v
    }
    func updateNSView(_ nsView: NSView, context: Context) {}
}

struct RootView: View {
    var body: some View {
        ZStack {
            WindowConfigurator().frame(width: 0, height: 0)
            WallpaperBackground()
            MainSplitView()
        }
        .ignoresSafeArea()
        .preferredColorScheme(nil)
    }
}
