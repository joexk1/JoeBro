import SwiftUI
import AppKit

/// Tools workspace tab. The first section, "API Tools", lets the user register
/// any JSON API as a tool the model can call in Agent mode. The panel is laid
/// out as a stack of sections so a future "MCP" section can be added below the
/// API Tools section without restructuring.
struct ToolsPanel: View {
    @Environment(AppStore.self) private var store
    @State private var editing: APITool?
    @State private var showAdd = false
    @State private var showAddMCP = false
    @State private var editingPlugin: Plugin?
    @State private var editingMCP: MCPServer?
    @State private var configuringComputerUse = false
    @State private var configuringAdvisor = false

    var body: some View {
        PanelChrome(title: "Tools", icon: "wrench.and.screwdriver") {
            Button {
                Task {
                    await store.loadAPITools()
                    await store.loadMCPServers()
                    await store.loadPlugins()
                }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.jbIcon)
        } content: {
            // Top half: API tools (left) + MCP (right). Bottom half: Plugins,
            // a single wide box spanning the full width.
            VStack(spacing: 14) {
                HStack(alignment: .top, spacing: 14) {
                    apiToolsSection
                    mcpSection
                }
                .frame(maxHeight: .infinity)
                pluginsSection
                    .frame(maxHeight: .infinity)
            }
            .padding(2)
        }
        .task {
            await store.loadAPITools()
            await store.loadMCPServers()
            await store.loadPlugins()
        }
        .sheet(isPresented: $showAdd) {
            APIToolSheet(tool: nil)
        }
        .sheet(item: $editing) { tool in
            APIToolSheet(tool: tool)
        }
        .sheet(isPresented: $showAddMCP) {
            MCPServerSheet()
        }
        .sheet(item: $editingPlugin) { plugin in
            PluginEditSheet(plugin: plugin)
        }
        .sheet(item: $editingMCP) { server in
            MCPServerSheet(server: server)
        }
        .sheet(isPresented: $configuringComputerUse) {
            ComputerUseSettingsSheet()
        }
        .sheet(isPresented: $configuringAdvisor) {
            AdvisorSettingsSheet()
        }
    }

