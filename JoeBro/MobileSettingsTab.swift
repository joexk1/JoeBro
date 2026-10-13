import CoreImage
import CoreImage.CIFilterBuiltins
import SwiftUI
import UniformTypeIdentifiers

/// Settings page for phone access: listener, QR pairing, connected phones, push
/// (APNs) setup and phone-agent switches. State lives in the backend; the token is
/// read fresh each time and never stored by the app.
struct MobileSettingsTab: View {
    @State private var enabled = false
    @State private var bind = "tailscale"
    @State private var urls: [(label: String, url: String)] = []
    @State private var token = ""
    @State private var pairURI = ""
    @State private var showToken = false
    @State private var confirmRotate = false
    @State private var devices: [JSONValue] = []
    @State private var testResult: String?
    @State private var message: String?
    @State private var loaded = false

    // Push
    @State private var pushConfigured = false
    @State private var pushBlocker = ""
    @State private var teamID = ""
    @State private var keyID = ""
    @State private var bundleID = ""
    @State private var keyFile: URL?
    @State private var pickingKey = false

    // Phone agent
    @State private var agentName = ""
    @State private var allowFull = false
    @State private var showSession = false
    @State private var location = false
    @State private var museLearn = true
    @State private var heartbeatOn = false
    @State private var heartbeatInterval = 30
    @State private var pulseOn = false
    @State private var pulseInterval = 60

    var body: some View {
        Form {
            Section("Phone access") {
                Toggle("Let my phone connect", isOn: $enabled)
                Picker("Reachable over", selection: $bind) {
                    Text("Tailscale only (recommended)").tag("tailscale")
                    Text("Wi-Fi and Tailscale").tag("all")
                    Text("This Wi-Fi network only").tag("lan")
                }
                Text(enabled ? "Listening on \(urls.first?.url ?? "…")" : "Off")
                    .font(.caption).foregroundStyle(.secondary)
                if enabled && bind != "tailscale" {
                    Text("Anyone on this network who gets the token can read your chats and run what the phone agent is allowed to run.")
                        .font(.caption).foregroundStyle(.orange)
                }
                Text("The phone only works while JoeBro is open and this Mac is awake.")
                    .font(.caption).foregroundStyle(.secondary)
                if let message {
                    Text(message).font(.caption).foregroundStyle(.red)
                }
            }
            if enabled {
                pairSection
                devicesSection
            }
            pushSection
            agentSection
        }
        .formStyle(.grouped)
        .task { await load() }
        .onChange(of: enabled) { applyMobile() }
        .onChange(of: bind) { applyMobile() }
        .fileImporter(isPresented: $pickingKey, allowedContentTypes: [UTType(filenameExtension: "p8") ?? .data]) { r in
            if case .success(let u) = r { keyFile = u }
        }
        .confirmationDialog("Rotate the token?", isPresented: $confirmRotate) {
            Button("Rotate token", role: .destructive) {
                Task {
                    do { try await APIClient.shared.rotateMobileToken() }
                    catch { message = error.localizedDescription }
                    await load()
                }
            }
        } message: {
            Text("Your phone will disconnect and need to scan again.")
        }
    }

    // MARK: Sections

