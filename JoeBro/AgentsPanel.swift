import SwiftUI

/// Everything running on its own — deep research, background chat agents, and
/// scheduled tasks — from the backend's one agent registry.
///
/// List on the left, the selected run's full output on the right. While anything
/// is live the list re-polls so status and progress move without a refresh.
struct AgentsPanel: View {
    @Environment(AppStore.self) private var store
    @State private var runs: Loadable<[AgentRun]> = .loading
    @State private var kind = ""                 // "" = all
    @State private var activeOnly = false
    @State private var selectedID: String?
    @State private var detail: AgentRun?
    @State private var detailLoading = false
    @State private var cancelNote: String?
    @State private var search = ""
    @State private var searchOpen = false

    private let kinds = [("", "All"), ("research", "Research"), ("chat", "Chats"), ("task", "Tasks"), ("subagent", "Subagents"), ("coding", "Coding")]

    @State private var showNewAgent = false
    @State private var resuming = false
    @State private var resumeNote: String?

    private var liveCount: Int { if case .loaded(let l) = runs { return l.filter(\.live).count } else { return 0 } }

    var body: some View {
        PanelChrome(title: "Agents", icon: "person.3.sequence") {
            PanelSearchField(text: $search, open: $searchOpen, placeholder: "Search agents")
            Button { showNewAgent = true } label: { Image(systemName: "plus") }
                .buttonStyle(JBIconButtonStyle())
                .help("New coding agent")
            Button {
                Task { await load() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(JBIconButtonStyle())
            .help("Refresh")
        } content: {
            VStack(spacing: JB.s3) {
                HStack(spacing: JB.s3) {
                    HStack(spacing: 6) {
                        ForEach(kinds, id: \.0) { k in
                            Button { kind = k.0 } label: {
                                Text(k.1).font(.jb(11, .medium))
                                    .foregroundStyle(kind == k.0 ? JB.ink : JB.ink2)
                                    .padding(.horizontal, 9).padding(.vertical, 5)
                                    .background(RoundedRectangle(cornerRadius: JB.rControl, style: .continuous)
                                        .fill(kind == k.0 ? JB.accentSoft : .clear))
                            }.buttonStyle(.plain)
                        }
                    }
                    Spacer()
                    HStack(spacing: 8) {
                        JBDotNumber(value: "\(liveCount)", dot: 2, tint: liveCount > 0 ? JB.live : JB.ink3)
                        Text("live").jbEyebrow()
                    }
                    Toggle("Active only", isOn: $activeOnly)
                        .toggleStyle(JBSwitchStyle()).font(.jb(11)).controlSize(.small)
                }

                HStack(spacing: JB.s3) {
                    list
                        .frame(width: 330)
                        .frame(maxHeight: .infinity)
                        .joeGlassRect(JB.rPanel)
                    detailPane
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .joeGlassRect(JB.rPanel)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
        .task { await poll() }
        .sheet(isPresented: $showNewAgent) { NewCodingAgentSheet { Task { await load() } } }
        .onChange(of: kind) { Task { await load() } }
    }

    // MARK: list

    @ViewBuilder
    private var list: some View {
        switch runs {
        case .loading:
            JBSpinner(size: 14).frame(maxWidth: .infinity, maxHeight: .infinity)
        case .failed:
            JBEmptyState(art: .plug, text: "Couldn't load agents").frame(maxWidth: .infinity, maxHeight: .infinity)
        case .loaded(let all) where filtered(all).isEmpty:
            JBEmptyState(art: .sleep, text: "Nothing running").frame(maxWidth: .infinity, maxHeight: .infinity)
        case .loaded(let all):
            ScrollView {
                LazyVStack(spacing: 2) {
                    ForEach(nested(filtered(all)), id: \.run.id) { item in
                        row(item.run, all: all)
                            .padding(.leading, item.depth > 0 ? 18 : 0)
                    }
                }
                .padding(JB.s2)
            }
        }
    }

    @ViewBuilder private func kindGlyph(_ run: AgentRun) -> some View {
        switch run.kind {
        case "coding": JBIcon(.coding, size: 13)
        case "research": JBIcon(.research, size: 13)
        case "task": JBIcon(.tasks, size: 13)
        case "chat", "": Image(systemName: "bubble.left").font(.jb(11))
        default: Image(systemName: run.kindIcon).font(.jb(11))
        }
    }

    private func row(_ run: AgentRun, all: [AgentRun]) -> some View {
        let isSelected = selectedID == run.id
        return Button {
            if run.kind == "coding" && Self.isOpenSession(run) {
                // A running session is a conversation: open it in the chat area (no second chat pane here).
                AgentNav.shared.open(run.id)
                store.activeTab = .chat
                return
            }
            selectedID = run.id
            Task { await loadDetail(run.id) }
        } label: {
            HStack(alignment: .top, spacing: 9) {
                statusDot(run).padding(.top, 5)
                VStack(alignment: .leading, spacing: 3) {
                    Text(run.title.isEmpty ? run.kindLabel : run.title)
                        .font(.jb(12.5, isSelected ? .semibold : .regular))
                        .foregroundStyle(JB.ink)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                    HStack(spacing: 5) {
                        kindGlyph(run)
                        Text(run.live && !run.progress.isEmpty ? run.progress : run.status)
                            .lineLimit(1)
                    }
                    .font(.jb(10.5))
                    .foregroundStyle(run.status == "waiting" ? JB.warn : JB.ink2)
                    if run.parentID.isEmpty, case let kids = all.filter({ $0.parentID == run.id }), !kids.isEmpty {
                        Text("\(kids.count) sub · \(kids.filter(\.live).count) live")
                            .font(.jb(10)).foregroundStyle(JB.ink3)
                    }
                    Text(relativeDate(run.finishedAt.isEmpty ? run.createdAt : run.finishedAt))
                        .font(.jb(10)).foregroundStyle(JB.ink3)
                }
                Spacer(minLength: 0)
            }
            .padding(.vertical, 7)
            .padding(.horizontal, 9)
            .frame(maxWidth: .infinity, alignment: .leading)
            .jbHoverRow(selected: isSelected)
        }
        .buttonStyle(.plain)
    }

    private func statusDot(_ run: AgentRun) -> some View {
        JBStatusDot(color: colour(run.status), live: run.live && run.status != "waiting", size: 7)
            .help(run.status)
    }

    private func colour(_ status: String) -> Color {
        switch status {
        case "running": return JB.live
        case "waiting": return JB.warn        // a coding agent waiting for approval
        case "done": return JB.ink2
        case "failed": return JB.danger
        default: return JB.ink3
        }
    }

    // MARK: detail

    @ViewBuilder
    private var detailPane: some View {
        if let run = detail {
            VStack(spacing: 0) {
                header(run).padding(JB.s4)
                Rectangle().fill(JB.line).frame(height: 1)
                if run.kind == "coding" {
                    // Finished: an overview (read-only transcript); Resume brings it back as a live chat.
                    AgentChatThread(run: run, readOnly: !Self.isOpenSession(run)) { await load(); await loadDetail(run.id, quiet: true) }
                        .id(run.id)
                } else {
                    ScrollView {
                        VStack(alignment: .leading, spacing: JB.s3) {
                            if !run.error.isEmpty {
                                Text(run.error).font(.jb(11.5))
                                    .foregroundStyle(run.status == "failed" ? JB.danger : JB.ink2)
                            }
                            if let note = cancelNote { Text(note).font(.jb(11)).foregroundStyle(JB.ink2) }
                            if run.result.isEmpty {
                                if run.live { HStack(spacing: 6) { JBSpinner(); JBStreamCursor() } }
                                else { Text("No output").font(.jb(11)).foregroundStyle(JB.ink3) }
                            } else {
                                MarkdownText(text: run.result).textSelection(.enabled)
                            }
                            if let msgs = run.messages, !msgs.isEmpty {
                                Text("Transcript").jbEyebrow().padding(.top, JB.s2)
                                ForEach(Array(msgs.prefix(12).enumerated()), id: \.offset) { _, m in
                                    if m.role == "user" {
                                        Text(m.content.prefix(600)).font(.jbBody).foregroundStyle(JB.ink)
                                            .textSelection(.enabled).jbCard(padding: JB.s3)
                                    } else {
                                        Text(m.content.prefix(600)).font(.jbBody).foregroundStyle(JB.ink)
                                            .textSelection(.enabled)
                                    }
                                }
                            }
                        }
                        .padding(JB.s4)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
            .overlay(alignment: .topTrailing) {
                if detailLoading { JBSpinner().padding(10) }
            }
        } else {
            JBEmptyState(art: .inbox, text: "Select an agent").frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func header(_ run: AgentRun) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                kindGlyph(run).foregroundStyle(JB.ink2)
                Text(run.title.isEmpty ? run.kindLabel : run.title)
                    .font(.jb(15, .semibold)).tracking(-0.2)
                    .foregroundStyle(JB.ink)
                Spacer(minLength: 8)
                if run.kind == "coding" && run.status == "waiting" {
                    AgentApprovalButtons(agentID: run.id) { await load(); await loadDetail(run.id, quiet: true) }
                }
                if run.live {
                    Button { Task { await cancel(run) } } label: { Text("Stop") }
                        .buttonStyle(.jbSecondary).help("Ask this agent to stop")
                }
                if run.kind == "coding" && run.parentID.isEmpty && !Self.isOpenSession(run) {
                    if let resumeNote { Text(resumeNote).font(.jb(11.5)).foregroundStyle(JB.ink2) }
                    Button { Task { await resume(run) } } label: {
                        Label(resuming ? "Resuming…" : "Resume chat", systemImage: "arrow.uturn.forward")
                    }
                    .buttonStyle(.jbPrimary).disabled(resuming)
                    .help("Carry this session on, on the computer it ran on")
                } else if run.kind == "coding" && Self.isOpenSession(run) {
                    Button { AgentNav.shared.open(run.id); store.activeTab = .chat } label: { Text("Open chat") }
                        .buttonStyle(.jbSecondary)
                }
                if !run.sessionID.isEmpty && run.kind != "coding" {
                    Button { store.select(run.sessionID) } label: { Text("Open chat") }
                        .buttonStyle(.jbSecondary).help("Open this agent's chat")
                }
            }
            HStack(spacing: 6) {
                statusDot(run)
                Text(run.live && !run.progress.isEmpty ? "\(run.status) · \(run.progress)" : run.status)
                    .lineLimit(1)
                if !run.model.isEmpty { Text("·"); Text(run.model) }
                if run.resultChars > 0 { Text("·"); Text("\(run.resultChars) chars") }
                Text("·"); Text(relativeDate(run.finishedAt.isEmpty ? run.createdAt : run.finishedAt))
            }
            .font(.jb(11))
            .foregroundStyle(JB.ink2)
        }
    }

    // MARK: data

    /// Still a conversation you can type into (running, waiting on you, or open between turns).
    static func isOpenSession(_ r: AgentRun) -> Bool { r.live || r.status == "waiting" || r.status == "idle" }

    /// Resume a finished session where it ran, then open it as a chat once it's back.
    private func resume(_ run: AgentRun) async {
        resuming = true; resumeNote = nil
        defer { resuming = false }
        do {
            let r = try await APIClient.shared.sendJSON("api/coding-agents/start", body: ["resume": run.id])
            guard (r["ok"] as? Bool) == true else { resumeNote = r["detail"] as? String ?? "Couldn't resume"; return }
            if (r["started"] as? Bool) != true { resumeNote = "Queued — it starts when that computer is online"; return }
            // Claude keeps the session id on --resume; others may report a new one in the same folder.
            for _ in 0..<10 {
                await AgentNav.shared.refresh()
                if let back = AgentNav.shared.runs.first(where: { $0.id == run.id && Self.isOpenSession($0) })
                    ?? AgentNav.shared.runs.first(where: { $0.live && (parseISO($0.startedAt) ?? .distantPast) > Date().addingTimeInterval(-60) }) {
                    AgentNav.shared.open(back.id); store.activeTab = .chat
                    return
                }
                try? await Task.sleep(for: .seconds(1.5))
            }
            AgentNav.shared.open(run.id); store.activeTab = .chat
        } catch {
            resumeNote = error.localizedDescription
        }
    }

    /// Name of the agent/session that spawned `run`, falling back to a short id.
    private func parentName(_ run: AgentRun, in all: [AgentRun]) -> String {
        if let p = all.first(where: { $0.id == run.parentID || (!$0.sessionID.isEmpty && $0.sessionID == run.parentID) }) {
            return p.title.isEmpty ? p.kindLabel : p.title
        }
        return String(run.parentID.prefix(8))
    }

    /// Children listed right under their parent (when it is in the list); the rest keep order.
    private func nested(_ list: [AgentRun]) -> [(run: AgentRun, depth: Int)] {
        let ids = Set(list.map(\.id))
        let sessions = Dictionary(list.filter { !$0.sessionID.isEmpty }.map { ($0.sessionID, $0.id) },
                                  uniquingKeysWith: { a, _ in a })
        func parentKey(_ r: AgentRun) -> String? {
            if r.parentID.isEmpty || r.parentID == r.id { return nil }
            if ids.contains(r.parentID) { return r.parentID }
            return sessions[r.parentID]
        }
        let kids = Dictionary(grouping: list.filter { parentKey($0) != nil }) { parentKey($0)! }
        var out: [(run: AgentRun, depth: Int)] = []
        for r in list where parentKey(r) == nil {
            out.append((r, 0))
            for k in kids[r.id] ?? [] { out.append((k, 1)) }
        }
        return out
    }

    private func filtered(_ list: [AgentRun]) -> [AgentRun] {
        let base = activeOnly ? list.filter(\.live) : list
        let q = search.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return base }
        return base.filter {
            ($0.title + " " + $0.progress + " " + $0.kindLabel).localizedCaseInsensitiveContains(q)
        }
    }

    private func load() async {
        do {
            let env = try await APIClient.shared.agents(kind: kind)
            runs = .loaded(env.agents)
            // Keep the open detail fresh while its run is still going.
            if let id = selectedID, env.agents.first(where: { $0.id == id })?.live == true {
                await loadDetail(id, quiet: true)
            }
        } catch {
            runs = .failed(error.localizedDescription)
        }
    }

    private func loadDetail(_ id: String, quiet: Bool = false) async {
        if !quiet { detailLoading = true; cancelNote = nil }
        defer { detailLoading = false }
        do {
            let run = try await APIClient.shared.agentDetail(id: id)
            guard selectedID == id else { return }      // selection moved on
            detail = run
        } catch {
            if !quiet { cancelNote = error.localizedDescription }
        }
    }

    /// Poll only while something is live; an idle panel makes no requests.
    private func poll() async {
        await load()
        while !Task.isCancelled {
            let live: Bool
            if case .loaded(let list) = runs { live = list.contains { $0.live } } else { live = false }
            try? await Task.sleep(for: .seconds(live ? 3 : 20))
            if Task.isCancelled { return }
            await load()
        }
    }

    private func cancel(_ run: AgentRun) async {
        do {
            // The backend tells us what stopping actually means for this kind;
            // show that instead of pretending it died on the spot.
            cancelNote = try await APIClient.shared.cancelAgent(id: run.id)
        } catch {
            cancelNote = error.localizedDescription
        }
        await load()
        await loadDetail(run.id, quiet: true)
    }
}