    private var pluginsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                JBSectionHeader("Plugins", count: store.plugins.count)
                Button { addPlugin() } label: {
                    Label("Add", systemImage: "plus")
                }
                .buttonStyle(JBButtonStyle(kind: .secondary, compact: true))
            }


            if store.plugins.isEmpty {
                Spacer()
                PanelEmpty(art: .plug, text: "No plugins yet")
                Spacer()
            } else {
                ScrollView {
                    LazyVStack(spacing: 8) {
                        ForEach(store.plugins) { plugin in
                            pluginRow(plugin)
                        }
                    }
                    .padding(.top, 2)
                }
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .jbSurface(.panel, radius: JB.rPanel)
    }

    private func pluginRow(_ plugin: Plugin) -> some View {
        HStack(spacing: 12) {
            Toggle("", isOn: Binding(
                get: { plugin.isEnabled ?? true },
                set: { newValue in store.togglePlugin(plugin, enabled: newValue) }
            ))
            .labelsHidden()
            .jbToggle()
            .controlSize(.small)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(plugin.name)
                        .font(.jb(13, .medium))
                    JBChip(text: plugin.kind == "background" ? "background" : "foreground")
                    if plugin.source == "bundled" { JBChip(text: "bundled", tint: JB.accent) }
                }
                if let d = plugin.description, !d.isEmpty {
                    Text(d)
                        .font(.jb(11))
                        .foregroundStyle(JB.ink2)
                        .lineLimit(2)
                }
                if !plugin.summary.isEmpty {
                    Text(plugin.summary)
                        .font(.jb(10.5))
                        .foregroundStyle(JB.ink3)
                }
                if let e = plugin.error, !e.isEmpty {
                    Text(e)
                        .font(.jb(10))
                        .foregroundStyle(JB.danger)
                        .lineLimit(3)
                }
            }

            Spacer()

            Button { editingPlugin = plugin } label: {
                Image(systemName: "pencil")
            }
            .buttonStyle(.jbIcon)
            .help("Edit")

            if plugin.id == "plugin_macos_use" {
                Button { configuringComputerUse = true } label: {
                    Image(systemName: "gearshape")
                }
                .buttonStyle(.jbIcon)
                .help("Settings")
            } else if plugin.id == "plugin_advisor" {
                Button { configuringAdvisor = true } label: {
                    Image(systemName: "gearshape")
                }
                .buttonStyle(.jbIcon)
                .help("Settings")
            } else if plugin.source != "bundled" {
                Button(role: .destructive) {
                    if JBConfirm.delete("plugin", plugin.name) { store.deletePlugin(id: plugin.id) }
                } label: {
                    Image(systemName: "trash")
                }
                .buttonStyle(.jbIcon)
                .help("Delete")
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .jbSurface(.raised, radius: JB.rCard)
    }

    /// Folder picker for adding a plugin: choose the plugin's repo folder and,
    /// in the same dialog, whether it's a foreground (active tool) or background
    /// (guardrail) plugin via an accessory control.
    private func addPlugin() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Add Plugin"
        panel.message = "Choose the folder containing the plugin's repo"
        let label = NSTextField(labelWithString: "Plugin type:")
        let seg = NSSegmentedControl(labels: ["Foreground (active tool)", "Background (guardrail)"],
                                     trackingMode: .selectOne, target: nil, action: nil)
        seg.segmentStyle = .roundRect
        seg.segmentDistribution = .fillEqually
        seg.selectedSegment = 0
        seg.setSelected(true, forSegment: 0)
        let stack = NSStackView(views: [label, seg])
        stack.orientation = .horizontal
        stack.spacing = 8
        stack.alignment = .centerY
        stack.edgeInsets = NSEdgeInsets(top: 10, left: 14, bottom: 10, right: 14)
        stack.frame = NSRect(x: 0, y: 0, width: 470, height: 44)
        panel.accessoryView = stack
        panel.isAccessoryViewDisclosed = true
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let kind = seg.selectedSegment == 1 ? "background" : "foreground"
        store.createPlugin(name: url.lastPathComponent, kind: kind, repoPath: url.path)
    }

    private var mcpSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                JBSectionHeader("MCP servers", count: store.mcpServers.count)
                if store.mcpBusy { JBSpinner() }
                Button {
                    showAddMCP = true
                } label: {
                    Label("Add", systemImage: "plus")
                }
                .buttonStyle(JBButtonStyle(kind: .secondary, compact: true))
            }


            if let err = store.mcpError, !err.isEmpty {
                Text(err)
                    .font(.jb(11))
                    .foregroundStyle(JB.danger)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }

            if store.mcpServers.isEmpty {
                Spacer()
                PanelEmpty(art: .plug, text: "No MCP servers yet")
                Spacer()
            } else {
                ScrollView {
                    LazyVStack(spacing: 8) {
                        ForEach(store.mcpServers) { server in
                            mcpRow(server)
                        }
                    }
                    .padding(.top, 2)
                }
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .jbSurface(.panel, radius: JB.rPanel)
    }

    private func mcpRow(_ server: MCPServer) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Toggle("", isOn: Binding(
                get: { server.enabled },
                set: { newValue in Task { await store.toggleMCPServer(server, enabled: newValue) } }
            ))
            .labelsHidden()
            .jbToggle()
            .controlSize(.small)

            VStack(alignment: .leading, spacing: 4) {
                Text(server.name)
                    .font(.jb(13, .medium))
                Text("\(server.command) \(server.args)")
                    .font(.jb(11))
                    .foregroundStyle(JB.ink2)
                    .lineLimit(1)
                    .truncationMode(.middle)
                if let err = server.error, !err.isEmpty {
                    Text(err)
                        .font(.jb(11))
                        .foregroundStyle(JB.danger)
                        .lineLimit(2)
                } else if server.tools.isEmpty {
                    Text("No tools")
                        .font(.jb(11))
                        .foregroundStyle(JB.ink3)
                } else {
                    Text(server.tools.joined(separator: ", "))
                        .font(.jb(11))
                        .foregroundStyle(JB.ink2)
                }
            }

            Spacer()

            Button { editingMCP = server } label: {
                Image(systemName: "pencil")
            }
            .buttonStyle(.jbIcon)
            .help("Edit")

            Button(role: .destructive) {
                if JBConfirm.delete("MCP server", server.name) { Task { await store.deleteMCPServer(id: server.id) } }
            } label: {
                Image(systemName: "trash")
            }
            .buttonStyle(.jbIcon)
            .help("Delete")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .jbSurface(.raised, radius: JB.rCard)
    }

    private var apiToolsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                JBSectionHeader("API tools", count: store.apiTools.count)
                Button {
                    showAdd = true
                } label: {
                    Label("Add", systemImage: "plus")
                }
                .buttonStyle(JBButtonStyle(kind: .secondary, compact: true))
            }


            if store.apiTools.isEmpty {
                Spacer()
                PanelEmpty(art: .plug, text: "No API tools yet")
                Spacer()
            } else {
                ScrollView {
                    LazyVStack(spacing: 8) {
                        ForEach(store.apiTools) { tool in
                            toolRow(tool)
                        }
                    }
                    .padding(.top, 2)
                }
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .jbSurface(.panel, radius: JB.rPanel)
    }

    private func toolRow(_ tool: APITool) -> some View {
        HStack(spacing: 12) {
            Toggle("", isOn: Binding(
                get: { tool.isEnabled ?? true },
                set: { newValue in Task { await store.toggleAPITool(tool, enabled: newValue) } }
            ))
            .labelsHidden()
            .jbToggle()
            .controlSize(.small)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(tool.name)
                        .font(.jb(13, .medium))
                    JBChip(text: (tool.method ?? "GET").uppercased())
                    if tool.hasKey == true {
                        Image(systemName: "key")
                            .font(.jb(10))
                            .foregroundStyle(JB.ink3)
                    }
                }
                if let url = tool.baseURL, !url.isEmpty {
                    Text(url)
                        .font(.jb(11))
                        .foregroundStyle(JB.ink2)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
            }

            Spacer()

            Button {
                editing = tool
            } label: {
                Image(systemName: "pencil")
            }
            .buttonStyle(.jbIcon)

            Button(role: .destructive) {
                if JBConfirm.delete("tool", tool.name) { Task { await store.deleteAPITool(id: tool.id) } }
            } label: {
                Image(systemName: "trash")
            }
            .buttonStyle(.jbIcon)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .jbSurface(.raised, radius: JB.rCard)
    }
}

