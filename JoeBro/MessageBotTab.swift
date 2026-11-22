import SwiftUI

/// Settings page for the Telegram control bot. Config is stored as backend prefs
/// (read once on appear, written per-field on change), so the always-on backend's
/// poll loop picks changes up live — no restart, no app-side bot logic.
///
/// The bot is an ORCHESTRATOR: it manages and delegates across your chats/agents
/// and never codes or edits files itself — so there's no per-bot file-access or
/// mode setting here. It has full permission across all chats; the per-chat
/// permission it sets when delegating governs what each delegated run may do.
struct MessageBotTab: View {
    @Environment(AppStore.self) private var store

    @State private var enabled = false
    @State private var token = ""
    @State private var allowedIDs = ""
    @State private var selectedModel = ""   // ModelChoice.id, or "" for default
    @State private var systemPrompt = ""
    @State private var askPermissions = false
    @State private var showSkills = false
    @State private var showMemories = false
    @State private var showTools = true
    @State private var showPlugins = false
    @State private var loaded = false   // gate writes until the initial load finishes

    var body: some View {
        Form {
            Section("Telegram") {
                Toggle("Enable Message Bot", isOn: $enabled).jbToggle()
                SecureField("Bot token", text: $token).font(.jb(12))
                TextField("Allowed Telegram IDs (comma-separated)", text: $allowedIDs)
                Text("Token from @BotFather. Leave IDs blank to allow anyone.")
                    .font(.jbCaption)
                    .foregroundStyle(JB.ink3)
            }
            Section("Model") {
                Picker("Model", selection: $selectedModel) {
                    Text("Default model").tag("")
                    ModelPickerItems(models: store.models)
                }
                Toggle("Ask before commands & edits", isOn: $askPermissions).jbToggle()
            }
            Section("System prompt") {
                TextEditor(text: $systemPrompt)
                    .padding(6).jbSurface(.raised, radius: JB.rControl)
                    .font(.jb(12))
                    .scrollContentBackground(.hidden)
                    .frame(height: 110)
            }
            Section("Show in replies") {
                Toggle("Skills used", isOn: $showSkills).jbToggle()
                Toggle("Memories used", isOn: $showMemories).jbToggle()
                Toggle("Tool calls", isOn: $showTools).jbToggle()
                Toggle("Plugins used", isOn: $showPlugins).jbToggle()
            }
        }
        .formStyle(.grouped)
        .scrollContentBackground(.hidden)
        .task {
            if store.models.isEmpty { await store.loadModels() }
            await load()
        }
        .onChange(of: enabled) { save("bot_enabled", enabled) }
        .onChange(of: token) { save("bot_token", token.trimmingCharacters(in: .whitespacesAndNewlines)) }
        .onChange(of: allowedIDs) { save("bot_allowed_ids", allowedIDs) }
        .onChange(of: selectedModel) { saveModel() }
        .onChange(of: systemPrompt) { save("bot_system_prompt", systemPrompt) }
        .onChange(of: askPermissions) { save("bot_ask_permissions", askPermissions) }
        .onChange(of: showSkills) { save("bot_show_skills", showSkills) }
        .onChange(of: showMemories) { save("bot_show_memories", showMemories) }
        .onChange(of: showTools) { save("bot_show_tools", showTools) }
        .onChange(of: showPlugins) { save("bot_show_plugins", showPlugins) }
    }

    private func load() async {
        defer { loaded = true }
        guard let p = try? await APIClient.shared.getPrefs() else { return }
        enabled = p["bot_enabled"]?.boolValue ?? false
        token = p["bot_token"]?.stringValue ?? ""
        allowedIDs = p["bot_allowed_ids"]?.stringValue ?? ""
        systemPrompt = p["bot_system_prompt"]?.stringValue ?? ""
        askPermissions = p["bot_ask_permissions"]?.boolValue ?? false
        showSkills = p["bot_show_skills"]?.boolValue ?? false
        showMemories = p["bot_show_memories"]?.boolValue ?? false
        showTools = p["bot_show_tools"]?.boolValue ?? true
        showPlugins = p["bot_show_plugins"]?.boolValue ?? false
        let model = p["bot_model"]?.stringValue ?? ""
        let ep = p["bot_endpoint_id"]?.stringValue ?? ""
        selectedModel = (model.isEmpty && ep.isEmpty) ? "" : "\(model)@\(ep)"
    }

    private func saveModel() {
        guard loaded else { return }
        if let m = store.models.first(where: { $0.id == selectedModel }) {
            Task { await APIClient.shared.setPref("bot_model", value: m.modelID)
                   await APIClient.shared.setPref("bot_endpoint_id", value: m.endpointID) }
        } else {
            Task { await APIClient.shared.setPref("bot_model", value: "")
                   await APIClient.shared.setPref("bot_endpoint_id", value: "") }
        }
    }

    private func save(_ key: String, _ value: Any?) {
        guard loaded else { return }
        Task { await APIClient.shared.setPref(key, value: value) }
    }
}