    private var pairSection: some View {
        Section("Pair your iPhone") {
            if let img = Self.qr(pairURI) {
                Image(nsImage: img)
                    .interpolation(.none)
                    .resizable()
                    .frame(width: 220, height: 220)
                    .frame(maxWidth: .infinity)
            }
            LabeledContent("URL") {
                Text(urls.first?.url ?? "—")
                    .font(.system(size: 12, design: .monospaced)).textSelection(.enabled)
            }
            ForEach(urls.dropFirst(), id: \.url) { u in
                LabeledContent(u.label) {
                    Text(u.url).font(.system(size: 12, design: .monospaced)).textSelection(.enabled)
                }
            }
            LabeledContent("Token") {
                HStack {
                    Text(showToken ? token : String(repeating: "•", count: 16))
                        .font(.system(size: 12, design: .monospaced)).textSelection(.enabled)
                    Button(showToken ? "Hide" : "Show") { showToken.toggle() }
                    Button("Copy") { copy(token) }
                }
                .buttonStyle(.borderless).font(.system(size: 11))
            }
            HStack {
                Button("Copy link") { copy(pairURI) }
                Button("Rotate token…") { confirmRotate = true }
                Button("Test from this Mac") { Task { await testLAN() } }
                Spacer()
            }
            if let testResult {
                Text(testResult).font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var devicesSection: some View {
        Section("Connected phones") {
            if devices.isEmpty {
                Text("No phones yet.").font(.caption).foregroundStyle(.secondary)
            }
            ForEach(Array(devices.enumerated()), id: \.offset) { _, d in
                let id = d["install_id"]?.stringValue ?? ""
                HStack {
                    Image(systemName: "iphone").foregroundStyle(.secondary)
                    VStack(alignment: .leading) {
                        Text(d["name"]?.stringValue ?? "iPhone")
                        Text("\(relativeDate(d["last_seen"]?.stringValue)) · push \((d["enabled"]?.boolValue ?? false) ? "on" : "off")")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Remove", role: .destructive) {
                        Task { try? await APIClient.shared.removeMobileDevice(id); await load() }
                    }
                    .controlSize(.small)
                }
            }
        }
    }

    private var pushSection: some View {
        Section("Notifications (push)") {
            Text(pushConfigured ? "Push is set up." : "Push is not set up.")
                .font(.system(size: 12, weight: .semibold))
            if !pushBlocker.isEmpty {
                Text(pushBlocker).font(.caption).foregroundStyle(.orange)
            }
            Text("1. In your Apple developer account, create an APNs Auth Key (.p8) and note its Key ID.\n2. Copy your Team ID from the Membership page.\n3. Enter the bundle ID of the iPhone app you built.\n4. Choose the .p8 file and save. It is stored only on this Mac and used only to talk to Apple's push servers.")
                .font(.caption).foregroundStyle(.secondary)
            TextField("Team ID", text: $teamID)
            TextField("Key ID", text: $keyID)
            TextField("Bundle ID", text: $bundleID)
            HStack {
                Button("Choose .p8 key…") { pickingKey = true }
                Text(keyFile?.lastPathComponent ?? "No key chosen")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
            }
            HStack {
                Button("Save") { Task { await saveAPNs() } }
                    .disabled(keyFile == nil || teamID.isEmpty || keyID.isEmpty || bundleID.isEmpty)
                Button("Send test notification") {
                    Task {
                        do { try await APIClient.shared.sendTestNotification(); message = nil }
                        catch { message = error.localizedDescription }
                    }
                }
                Button("Remove key", role: .destructive) {
                    Task { try? await APIClient.shared.removeAPNsKey(); await load() }
                }
                Spacer()
            }
            Text("Push is optional. Without it the phone still works, it just can't buzz you.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private var agentSection: some View {
        Section("Phone agent") {
            TextField("Agent name", text: $agentName)
                .onSubmit { pref("phone_agent_name", agentName) }
            Toggle("Let the phone agent run commands", isOn: $allowFull)
                .onChange(of: allowFull) { pref("mobile_allow_full", allowFull) }
            Toggle("Show the phone chat in my sidebar", isOn: $showSession)
                .onChange(of: showSession) { pref("show_phone_session", showSession) }
            Toggle("Use my phone's location for weather", isOn: $location)
                .onChange(of: location) { pref("location_enabled", location) }
            Text("Weather comes from open-meteo.com and place names from nominatim.openstreetmap.org, only while this is on.")
                .font(.caption).foregroundStyle(.secondary)
            Toggle("Learn durable facts into phone memory", isOn: $museLearn)
                .onChange(of: museLearn) { pref("muse_auto_learn", museLearn) }
            Toggle("Background checks (heartbeat)", isOn: $heartbeatOn)
                .onChange(of: heartbeatOn) {
                    guard loaded else { return }
                    Task { _ = try? await APIClient.shared.sendJSON("api/heartbeat", body: ["action": heartbeatOn ? "resume" : "pause"]) }
                }
            Stepper("Check every \(heartbeatInterval) min", value: $heartbeatInterval, in: 5...240, step: 5)
                .onChange(of: heartbeatInterval) { pref("heartbeat_interval_minutes", heartbeatInterval) }
            Toggle("Pulse", isOn: $pulseOn)
                .onChange(of: pulseOn) { pulse(["enabled": pulseOn]) }
            Stepper("Pulse every \(pulseInterval) min", value: $pulseInterval, in: 15...480, step: 15)
                .onChange(of: pulseInterval) { pulse(["interval_minutes": pulseInterval]) }
            Text("Anything the phone agent finds is posted into its chat. Reply there yes or no.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    // MARK: Actions

    private func load() async {
        loaded = false
        defer { loaded = true }
        let api = APIClient.shared
        if let m = try? await api.mobileStatus() {
            enabled = m["enabled"]?.boolValue ?? false
            if let b = m["bind"]?.stringValue, ["tailscale", "all", "lan"].contains(b) { bind = b }
            token = m["token"]?.stringValue ?? ""
            pairURI = m["pair_uri"]?.stringValue ?? ""
            urls = (m["urls"]?.arrayValue ?? []).compactMap {
                guard let u = $0["url"]?.stringValue else { return nil }
                return ($0["label"]?.stringValue ?? "URL", u)
            }
            if pairURI.isEmpty, let u = urls.first?.url, !token.isEmpty {
                pairURI = Self.pairLink(url: u, token: token)
            }
        }
        devices = (try? await api.mobileDevices()) ?? []
        if let p = try? await api.pushStatus() {
            pushConfigured = p["configured"]?.boolValue ?? false
            pushBlocker = p["blocker"]?.stringValue ?? ""
            if bundleID.isEmpty { bundleID = p["bundle_id"]?.stringValue ?? "" }
        }
        if let p = try? await api.getPrefs() {
            agentName = p["phone_agent_name"]?.stringValue ?? "JoeBro"
            allowFull = p["mobile_allow_full"]?.boolValue ?? false
            showSession = p["show_phone_session"]?.boolValue ?? false
            location = p["location_enabled"]?.boolValue ?? false
            museLearn = p["muse_auto_learn"]?.boolValue ?? true
            heartbeatInterval = p["heartbeat_interval_minutes"]?.intValue ?? 30
        }
        if let h: JSONValue = try? await api.getJSON("api/heartbeat") {
            heartbeatOn = h["enabled"]?.boolValue ?? false
        }
        if let r: JSONValue = try? await api.getJSON("api/pulse"), let p = r["pulse"] {
            pulseOn = p["enabled"]?.boolValue ?? false
            pulseInterval = p["interval_minutes"]?.intValue ?? 60
        }
    }

    private func applyMobile() {
        guard loaded else { return }
        Task {
            do { try await APIClient.shared.setMobile(enabled: enabled, bind: bind); message = nil }
            catch { message = error.localizedDescription }
            await load()
        }
    }

    private func saveAPNs() async {
        guard let keyFile else { return }
        let scoped = keyFile.startAccessingSecurityScopedResource()
        defer { if scoped { keyFile.stopAccessingSecurityScopedResource() } }
        do {
            try await APIClient.shared.saveAPNs(keyFile: keyFile, teamID: teamID, keyID: keyID, bundleID: bundleID)
            self.keyFile = nil
            message = nil
        } catch { message = error.localizedDescription }
        await load()
    }

    private func testLAN() async {
        guard let base = urls.first?.url, let url = URL(string: base + "/api/ping") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 5
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        do {
            let (_, resp) = try await URLSession.shared.data(for: req)
            testResult = (resp as? HTTPURLResponse)?.statusCode == 200
                ? "Reachable" : "Rejected: the token did not match"
        } catch {
            testResult = "Blocked: check System Settings > Network > Firewall"
        }
    }

    private func pref(_ key: String, _ value: Any?) {
        guard loaded else { return }
        Task { await APIClient.shared.setPref(key, value: value) }
    }

    private func pulse(_ body: [String: Any]) {
        guard loaded else { return }
        Task { _ = try? await APIClient.shared.sendJSON("api/pulse", body: body) }
    }

    private func copy(_ s: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(s, forType: .string)
    }

    // MARK: QR

    /// Fallback when the backend sends no pair_uri; same shape the iPhone parses.
    static func pairLink(url: String, token: String) -> String {
        var c = URLComponents()
        c.scheme = "joebro"
        c.host = "pair"
        c.queryItems = [URLQueryItem(name: "u", value: url), URLQueryItem(name: "t", value: token)]
        return c.string ?? ""
    }

    static func qr(_ text: String) -> NSImage? {
        guard !text.isEmpty else { return nil }
        let f = CIFilter.qrCodeGenerator()
        f.message = Data(text.utf8)
        f.correctionLevel = "M"
        guard let out = f.outputImage?.transformed(by: CGAffineTransform(scaleX: 10, y: 10)) else { return nil }
        let rep = NSCIImageRep(ciImage: out)
        let img = NSImage(size: rep.size)
        img.addRepresentation(rep)
        return img
    }
}