/// Add / edit sheet for a custom API tool.
struct APIToolSheet: View {
    let tool: APITool?
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss

    @State private var name: String
    @State private var baseURL: String
    @State private var apiKey: String
    @State private var method: String
    @State private var description: String
    @State private var saving = false

    init(tool: APITool?) {
        self.tool = tool
        _name = State(initialValue: tool?.name ?? "")
        _baseURL = State(initialValue: tool?.baseURL ?? "")
        _apiKey = State(initialValue: "")
        _method = State(initialValue: (tool?.method ?? "GET").uppercased())
        _description = State(initialValue: tool?.description ?? "")
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(tool == nil ? "Add API Tool" : "Edit API Tool")
                    .font(.jb(14, .semibold))
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction).buttonStyle(.jbGhost)
                Button {
                    save()
                } label: {
                    if saving { JBSpinner(size: 12, tint: JB.onAccent) }
                    else { Text(tool == nil ? "Add" : "Save") }
                }
                .buttonStyle(.jbPrimary)
                .tint(JB.accent)
                .disabled(saving || name.trimmingCharacters(in: .whitespaces).isEmpty || baseURL.isEmpty)
            }
            .padding(14)
            Rectangle().fill(JB.line).frame(height: 1)
            Form {
                TextField("Name (e.g. LinkedIn Search)", text: $name)
                TextField("Base URL (https://api.example.com/v2/search)", text: $baseURL)
                    .font(.jb(12))
                Text("{query} in the URL marks where the input goes")
                    .font(.jbCaption2)
                    .foregroundStyle(JB.ink2)
                SecureField(tool?.hasKey == true ? "API key (leave blank to keep)" : "API key (optional)", text: $apiKey)
                Picker("Method", selection: $method) {
                    Text("GET").tag("GET")
                    Text("POST").tag("POST")
                }
                .pickerStyle(.segmented)
                VStack(alignment: .leading, spacing: 4) {
                    Text("Description")
                        .font(.jbCaption)
                        .foregroundStyle(JB.ink2)
                    TextEditor(text: $description)
                        .font(.jb(12))
                        .frame(minHeight: 90)
                    Text("The model reads this to know when and how to call the tool.")
                        .font(.jbCaption2)
                        .foregroundStyle(JB.ink2)
                }
            }
            .formStyle(.grouped)
        }
        .frame(width: 460, height: 440)
    }

    private func save() {
        saving = true
        Task {
            defer { saving = false }
            if let tool {
                var fields = [
                    "name": name,
                    "base_url": baseURL,
                    "method": method,
                    "description": description,
                ]
                // Only send the key when the user typed a new one, so editing
                // other fields never wipes the stored key.
                if !apiKey.isEmpty { fields["api_key"] = apiKey }
                await store.updateAPITool(id: tool.id, fields: fields)
            } else {
                await store.createAPITool(name: name, baseURL: baseURL, apiKey: apiKey,
                                          method: method, description: description)
            }
            dismiss()
        }
    }
}

