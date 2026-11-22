import EventKit
import SwiftUI
import UniformTypeIdentifiers

struct SettingsView: View {
    @Environment(AppStore.self) private var store
    @ObservedObject private var updater = UpdaterManager.shared
    @State private var autoUpdate = UpdaterManager.shared.automaticallyChecksForUpdates
    @AppStorage("serverURL") private var serverURL = "http://127.0.0.1:8765"
    @AppStorage("emailLoadCount") private var emailLoadCount = 0
    @State private var backendToken = BackendToken.load() ?? ""
    @State private var showWallpaperImporter = false
    @State private var showVideoImporter = false
    @State private var userBackdrops: [URL] = []
    @State private var backdropNote: String?
    @State private var showEmailSheet = false
    @State private var showCalDAVSheet = false
    @State private var emailStatus: IntegrationStatus?
    @State private var calendarStatus: IntegrationStatus?
    @State private var integrationError: String?
    @State private var requestingCalendar = false
    @State private var cloakMode = true

    @State private var section: SettingsSection = .general

    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 2) {
                ForEach(SettingsSection.allCases) { item in
                    Button {
                        withAnimation(JB.snap) { section = item }
                    } label: {
                        HStack(spacing: 10) {
                            Image(systemName: item.icon)
                                .font(.system(size: 13, weight: .regular))
                                .frame(width: 18)
                                .foregroundStyle(section == item ? JB.ink : JB.ink3)
                            Text(item.title)
                                .font(.jb(13, section == item ? .medium : .regular))
                                .foregroundStyle(section == item ? JB.ink : JB.ink2)
                            Spacer(minLength: 0)
                        }
                        .padding(.horizontal, 10)
                        .frame(height: 30)
                        .contentShape(Rectangle())
                        .jbHoverRow(selected: section == item)
                    }
                    .buttonStyle(.plain)
                }
                Spacer()
            }
            .padding(.horizontal, 10)
            .padding(.top, 14)
            .frame(width: 196)
            .background(JB.surface)

            Rectangle().fill(JB.line).frame(width: 1)

            Group {
                switch section {
                case .general: generalTab
                case .appearance: appearanceTab
                case .models: ModelsSettingsTab()
                case .agents: agentsTab
                case .mobile: MobileSettingsTab()
                case .integrations: integrationsTab
                case .messageBot: MessageBotTab()
                case .advanced: advancedTab
                }
            }
            .formStyle(.grouped)
            .scrollContentBackground(.hidden)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(JB.bg)
            .id(section)
            .transition(.opacity)
        }
        .frame(width: 780, height: 600)
        .background(JB.bg)
        .navigationTitle(section.title)
    }

    // MARK: General

    private var generalTab: some View {
        Form {
            Section("Privacy") {
                Toggle("Cloak mode", isOn: $cloakMode)
                    .onChange(of: cloakMode) { _, v in
                        Task { await APIClient.shared.setPref("cloak_mode", value: v) }
                    }
                Text("Scrubs secrets and personal data before anything reaches a model or search engine.")
                    .font(.jbCaption)
                    .foregroundStyle(JB.ink3)
            }
            Section("Updates") {
                Toggle("Check automatically", isOn: Binding(
                    get: { autoUpdate },
                    set: { autoUpdate = $0; updater.automaticallyChecksForUpdates = $0 }
                ))
                HStack {
                    Button("Check for updates…") { updater.checkForUpdates() }
                        .disabled(!updater.canCheckForUpdates)
                    Spacer()
                    Text(appVersionString)
                        .font(.jb(11))
                        .foregroundStyle(JB.ink3)
                }
            }
        }
        .formStyle(.grouped)
        .task {
            if let p = try? await APIClient.shared.getPrefs() {
                cloakMode = p["cloak_mode"]?.boolValue ?? true
            }
        }
    }

    // MARK: Agents

    private var agentsTab: some View {
        @Bindable var store = store
        return Form {
            Section("Documents") {
                Toggle("Require approval for AI edits", isOn: $store.requireDocApproval)
            }
            Section("Tools") {
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
                        .font(.jb(12))
                        .frame(width: 64)
                    }
                }
                Toggle("Ask before running commands", isOn: $store.askBeforeCommands)
            }
        }
        .formStyle(.grouped)
    }

    // MARK: Integrations

    private var integrationsTab: some View {
        Form {
            Section("Email") {
                if let sources = emailStatus?.sources, !sources.isEmpty {
                    ForEach(sources) { source in
                        HStack {
                            Image(systemName: "envelope")
                                .foregroundStyle(JB.ink2)
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
                Button("Add email account…") { showEmailSheet = true }
                Picker("Emails to load", selection: $emailLoadCount) {
                    Text("All").tag(0)
                    Text("50").tag(50)
                    Text("100").tag(100)
                    Text("200").tag(200)
                    Text("500").tag(500)
                }
                .pickerStyle(.menu)
            }
            Section("Calendar") {
                LabeledContent("Status", value: calendarStatusLabel)
                HStack {
                    Button("Connect CalDAV…") { showCalDAVSheet = true }
                    Button {
                        connectMacOSCalendar()
                    } label: {
                        if requestingCalendar { JBSpinner() }
                        else { Text("Connect macOS Calendar") }
                    }
                    .disabled(requestingCalendar)
                    if calendarStatus?.configured == true {
                        Button("Disconnect", role: .destructive) {
                            Task { await disconnectCalendar() }
                        }
                    }
                }
            }
            if let integrationError {
                Section {
                    Text(integrationError)
                        .font(.jbCaption)
                        .foregroundStyle(JB.danger)
                }
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

    // MARK: Advanced

    private var advancedTab: some View {
        Form {
            Section("Server") {
                TextField("Server URL", text: $serverURL)
                    .font(.jb(12))
                SecureField("Bearer token (remote backend only)", text: $backendToken)
                    .font(.jb(12))
                    .onChange(of: backendToken) { _, v in BackendToken.save(v) }
            }
        }
        .formStyle(.grouped)
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

    // MARK: Appearance

    private var appearanceTab: some View {
        @Bindable var look = JBAppearance.shared
        return Form {
            Section("Palette") {
                JBPaletteRow(selection: $look.palette)
            }
            Section("Panels") {
                HStack {
                    Text("Opacity")
                    Slider(value: $look.solidity, in: 0.35...1)
                        .tint(JB.accent)
                    Text("\(Int(look.solidity * 100))%")
                        .font(.jb(11))
                        .foregroundStyle(JB.ink3)
                        .frame(width: 36, alignment: .trailing)
                }
            }
            Section("Backdrop") {
                Picker("Backdrop", selection: Binding(get: { look.backdrop }, set: { look.backdrop = $0; if $0 != .palette { look.palette = .auto } })) {
                    Text("Palette").tag(JBBackdropKind.palette)
                    Text("Photo").tag(JBBackdropKind.photo)
                    Text("Animated").tag(JBBackdropKind.scene)
                }
                .pickerStyle(.segmented)
                .labelsHidden()

                if look.backdrop == .photo {
                    if let url = store.wallpaperURL, let img = NSImage(contentsOf: url) {
                        Image(nsImage: img)
                            .resizable()
                            .scaledToFill()
                            .frame(height: 110)
                            .clipShape(RoundedRectangle(cornerRadius: JB.rCard))
                    }
                    HStack {
                        Button(store.wallpaperURL == nil ? "Choose image…" : "Change image…") {
                            showWallpaperImporter = true
                        }
                        if store.wallpaperURL != nil {
                            Button("Remove", role: .destructive) {
                                store.clearWallpaper()
                            }
                        }
                    }
                }
                if look.backdrop == .scene {
                    Picker("Scene", selection: $look.sceneID) {
                        Text("None").tag("")
                        if !userBackdrops.isEmpty {
                            Section("Yours") {
                                ForEach(userBackdrops, id: \.self) { u in
                                    Text(u.deletingPathExtension().lastPathComponent).tag(u.absoluteString)
                                }
                            }
                        }
                        Section("ASCII scenes") {
                            ForEach(JBBackdropLibrary.bundledScenes, id: \.self) { n in
                                Text(JBBackdropLibrary.displayName(n).capitalized).tag(n)
                            }
                        }
                    }
                    HStack {
                        Button("Add video…") { showVideoImporter = true }
                        Button("Open backdrops folder") {
                            NSWorkspace.shared.open(JBBackdropLibrary.folder)
                        }
                        Button("Rescan") { userBackdrops = JBBackdropLibrary.userBackdrops() }
                    }
                    HStack {
                        Button("Install backdrop skill for agents") { installBackdropSkill() }
                        if let backdropNote {
                            Text(backdropNote)
                                .font(.jb(11))
                                .foregroundStyle(JB.ink3)
                                .lineLimit(2)
                        }
                    }
                }
            }
        }
        .formStyle(.grouped)
        .onAppear { userBackdrops = JBBackdropLibrary.userBackdrops() }
        .fileImporter(isPresented: $showWallpaperImporter,
                      allowedContentTypes: [.image]) { result in
            if case .success(let url) = result {
                let scoped = url.startAccessingSecurityScopedResource()
                store.setWallpaper(from: url)
                if scoped { url.stopAccessingSecurityScopedResource() }
                JBAppearance.shared.backdrop = .photo; JBAppearance.shared.palette = .auto
            }
        }
        .fileImporter(isPresented: $showVideoImporter,
                      allowedContentTypes: [.mpeg4Movie, .quickTimeMovie]) { result in
            if case .success(let url) = result {
                do {
                    let id = try JBBackdropLibrary.importVideo(url)
                    userBackdrops = JBBackdropLibrary.userBackdrops()
                    JBAppearance.shared.sceneID = id
                } catch {
                    backdropNote = error.localizedDescription
                }
            }
        }
    }

    private func installBackdropSkill() {
        do {
            let dests = try JBBackdropLibrary.installSkill()
            backdropNote = "Installed to " + dests.joined(separator: ", ")
        } catch {
            backdropNote = error.localizedDescription
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
    @State private var subagentModelID: String?
    @State private var subagentAuto = false
    @State private var error: String?

    var body: some View {
        Form {
            Section {
                LabeledContent("Default model") {
                    HStack(spacing: 6) {
                        Picker("", selection: Binding(
                            get: { defaultModelID ?? "" },
                            set: { setDefault($0) }
                        )) {
                            Text("None").tag("")
                            ModelPickerItems(models: store.models)
                        }
                        .labelsHidden()
                        .fixedSize()
                        Button { Task { await store.loadModels() } } label: {
                            Image(systemName: "arrow.clockwise")
                        }
                        .buttonStyle(.borderless)
                        .help("Refresh models")
                    }
                }
                Text("Used for compaction, calendar quick-add and other background AI jobs.")
                    .font(.jbCaption)
                    .foregroundStyle(JB.ink2)
            } header: {
                Text("Defaults")
            }

            subagentsSection

            Section {
                switch endpoints {
                case .loading:
                    JBSpinner()
                case .failed(let e):
                    Text(e).foregroundStyle(JB.danger).font(.jbCaption)
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
                    Text(error).font(.jbCaption).foregroundStyle(JB.danger)
                }
            } header: {
                Text("Endpoints & API Keys")
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

    private var subagentsSection: some View {
        Section {
            LabeledContent("Subagent model") {
                Picker("", selection: Binding(
                    get: { subagentModelID ?? "" },
                    set: { setSubagentModel($0) }
                )) {
                    Text("Same as default").tag("")
                    ModelPickerItems(models: store.models)
                }
                .labelsHidden()
                .fixedSize()
                .disabled(subagentAuto)
                .opacity(subagentAuto ? 0.4 : 1)
            }
            Toggle("Let the main agent choose the model per task", isOn: Binding(
                get: { subagentAuto },
                set: { setSubagentAuto($0) }
            ))
            Text("Subagents are helpers the main agent spawns for side tasks. When the switch is on, the agent picks a model for each one and the fixed model above is not used.")
                .font(.jbCaption)
                .foregroundStyle(JB.ink2)
        } header: {
            Text("Subagents")
        }
    }

    private func setSubagentModel(_ id: String) {
        subagentModelID = id.isEmpty ? nil : id
        let choice = store.models.first { $0.id == id }
        Task {
            await APIClient.shared.setPref("subagent_model", value: choice?.modelID)
            await APIClient.shared.setPref("subagent_endpoint_id", value: choice?.endpointID)
        }
    }

    private func setSubagentAuto(_ on: Bool) {
        subagentAuto = on
        Task { await APIClient.shared.setPref("subagent_model_auto", value: on ? "true" : "false") }
    }

    private var appleRow: some View {
        HStack {
            Image(systemName: "apple.logo")
                .font(.jb(12))
                .frame(width: 16)
            Text("Apple Intelligence")
                .font(.jb(13, .medium))
            Text("on-device")
                .font(.jb(10))
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
            if let sm = prefs["subagent_model"]?.stringValue,
               let se = prefs["subagent_endpoint_id"]?.stringValue { subagentModelID = sm + "@" + se }
            let auto = prefs["subagent_model_auto"]
            subagentAuto = auto?.boolValue ?? (auto?.stringValue?.lowercased() == "true")
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
                    Text("No models discovered").font(.jbCaption).foregroundStyle(JB.ink2)
                } else {
                    HStack {
                        Button("Select all") { setAll(epID: ep.id, visible: true) }
                        Button("Deselect all") { setAll(epID: ep.id, visible: false) }
                        Spacer()
                    }
                    .buttonStyle(.borderless)
                    .font(.jb(11))
                    .foregroundStyle(JB.accent)
                }
                ForEach(models) { m in
                    Toggle(isOn: Binding(
                        get: { !(epModels[ep.id]?.first { $0.id == m.id }?.isHidden ?? false) },
                        set: { visible in
                            toggleVisibility(epID: ep.id, modelID: m.id, visible: visible)
                        }
                    )) {
                        Text(m.display ?? m.id)
                            .font(.jb(12))
                    }
                    .toggleStyle(.checkbox)
                }
            } else {
                JBSpinner()
            }
        } label: {
            HStack {
                Text(ep.name ?? ep.id)
                    .font(.jb(13, .medium))
                if let cat = ep.category {
                    Text(cat)
                        .font(.jb(10))
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
                        .font(.jb(11))
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
                Text("Add Endpoint").font(.jb(14, .semibold))
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button {
                    save()
                } label: {
                    if saving { JBSpinner() }
                    else { Text("Add") }
                }
                .buttonStyle(.jbPrimary)
                .tint(JB.accent)
                .disabled(saving || baseURL.isEmpty)
            }
            .padding(14)
            Divider()
            Form {
                TextField("Name (e.g. Groq, OpenRouter)", text: $name)
                TextField("Base URL (https://api.../v1)", text: $baseURL)
                    .font(.jb(12))
                SecureField("API key (optional for local)", text: $apiKey)
                if let error {
                    Text(error).font(.jbCaption).foregroundStyle(JB.danger)
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
                        .font(.jb(12))
                    TextField("Port", text: $imapPort)
                        .frame(width: 70)
                }
                GridRow {
                    TextField("SMTP host", text: $smtpHost)
                        .font(.jb(12))
                    TextField("Port", text: $smtpPort)
                        .frame(width: 70)
                }
            }
            Text("Uses SSL by default (IMAP 993 / SMTP 465). For Gmail/iCloud, create an app-specific password. Mail stays local on this Mac.")
                .font(.jbCaption)
                .foregroundStyle(JB.ink2)
            if let error {
                Text(error).font(.jbCaption).foregroundStyle(JB.danger)
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
                .font(.jb(12))
            TextField("Principal", text: $principal)
            SecureField("Password", text: $password)
            Text("Calendar data remains local to this Mac; this stores the CalDAV source for the local backend.")
                .font(.jbCaption)
                .foregroundStyle(JB.ink2)
            if let error { Text(error).font(.jbCaption).foregroundStyle(JB.danger) }
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
                Text(title).font(.jb(14, .semibold))
                Spacer()
                Button("Cancel", action: onCancel)
                    .keyboardShortcut(.cancelAction)
                Button {
                    onSave()
                } label: {
                    if saving { JBSpinner() }
                    else { Text("Connect") }
                }
                .buttonStyle(.jbPrimary)
                .tint(JB.accent)
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

    var isDeniedOrRestricted: Bool {
        let st = EKEventStore.authorizationStatus(for: .event)
        return st == .denied || st == .restricted
    }

    func authorizationAllowsEvents() -> Bool {
        let status = EKEventStore.authorizationStatus(for: .event)
        if #available(macOS 14.0, *) {
            return status == .fullAccess
        }
        return status.rawValue == 3
    }
}


enum SettingsSection: String, CaseIterable, Identifiable {
    case general, appearance, models, agents, mobile, integrations, messageBot, advanced
    var id: String { rawValue }
    var title: String {
        switch self {
        case .general: return "General"
        case .appearance: return "Appearance"
        case .models: return "Models & Keys"
        case .agents: return "Agents"
        case .mobile: return "Mobile"
        case .integrations: return "Integrations"
        case .messageBot: return "Message Bot"
        case .advanced: return "Advanced"
        }
    }
    var icon: String {
        switch self {
        case .general: return "switch.2"
        case .appearance: return "circle.lefthalf.filled"
        case .models: return "key"
        case .agents: return "terminal"
        case .mobile: return "iphone"
        case .integrations: return "link"
        case .messageBot: return "paperplane"
        case .advanced: return "wrench.adjustable"
        }
    }
}
