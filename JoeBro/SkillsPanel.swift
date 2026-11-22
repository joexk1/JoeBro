import AppKit
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
    @State private var showLearn = false
    @State private var showImporter = false
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
            .buttonStyle(.jbIcon)
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
                    Label { Text("Generate with AI…") } icon: { JBIcon(.spark, size: 13) }
                }
                Button {
                    showLearn = true
                } label: {
                    Label("Learn from sources…", systemImage: "books.vertical")
                }
            } label: {
                Image(systemName: "plus")
                    .frame(width: 26, height: 26)
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
                    PanelLoading()
                case .failed(let e):
                    PanelFailure(title: "Couldn't load skills", detail: e)
                case .loaded(let list) where list.isEmpty:
                    PanelEmpty(art: .files, text: "No skills yet")
                case .loaded(let list):
                    let shown = filtered(list)
                    VStack(spacing: 0) {
                        if !selection.isEmpty {
                            SelectionBar(total: shown.count,
                                         allIDs: shown.compactMap { $0["id"]?.stringValue ?? $0["name"]?.stringValue },
                                         selection: $selection) { ids in
                                Task {
                                    for id in ids {
                                        _ = try? await APIClient.shared.sendJSON("api/skills/\(id)", method: "DELETE")
                                    }
                                    await load()
                                }
                            }
                        }
                        ScrollView {
                            LazyVStack(spacing: 8) {
                                ForEach(shown, id: \.skillRowID) { s in
                                    skillRow(s)
                                }
                            }
                            .padding(12)
                            .animation(JB.smooth, value: shown.map(\.skillRowID))
                        }
                    }
                    .animation(JB.snap, value: selection.isEmpty)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .jbSurface(.panel, radius: JB.rPanel)
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
        .sheet(isPresented: $showLearn) {
            LearnSkillsSheet { await load() }
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



    private func isActive(_ s: JSONValue) -> Bool {
        let status = s["status"]?.stringValue ?? "active"
        return status != "disabled" && status != "archived"
    }

    private func skillRow(_ s: JSONValue) -> some View {
        let id = s["id"]?.stringValue ?? s["name"]?.stringValue ?? ""
        let active = isActive(s)
        return SkillRowShell(id: id, checked: selection.contains(id),
                             selecting: !selection.isEmpty,
                             onToggleCheck: { toggleSelection(id, in: &selection) }) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 7) {
                    JBStatusDot(color: active ? JB.live : JB.ink3, size: 6)
                    Text(s["name"]?.stringValue ?? "Unnamed skill")
                        .font(.jb(13, .semibold))
                        .foregroundStyle(active ? JB.ink : JB.ink2)
                    if let cat = s["category"]?.stringValue, !cat.isEmpty {
                        JBChip(text: cat)
                    }
                }
                if let desc = s["description"]?.stringValue, !desc.isEmpty {
                    Text(desc)
                        .font(.jb(12))
                        .foregroundStyle(JB.ink2)
                        .lineLimit(2)
                }
                HStack(spacing: 8) {
                    if let uses = s["uses"]?.intValue { Text("\(uses) uses") }
                    if let conf = s["confidence"]?.doubleValue { Text("\(Int(conf))%") }
                    if let src = s["source"]?.stringValue { Text(src) }
                }
                .font(.jb(10.5))
                .foregroundStyle(JB.ink3)
            }
            Spacer()
        }
        .opacity(active ? 1 : 0.65)
        .contentShape(Rectangle())
        .onTapGesture(count: 2) { editing = SkillDraft(from: s) }
        .jbHoverActions {
            Button { editing = SkillDraft(from: s) } label: { Image(systemName: "pencil") }
                .buttonStyle(.jbIcon).help("Edit")
            Button { toggle(id: id, currentlyActive: active) } label: { Image(systemName: "power") }
                .buttonStyle(JBIconButtonStyle(size: 26, active: active)).help(active ? "Disable" : "Activate")
            Button {
                Task {
                    _ = try? await APIClient.shared.sendJSON("api/skills/\(id)", method: "DELETE")
                    await load()
                }
            } label: { Image(systemName: "trash") }
                .buttonStyle(.jbIcon).help("Delete")
        }
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
        Task {
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
                    .font(.jb(14, .semibold))
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction).buttonStyle(.jbGhost)
                Button { save() } label: {
                    if saving { JBSpinner(size: 12, tint: JB.onAccent) }
                    else { Text(draft.skillID == nil ? "Add" : "Save") }
                }
                .buttonStyle(.jbPrimary)
                .tint(JB.accent)
                .keyboardShortcut(.defaultAction)
                .disabled(saving || draft.name.isEmpty || draft.procedure.isEmpty)
            }
            .padding(14)
            Rectangle().fill(JB.line).frame(height: 1)
            Form {
                TextField("Name", text: $draft.name)
                TextField("Description", text: $draft.description)
                TextField("Category", text: $draft.category)
                TextField("When to use", text: $draft.whenToUse, axis: .vertical)
                    .lineLimit(2...4)
                VStack(alignment: .leading, spacing: 4) {
                    Text("Procedure").jbEyebrow()
                    // Fixed-height TextEditor so a long procedure scrolls inside
                    // the field instead of stretching the whole sheet.
                    TextEditor(text: $draft.procedure)
                        .font(.jb(12))
                        .frame(height: 240)
                        .scrollContentBackground(.hidden)
                        .padding(6)
                        .overlay(RoundedRectangle(cornerRadius: JB.rControl, style: .continuous).strokeBorder(JB.line, lineWidth: 1))
                }
                if let error {
                    Text(error).font(.jbCaption).foregroundStyle(JB.danger)
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
                Label { Text("Generate skill") } icon: { JBIcon(.spark, size: 14) }
                    .font(.jb(14, .semibold))
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction).buttonStyle(.jbGhost)
                Button {
                    generate()
                } label: {
                    if generating { JBSpinner(size: 12, tint: JB.onAccent) }
                    else { Text("Generate") }
                }
                .buttonStyle(.jbPrimary)
                .tint(JB.accent)
                .disabled(generating || prompt.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            .padding(14)
            Rectangle().fill(JB.line).frame(height: 1)
            Form {
                TextField("Describe the skill — e.g. \"how to review a pull request\"",
                          text: $prompt, axis: .vertical)
                    .lineLimit(4...8)
                if let error {
                    Text(error).font(.jbCaption).foregroundStyle(JB.danger)
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
                .font(.jb(15))
                .foregroundStyle(checked ? JB.accent : JB.ink3)
                .frame(width: 24, height: 24, alignment: .center)
                .padding(.top, -3)
                .contentShape(Rectangle())
                .onTapGesture(perform: onToggleCheck)
                .opacity(showCheck ? 1 : 0)
                .allowsHitTesting(showCheck)
            content
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background {
            RoundedRectangle(cornerRadius: JB.rCard, style: .continuous)
                .fill(checked ? JB.accentSoft : hovering ? JB.hover : JB.raised)
        }
        .overlay {
            RoundedRectangle(cornerRadius: JB.rCard, style: .continuous)
                .strokeBorder(checked ? JB.accent.opacity(0.4) : JB.line, lineWidth: 1)
        }
        .animation(JB.snap, value: checked)
        .onHover { h in withAnimation(JB.fade) { hovering = h } }
    }
}

/// Stable list identity for skill rows — position-based identity made
/// SwiftUI recycle row state (hover/checkbox) across the wrong skills.
private extension JSONValue {
    var skillRowID: String {
        self["id"]?.stringValue ?? self["name"]?.stringValue ?? ""
    }
}


/// Point the default model at a folder of source material (past papers,
/// analyses, templates…) — it studies the documents and saves skill(s) that
/// teach it to produce the same kind of work.
struct LearnSkillsSheet: View {
    let onSaved: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var folder: URL?
    @State private var running = false
    @State private var detail = ""
    @State private var made: [String] = []
    @State private var error: String?
    private let sourceDevice = Flavor.deviceName

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Label { Text("Learn from sources") } icon: { JBIcon(.skills, size: 14) }
                    .font(.jb(14, .semibold))
                Spacer()
                Button(made.isEmpty ? "Cancel" : "Done") { dismiss() }.keyboardShortcut(.cancelAction)
                if made.isEmpty {
                    Button {
                        start()
                    } label: {
                        if running { JBSpinner(size: 12, tint: JB.onAccent) } else { Text("Learn") }
                    }
                    .buttonStyle(.jbPrimary)
                    .tint(JB.accent)
                    .disabled(folder == nil || running)
                }
            }
            .padding(14)
            Rectangle().fill(JB.line).frame(height: 1)
            Form {
                HStack {
                    Text(folder?.path ?? "No folder selected")
                        .lineLimit(1).truncationMode(.middle)
                        .foregroundStyle(folder == nil ? JB.ink3 : JB.ink)
                    Spacer()
                    Button("Choose folder…") { pick() }.disabled(running)
                }
                if running {
                    HStack(spacing: 8) {
                        JBSpinner()
                        Text(detail.isEmpty ? "Working" : detail).font(.jb(11)).foregroundStyle(JB.ink2)
                    }
                }
                if !made.isEmpty {
                    Label("Learned: " + made.joined(separator: ", "), systemImage: "checkmark")
                        .font(.jbCaption)
                        .foregroundStyle(JB.live)
                }
                if let error {
                    Text(error).font(.jbCaption).foregroundStyle(JB.danger)
                }
            }
            .formStyle(.grouped)
        }
        .frame(width: 500, height: 320)
    }

    private func pick() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK { folder = panel.url }
    }

    private func start() {
        guard let folder else { return }
        error = nil
        running = true
        Task {
            defer { running = false }
            do {
                let job = try await APIClient.shared.learnSkills(path: folder.path, device: sourceDevice)
                while true {
                    try await Task.sleep(for: .seconds(2))
                    let s = try await APIClient.shared.learnSkillsStatus(job: job)
                    detail = s.detail ?? ""
                    if s.status == "done" {
                        made = s.skills ?? []
                        await onSaved()
                        return
                    }
                    if s.status == "error" {
                        self.error = s.error ?? "Learning failed."
                        return
                    }
                }
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}