/// Add sheet for an MCP server (stdio transport): a name, a launch command, and
/// its arguments. On save the backend launches the server and discovers its
/// tools, so this may take a moment the first time (npx may download a package).
struct MCPServerSheet: View {
    var server: MCPServer? = nil
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss

    @State private var name = ""
    @State private var command = ""
    @State private var args = ""
    @State private var permission = "sandbox"
    @State private var saving = false

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(server == nil ? "Add MCP Server" : "Edit MCP Server")
                    .font(.jb(14, .semibold))
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction).buttonStyle(.jbGhost)
                Button {
                    save()
                } label: {
                    if saving { JBSpinner(size: 12, tint: JB.onAccent) }
                    else { Text(server == nil ? "Add" : "Save") }
                }
                .buttonStyle(.jbPrimary)
                .tint(JB.accent)
                .disabled(saving
                          || name.trimmingCharacters(in: .whitespaces).isEmpty
                          || command.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            .padding(14)
            Rectangle().fill(JB.line).frame(height: 1)
            Form {
                TextField("Name (e.g. Filesystem)", text: $name)
                TextField("Command (e.g. npx)", text: $command)
                    .font(.jb(12))
                VStack(alignment: .leading, spacing: 4) {
                    Text("Arguments")
                        .font(.jbCaption)
                        .foregroundStyle(JB.ink2)
                    TextEditor(text: $args)
                        .font(.jb(12))
                        .frame(minHeight: 60)
                    Text("e.g. -y @modelcontextprotocol/server-filesystem /Users/me/Documents")
                        .font(.jbCaption2)
                        .foregroundStyle(JB.ink2)
                }
                Picker("Agent access", selection: $permission) {
                    Text("Bound folder").tag("sandbox")
                    Text("Read-only").tag("readonly")
                    Text("Full access").tag("full")
                }
            }
            .formStyle(.grouped)
        }
        .frame(width: 460, height: 460)
        .onAppear {
            if let s = server { name = s.name; command = s.command; args = s.args; permission = s.permissionMode ?? "sandbox" }
        }
    }

    private func save() {
        saving = true
        Task {
            defer { saving = false }
            let n = name.trimmingCharacters(in: .whitespaces)
            let c = command.trimmingCharacters(in: .whitespaces)
            let a = args.trimmingCharacters(in: .whitespacesAndNewlines)
            if let s = server {
                await store.updateMCPServer(id: s.id, fields: ["name": n, "command": c, "args": a, "permission_mode": permission])
            } else {
                await store.createMCPServer(name: n, command: c, args: a, permission: permission)
            }
            dismiss()
        }
    }
}

