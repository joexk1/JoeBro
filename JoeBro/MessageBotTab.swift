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
            Section {
                Text("Control JoeBro from Telegram — your pocket orchestrator over every chat and agent.")
                    .font(.system(size: 12, weight: .semibold))
                Text("Message the bot and it manages your work: it searches and reads across all your chats, creates chats, binds them to folders, sets their mode and file-access level, and delegates coding/file jobs to them — reporting back what happened. It also uses your email, calendar, memory, web search and deep research directly. It does NOT write code or edit files itself; it hands that to a chat. Reply y/n to approve actions; send /chats to list your chats, or /compact to trim this conversation.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("Telegram") {
                Toggle("Enable Message Bot", isOn: $enabled)
                SecureField("Bot token", text: $token)
                TextField("Allowed Telegram IDs (comma-separated)", text: $allowedIDs)
                Text("Create a bot with @BotFather and paste its token here. Message your bot once — if you're not on the allowed list it replies with your numeric ID to add. Leaving IDs blank allows anyone who finds the bot (not recommended).")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("Model") {
                Picker("Model", selection: $selectedModel) {
                    Text("Default model").tag("")
                    ForEach(modelGroups, id: \.endpoint) { group in
                        Section(group.endpoint) {
                            ForEach(group.models) { m in
                                Text(m.displayWithTag).tag(m.id)
                            }
                        }
                    }
                }
                Toggle("Ask before commands & edits", isOn: $askPermissions)
                Text("When on, anything the bot delegates that needs Full Access (a command) or edits an open document asks you to approve it in Telegram — reply y or n.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("System prompt") {
                TextEditor(text: $systemPrompt)
                    .font(.system(size: 12, design: .monospaced))
                    .frame(height: 110)
                    .scrollContentBackground(.hidden)
                Text("Extra instructions appended to the bot's built-in orchestrator prompt. Leave blank for the default behaviour.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section("Show in replies") {
                Toggle("Skills used", isOn: $showSkills)
                Toggle("Memories used", isOn: $showMemories)
                Toggle("Tool calls", isOn: $showTools)
                Toggle("Plugins used", isOn: $showPlugins)
            }
        }
        .formStyle(.grouped)
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

    // Endpoint-grouped like the app's other model pickers (local machines first).
    private var modelGroups: [(endpoint: String, models: [ModelChoice])] {
        var order: [String] = []
        var buckets: [String: [ModelChoice]] = [:]
        for m in store.models {
            if buckets[m.endpointName] == nil { order.append(m.endpointName) }
            buckets[m.endpointName, default: []].append(m)
        }
        order.sort { a, b in
            let la = buckets[a]?.first?.category == "local"
            let lb = buckets[b]?.first?.category == "local"
            if la != lb { return la }
            return a < b
        }
        return order.map { ($0, buckets[$0] ?? []) }
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
