import SwiftUI

/// Night Shift: queue coding tasks for your agents overnight, review the results in the morning.
struct NightShiftPanel: View {
    private enum Page: String, CaseIterable { case review = "Review", queue = "Queue" }
    @State private var page = Page.review
    @State private var tasks: [NightTask] = []
    @State private var hosts: [NightHost] = []
    @State private var enabled = true
    @State private var composing = false
    @State private var followUp: NightTask?
    @State private var followUpText = ""
    @State private var rejecting: NightTask?
    @State private var diffTask: NightTask?
    @State private var note: String?

    private var reviews: [NightTask] { tasks.filter(\.needsReview) }
    private var queue: [NightTask] { tasks.filter(\.isOpen) }
    private var multiHost: Bool { hosts.count > 1 }

    var body: some View {
        ExtrasShell(title: "Night Shift", glyph: .nightShift) {
            Toggle("Overnight", isOn: Binding(get: { enabled }, set: { v in enabled = v; Task { try? await APIClient.shared.nightConfig(enabled: v) } }))
                .toggleStyle(JBSwitchStyle()).controlSize(.small).font(.jbMono(11)).foregroundStyle(JB.ink2)
            ExtrasIconButton(symbol: "plus", help: "Queue a task") { composing = true }
            ExtrasIconButton(symbol: "arrow.clockwise", help: "Refresh") { Task { await load() } }
        } content: {
            VStack(spacing: JB.s3) {
                HStack(spacing: JB.s2) {
                    ForEach(Page.allCases, id: \.self) { p in
                        Button { withAnimation(JB.snap) { page = p } } label: {
                            HStack(spacing: 6) {
                                Text(p.rawValue)
                                let n = p == .review ? reviews.count : queue.count
                                if n > 0 { Text("\(n)").font(.jbMono(10.5)).foregroundStyle(page == p ? JB.accent : JB.ink3) }
                            }
                        }
                        .buttonStyle(JBButtonStyle(kind: page == p ? .secondary : .ghost, compact: true))
                    }
                    Spacer()
                    if let note { Text(note).font(.jbMono(11)).foregroundStyle(JB.danger).lineLimit(1) }
                }
                ScrollView {
                    LazyVStack(spacing: JB.s2) {
                        switch page {
                        case .review:
                            if reviews.isEmpty { JBEmptyState(art: .done, text: "Nothing to review") }
                            ForEach(reviews) { card($0).transition(.opacity.combined(with: .move(edge: .top))) }
                        case .queue:
                            if queue.isEmpty { JBEmptyState(art: .sleep, text: "Nothing queued") }
                            ForEach(Array(queue.enumerated()), id: \.element.id) { i, t in
                                queueRow(t, index: i).transition(.opacity)
                            }
                        }
                    }
                    .padding(JB.s3)
                    .animation(JB.smooth, value: tasks.map(\.id))
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity).jbSurface(.panel)
            }
        }
        .task { await load() }
        .sheet(isPresented: $composing) { NightComposerSheet(hosts: hosts) { Task { await load() } } }
        .sheet(item: $diffTask) { NightDiffSheet(task: $0) }
        .alert("Follow-up", isPresented: Binding(get: { followUp != nil }, set: { if !$0 { followUp = nil } })) {
            TextField("What should change?", text: $followUpText)
            Button("Send") { if let t = followUp { Task { await decide(t, "followup", followUpText) } }; followUp = nil }
            Button("Cancel", role: .cancel) { followUp = nil }
        }
        .alert("Reject this work?", isPresented: Binding(get: { rejecting != nil }, set: { if !$0 { rejecting = nil } })) {
            Button("Reject", role: .destructive) { if let t = rejecting { Task { await decide(t, "reject") } }; rejecting = nil }
            Button("Keep reviewing", role: .cancel) { rejecting = nil }
        } message: { Text("The worktree and branch are removed.") }
    }

    private func meta(_ t: NightTask) -> String {
        var bits = [t.repoName, t.agentLabel, t.model]
        if multiHost, let h = t.host, !h.isEmpty { bits.append(hosts.first { $0.id == h }?.label ?? h) }
        return bits.compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
    }

    private func card(_ t: NightTask) -> some View {
        VStack(alignment: .leading, spacing: JB.s2) {
            HStack(spacing: 8) {
                JBStatusDot(color: t.state == "failed" ? JB.warn : JB.accent)
                Text(t.name).font(.jb(13.5, .semibold)).tracking(-0.1).foregroundStyle(JB.ink)
                Spacer()
                if let ok = t.result?.tests?.passed { JBChip(text: ok ? "tests ok" : "tests failed", tint: ok ? JB.live : JB.danger) }
            }
            Text(meta(t)).font(.jbMono(11)).foregroundStyle(JB.ink3)
            if let d = t.result?.diff {
                HStack(spacing: 12) {
                    Text("\(d.files ?? 0) files").foregroundStyle(JB.ink2)
                    Text("+\(d.insertions ?? 0)").foregroundStyle(JB.live)
                    Text("-\(d.deletions ?? 0)").foregroundStyle(JB.danger)
                    if let s = t.result?.durationS { Text("\(max(1, Int(s / 60)))m").foregroundStyle(JB.ink3) }
                }.font(.jbMono(11.5))
                ForEach(d.fileList?.prefix(3).map { $0 } ?? [], id: \.path) { f in
                    HStack { Text(f.path).lineLimit(1).truncationMode(.head); Spacer(); Text("+\(f.added ?? 0) -\(f.removed ?? 0)").foregroundStyle(JB.ink3) }
                        .font(.jbMono(11)).foregroundStyle(JB.ink2)
                }
            }
            if let s = t.result?.summary ?? t.error, !s.isEmpty { Text(s).font(.jb(12)).foregroundStyle(JB.ink2).fixedSize(horizontal: false, vertical: true) }
            HStack(spacing: JB.s2) {
                if t.state == "review" { Button("Approve") { Task { await decide(t, "approve") } }.buttonStyle(JBButtonStyle(kind: .primary, compact: true)) }
                Button("Follow-up") { followUp = t; followUpText = "" }.buttonStyle(JBButtonStyle(kind: .secondary, compact: true))
                Button("Reject") { rejecting = t }.buttonStyle(JBButtonStyle(kind: .danger, compact: true))
                Spacer()
                Button("Diff") { diffTask = t }.buttonStyle(JBButtonStyle(kind: .ghost, compact: true))
            }
        }
        .jbCard()
    }

