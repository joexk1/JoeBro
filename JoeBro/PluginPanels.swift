import SwiftUI
import WebKit

/// Plugins tab: hosts the UI panels that joebro.plugin/1 plugins contribute.
///
/// A panel is the plugin's own HTML (from its `ui/` folder on the backend),
/// loaded through the `joebro-plugin://<slug>/…` scheme so every file request
/// carries the backend token without the page ever seeing it. The page has no
/// network access of its own (the backend's CSP sets connect-src 'none'); it
/// reaches its plugin only through the injected bridge:
///
///     const r = await joebro.call("tool_name", {arg: 1})
///
/// which can call that plugin's tools and nothing else.
struct PluginPanelsView: View {
    @Environment(AppStore.self) private var store
    @State private var selected: String?

    private struct Entry: Identifiable, Hashable {
        let plugin: Plugin
        let panel: PluginPanel
        var id: String { (plugin.slug ?? plugin.id) + "/" + panel.id }
    }

    private var entries: [Entry] {
        store.plugins
            .filter { ($0.isEnabled ?? false) && !($0.slug ?? "").isEmpty }
            .flatMap { p in
                (p.contributes?.panels ?? [])
                    .filter { $0.surfaces == nil || $0.surfaces!.contains("mac") }
                    .map { Entry(plugin: p, panel: $0) }
            }
    }

