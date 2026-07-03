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
            .buttonStyle(.borderless)
        } content: {
            // Top half: API tools (left) + MCP (right). Bottom half: Plugins,
            // a single wide glass box spanning the full width.
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
    }

    private var pluginsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label("Plugins", systemImage: "puzzlepiece.extension")
                    .font(.system(size: 13, weight: .semibold, design: .rounded))
                Spacer()
                Button { addPlugin() } label: {
                    Label("Add Plugin…", systemImage: "plus")
                        .font(.system(size: 12))
                }
                .buttonStyle(.borderless)
            }

            Text("Install richer capabilities the model can use in Agent mode.")
                .font(.caption)
                .foregroundStyle(.secondary)

            if store.plugins.isEmpty {
                Spacer()
                Text("No plugins yet. Add a plugin's repo folder to install it.")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center)
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
        .joeGlassRect(16)
    }

    private func pluginRow(_ plugin: Plugin) -> some View {
        HStack(spacing: 12) {
            Toggle("", isOn: Binding(
                get: { plugin.isEnabled ?? true },
                set: { newValue in store.togglePlugin(plugin, enabled: newValue) }
            ))
            .labelsHidden()
            .toggleStyle(.switch)
            .controlSize(.small)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(plugin.name)
                        .font(.system(size: 13, weight: .medium))
                    Text(plugin.kind == "background" ? "BACKGROUND" : "FOREGROUND")
                        .font(.system(size: 9, weight: .semibold))
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(.quaternary, in: Capsule())
                    if plugin.source == "bundled" {
                        Text("BUNDLED")
                            .font(.system(size: 9, weight: .semibold))
                            .foregroundStyle(Color.accentColor)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(Color.accentColor.opacity(0.15), in: Capsule())
                    }
                }
                if let d = plugin.description, !d.isEmpty {
                    Text(d)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }

            Spacer()

            Button { editingPlugin = plugin } label: {
                Image(systemName: "pencil")
            }
            .buttonStyle(.borderless)
            .help("Edit")

            if plugin.id == "plugin_macos_use" {
                Button { configuringComputerUse = true } label: {
                    Image(systemName: "gearshape")
                }
                .buttonStyle(.borderless)
                .help("Computer Use settings — permissions and blocked apps")
            } else if plugin.source != "bundled" {
                Button(role: .destructive) {
                    store.deletePlugin(id: plugin.id)
                } label: {
                    Image(systemName: "trash")
                }
                .buttonStyle(.borderless)
                .help("Delete")
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 10))
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
                Label("MCP Servers", systemImage: "server.rack")
                    .font(.system(size: 13, weight: .semibold, design: .rounded))
                Spacer()
                if store.mcpBusy {
                    ProgressView().controlSize(.small)
                }
                Button {
                    showAddMCP = true
                } label: {
                    Label("Add MCP Server…", systemImage: "plus")
                        .font(.system(size: 12))
                }
                .buttonStyle(.borderless)
            }

            Text("Connect any MCP server (stdio). JoeBro launches it, discovers its tools, and offers them to the model in Agent mode.")
                .font(.caption)
                .foregroundStyle(.secondary)

            if let err = store.mcpError, !err.isEmpty {
                Text(err)
                    .font(.system(size: 11))
                    .foregroundStyle(.red)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }

            if store.mcpServers.isEmpty {
                Spacer()
                Text("No MCP servers yet. Add one (e.g. command npx, args -y @modelcontextprotocol/server-everything).")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center)
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
        .joeGlassRect(16)
    }

    private func mcpRow(_ server: MCPServer) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Toggle("", isOn: Binding(
                get: { server.enabled },
                set: { newValue in Task { await store.toggleMCPServer(server, enabled: newValue) } }
            ))
            .labelsHidden()
            .toggleStyle(.switch)
            .controlSize(.small)

            VStack(alignment: .leading, spacing: 4) {
                Text(server.name)
                    .font(.system(size: 13, weight: .medium))
                Text("\(server.command) \(server.args)")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                if let err = server.error, !err.isEmpty {
                    Text(err)
                        .font(.system(size: 11))
                        .foregroundStyle(.red)
                        .lineLimit(2)
                } else if server.tools.isEmpty {
                    Text("No tools discovered.")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                } else {
                    Text(server.tools.joined(separator: ", "))
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
            }

            Spacer()

            Button { editingMCP = server } label: {
                Image(systemName: "pencil")
            }
            .buttonStyle(.borderless)
            .help("Edit")

            Button(role: .destructive) {
                Task { await store.deleteMCPServer(id: server.id) }
            } label: {
                Image(systemName: "trash")
            }
            .buttonStyle(.borderless)
            .help("Delete")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 10))
    }

    private var apiToolsSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label("API Tools", systemImage: "network")
                    .font(.system(size: 13, weight: .semibold, design: .rounded))
                Spacer()
                Button {
                    showAdd = true
                } label: {
                    Label("Add API Tool…", systemImage: "plus")
                        .font(.system(size: 12))
                }
                .buttonStyle(.borderless)
            }

            Text("Register any JSON API as a tool the model can call in Agent mode. The description tells the model when and how to use it.")
                .font(.caption)
                .foregroundStyle(.secondary)

            if store.apiTools.isEmpty {
                Spacer()
                Text("No API tools yet. Add one to make it callable in Agent mode.")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center)
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
        .joeGlassRect(16)
    }

    private func toolRow(_ tool: APITool) -> some View {
        HStack(spacing: 12) {
            Toggle("", isOn: Binding(
                get: { tool.isEnabled ?? true },
                set: { newValue in Task { await store.toggleAPITool(tool, enabled: newValue) } }
            ))
            .labelsHidden()
            .toggleStyle(.switch)
            .controlSize(.small)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(tool.name)
                        .font(.system(size: 13, weight: .medium))
                    Text((tool.method ?? "GET").uppercased())
                        .font(.system(size: 10, weight: .semibold))
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(.quaternary, in: Capsule())
                    if tool.hasKey == true {
                        Image(systemName: "key.fill")
                            .font(.system(size: 9))
                            .foregroundStyle(.secondary)
                    }
                }
                if let url = tool.baseURL, !url.isEmpty {
                    Text(url)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
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
            .buttonStyle(.borderless)

            Button(role: .destructive) {
                Task { await store.deleteAPITool(id: tool.id) }
            } label: {
                Image(systemName: "trash")
            }
            .buttonStyle(.borderless)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 10))
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
                    .font(.system(size: 14, weight: .semibold))
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button {
                    save()
                } label: {
                    if saving { ProgressView().controlSize(.small) }
                    else { Text(tool == nil ? "Add" : "Save") }
                }
                .buttonStyle(.glassProminent)
                .tint(Color.accentColor)
                .disabled(saving || name.trimmingCharacters(in: .whitespaces).isEmpty || baseURL.isEmpty)
            }
            .padding(14)
            Divider()
            Form {
                TextField("Name (e.g. LinkedIn Search)", text: $name)
                TextField("Base URL (https://api.example.com/v2/search)", text: $baseURL)
                    .font(.system(size: 12, design: .monospaced))
                Text("Put {query} anywhere in the URL to place the model's input exactly (e.g. …/search?name={query}). Otherwise it's appended as ?query=…")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                SecureField(tool?.hasKey == true ? "API key (leave blank to keep)" : "API key (optional)", text: $apiKey)
                Picker("Method", selection: $method) {
                    Text("GET").tag("GET")
                    Text("POST").tag("POST")
                }
                .pickerStyle(.segmented)
                VStack(alignment: .leading, spacing: 4) {
                    Text("Description")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    TextEditor(text: $description)
                        .font(.system(size: 12))
                        .frame(minHeight: 90)
                    Text("The model reads this to know when and how to call the tool.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
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
                    .font(.system(size: 14, weight: .semibold))
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button {
                    save()
                } label: {
                    if saving { ProgressView().controlSize(.small) }
                    else { Text(server == nil ? "Add" : "Save") }
                }
                .buttonStyle(.glassProminent)
                .tint(Color.accentColor)
                .disabled(saving
                          || name.trimmingCharacters(in: .whitespaces).isEmpty
                          || command.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            .padding(14)
            Divider()
            Form {
                TextField("Name (e.g. Filesystem)", text: $name)
                TextField("Command (e.g. npx)", text: $command)
                    .font(.system(size: 12, design: .monospaced))
                VStack(alignment: .leading, spacing: 4) {
                    Text("Arguments")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    TextEditor(text: $args)
                        .font(.system(size: 12, design: .monospaced))
                        .frame(minHeight: 60)
                    Text("e.g. -y @modelcontextprotocol/server-filesystem /Users/me/Documents")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                Picker("Agent access", selection: $permission) {
                    Text("Bound folder").tag("sandbox")
                    Text("Read-only").tag("readonly")
                    Text("Full access").tag("full")
                }
                Text("The permission level the agent must be in to use this server's tools (e.g. phone or computer control should require Full access).")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Text("JoeBro launches the server, discovers its tools, and offers them to the model in Agent mode. The first connection may take a moment.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
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
                Text("Edit Plugin").font(.system(size: 14, weight: .semibold))
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Save") { save() }
                    .buttonStyle(.glassProminent)
                    .tint(Color.accentColor)
            }
            .padding(14)
            Divider()
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
                Text("The permission level the agent must be in to use this plugin (computer-use plugins need Full access).")
                    .font(.caption).foregroundStyle(.secondary)
                if let repo = plugin.repoPath, !repo.isEmpty {
                    Text(repo)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
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

/// Settings for the bundled Computer Use plugin: explains the access it has and
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
                Label("Computer Use", systemImage: "cpu")
                    .font(.system(size: 14, weight: .semibold))
                Spacer()
                Button("Done") { save(); dismiss() }
                    .buttonStyle(.glassProminent)
                    .tint(Color.accentColor)
                    .keyboardShortcut(.defaultAction)
            }
            .padding(14)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("What it can do")
                            .font(.system(size: 12, weight: .semibold))
                        Text("In Agent mode (Full access), JoeBro can drive this Mac: read the frontmost app's windows, menus and buttons as text, open apps, click elements, type, press shortcuts and use menus. It reads the screen as text first and only takes a picture when it must, so it stays fast and cheap.")
                            .font(.system(size: 12))
                            .foregroundStyle(.secondary)
                        Text("It needs Accessibility access (System Settings > Privacy & Security > Accessibility). Turn the plugin off above to revoke all of this at once.")
                            .font(.system(size: 12))
                            .foregroundStyle(.secondary)
                    }
                    Divider()
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Blocked apps")
                            .font(.system(size: 12, weight: .semibold))
                        Text("One app name per line. JoeBro will refuse to open, click, type into or read any app whose name matches — even if you ask it to. Good for banking and other sensitive apps.")
                            .font(.system(size: 12))
                            .foregroundStyle(.secondary)
                        TextEditor(text: $blocked)
                            .font(.system(size: 13, design: .monospaced))
                            .frame(minHeight: 120)
                            .padding(6)
                            .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 8))
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