    private func queueRow(_ t: NightTask, index: Int) -> some View {
        HoverActionRow(pinned: t.state == "running") {
            if t.state == "running" { JBStatusDot(live: true) } else { JBIcon(.nightShift, size: 14).foregroundStyle(JB.ink3) }
            VStack(alignment: .leading, spacing: 2) {
                Text(t.name).font(.jb(12.5, .medium)).foregroundStyle(JB.ink).lineLimit(2)
                Text(meta(t)).font(.jbMono(11)).foregroundStyle(JB.ink3)
            }
        } actions: {
            HStack(spacing: 2) {
                if t.state == "queued" {
                    ExtrasIconButton(symbol: "arrow.up", help: "Move up") { move(index, -1) }.disabled(index == 0 || queue[index - 1].state != "queued")
                    ExtrasIconButton(symbol: "arrow.down", help: "Move down") { move(index, 1) }.disabled(index >= queue.count - 1)
                    ExtrasIconButton(symbol: "play", help: "Run now") { Task { await act { try await APIClient.shared.nightRun(t.id) } } }
                }
                ExtrasIconButton(symbol: "xmark", help: "Cancel") { Task { await act { try await APIClient.shared.nightCancel(t.id) } } }
            }
        }
        .contextMenu {
            if t.state == "queued" { Button("Run now") { Task { await act { try await APIClient.shared.nightRun(t.id) } } } }
            Button("Cancel", role: .destructive) { Task { await act { try await APIClient.shared.nightCancel(t.id) } } }
        }
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
        do {
            let r = try await APIClient.shared.nightShift()
            withAnimation(JB.smooth) { tasks = r.tasks }
            hosts = r.hosts; enabled = r.config?.enabled ?? true; note = nil
        }
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
        VStack(spacing: 0) {
            ExtrasSheetHeader(title: task.name) { Button("Done") { dismiss() }.buttonStyle(JBButtonStyle(kind: .secondary, compact: true)) }
            VStack(alignment: .leading, spacing: JB.s2) {
                if let out = task.result?.tests?.outputTail, !out.isEmpty {
                    Text(out).font(.jbMono(11)).foregroundStyle(JB.ink2).jbCard(padding: JB.s2)
                }
                ForEach(task.result?.learned ?? [], id: \.self) { l in
                    HStack(alignment: .top, spacing: 6) { JBIcon(.memory, size: 13).foregroundStyle(JB.accent); Text(l).font(.jb(12)).foregroundStyle(JB.ink2) }
                }
                ScrollView { Text(diff).font(.jbMono(11)).foregroundStyle(JB.ink).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
            }.padding(JB.s3)
        }
        .frame(minWidth: 640, minHeight: 440).background(JB.surface)
        .task { diff = (try? await APIClient.shared.nightDiff(task.id)) ?? "" }
    }
}

struct NightComposerSheet: View {
    var hosts: [NightHost] = []
    let saved: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var repo = ""
    @State private var prompt = ""
    @State private var title = ""
    @State private var agent = "claude"
    @State private var model = ""
    @State private var host = ""
    @State private var tests = ""
    @State private var error: String?
    @FocusState private var focus: Field?
    private enum Field { case prompt, title, tests }
    var body: some View {
        VStack(spacing: 0) {
            ExtrasSheetHeader(title: "Queue a task") {
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction).buttonStyle(JBButtonStyle(kind: .ghost, compact: true))
                Button("Queue") { Task { await save() } }.keyboardShortcut(.defaultAction).buttonStyle(JBButtonStyle(kind: .primary, compact: true))
                    .disabled(prompt.trimmingCharacters(in: .whitespaces).isEmpty || repo.isEmpty)
            }
            VStack(alignment: .leading, spacing: JB.s3) {
                TextField("What should the agent do?", text: $prompt, axis: .vertical).lineLimit(3...8).focused($focus, equals: .prompt).jbField(focused: focus == .prompt)
                TextField("Title", text: $title).focused($focus, equals: .title).jbField(focused: focus == .title)
                CodingTargetFields(agent: $agent, model: $model, host: $host, repo: $repo, hosts: hosts)
                TextField("Test command", text: $tests).focused($focus, equals: .tests).jbField(focused: focus == .tests)
                if let error { Text(error).font(.jbMono(11)).foregroundStyle(JB.danger) }
            }.padding(JB.s4)
        }.frame(width: 540).background(JB.surface)
    }
    private func save() async {
        var body: [String: Any] = ["prompt": prompt, "agent": agent, "repo": repo]
        if !title.isEmpty { body["title"] = title }
        if !model.isEmpty { body["model"] = model }
        if !host.isEmpty { body["host"] = host }
        if !tests.isEmpty { body["test_command"] = tests }
        do { try await APIClient.shared.nightAdd(body); saved(); dismiss() } catch { self.error = error.localizedDescription }
    }
}
