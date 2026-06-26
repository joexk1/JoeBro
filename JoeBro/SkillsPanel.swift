import SwiftUI
import UniformTypeIdentifiers

/// Skills — the agent's learned procedures. Add manually, upload a .md,
/// or prompt one into existence; select which are active.
struct SkillsPanel: View {
    @Environment(AppStore.self) private var store
    @State private var skills: Loadable<[JSONValue]> = .loading
    @State private var editing: SkillDraft?
    @State private var showNew = false
    @State private var showGenerate = false
    @State private var showImporter = false
    @State private var busyID: String?
    @State private var selection: Set<String> = []
    @State private var generatePrefill = ""
    @State private var search = ""
    @State private var searchOpen = false

    var body: some View {
        PanelChrome(title: "Skills", icon: "graduationcap") {
            PanelSearchField(text: $search, open: $searchOpen, placeholder: "Search skills")
            Button {
                Task { await load() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            Menu {
                Button {
                    showNew = true
                } label: {
                    Label("Write manually…", systemImage: "square.and.pencil")
                }
                Button {
                    showImporter = true
                } label: {
                    Label("Upload markdown…", systemImage: "arrow.up.doc")
                }
                Button {
                    generatePrefill = ""
                    showGenerate = true
                } label: {
                    Label("Generate with AI…", systemImage: "sparkles")
                }
            } label: {
                Image(systemName: "plus")
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .help("Add skill")
        } content: {
            VStack(spacing: 10) {
                QuickAddBar(placeholder: "Describe a new skill…") { startGenerate($0) }

            ZStack {
                switch skills {
                case .loading:
                    ProgressView()
                case .failed(let e):
                    ContentUnavailableView("Couldn't load skills", systemImage: "graduationcap", description: Text(e))
                case .loaded(let list) where list.isEmpty:
                    ContentUnavailableView("No skills yet", systemImage: "graduationcap",
                                           description: Text("The agent learns skills from successful sessions — or add one with +."))
                case .loaded(let list):
                    let shown = filtered(list)
                    VStack(spacing: 0) {
                        if !selection.isEmpty {
                            selectionBar(total: shown.count, allIDs: shown.compactMap { $0["id"]?.stringValue ?? $0["name"]?.stringValue })
                        }
                        ScrollView {
                            LazyVStack(spacing: 8) {
                                ForEach(shown, id: \.skillRowID) { s in
                                    skillRow(s)
                                }
                            }
                            .padding(12)
                        }
                    }
                    .animation(.spring(duration: 0.25), value: selection.isEmpty)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .joeGlassRect(16)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
        .task { await load() }
        .sheet(item: $editing) { draft in
            SkillEditorSheet(draft: draft) { await load() }
        }
        .sheet(isPresented: $showNew) {
            SkillEditorSheet(draft: SkillDraft()) { await load() }
        }
        .sheet(isPresented: $showGenerate) {
            GenerateSkillSheet(initialPrompt: generatePrefill) { await load() }
        }
        .fileImporter(isPresented: $showImporter,
                      allowedContentTypes: [.plainText, UTType(filenameExtension: "md") ?? .plainText]) { result in
            if case .success(let url) = result {
                let scoped = url.startAccessingSecurityScopedResource()
                defer { if scoped { url.stopAccessingSecurityScopedResource() } }
                guard let content = try? String(contentsOf: url, encoding: .utf8) else { return }
                var d = SkillDraft()
                d.name = url.deletingPathExtension().lastPathComponent
                // First markdown heading becomes the description if present
                if let head = content.components(separatedBy: "\n").first(where: { $0.hasPrefix("#") }) {
                    d.description = String(head.drop(while: { $0 == "#" || $0 == " " })).trimmingCharacters(in: .whitespaces)
                }
                d.procedure = content
                editing = d
            }
        }
    }

    /// The quick-add capsule: hand the description to the AI generator,
    /// which starts drafting immediately.
    private func startGenerate(_ described: String) {
        guard !described.isEmpty else { return }
        generatePrefill = described
        showGenerate = true
    }

    private func filtered(_ list: [JSONValue]) -> [JSONValue] {
        let q = search.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return list }
        return list.filter { s in
            let name = s["name"]?.stringValue ?? ""
            let desc = s["description"]?.stringValue ?? ""
            let body = s["content"]?.stringValue ?? ""
            return (name + " " + desc + " " + body).localizedCaseInsensitiveContains(q)
        }
    }

    private func load() async {
        do {
            let v: JSONValue = try await APIClient.shared.getJSON("api/skills")
            skills = .loaded(v["skills"]?.arrayValue ?? [])
        } catch {
            skills = .failed(error.localizedDescription)
        }
    }

    private func selectionBar(total: Int, allIDs: [String]) -> some View {
        HStack(spacing: 10) {
            Text("\(selection.count) selected")
                .font(.system(size: 11, weight: .semibold))
            Button(selection.count == total ? "None" : "All") {
                withAnimation(.spring(duration: 0.25)) {
                    selection = selection.count == total ? [] : Set(allIDs)
                }
            }
            .buttonStyle(.borderless)
            .font(.system(size: 11))
            Spacer()
            Button {
                let ids = selection
                selection = []
                Task {
                    for id in ids {
                        _ = try? await APIClient.shared.sendJSON("api/skills/\(id)", method: "DELETE")
                    }
                    await load()
                }
            } label: {
                Image(systemName: "trash").font(.system(size: 12))
            }
            .buttonStyle(.borderless)
            .help("Delete selected")
            Button {
                withAnimation(.spring(duration: 0.25)) { selection = [] }
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.borderless)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color.accentColor.opacity(0.10))
        .transition(.move(edge: .top).combined(with: .opacity))
    }

    private func toggleSelect(_ id: String) {
        withAnimation(.spring(duration: 0.2)) {
            if selection.contains(id) { selection.remove(id) } else { selection.insert(id) }
        }
    }

    private func isActive(_ s: JSONValue) -> Bool {
        let status = s["status"]?.stringValue ?? "active"
        return status != "disabled" && status != "archived"
    }

    private func skillRow(_ s: JSONValue) -> some View {
        let id = s["id"]?.stringValue ?? s["name"]?.stringValue ?? ""
        let active = isActive(s)
        return SkillRowShell(id: id, checked: selection.contains(id),
                             selecting: !selection.isEmpty,
                             onToggleCheck: { toggleSelect(id) }) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 7) {
                    Text(s["name"]?.stringValue ?? "Unnamed skill")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(active ? .primary : .secondary)
                    if let cat = s["category"]?.stringValue, !cat.isEmpty {
                        Text(cat)
                            .font(.system(size: 10))
                            .padding(.horizontal, 6)
                            .padding(.vertical, 1)
                            .background(.quaternary.opacity(0.6), in: Capsule())
                    }
                    if !active {
                        Text("off")
                            .font(.system(size: 10))
                            .foregroundStyle(.tertiary)
                    }
                }
                if let desc = s["description"]?.stringValue, !desc.isEmpty {
                    Text(desc)
                        .font(.system(size: 11.5))
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                HStack(spacing: 8) {
                    if let uses = s["uses"]?.intValue { Text("\(uses) uses") }
                    if let conf = s["confidence"]?.doubleValue { Text("\(Int(conf))% confidence") }
                    if let src = s["source"]?.stringValue { Text(src) }
                }
                .font(.system(size: 10))
                .foregroundStyle(.tertiary)
            }
            Spacer()
        }
        .opacity(active ? 1 : 0.55)
        .contentShape(Rectangle())
        .contextMenu {
            Button("Edit…") { editing = SkillDraft(from: s) }
            Button(active ? "Disable" : "Activate") { toggle(id: id, currentlyActive: active) }
            Button("Delete", role: .destructive) {
                Task {
                    _ = try? await APIClient.shared.sendJSON("api/skills/\(id)", method: "DELETE")
                    await load()
                }
            }
        }
    }

    private func toggle(id: String, currentlyActive: Bool) {
        guard !id.isEmpty else { return }
        busyID = id
        Task {
            defer { busyID = nil }
            _ = try? await APIClient.shared.sendJSON("api/skills/\(id)", method: "PUT",
                                                     body: ["status": currentlyActive ? "disabled" : "active"])
            await load()
        }
    }
}

// MARK: - Editor sheet

struct SkillDraft: Identifiable {
    var id = UUID()
    var skillID: String?
    var name = ""
    var description = ""
    var category = "general"
    var whenToUse = ""
    var procedure = ""

    init() {}

    init(from s: JSONValue) {
        skillID = s["id"]?.stringValue
        name = s["name"]?.stringValue ?? ""
        description = s["description"]?.stringValue ?? ""
        category = s["category"]?.stringValue ?? "general"
        whenToUse = s["when_to_use"]?.stringValue ?? ""
        procedure = s["procedure"]?.stringValue ?? ""
    }
}

struct SkillEditorSheet: View {
    @State var draft: SkillDraft
    let onSaved: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var saving = false
    @State private var error: String?

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(draft.skillID == nil ? "New Skill" : "Edit Skill")
                    .font(.system(size: 14, weight: .semibold))
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button(draft.skillID == nil ? "Add" : "Save") { save() }
                    .buttonStyle(.glassProminent)
                    .tint(Color.accentColor)
                    .disabled(saving || draft.name.isEmpty || draft.procedure.isEmpty)
            }
            .padding(14)
            Divider()
            Form {
                TextField("Name", text: $draft.name)
                TextField("Description", text: $draft.description)
                TextField("Category", text: $draft.category)
                TextField("When to use", text: $draft.whenToUse, axis: .vertical)
                    .lineLimit(2...4)
                VStack(alignment: .leading, spacing: 4) {
                    Text("Procedure (markdown)")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                    // Fixed-height TextEditor so a long procedure scrolls inside
                    // the field instead of stretching the whole sheet.
                    TextEditor(text: $draft.procedure)
                        .font(.system(size: 12, design: .monospaced))
                        .frame(height: 240)
                        .scrollContentBackground(.hidden)
                        .padding(6)
                        .overlay(RoundedRectangle(cornerRadius: 6).stroke(.quaternary))
                }
                if let error {
                    Text(error).font(.caption).foregroundStyle(.red)
                }
            }
            .formStyle(.grouped)
        }
        .frame(width: 520, height: 520)
    }

    private func save() {
        saving = true
        error = nil
        Task {
            defer { saving = false }
            do {
                let body: [String: Any] = [
                    "name": draft.name, "description": draft.description,
                    "category": draft.category, "when_to_use": draft.whenToUse,
                    "procedure": draft.procedure, "source": "user",
                ]
                if let id = draft.skillID {
                    _ = try await APIClient.shared.sendJSON("api/skills/\(id)", method: "PUT", body: body)
                } else {
                    _ = try await APIClient.shared.sendJSON("api/skills/add", body: body)
                }
                await onSaved()
                dismiss()
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}

// MARK: - Generate with AI

struct GenerateSkillSheet: View {
    var initialPrompt = ""
    let onSaved: () async -> Void
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var prompt = ""
    @State private var generating = false
    @State private var error: String?

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Label("Generate Skill", systemImage: "sparkles")
                    .font(.system(size: 14, weight: .semibold))
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button {
                    generate()
                } label: {
                    if generating { ProgressView().controlSize(.small) }
                    else { Text("Generate") }
                }
                .buttonStyle(.glassProminent)
                .tint(Color.accentColor)
                .disabled(generating || prompt.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            .padding(14)
            Divider()
            Form {
                TextField("Describe the skill — e.g. \"how to review a pull request\"",
                          text: $prompt, axis: .vertical)
                    .lineLimit(4...8)
                Text("Your default model drafts the skill, then it's saved to the library.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if let error {
                    Text(error).font(.caption).foregroundStyle(.red)
                }
            }
            .formStyle(.grouped)
        }
        .frame(width: 480, height: 300)
        .onAppear {
            // Launched from the quick-add capsule: prefill and draft at once.
            if !initialPrompt.isEmpty && prompt.isEmpty {
                prompt = initialPrompt
                generate()
            }
        }
    }

    private func generate() {
        generating = true
        error = nil
        Task {
            defer { generating = false }
            do {
                let skill = try await generateSkillJSON(prompt: prompt)
                _ = try await APIClient.shared.sendJSON("api/skills/add", body: skill)
                await onSaved()
                dismiss()
            } catch {
                self.error = error.localizedDescription
            }
        }
    }

    /// Drafts the skill via a throwaway chat turn on the user's DEFAULT
    /// model (the designated background-jobs model), then parses the
    /// strict-JSON reply. Falls back to the current chat model.
    private func generateSkillJSON(prompt: String) async throws -> [String: Any] {
        var model = store.selectedModel
        if let prefs = try? await APIClient.shared.getPrefs(),
           let dm = prefs["default_model"]?.stringValue,
           let dep = prefs["default_endpoint_id"]?.stringValue,
           let match = store.models.first(where: { $0.modelID == dm && $0.endpointID == dep }) {
            model = match
        }
        let session = try await APIClient.shared.createSession(
            name: "__skillgen__", model: model?.modelID ?? "",
            endpointURL: model?.endpointURL ?? "", endpointID: model?.endpointID ?? "")
        defer { Task { try? await APIClient.shared.deleteSession(session.id) } }

        let instruction = """
        Write an agent skill for this request: "\(prompt)".
        Reply with STRICT JSON only, no prose, shaped exactly:
        {"name": "...", "description": "...", "category": "...", "when_to_use": "...", "procedure": "... (markdown steps)"}
        """
        let r = try await APIClient.shared.postForm("api/chat", fields: [
            "message": instruction, "session": session.id,
        ])
        guard let text = r["response"] as? String else { throw APIError.badResponse }
        // Extract the JSON object from the reply
        guard let start = text.firstIndex(of: "{"), let end = text.lastIndex(of: "}") else {
            throw APIError.http(422, "Model didn't return JSON")
        }
        let json = String(text[start...end])
        guard let data = json.data(using: .utf8),
              var obj = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw APIError.http(422, "Couldn't parse the generated skill")
        }
        obj["source"] = "ai-generated"
        return obj
    }
}


/// Email-style selectable row shell: hover checkbox, accent when checked,
/// bounce + shadow — shared look for skills/memories batches.
struct SkillRowShell<Content: View>: View {
    let id: String
    let checked: Bool
    let selecting: Bool
    let onToggleCheck: () -> Void
    @ViewBuilder var content: Content
    @State private var hovering = false

    var body: some View {
        // The checkbox slot is ALWAYS reserved — conditionally inserting it
        // reflowed the row under the pointer and made the circle flicker.
        let showCheck = selecting || hovering || checked
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: checked ? "checkmark.circle.fill" : "circle")
                .font(.system(size: 15))
                .foregroundStyle(checked ? Color.accentColor : .secondary)
                .frame(width: 24, height: 24, alignment: .center)
                .padding(.top, -3)
                .contentShape(Rectangle())
                .onTapGesture(perform: onToggleCheck)
                .opacity(showCheck ? 1 : 0)
                .scaleEffect(showCheck ? 1 : 0.5)
                .allowsHitTesting(showCheck)
            content
        }
        .panelCard()
        .background(
            RoundedRectangle(cornerRadius: 14)
                .fill(checked ? Color.accentColor.opacity(0.14) : .clear)
        )
        .scaleEffect(hovering ? 1.012 : 1)
        .shadow(color: .black.opacity(0.10), radius: 6, y: 3)
        .onHover { h in
            withAnimation(.spring(duration: 0.22)) { hovering = h }
        }
    }
}

/// Stable list identity for skill rows — position-based identity made
/// SwiftUI recycle row state (hover/checkbox) across the wrong skills.
private extension JSONValue {
    var skillRowID: String {
        self["id"]?.stringValue ?? self["name"]?.stringValue ?? ""
    }
}