/// Edit a plugin: its type (foreground tool / background guardrail) and the
/// agent file-access level it runs at. Bundled plugins keep their name + repo.
struct PluginEditSheet: View {
    let plugin: Plugin
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss

    @State private var name = ""
    @State private var kind = "foreground"
    @State private var permission = "sandbox"

    private var isBundled: Bool { plugin.source == "bundled" }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Edit Plugin").font(.jb(14, .semibold))
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction).buttonStyle(.jbGhost)
                Button("Save") { save() }
                    .buttonStyle(.jbPrimary)
                    .tint(JB.accent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(name.isEmpty)
            }
            .padding(14)
            Rectangle().fill(JB.line).frame(height: 1)
            Form {
                TextField("Name", text: $name)
                    .disabled(isBundled)
                Picker("Type", selection: $kind) {
                    Text("Foreground (active tool)").tag("foreground")
                    Text("Background (guardrail)").tag("background")
                }
                Picker("Agent access", selection: $permission) {
                    Text("Bound folder").tag("sandbox")
                    Text("Read-only").tag("readonly")
                    Text("Full access").tag("full")
                }
                if let repo = plugin.repoPath, !repo.isEmpty {
                    Text(repo)
                        .font(.jb(11))
                        .foregroundStyle(JB.ink2)
                        .lineLimit(1).truncationMode(.middle)
                }
            }
            .formStyle(.grouped)
        }
        .frame(width: 460, height: 360)
        .onAppear {
            name = plugin.name
            kind = plugin.kind
            permission = plugin.permissionMode ?? "sandbox"
        }
    }

    private func save() {
        store.updatePlugin(id: plugin.id, fields: [
            "name": name, "kind": kind, "permission_mode": permission,
        ])
        dismiss()
    }
}