    var body: some View {
        PanelChrome(title: "Plugins", icon: "puzzlepiece.extension") {
            Button {
                Task { await store.loadPlugins() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .help("Reload plugins")
        } content: {
            let list = entries
            if list.isEmpty {
                ContentUnavailableView(
                    "No plugin panels",
                    systemImage: "puzzlepiece.extension",
                    description: Text("Panels from plugins you switch on (Tools → Plugins) appear here. Ask JoeBro or a coding agent to build one: `joebro-plugin init my-plugin`."))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .joeGlassRect(16)
            } else {
                let current = list.first { $0.id == selected } ?? list[0]
                HStack(spacing: 12) {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(list) { e in
                            Button {
                                selected = e.id
                            } label: {
                                HStack(spacing: 8) {
                                    Image(systemName: e.panel.icon ?? "puzzlepiece.extension")
                                        .frame(width: 18)
                                    VStack(alignment: .leading, spacing: 1) {
                                        Text(e.panel.title).font(.system(size: 13, weight: .medium))
                                        Text(e.plugin.name).font(.system(size: 10)).foregroundStyle(.secondary)
                                    }
                                    Spacer(minLength: 0)
                                }
                                .padding(.horizontal, 10)
                                .padding(.vertical, 6)
                                .background(e.id == current.id ? Color.accentColor.opacity(0.18) : .clear,
                                            in: RoundedRectangle(cornerRadius: 8))
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                        }
                        Spacer()
                    }
                    .padding(8)
                    .frame(width: 210)
                    .frame(maxHeight: .infinity)
                    .joeGlassRect(16)

                    PluginWebView(slug: current.plugin.slug ?? "", entryURL: current.panel.url)
                        .id(current.id)
                        .clipShape(RoundedRectangle(cornerRadius: 16))
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .joeGlassRect(16)
                }
            }
        }
        .task { await store.loadPlugins() }
    }
}

/// WKWebView for one plugin panel.
struct PluginWebView: NSViewRepresentable {
    let slug: String
    /// Backend path, e.g. /api/plugins/plant-care/ui/index.html
    let entryURL: String
    @Environment(\.colorScheme) private var colorScheme

    func makeCoordinator() -> PluginBridge { PluginBridge(slug: slug) }

    func makeNSView(context: Context) -> WKWebView {
        let cfg = WKWebViewConfiguration()
        cfg.setURLSchemeHandler(context.coordinator, forURLScheme: PluginBridge.scheme)
        cfg.websiteDataStore = .nonPersistent()
        let theme = colorScheme == .dark ? "dark" : "light"
        let js = """
        window.joebro = Object.freeze({
          plugin: \(jsString(slug)),
          theme: \(jsString(theme)),
          call: function (tool, args) {
            return window.webkit.messageHandlers.joebro.postMessage({tool: String(tool), arguments: args || {}});
          }
        });
        """
        cfg.userContentController.addUserScript(WKUserScript(source: js, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        cfg.userContentController.addScriptMessageHandler(context.coordinator, contentWorld: .page, name: "joebro")
        let web = WKWebView(frame: .zero, configuration: cfg)
        web.navigationDelegate = context.coordinator
        web.setValue(false, forKey: "drawsBackground")
        let rel = entryURL.components(separatedBy: "/ui/").dropFirst().joined(separator: "/ui/")
        if let url = URL(string: "\(PluginBridge.scheme)://\(slug)/\(rel.isEmpty ? "index.html" : rel)") {
            web.load(URLRequest(url: url))
        }
        return web
    }

    func updateNSView(_ nsView: WKWebView, context: Context) {}

    static func dismantleNSView(_ nsView: WKWebView, coordinator: PluginBridge) {
        nsView.configuration.userContentController.removeAllScriptMessageHandlers()
    }

    private func jsString(_ s: String) -> String {
        let data = try? JSONSerialization.data(withJSONObject: [s])
        let arr = data.flatMap { String(data: $0, encoding: .utf8) } ?? "[\"\"]"
        return String(arr.dropFirst().dropLast())
    }
}

/// Serves the panel's files (authenticated) and answers joebro.call().
final class PluginBridge: NSObject, WKURLSchemeHandler, WKScriptMessageHandlerWithReply, WKNavigationDelegate {
    static let scheme = "joebro-plugin"
    let slug: String
    private var stopped = Set<ObjectIdentifier>()

    init(slug: String) { self.slug = slug }

    // MARK: files
    func webView(_ webView: WKWebView, start task: WKURLSchemeTask) {
        guard let url = task.request.url, url.host == slug else {
            task.didFailWithError(URLError(.unsupportedURL))
            return
        }
        let path = String(url.path.drop { $0 == "/" })
        let key = ObjectIdentifier(task)
        Task { @MainActor in
            do {
                let (data, mime) = try await APIClient.shared.pluginUIFile(slug: slug, path: path.isEmpty ? "index.html" : path)
                guard !stopped.contains(key) else { return }
                // The panel gets no network of its own: only its own files + the bridge.
                let headers = ["Content-Type": mime, "Cache-Control": "no-store",
                               "Content-Security-Policy": "default-src joebro-plugin: 'unsafe-inline' data: blob:; connect-src 'none'"]
                task.didReceive(HTTPURLResponse(url: url, statusCode: 200, httpVersion: "HTTP/1.1", headerFields: headers)!)
                task.didReceive(data)
                task.didFinish()
            } catch {
                guard !stopped.contains(key) else { return }
                task.didFailWithError(error)
            }
            stopped.remove(key)
        }
    }

    func webView(_ webView: WKWebView, stop task: WKURLSchemeTask) {
        stopped.insert(ObjectIdentifier(task))
    }

    // MARK: joebro.call bridge
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        guard let body = message.body as? [String: Any], let tool = body["tool"] as? String, !tool.isEmpty else {
            replyHandler(nil, "joebro.call(tool, args): tool name required")
            return
        }
        let args = body["arguments"] as? [String: Any] ?? [:]
        Task { @MainActor in
            do {
                let out = try await APIClient.shared.callPlugin(slug: slug, tool: tool, arguments: args)
                let text = out["output"] as? String ?? ""
                if (out["exit_code"] as? Int ?? 0) != 0 {
                    replyHandler(nil, text.isEmpty ? "tool failed" : text)
                    return
                }
                if let data = text.data(using: .utf8),
                   let value = try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]) {
                    replyHandler(value, nil)
                } else {
                    replyHandler(text, nil)
                }
            } catch {
                replyHandler(nil, error.localizedDescription)
            }
        }
    }

    // MARK: navigation: stay inside the plugin; real links open in the browser
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if url.scheme == Self.scheme || url.scheme == "about" || url.scheme == "data" || url.scheme == "blob" {
            return decisionHandler(.allow)
        }
        if action.navigationType == .linkActivated, ["http", "https", "mailto"].contains(url.scheme ?? "") {
            NSWorkspace.shared.open(url)
        }
        decisionHandler(.cancel)
    }
}
