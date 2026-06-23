import EventKit
import SwiftUI

struct SettingsView: View {
    @Environment(AppStore.self) private var store
    @ObservedObject private var updater = UpdaterManager.shared
    @State private var autoUpdate = UpdaterManager.shared.automaticallyChecksForUpdates
    @AppStorage("serverURL") private var serverURL = "http://127.0.0.1:8765"
    @AppStorage("emailLoadCount") private var emailLoadCount = 0
    @State private var showWallpaperImporter = false
    @State private var showEmailSheet = false
    @State private var showCalDAVSheet = false
    @State private var emailStatus: IntegrationStatus?
    @State private var calendarStatus: IntegrationStatus?
    @State private var integrationError: String?
    @State private var requestingCalendar = false

    var body: some View {
        TabView {
            generalTab
                .tabItem { Label("General", systemImage: "gearshape") }
            ModelsSettingsTab()
                .tabItem { Label("Models", systemImage: "cpu") }
            appearanceTab
                .tabItem { Label("Appearance", systemImage: "paintbrush") }
        }
        .frame(width: 560, height: 480)
    }

    private var generalTab: some View {
        @Bindable var store = store
        return Form {
            Section("Email") {
                if let sources = emailStatus?.sources, !sources.isEmpty {
                    ForEach(sources) { source in
                        HStack {
                            Image(systemName: "envelope")
                                .foregroundStyle(.secondary)
                            Text(source.display ?? "Mailbox")
                            Spacer()
                            Button("Remove", role: .destructive) {
                                Task {
                                    try? await APIClient.shared.removeEmailSource(id: source.id)
                                    await loadIntegrationStatus()
                                }
                            }
                            .controlSize(.small)
                        }
                    }
                } else {
                    LabeledContent("Status", value: "Not connected")
                }
                Button("Add Email Account…") { showEmailSheet = true }
                Picker("Emails to load", selection: $emailLoadCount) {
                    Text("All").tag(0)
                    Text("50").tag(50)
                    Text("100").tag(100)
                    Text("200").tag(200)
                    Text("500").tag(500)
                }
                .pickerStyle(.menu)
                Text("Mail stays local on this Mac. Connect any mailbox with its IMAP/SMTP settings (use an app-specific password for Gmail/iCloud).")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("Calendar") {
                LabeledContent("Status", value: calendarStatusLabel)
                HStack {
                    Button("Connect CalDAV…") { showCalDAVSheet = true }
                    Button {
                        connectMacOSCalendar()
                    } label: {
                        if requestingCalendar { ProgressView().controlSize(.small) }
                        else { Text("Connect macOS Calendar") }
                    }
                    .disabled(requestingCalendar)
                    if calendarStatus?.configured == true {
                        Button("Disconnect", role: .destructive) {
                            Task { await disconnectCalendar() }
                        }
                    }
                }
                Text("macOS Calendar asks for full calendar permission so JoeBro can read, create, edit, and delete events locally.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if let integrationError {
                Section {
                    Text(integrationError)
                        .font(.caption)
                        .foregroundStyle(.red)
                }
            }
            Section("Server") {
                TextField("Server URL", text: $serverURL)
                    .font(.system(size: 13, design: .monospaced))
                Text("JoeBro runs a private local backend on this Mac. Model endpoints can still point at local or Tailscale devices.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("Documents") {
                Toggle("Require approval for AI edits", isOn: Binding(
                    get: { store.requireDocApproval },
                    set: {
                        store.requireDocApproval = $0
                        UserDefaults.standard.set($0, forKey: "requireDocApproval")
                    }
                ))
                Text("When on, the AI's changes to an open document wait for your Apply/Reject — review them in the editor. When off, the AI edits the open document directly, whether you're editing it or just viewing it.")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Toggle("Unlimited tool use", isOn: Binding(
                    get: { store.maxToolCalls == 0 },
                    set: { store.maxToolCalls = $0 ? 0 : 10 }
                ))
                if store.maxToolCalls > 0 {
                    HStack {
                        Text("Tool-use limit per turn")
                        Spacer()
                        TextField("", value: Binding(
                            get: { store.maxToolCalls },
                            set: { store.maxToolCalls = max(1, $0) }
                        ), format: .number)
                        .labelsHidden()
                        .multilineTextAlignment(.trailing)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 64)
                    }
                }
                Text("Caps how many tools the agent may call in a single turn before it must answer.")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Toggle("Ask before running commands", isOn: Binding(
                    get: { store.askBeforeCommands },
                    set: { store.askBeforeCommands = $0 }
                ))
                Text("In Full Access mode, the AI must get your approval (Allow / Always allow this session / Deny) before each terminal command.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("Updates") {
                Toggle("Check for updates automatically", isOn: Binding(
                    get: { autoUpdate },
                    set: { autoUpdate = $0; updater.automaticallyChecksForUpdates = $0 }
                ))
                HStack {
                    Button("Check for Updates…") { updater.checkForUpdates() }
                        .disabled(!updater.canCheckForUpdates)
                    Spacer()
                    Text("Version \(appVersionString)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Text("JoeBro checks GitHub for new releases and installs them in place. The first launch of an update may need the same right-click → Open as the original install.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .task { await loadIntegrationStatus() }
        .sheet(isPresented: $showEmailSheet) {
            EmailConnectionSheet {
                await loadIntegrationStatus()
                store.prefetchEmail()
            }
        }
        .sheet(isPresented: $showCalDAVSheet) {
            CalDAVConnectionSheet {
                await loadIntegrationStatus()
            }
        }
    }

    private var appVersionString: String {
        let v = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
        let b = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? "?"
        return "\(v) (\(b))"
    }

    private var calendarStatusLabel: String {
        guard let calendarStatus, calendarStatus.configured else { return "Not connected" }
        switch calendarStatus.type {
        case "macos": return calendarStatus.macosAuthorized == true ? "macOS Calendar" : "macOS Calendar needs permission"
        case "caldav":
            if let display = calendarStatus.display, !display.isEmpty { return "CalDAV: \(display)" }
            return "CalDAV"
        default:
            return calendarStatus.display ?? "Connected"
        }
    }

    private func loadIntegrationStatus() async {
        emailStatus = try? await APIClient.shared.emailStatus()
        calendarStatus = try? await APIClient.shared.calendarStatus()
    }

    private func disconnectEmail() async {
        do {
            try await APIClient.shared.disconnectEmail()
            await loadIntegrationStatus()
            store.emailCache = nil
            store.emailFoldersCache = nil
        } catch {
            integrationError = error.localizedDescription
        }
    }

    private func disconnectCalendar() async {
        do {
            try await APIClient.shared.disconnectCalendar()
            await loadIntegrationStatus()
        } catch {
            integrationError = error.localizedDescription
        }
    }

    private func connectMacOSCalendar() {
        requestingCalendar = true
        integrationError = nil
        Task {
            defer { requestingCalendar = false }
            do {
                let granted = await MacCalendarBridge.shared.requestFullAccess()
                guard granted else {
                    integrationError = "Calendar permission was not granted."
                    return
                }
                try await APIClient.shared.connectMacOSCalendar(authorized: true)
                await loadIntegrationStatus()
            } catch {
                integrationError = error.localizedDescription
            }
        }
    }

    private var appearanceTab: some View {
        @Bindable var glass = GlassSettings.shared
        return Form {
            let _ = glass.backgroundPreset   // observe preset changes
            Section("Glass") {
                Text("Glass opacity is tuned from the main window — the ◐ button next to the gear in the sidebar. (macOS renders glass differently while this Settings window has focus, so a slider here would lie to you.)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("Background") {
                HStack(spacing: 12) {
                    ForEach(BackgroundPreset.allCases) { preset in
                        Button {
                            withAnimation(.spring(duration: 0.25)) { glass.backgroundPreset = preset.rawValue }
                        } label: {
                            VStack(spacing: 5) {
                                Circle()
                                    .fill(LinearGradient(colors: preset.colors(dark: true),
                                                         startPoint: .topLeading, endPoint: .bottomTrailing))
                                    .frame(width: 34, height: 34)
                                    .overlay(Circle().strokeBorder(
                                        glass.backgroundPreset == preset.rawValue ? Color.accentColor : .clear,
                                        lineWidth: 2.5))
                                Text(preset.label)
                                    .font(.system(size: 10))
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .buttonStyle(.borderless)
                    }
                }
                Text("Used when no wallpaper is set — the glass panels refract whichever backdrop you pick.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("Wallpaper") {
                if let url = store.wallpaperURL, let img = NSImage(contentsOf: url) {
                    Image(nsImage: img)
                        .resizable()
                        .scaledToFill()
                        .frame(height: 110)
                        .clipShape(RoundedRectangle(cornerRadius: 10))
                }
                HStack {
                    Button(store.wallpaperURL == nil ? "Choose Image…" : "Change Image…") {
                        showWallpaperImporter = true
                    }
                    if store.wallpaperURL != nil {
                        Button("Remove", role: .destructive) {
                            store.clearWallpaper()
                        }
                    }
                }
                Text("Panels render with liquid glass over the wallpaper.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .fileImporter(isPresented: $showWallpaperImporter,
                      allowedContentTypes: [.image]) { result in
            if case .success(let url) = result {
                let scoped = url.startAccessingSecurityScopedResource()
                store.setWallpaper(from: url)
                if scoped { url.stopAccessingSecurityScopedResource() }
            }
        }
    }
}

// MARK: - Models tab (local endpoints, visibility, defaults)

struct ModelsSettingsTab: View {
    @Environment(AppStore.self) private var store
    @State private var endpoints: Loadable<[EndpointInfo]> = .loading
    @State private var expandedEp: String?
    @State private var epModels: [String: [EndpointModelInfo]] = [:]
    @State private var showAdd = false
    @State private var defaultModelID: String?
    @State private var error: String?

    var body: some View {
        Form {
            Section {
                LabeledContent("Default model") {
                    Menu {
                        ModelMenuItems(models: store.models,
                                       selectedID: defaultModelID,
                                       onPick: { setDefault($0.id) })
                        Divider()
                        Button("None") { setDefault("") }
                        Button("Refresh models") { Task { await store.loadModels() } }
                    } label: {
                        HStack(spacing: 5) {
                            if let m = store.models.first(where: { $0.id == defaultModelID }) {
                                ProviderLogoView(model: m.modelID, size: 11)
                                    .foregroundStyle(.secondary)
                                Text(m.displayWithTag)
                            } else {
                                Text("None")
                            }
                            Image(systemName: "chevron.up.chevron.down")
                                .font(.system(size: 8, weight: .bold))
                                .foregroundStyle(.secondary)
                        }
                        .font(.system(size: 12))
                    }
                    .menuStyle(.borderlessButton)
                    .menuIndicator(.hidden)
                    .fixedSize()
                }
                Text("Used for compaction, calendar quick-add and other background AI jobs.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } header: {
                Text("Defaults")
            }

            Section {
                switch endpoints {
                case .loading:
                    ProgressView()
                case .failed(let e):
                    Text(e).foregroundStyle(.red).font(.caption)
                case .loaded(let list):
                    appleRow
                    ForEach(list) { ep in
                        endpointRow(ep)
                    }
                }
                Button {
                    showAdd = true
                } label: {
                    Label("Add Endpoint…", systemImage: "plus")
                }
                if let error {
                    Text(error).font(.caption).foregroundStyle(.red)
                }
            } header: {
                Text("Endpoints & API Keys")
            } footer: {
                Text("Untick a model to hide it from the picker.")
                    .font(.caption)
            }
        }
        .formStyle(.grouped)
        .task { await load() }
        .sheet(isPresented: $showAdd) {
            AddEndpointSheet {
                await load()
                await store.loadModels()
            }
        }
    }

    private var appleRow: some View {
        HStack {
            Image(systemName: "apple.logo")
                .font(.system(size: 12))
                .frame(width: 16)
            Text("Apple Intelligence")
                .font(.system(size: 13, weight: .medium))
            Text("on-device")
                .font(.system(size: 10))
                .padding(.horizontal, 6)
                .padding(.vertical, 1)
                .background(.quaternary.opacity(0.6), in: Capsule())
            Spacer()
            Toggle("", isOn: Binding(
                get: { !UserDefaults.standard.bool(forKey: "hideAppleIntelligence") },
                set: { visible in
                    UserDefaults.standard.set(!visible, forKey: "hideAppleIntelligence")
                    Task { await store.loadModels() }
                }
            ))
            .toggleStyle(.switch)
            .controlSize(.mini)
            .help("Show in the model picker")
        }
    }

    private func load() async {
        do { endpoints = .loaded(try await APIClient.shared.modelEndpoints()) }
        catch { endpoints = .failed(error.localizedDescription) }
        if let prefs = try? await APIClient.shared.getPrefs() {
            let model = prefs["default_model"]?.stringValue
            let ep = prefs["default_endpoint_id"]?.stringValue
            if let model, let ep { defaultModelID = model + "@" + ep }
        }
    }

    private func setDefault(_ id: String) {
        defaultModelID = id.isEmpty ? nil : id
        let choice = store.models.first { $0.id == id }
        Task {
            await APIClient.shared.setPref("default_model", value: choice?.modelID)
            await APIClient.shared.setPref("default_endpoint_id", value: choice?.endpointID)
        }
    }

    @ViewBuilder
    private func endpointRow(_ ep: EndpointInfo) -> some View {
        DisclosureGroup(isExpanded: Binding(
            get: { expandedEp == ep.id },
            set: { open in
                expandedEp = open ? ep.id : nil
                if open && epModels[ep.id] == nil {
                    Task { epModels[ep.id] = (try? await APIClient.shared.endpointModels(epID: ep.id)) ?? [] }
                }
            }
        )) {
            if let models = epModels[ep.id] {
                if models.isEmpty {
                    Text("No models discovered").font(.caption).foregroundStyle(.secondary)
                } else {
                    HStack {
                        Button("Select all") { setAll(epID: ep.id, visible: true) }
                        Button("Deselect all") { setAll(epID: ep.id, visible: false) }
                        Spacer()
                    }
                    .buttonStyle(.borderless)
                    .font(.system(size: 11))
                    .foregroundStyle(Color.accentColor)
                }
                ForEach(models) { m in
                    Toggle(isOn: Binding(
                        get: { !(epModels[ep.id]?.first { $0.id == m.id }?.isHidden ?? false) },
                        set: { visible in
                            toggleVisibility(epID: ep.id, modelID: m.id, visible: visible)
                        }
                    )) {
                        Text(m.display ?? m.id)
                            .font(.system(size: 12))
                    }
                    .toggleStyle(.checkbox)
                }
            } else {
                ProgressView().controlSize(.small)
            }
        } label: {
            HStack {
                Text(ep.name ?? ep.id)
                    .font(.system(size: 13, weight: .medium))
                if let cat = ep.category {
                    Text(cat)
                        .font(.system(size: 10))
                        .padding(.horizontal, 6)
                        .padding(.vertical, 1)
                        .background(.quaternary.opacity(0.6), in: Capsule())
                }
                Spacer()
                Button(role: .destructive) {
                    Task {
                        do {
                            try await APIClient.shared.deleteModelEndpoint(id: ep.id)
                            await load()
                            await store.loadModels()
                        } catch {
                            self.error = "Delete: " + error.localizedDescription
                        }
                    }
                } label: {
                    Image(systemName: "trash")
                        .font(.system(size: 11))
                }
                .buttonStyle(.borderless)
            }
        }
    }

    private func setAll(epID: String, visible: Bool) {
        guard var models = epModels[epID] else { return }
        for i in models.indices { models[i].isHidden = !visible }
        epModels[epID] = models
        let hidden = visible ? [] : models.map(\.id)
        Task {
            try? await APIClient.shared.setHiddenModels(epID: epID, hidden: hidden)
            await store.loadModels()
        }
    }

    private func toggleVisibility(epID: String, modelID: String, visible: Bool) {
        guard var models = epModels[epID] else { return }
        if let i = models.firstIndex(where: { $0.id == modelID }) {
            models[i].isHidden = !visible
            epModels[epID] = models
        }
        let hidden = models.filter { $0.isHidden == true }.map(\.id)
        Task {
            do {
                try await APIClient.shared.setHiddenModels(epID: epID, hidden: hidden)
                await store.loadModels()
            } catch {
                self.error = "Visibility: " + error.localizedDescription
            }
        }
    }
}

struct AddEndpointSheet: View {
    let onSaved: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var baseURL = ""
    @State private var apiKey = ""
    @State private var saving = false
    @State private var error: String?

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Add Endpoint").font(.system(size: 14, weight: .semibold))
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button {
                    save()
                } label: {
                    if saving { ProgressView().controlSize(.small) }
                    else { Text("Add") }
                }
                .buttonStyle(.glassProminent)
                .tint(Color.accentColor)
                .disabled(saving || baseURL.isEmpty)
            }
            .padding(14)
            Divider()
            Form {
                TextField("Name (e.g. Groq, OpenRouter)", text: $name)
                TextField("Base URL (https://api.../v1)", text: $baseURL)
                    .font(.system(size: 12, design: .monospaced))
                SecureField("API key (optional for local)", text: $apiKey)
                if let error {
                    Text(error).font(.caption).foregroundStyle(.red)
                }
            }
            .formStyle(.grouped)
        }
        .frame(width: 440, height: 260)
    }

    private func save() {
        saving = true
        error = nil
        Task {
            defer { saving = false }
            do {
                try await APIClient.shared.addModelEndpoint(name: name, baseURL: baseURL, apiKey: apiKey)
                await onSaved()
                dismiss()
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}

struct EmailConnectionSheet: View {
    let onSaved: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var email = ""
    @State private var password = ""
    @State private var imapHost = ""
    @State private var imapPort = "993"
    @State private var smtpHost = ""
    @State private var smtpPort = "465"
    @State private var saving = false
    @State private var error: String?

    private var canSave: Bool {
        let address = email.trimmingCharacters(in: .whitespacesAndNewlines)
        return !address.isEmpty && !password.isEmpty && !imapHost.isEmpty && Int(imapPort) != nil
            && !smtpHost.isEmpty && Int(smtpPort) != nil
    }

    var body: some View {
        ConnectionSheetChrome(title: "Connect Email", saving: saving,
                              canSave: canSave,
                              onCancel: { dismiss() }, onSave: save) {
            TextField("Email address", text: $email)
                .textContentType(.emailAddress)
            SecureField("Password (or app-specific password)", text: $password)
            Grid(alignment: .leading, horizontalSpacing: 8, verticalSpacing: 8) {
                GridRow {
                    TextField("IMAP host", text: $imapHost)
                        .font(.system(size: 12, design: .monospaced))
                    TextField("Port", text: $imapPort)
                        .frame(width: 70)
                }
                GridRow {
                    TextField("SMTP host", text: $smtpHost)
                        .font(.system(size: 12, design: .monospaced))
                    TextField("Port", text: $smtpPort)
                        .frame(width: 70)
                }
            }
            Text("Uses SSL by default (IMAP 993 / SMTP 465). For Gmail/iCloud, create an app-specific password. Mail stays local on this Mac.")
                .font(.caption)
                .foregroundStyle(.secondary)
            if let error {
                Text(error).font(.caption).foregroundStyle(.red)
            }
        }
    }

    private func save() {
        saving = true
        error = nil
        Task {
            defer { saving = false }
            do {
                try await APIClient.shared.connectIMAP(
                    email: email,
                    imapHost: imapHost,
                    imapPort: Int(imapPort) ?? 993,
                    imapPassword: password,
                    smtpHost: smtpHost,
                    smtpPort: Int(smtpPort) ?? 465,
                    smtpPassword: password)
                await onSaved()
                dismiss()
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}

struct CalDAVConnectionSheet: View {
    let onSaved: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var url = ""
    @State private var principal = ""
    @State private var password = ""
    @State private var saving = false
    @State private var error: String?

    var body: some View {
        ConnectionSheetChrome(title: "Connect CalDAV", saving: saving,
                              canSave: !url.isEmpty && !principal.isEmpty,
                              onCancel: { dismiss() }, onSave: save) {
            TextField("CalDAV URL", text: $url)
                .font(.system(size: 12, design: .monospaced))
            TextField("Principal", text: $principal)
            SecureField("Password", text: $password)
            Text("Calendar data remains local to this Mac; this stores the CalDAV source for the local backend.")
                .font(.caption)
                .foregroundStyle(.secondary)
            if let error { Text(error).font(.caption).foregroundStyle(.red) }
        }
    }

    private func save() {
        saving = true
        error = nil
        Task {
            defer { saving = false }
            do {
                try await APIClient.shared.connectCalDAV(url: url, principal: principal, password: password)
                await onSaved()
                dismiss()
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}

private struct ConnectionSheetChrome<Content: View>: View {
    let title: String
    let saving: Bool
    let canSave: Bool
    let onCancel: () -> Void
    let onSave: () -> Void
    @ViewBuilder var content: Content

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(title).font(.system(size: 14, weight: .semibold))
                Spacer()
                Button("Cancel", action: onCancel)
                    .keyboardShortcut(.cancelAction)
                Button {
                    onSave()
                } label: {
                    if saving { ProgressView().controlSize(.small) }
                    else { Text("Connect") }
                }
                .buttonStyle(.glassProminent)
                .tint(Color.accentColor)
                .disabled(saving || !canSave)
            }
            .padding(14)
            Divider()
            Form { content }
                .formStyle(.grouped)
        }
        .frame(width: 460, height: 390)
    }
}

@MainActor
final class MacCalendarBridge {
    static let shared = MacCalendarBridge()
    let store = EKEventStore()

    func requestFullAccess() async -> Bool {
        if authorizationAllowsEvents() { return true }
        return await withCheckedContinuation { continuation in
            if #available(macOS 14.0, *) {
                store.requestFullAccessToEvents { granted, _ in
                    continuation.resume(returning: granted)
                }
            } else {
                store.requestAccess(to: .event) { granted, _ in
                    continuation.resume(returning: granted)
                }
            }
        }
    }

    func authorizationAllowsEvents() -> Bool {
        let status = EKEventStore.authorizationStatus(for: .event)
        if #available(macOS 14.0, *) {
            return status == .fullAccess
        }
        return status.rawValue == 3
    }
}
