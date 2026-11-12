import SwiftUI

/// Night Shift: queue coding tasks for your agents overnight, review the results in the morning.
struct NightShiftPanel: View {
    private enum Page: String, CaseIterable { case review = "Morning review", queue = "Queue" }
    @State private var page = Page.review
    @State private var tasks: [NightTask] = []
    @State private var enabled = true
    @State private var composing = false
    @State private var followUp: NightTask?
    @State private var followUpText = ""
    @State private var rejecting: NightTask?
    @State private var diffTask: NightTask?
    @State private var note: String?

    private var reviews: [NightTask] { tasks.filter(\.needsReview) }
    private var queue: [NightTask] { tasks.filter(\.isOpen) }

    var body: some View {
        PanelChrome(title: "Night Shift", icon: "moon.stars") {
            Toggle("Run overnight", isOn: Binding(get: { enabled }, set: { v in enabled = v; Task { try? await APIClient.shared.nightConfig(enabled: v) } }))
                .toggleStyle(.switch).tint(.green).controlSize(.small)
            Button { composing = true } label: { Image(systemName: "plus") }.buttonStyle(.borderless).help("Queue a task")
            Button { Task { await load() } } label: { Image(systemName: "arrow.clockwise") }.buttonStyle(.borderless).help("Refresh")
        } content: {
            VStack(spacing: 10) {
                Picker("", selection: $page) { ForEach(Page.allCases, id: \.self) { Text($0.rawValue).tag($0) } }
                    .pickerStyle(.segmented).labelsHidden().frame(maxWidth: 420)
                if let note { Text(note).font(.system(size: 11.5)).foregroundStyle(.secondary) }
                ScrollView {
                    LazyVStack(spacing: 10) {
                        switch page {
                        case .review:
                            if reviews.isEmpty { emptyState("sun.max", "Nothing to review", "Finished overnight work shows up here with its diff and test result.") }
                            ForEach(reviews) { card($0) }
                        case .queue:
                            if queue.isEmpty { emptyState("moon.zzz", "Nothing queued", "Queue a task with + and your agents work on it overnight in separate git worktrees.") }
                            ForEach(Array(queue.enumerated()), id: \.element.id) { i, t in queueRow(t, index: i) }
                        }
                    }.padding(14)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity).joeGlassRect(16)
            }
        }
        .task { await load() }
        .sheet(isPresented: $composing) { NightComposerSheet { Task { await load() } } }
        .sheet(item: $diffTask) { NightDiffSheet(task: $0) }
        .alert("Ask a follow-up", isPresented: Binding(get: { followUp != nil }, set: { if !$0 { followUp = nil } })) {
            TextField("What should change?", text: $followUpText)
            Button("Send") { if let t = followUp { Task { await decide(t, "followup", followUpText) } }; followUp = nil }
            Button("Cancel", role: .cancel) { followUp = nil }
        } message: { Text("The agent continues on the same branch tonight.") }
        .alert("Reject this work?", isPresented: Binding(get: { rejecting != nil }, set: { if !$0 { rejecting = nil } })) {
            Button("Reject", role: .destructive) { if let t = rejecting { Task { await decide(t, "reject") } }; rejecting = nil }
            Button("Keep reviewing", role: .cancel) { rejecting = nil }
        } message: { Text("The worktree and branch are removed. The commit stays in git until it is garbage collected.") }
    }

    private func emptyState(_ icon: String, _ title: String, _ text: String) -> some View {
        ContentUnavailableView(title, systemImage: icon, description: Text(text)).frame(maxWidth: .infinity).padding(.vertical, 40)
    }