/// Settings for the bundled Agent Desktop (computer use) plugin: explains the access it has and
/// lets the user bar apps it must never open, click, type into or read (e.g.
/// banking apps). Blocked names persist in the `macos_blocked_apps` pref, which
/// the backend enforces inside every AppleScript it runs.
struct ComputerUseSettingsSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var blocked = ""
    @State private var loaded = false

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                HStack(spacing: 6) { JBSymbol(name: "cpu"); Text("Agent Desktop") }
                    .font(.jb(14, .semibold))
                Spacer()
                Button("Done") { save(); dismiss() }
                    .buttonStyle(.jbPrimary)
                    .tint(JB.accent)
                    .keyboardShortcut(.defaultAction)
            }
            .padding(14)
            Rectangle().fill(JB.line).frame(height: 1)
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("What it can do")
                            .font(.jb(12, .semibold))
                        Text("npm i -g agent-desktop")
                            .font(.jb(12))
                            .foregroundStyle(JB.ink2)
                        Text("Needs Accessibility access")
                            .font(.jb(12))
                            .foregroundStyle(JB.ink2)
                    }
                    Rectangle().fill(JB.line).frame(height: 1)
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Blocked apps")
                            .font(.jb(12, .semibold))
                        TextEditor(text: $blocked)
                            .font(.jb(13))
                            .frame(minHeight: 120)
                            .padding(6)
                            .scrollContentBackground(.hidden)
                            .jbSurface(.raised, radius: JB.rControl)
                    }
                }
                .padding(14)
            }
        }
        .frame(width: 440, height: 460)
        .task {
            guard !loaded else { return }
            loaded = true
            if let p = try? await APIClient.shared.getPrefs(),
               let arr = p["macos_blocked_apps"]?.arrayValue {
                blocked = arr.compactMap { $0.stringValue }.joined(separator: "\n")
            }
        }
    }

    private func save() {
        let list = blocked.split(whereSeparator: { $0 == "\n" || $0 == "," })
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        Task { await APIClient.shared.setPref("macos_blocked_apps", value: list) }
    }
}
/// Settings for the bundled Advisor plugin: which (stronger) model the everyday
/// model may consult, and the editable tool description that tells it WHEN to.
/// Saved as prefs (advisor_model / advisor_endpoint_id / advisor_description)
/// that the backend reads when building the advisor tool schema.
struct AdvisorSettingsSheet: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var modelID = ""
    @State private var desc = ""
    @State private var loaded = false

    /// Keep in sync with ADVISOR_DEFAULT_DESC in jb_core.py.
    static let defaultDesc = "Ask the user's Advisor — a more powerful model they chose — for a second opinion. Call it when you're genuinely stuck, the problem needs deeper reasoning than you can do (tricky bugs, maths, architecture, high-stakes decisions), your attempts keep failing, or the user asks for a second opinion. Send ONE self-contained question containing ALL the context needed (relevant code, errors, constraints) — the advisor cannot see this chat and has no tools. Weigh its advice against what you know, then act; you own the final answer."

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                HStack(spacing: 6) { JBSymbol(name: "graduationcap"); Text("Advisor") }
                    .font(.jb(14, .semibold))
                Spacer()
                Button("Done") { save(); dismiss() }
                    .buttonStyle(.jbPrimary)
                    .tint(JB.accent)
                    .keyboardShortcut(.defaultAction)
            }
            .padding(14)
            Rectangle().fill(JB.line).frame(height: 1)
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Advisor model")
                            .font(.jb(12, .semibold))
                        Picker("", selection: $modelID) {
                            Text("Choose a model…").tag("")
                            ModelPickerItems(models: store.models)
                        }
                        .labelsHidden()
                        .fixedSize()
                    }
                    Rectangle().fill(JB.line).frame(height: 1)
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text("When should it be called?")
                                .font(.jb(12, .semibold))
                            Spacer()
                            Button("Reset to default") { desc = Self.defaultDesc }
                                .buttonStyle(JBButtonStyle(kind: .ghost, compact: true))
                        }
                        TextEditor(text: $desc)
                            .font(.jb(12))
                            .frame(minHeight: 140)
                            .padding(6)
                            .scrollContentBackground(.hidden)
                            .jbSurface(.raised, radius: JB.rControl)
                    }
                }
                .padding(14)
            }
        }
        .frame(width: 460, height: 480)
        .task {
            guard !loaded else { return }
            loaded = true
            if store.models.isEmpty { await store.loadModels() }
            if let p = try? await APIClient.shared.getPrefs() {
                if let m = p["advisor_model"]?.stringValue,
                   let e = p["advisor_endpoint_id"]?.stringValue,
                   !m.isEmpty, !e.isEmpty {
                    modelID = m + "@" + e
                }
                let d = p["advisor_description"]?.stringValue ?? ""
                desc = d.isEmpty ? Self.defaultDesc : d
            }
        }
    }

    private func save() {
        let choice = store.models.first { $0.id == modelID }
        let d = desc.trimmingCharacters(in: .whitespacesAndNewlines)
        Task {
            await APIClient.shared.setPref("advisor_model", value: choice?.modelID ?? "")
            await APIClient.shared.setPref("advisor_endpoint_id", value: choice?.endpointID ?? "")
            // Empty pref = "use the built-in default", so the default can improve
            // in updates without a stale copy pinned in everyone's settings.
            await APIClient.shared.setPref("advisor_description", value: d == Self.defaultDesc ? "" : d)
        }
    }
}
