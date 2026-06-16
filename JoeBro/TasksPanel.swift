import SwiftUI

/// Scheduled tasks — local LLM prompts on a schedule.
struct TasksPanel: View {
    @Environment(AppStore.self) private var store
    @State private var tasks: Loadable<[TaskItem]> = .loading
    @State private var showNew = false
    @State private var editing: TaskItem?
    @State private var selection: Set<String> = []
    @State private var generating = false
    @State private var genError: String?

    var body: some View {
        PanelChrome(title: "Tasks", icon: "clock.badge.checkmark") {
            Button {
                Task { await load() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            Button {
                showNew = true
            } label: {
                Image(systemName: "plus")
            }
            .buttonStyle(.borderless)
            .help("New task")
        } content: {
            VStack(spacing: 10) {
                QuickAddBar(placeholder: "Describe a new task…", busy: generating) { generateTask($0) }
                if let genError {
                    Text(genError)
                        .font(.system(size: 11))
                        .foregroundStyle(.red)
                }

            ZStack {
                switch tasks {
                case .loading:
                    ProgressView()
                case .failed(let e):
                    ContentUnavailableView("Couldn't load tasks", systemImage: "clock.badge.exclamationmark", description: Text(e))
                case .loaded(let list) where list.isEmpty:
                    ContentUnavailableView("No scheduled tasks", systemImage: "clock")
                case .loaded(let list):
                    VStack(spacing: 0) {
                        if !selection.isEmpty {
                            selectionBar(total: list.count, allIDs: list.map(\.id))
                        }
                        ScrollView {
                            LazyVStack(spacing: 8) {
                                ForEach(list) { t in
                                    taskRow(t)
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
        .sheet(isPresented: $showNew) {
            NewTaskSheet { await load() }
        }
        .sheet(item: $editing) { t in
            EditTaskSheet(task: t) { await load() }
        }
    }

    private func load() async {
        do { tasks = .loaded(try await APIClient.shared.tasks()) }
        catch { tasks = .failed(error.localizedDescription) }
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
                    for id in ids { await APIClient.shared.deleteTask(id) }
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

    private func taskRow(_ t: TaskItem) -> some View {
        let paused = t.status == "paused"
        return SkillRowShell(id: t.id, checked: selection.contains(t.id),
                             selecting: !selection.isEmpty,
                             onToggleCheck: { toggleSelect(t.id) }) {
            Image(systemName: paused ? "pause.circle" : "clock.arrow.circlepath")
                .font(.system(size: 14))
                .foregroundStyle(paused ? Color.secondary : Color.accentColor)
                .frame(width: 24)
            VStack(alignment: .leading, spacing: 2) {
                Text(t.name ?? "Task")
                    .font(.system(size: 13, weight: .medium))
                    .lineLimit(1)
                HStack(spacing: 8) {
                    if let s = t.schedule {
                        Label("\(s)\(t.scheduledTime.map { " @ \($0)" } ?? "")", systemImage: "calendar")
                    }
                    if let next = t.nextRun, let d = parseISO(next) {
                        Text("next \(d.formatted(.relative(presentation: .named)))")
                    }
                }
                .font(.system(size: 10.5))
                .foregroundStyle(.tertiary)
                if let p = t.prompt, !p.isEmpty {
                    Text(p)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
            Spacer()
            if paused {
                Text("paused")
                    .font(.system(size: 10))
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background(.quaternary.opacity(0.6), in: Capsule())
            }
        }
        .opacity(paused ? 0.55 : 1)
        .contentShape(Rectangle())
        .contextMenu {
            Button("Run now", systemImage: "play.fill") {
                store.runTaskInChat(name: t.name ?? "Task", prompt: t.prompt ?? "")
            }
            Button("Edit…") { editing = t }
            Button(paused ? "Resume" : "Pause") {
                Task {
                    await APIClient.shared.taskAction(t.id, paused ? "resume" : "pause")
                    await load()
                }
            }
            Button("Delete", role: .destructive) {
                Task {
                    await APIClient.shared.deleteTask(t.id)
                    await load()
                }
            }
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
                        ? t["schedule"] as! String : "daily",
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

struct NewTaskSheet: View {
    let onSaved: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var prompt = ""
    @State private var schedule = "daily"
    @State private var time = "09:00"
    @State private var error: String?

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("New Task").font(.system(size: 14, weight: .semibold))
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Create") { create() }
                    .buttonStyle(.glassProminent)
                    .tint(Color.accentColor)
                    .disabled(prompt.isEmpty)
            }
            .padding(14)
            Divider()
            Form {
                TextField("Name", text: $name)
                TextField("What should JoeBro do?", text: $prompt, axis: .vertical)
                    .lineLimit(3...6)
                Picker("Repeat", selection: $schedule) {
                    Text("Daily").tag("daily")
                    Text("Weekly").tag("weekly")
                    Text("Monthly").tag("monthly")
                }
                TextField("Time (HH:MM)", text: $time)
                if let error {
                    Text(error).font(.caption).foregroundStyle(.red)
                }
            }
            .formStyle(.grouped)
        }
        .frame(width: 420, height: 340)
    }

    private func create() {
        Task {
            do {
                try await APIClient.shared.createTask(
                    name: name.isEmpty ? String(prompt.prefix(40)) : name,
                    prompt: prompt, schedule: schedule, time: time)
                await onSaved()
                dismiss()
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}


struct EditTaskSheet: View {
    let task: TaskItem
    let onSaved: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var prompt = ""
    @State private var schedule = "daily"
    @State private var time = "09:00"
    @State private var mode = "sandbox"
    @State private var error: String?

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Edit Task").font(.system(size: 14, weight: .semibold))
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Save") { save() }
                    .buttonStyle(.glassProminent)
                    .tint(Color.accentColor)
                    .disabled(prompt.isEmpty)
            }
            .padding(14)
            Divider()
            Form {
                TextField("Name", text: $name)
                TextField("Prompt", text: $prompt, axis: .vertical)
                    .lineLimit(3...6)
                Picker("Repeat", selection: $schedule) {
                    Text("Daily").tag("daily")
                    Text("Weekly").tag("weekly")
                    Text("Monthly").tag("monthly")
                }
                TextField("Time (HH:MM)", text: $time)
                Picker("Agent access", selection: $mode) {
                    Text("Bound folder").tag("sandbox")
                    Text("Read-only").tag("readonly")
                    Text("Full access").tag("full")
                }
                Text("Tasks run as an agent. This sets how much of your files it can touch when it runs.")
                    .font(.caption).foregroundStyle(.secondary)
                if let error {
                    Text(error).font(.caption).foregroundStyle(.red)
                }
            }
            .formStyle(.grouped)
        }
        .frame(width: 420, height: 410)
        .onAppear {
            name = task.name ?? ""
            prompt = task.prompt ?? ""
            schedule = task.schedule ?? "daily"
            time = task.scheduledTime ?? "09:00"
            mode = task.permissionMode ?? "sandbox"
        }
    }

    private func save() {
        Task {
            do {
                try await APIClient.shared.updateTask(
                    id: task.id, name: name, prompt: prompt, schedule: schedule, time: time, permissionMode: mode)
                await onSaved()
                dismiss()
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}