    private func card(_ t: NightTask) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(t.name).font(.system(size: 13.5, weight: .semibold))
                Spacer()
                if let ok = t.result?.tests?.passed {
                    Label(ok ? "Tests passed" : "Tests failed", systemImage: ok ? "checkmark.circle.fill" : "xmark.octagon.fill")
                        .font(.system(size: 11, weight: .semibold)).foregroundStyle(ok ? .green : .red)
                }
            }
            Text([t.repoName, t.agentLabel, t.model].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "))
                .font(.system(size: 11)).foregroundStyle(.secondary)
            if let d = t.result?.diff {
                HStack(spacing: 12) {
                    Text("\(d.files ?? 0) files")
                    Text("+\(d.insertions ?? 0)").foregroundStyle(.green)
                    Text("-\(d.deletions ?? 0)").foregroundStyle(.red)
                    if let s = t.result?.durationS { Text("\(Int(s / 60)) min").foregroundStyle(.secondary) }
                }.font(.system(size: 12, design: .monospaced))
                ForEach(d.fileList?.prefix(3).map { $0 } ?? [], id: \.path) { f in
                    HStack { Text(f.path).lineLimit(1); Spacer(); Text("+\(f.added ?? 0) -\(f.removed ?? 0)").foregroundStyle(.secondary) }
                        .font(.system(size: 11, design: .monospaced))
                }
            }
            if let s = t.result?.summary, !s.isEmpty { Text(s).font(.system(size: 12)).fixedSize(horizontal: false, vertical: true) }
            HStack(spacing: 8) {
                Button("Approve") { Task { await decide(t, "approve") } }.buttonStyle(.borderedProminent)
                Button("Reject", role: .destructive) { rejecting = t }
                Button("Follow-up") { followUp = t; followUpText = "" }
                Spacer()
                Button("See diff") { diffTask = t }.buttonStyle(.link)
            }.controlSize(.small)
        }
        .padding(12).frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 12))
    }

    private func queueRow(_ t: NightTask, index: Int) -> some View {
        HStack(spacing: 10) {
            if t.state == "running" { ProgressView().controlSize(.small) } else { Image(systemName: "moon.zzz").foregroundStyle(.secondary) }
            VStack(alignment: .leading, spacing: 2) {
                Text(t.name).font(.system(size: 12.5, weight: .medium)).lineLimit(2)
                Text([t.repoName, t.agentLabel, t.model].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · "))
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
            Spacer()
            if t.state == "queued" {
                Button { move(index, -1) } label: { Image(systemName: "arrow.up") }.disabled(index == 0 || queue[index - 1].state != "queued")
                Button { move(index, 1) } label: { Image(systemName: "arrow.down") }.disabled(index >= queue.count - 1)
                Button("Run now") { Task { await act { try await APIClient.shared.nightRun(t.id) } } }
            }
            Button("Cancel", role: .destructive) { Task { await act { try await APIClient.shared.nightCancel(t.id) } } }
        }
        .buttonStyle(.borderless).controlSize(.small)
        .padding(10).background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 10))
    }

    private func move(_ index: Int, _ delta: Int) {
        var ids = queue.filter { $0.state == "queued" }.map(\.id)
        let running = queue.count - ids.count
        let i = index - running, j = i + delta
        guard ids.indices.contains(i), ids.indices.contains(j) else { return }
        ids.swapAt(i, j)
        Task { await act { try await APIClient.shared.nightReorder(ids) } }
    }
    private func decide(_ t: NightTask, _ d: String, _ text: String? = nil) async {
        await act { try await APIClient.shared.nightDecide(t.id, decision: d, text: text) }
    }
    private func load() async {
        do { let r = try await APIClient.shared.nightShift(); tasks = r.tasks; enabled = r.config?.enabled ?? true; note = nil }
        catch where error.isNotFound { tasks = []; note = nil }
        catch { note = error.localizedDescription }
    }
    private func act(_ work: () async throws -> Void) async {
        do { try await work(); await load() } catch { note = error.localizedDescription }
    }
}

struct NightDiffSheet: View {
    let task: NightTask
    @State private var diff = ""
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack { Text(task.name).font(.headline); Spacer(); Button("Done") { dismiss() } }
            if let out = task.result?.tests?.outputTail, !out.isEmpty {
                Text(out).font(.system(size: 11, design: .monospaced)).padding(8).frame(maxWidth: .infinity, alignment: .leading)
                    .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
            }
            ForEach(task.result?.learned ?? [], id: \.self) { Label($0, systemImage: "brain").font(.system(size: 12)) }
            ScrollView { Text(diff).font(.system(size: 11, design: .monospaced)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
        }
        .padding(18).frame(minWidth: 640, minHeight: 440)
        .task { diff = (try? await APIClient.shared.nightDiff(task.id)) ?? "" }
    }
}

struct NightComposerSheet: View {
    let saved: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var repo = ""
    @State private var prompt = ""
    @State private var title = ""
    @State private var agent = "claude"
    @State private var model = ""
    @State private var tests = ""
    @State private var error: String?
    private static let models = ["claude": ["opus", "sonnet", "haiku"], "codex": ["gpt-5-codex", "gpt-5"],
                                 "opencode": ["qwen3-coder:30b", "llama3.3:70b", "deepseek-v3.2"], "any": []]
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Queue a task").font(.headline)
            TextField("What should the agent do?", text: $prompt, axis: .vertical).lineLimit(3...8).textFieldStyle(.roundedBorder)
            TextField("Title (optional)", text: $title).textFieldStyle(.roundedBorder)
            TextField("Repository path", text: $repo).textFieldStyle(.roundedBorder)
            HStack {
                Picker("Agent", selection: $agent) {
                    Text("Claude Code").tag("claude"); Text("Codex").tag("codex"); Text("OpenCode").tag("opencode"); Text("Any").tag("any")
                }
                Picker("Model", selection: $model) {
                    Text("Default").tag("")
                    ForEach(Self.models[agent] ?? [], id: \.self) { Text($0).tag($0) }
                }
            }.onChange(of: agent) { model = "" }
            TextField("Test command (optional)", text: $tests).textFieldStyle(.roundedBorder)
            if let error { Text(error).foregroundStyle(.red).font(.caption) }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Queue") { Task { await save() } }.keyboardShortcut(.defaultAction).disabled(prompt.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }.padding(20).frame(width: 520)
    }
    private func save() async {
        var body: [String: Any] = ["prompt": prompt, "agent": agent]
        if !repo.isEmpty { body["repo"] = repo }
        if !title.isEmpty { body["title"] = title }
        if !model.isEmpty { body["model"] = model }
        if !tests.isEmpty { body["test_command"] = tests }
        do { try await APIClient.shared.nightAdd(body); saved(); dismiss() } catch { self.error = error.localizedDescription }
    }
}
