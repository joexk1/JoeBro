import SwiftUI

/// Scheduled tasks: a model prompt on a schedule, or a coding agent started on a schedule (or now) through Night Shift.
struct TasksPanel: View {
    @Environment(AppStore.self) private var store
    @State private var tasks: Loadable<[TaskItem]> = .loading
    @State private var hosts: [NightHost] = []
    @State private var showNew = false
    @State private var editing: TaskItem?
    @State private var selection: Set<String> = []
    @State private var generating = false
    @State private var genError: String?
    @State private var search = ""
    @State private var searchOpen = false
    @State private var phoneScope = false
    @State private var note: String?

    var body: some View {
        ExtrasShell(title: "Tasks", glyph: .tasks) {
            HStack(spacing: 2) {
                Button("Tasks") { phoneScope = false }.buttonStyle(JBButtonStyle(kind: phoneScope ? .ghost : .secondary, compact: true))
                Button("Personal agent") { phoneScope = true }.buttonStyle(JBButtonStyle(kind: phoneScope ? .secondary : .ghost, compact: true))
            }
            if phoneScope {
                JBMenu {
                    Button("Morning briefing") { addStarter("morning") }
                    Button("Evening wrap-up") { addStarter("evening") }
                } label: { JBIcon(.spark, size: 14) }
                .menuStyle(.borderlessButton).fixedSize().help("Starter tasks")
            }
            PanelSearchField(text: $search, open: $searchOpen, placeholder: "Search tasks")
            ExtrasIconButton(symbol: "arrow.clockwise", help: "Refresh") { Task { await load() } }
            ExtrasIconButton(symbol: "plus", help: "New task") { showNew = true }
        } content: {
            VStack(spacing: JB.s3) {
                if !phoneScope { QuickAddBar(placeholder: "Describe a new task", actionIcon: "arrow.up", busy: generating) { generateTask($0) } }
                if let m = genError ?? note { Text(m).font(.jb(11)).foregroundStyle(JB.danger) }
                ZStack {
                    switch tasks {
                    case .loading:
                        JBSpinner()
                    case .failed(let e):
                        JBEmptyState(art: .plug, text: e)
                    case .loaded(let list) where list.isEmpty:
                        JBEmptyState(art: .inbox, text: phoneScope ? "No personal-agent tasks" : "No tasks")
                    case .loaded(let list):
                        let shown = filtered(list)
                        VStack(spacing: 0) {
                            if !selection.isEmpty {
                                SelectionBar(total: shown.count, allIDs: shown.map(\.id), selection: $selection) { ids in
                                    Task {
                                        for id in ids { await APIClient.shared.deleteTask(id) }
                                        await load()
                                    }
                                }
                            }
                            ScrollView {
                                LazyVStack(spacing: JB.s2) {
                                    ForEach(shown) { t in taskRow(t).transition(.opacity.combined(with: .move(edge: .top))) }
                                }
                                .padding(JB.s3)
                                .animation(JB.smooth, value: shown.map(\.id))
                            }
                        }
                        .animation(JB.snap, value: selection.isEmpty)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .jbSurface(.panel)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
        .task { await load() }
        .onChange(of: phoneScope) { selection = []; tasks = .loading; Task { await load() } }
        .sheet(isPresented: $showNew) { NewTaskSheet(personal: phoneScope, hosts: hosts) { await load() } }
        .sheet(item: $editing) { t in EditTaskSheet(task: t, hosts: hosts) { await load() } }
    }

    private func filtered(_ list: [TaskItem]) -> [TaskItem] {
        let q = search.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return list }
        return list.filter { t in ((t.name ?? "") + " " + (t.prompt ?? "")).localizedCaseInsensitiveContains(q) }
    }

    private func load() async {
        do {
            tasks = .loaded(phoneScope ? try await APIClient.shared.personalTasks() : try await APIClient.shared.tasks())
            if !phoneScope, hosts.isEmpty { hosts = await APIClient.shared.nightHosts() }
        } catch { tasks = .failed(error.localizedDescription) }
    }

    private func addStarter(_ id: String) {
        Task {
            do { try await APIClient.shared.createStarterTask(id) }
            catch { genError = error.localizedDescription }
            await load()
        }
    }

    private func run(_ t: TaskItem) {
        guard t.isCoding else { store.runTask(t); return }
        Task {
            do { note = try await APIClient.shared.runTask(t.id); try? await Task.sleep(for: .seconds(2)); await load() }
            catch { note = error.localizedDescription }
        }
    }

    private func taskRow(_ t: TaskItem) -> some View {
        let paused = t.status == "paused"
        return SkillRowShell(id: t.id, checked: selection.contains(t.id), selecting: !selection.isEmpty,
                             onToggleCheck: { toggleSelection(t.id, in: &selection) }) {
            Group {
                if t.isCoding { JBIcon(.coding, size: 15) } else { JBIcon(.tasks, size: 15) }
            }
            .foregroundStyle(paused ? JB.ink3 : JB.accent).frame(width: 22)
            VStack(alignment: .leading, spacing: 3) {
                Text(t.name ?? "Task").font(.jb(13, .medium)).foregroundStyle(JB.ink).lineLimit(1)
                HStack(spacing: 8) {
                    if let s = t.schedule, !s.isEmpty { Text("\(s)\(t.scheduledTime.map { " " + $0 } ?? "")") }
                    if let next = t.nextRun, let d = parseISO(next) { Text("next \(d.formatted(.relative(presentation: .named)))") }
                    if t.isCoding { Text([t.codingAgent == "any" ? "any agent" : t.codingAgent, hostLabel(t)].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")) }
                }
                .font(.jb(11)).foregroundStyle(JB.ink3)
                if let p = t.prompt, !p.isEmpty { Text(p).font(.jb(11.5)).foregroundStyle(JB.ink2).lineLimit(2) }
            }
            Spacer()
            if let c = t.codingStatus, !c.isEmpty { codingChip(c) }
            if paused { JBChip(text: "paused") }
        }
        .opacity(paused ? 0.55 : 1)
        .contentShape(Rectangle())
        .jbContextMenu {
            Button("Run now") { run(t) }
            Button("Edit") { editing = t }
            Button(paused ? "Resume" : "Pause") {
                Task { await APIClient.shared.taskAction(t.id, paused ? "resume" : "pause"); await load() }
            }
            Button("Delete", role: .destructive) { Task { await APIClient.shared.deleteTask(t.id); await load() } }
        }
        .onTapGesture(count: 2) { editing = t }
    }

    private func hostLabel(_ t: TaskItem) -> String? {
        guard hosts.count > 1, let h = t.codingHost, !h.isEmpty else { return nil }
        return hosts.first { $0.id == h }?.label ?? h
    }

    private func codingChip(_ status: String) -> some View {
        let tint: Color = switch status { case "running": JB.live; case "review": JB.accent; case "failed": JB.danger; default: JB.ink2 }
        return HStack(spacing: 5) {
            if status == "running" { JBStatusDot(live: true, size: 6) }
            JBChip(text: status, tint: tint)
        }
    }

    /// "Describe a new task…": the default model turns plain English into
    /// {name, prompt, schedule, time} and the task is created directly.
    private func generateTask(_ described: String) {
        guard !described.isEmpty, !generating else { return }
        generating = true
        genError = nil
        Task {
            defer { generating = false }
            do {
                let t = try await generateTaskJSON(described: described)
                try await APIClient.shared.createTask(
                    name: t["name"] as? String ?? String(described.prefix(40)),
                    prompt: t["prompt"] as? String ?? described,
                    schedule: ["daily", "weekly", "monthly"].contains(t["schedule"] as? String ?? "")
                        ? (t["schedule"] as? String ?? "daily") : "daily",
                    time: t["time"] as? String ?? "09:00")
                await load()
            } catch {
                genError = error.localizedDescription
            }
        }
    }

    /// Same throwaway-session pattern as the skill generator: draft on the
    /// user's DEFAULT model, parse strict JSON, clean up the temp session.
    private func generateTaskJSON(described: String) async throws -> [String: Any] {
        var model = store.selectedModel
        if let prefs = try? await APIClient.shared.getPrefs(),
           let dm = prefs["default_model"]?.stringValue,
           let dep = prefs["default_endpoint_id"]?.stringValue,
           let match = store.models.first(where: { $0.modelID == dm && $0.endpointID == dep }) {
            model = match
        }
        let session = try await APIClient.shared.createSession(
            name: "__taskgen__", model: model?.modelID ?? "",
            endpointURL: model?.endpointURL ?? "", endpointID: model?.endpointID ?? "")
        defer { Task { try? await APIClient.shared.deleteSession(session.id) } }

        let instruction = """
        Turn this into a scheduled AI task: "\(described)".
        Reply with STRICT JSON only, no prose, shaped exactly:
        {"name": "short title", "prompt": "what the assistant should do each run", "schedule": "daily|weekly|monthly", "time": "HH:MM 24h"}
        """
        let r = try await APIClient.shared.postForm("api/chat", fields: [
            "message": instruction, "session": session.id,
        ])
        guard let text = r["response"] as? String else { throw APIError.badResponse }
        guard let start = text.firstIndex(of: "{"), let end = text.lastIndex(of: "}"),
              let data = String(text[start...end]).data(using: .utf8),
              let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw APIError.http(422, "Model didn't return a valid task")
        }
        return obj
    }
}

struct TaskFields: View {
    @Binding var name: String
    @Binding var prompt: String
    @Binding var schedule: String
    @Binding var time: String
    @Binding var repeatDay: Int
    @Binding var mode: String
    @Binding var coding: Bool
    @Binding var agent: String
    @Binding var model: String
    @Binding var host: String
    @Binding var repo: String
    var hosts: [NightHost]
    var allowCoding = true
    @FocusState private var focus: Int?
    private static let days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    var body: some View {
        VStack(alignment: .leading, spacing: JB.s3) {
            if allowCoding {
                HStack(spacing: 2) {
                    Button("AI task") { coding = false }.buttonStyle(JBButtonStyle(kind: coding ? .ghost : .secondary, compact: true))
                    Button("Coding agent") { coding = true }.buttonStyle(JBButtonStyle(kind: coding ? .secondary : .ghost, compact: true))
                }
            }
            TextField("Name", text: $name).focused($focus, equals: 0).jbField(focused: focus == 0)
            TextField(coding ? "What should the agent do?" : "What should JoeBro do?", text: $prompt, axis: .vertical)
                .lineLimit(3...6).focused($focus, equals: 1).jbField(focused: focus == 1)
            HStack(spacing: JB.s3) {
                ExtrasField(label: "Repeat") {
                    Picker("", selection: $schedule) { Text("Daily").tag("daily"); Text("Weekly").tag("weekly"); Text("Monthly").tag("monthly") }.labelsHidden()
                }
                if schedule == "weekly" {
                    ExtrasField(label: "On") { Picker("", selection: $repeatDay) { ForEach(0..<7, id: \.self) { Text(Self.days[$0]).tag($0) } }.labelsHidden() }
                }
                ExtrasField(label: "Time") { TextField("09:00", text: $time).font(.jb(13)).focused($focus, equals: 2).jbField(focused: focus == 2).frame(width: 80) }
            }
            if coding {
                CodingTargetFields(agent: $agent, model: $model, host: $host, repo: $repo, hosts: hosts)
            } else {
                ExtrasField(label: "File access") {
                    Picker("", selection: $mode) { Text("Bound folder").tag("sandbox"); Text("Read-only").tag("readonly"); Text("Full access").tag("full") }.labelsHidden()
                }
            }
        }
    }
    var codingBody: [String: Any] {
        coding ? ["coding_agent": agent, "coding_repo": repo, "coding_host": host, "coding_model": model]
               : ["coding_agent": "", "coding_repo": "", "coding_host": "", "coding_model": ""]
    }
    var canSave: Bool { !prompt.isEmpty && (!coding || !repo.isEmpty) }
}

struct NewTaskSheet: View {
    var personal = false
    var hosts: [NightHost] = []
    let onSaved: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var prompt = ""
    @State private var schedule = "daily"
    @State private var time = "09:00"
    @State private var repeatDay = 0
    @State private var mode = "sandbox"
    @State private var coding = false
    @State private var agent = "claude"
    @State private var model = ""
    @State private var host = ""
    @State private var repo = ""
    @State private var error: String?
    @State private var saving = false

    var body: some View {
        let f = TaskFields(name: $name, prompt: $prompt, schedule: $schedule, time: $time, repeatDay: $repeatDay, mode: $mode, coding: $coding,
                           agent: $agent, model: $model, host: $host, repo: $repo, hosts: hosts, allowCoding: !personal)
        return VStack(spacing: 0) {
            ExtrasSheetHeader(title: "New task") {
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction).buttonStyle(JBButtonStyle(kind: .ghost, compact: true))
                Button { create(f.codingBody) } label: { if saving { JBSpinner() } else { Text("Create") } }
                    .buttonStyle(JBButtonStyle(kind: .primary, compact: true)).keyboardShortcut(.defaultAction).disabled(saving || !f.canSave)
            }
            VStack(alignment: .leading, spacing: JB.s3) {
                f
                if let error { Text(error).font(.jb(11)).foregroundStyle(JB.danger) }
            }.padding(JB.s4)
        }
        .frame(width: 500).background(JB.surface)
    }

    private func create(_ codingBody: [String: Any]) {
        saving = true
        Task {
            defer { saving = false }   // a second click while in flight made two tasks
            do {
                try await APIClient.shared.createTask(
                    name: name.isEmpty ? String(prompt.prefix(40)) : name,
                    prompt: prompt, schedule: schedule, time: time, permissionMode: mode, repeatDay: repeatDay, personal: personal,
                    coding: coding ? codingBody : [:])
                await onSaved()
                dismiss()
            } catch { self.error = error.localizedDescription }
        }
    }
}

struct EditTaskSheet: View {
    let task: TaskItem
    var hosts: [NightHost] = []
    let onSaved: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var prompt = ""
    @State private var schedule = "daily"
    @State private var time = "09:00"
    @State private var mode = "sandbox"
    @State private var repeatDay = 0
    @State private var coding = false
    @State private var agent = "claude"
    @State private var model = ""
    @State private var host = ""
    @State private var repo = ""
    @State private var error: String?
    @State private var saving = false

    var body: some View {
        let f = TaskFields(name: $name, prompt: $prompt, schedule: $schedule, time: $time, repeatDay: $repeatDay, mode: $mode, coding: $coding,
                           agent: $agent, model: $model, host: $host, repo: $repo, hosts: hosts, allowCoding: task.id.hasPrefix("ptask_") == false)
        return VStack(spacing: 0) {
            ExtrasSheetHeader(title: "Edit task") {
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction).buttonStyle(JBButtonStyle(kind: .ghost, compact: true))
                Button { save(f.codingBody) } label: { if saving { JBSpinner() } else { Text("Save") } }
                    .buttonStyle(JBButtonStyle(kind: .primary, compact: true)).keyboardShortcut(.defaultAction).disabled(saving || !f.canSave)
            }
            VStack(alignment: .leading, spacing: JB.s3) {
                f
                if let error { Text(error).font(.jb(11)).foregroundStyle(JB.danger) }
            }.padding(JB.s4)
        }
        .frame(width: 500).background(JB.surface)
        .onAppear {
            name = task.name ?? ""
            prompt = task.prompt ?? ""
            schedule = task.schedule ?? "daily"
            time = task.scheduledTime ?? "09:00"
            mode = task.permissionMode ?? "sandbox"
            repeatDay = task.repeatDay ?? 0
            coding = task.isCoding
            agent = task.isCoding ? (task.codingAgent ?? "claude") : "claude"
            model = task.codingModel ?? ""
            host = task.codingHost ?? ""
            repo = task.codingRepo ?? ""
        }
    }

    private func save(_ codingBody: [String: Any]) {
        saving = true
        Task {
            defer { saving = false }
            do {
                try await APIClient.shared.updateTask(
                    id: task.id, name: name, prompt: prompt, schedule: schedule, time: time, permissionMode: mode, repeatDay: repeatDay,
                    coding: task.id.hasPrefix("ptask_") ? [:] : codingBody)
                await onSaved()
                dismiss()
            } catch { self.error = error.localizedDescription }
        }
    }
}
