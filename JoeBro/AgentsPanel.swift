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

    var body: some View {
        PanelChrome(title: "Agents", icon: "person.3.sequence") {
            PanelSearchField(text: $search, open: $searchOpen, placeholder: "Search agents")
            Button {
                Task { await load() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .help("Refresh")
        } content: {
            VStack(spacing: 10) {
                Picker("", selection: $kind) {
                    ForEach(kinds, id: \.0) { Text($0.1).tag($0.0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(maxWidth: 420)

                Toggle("Active only", isOn: $activeOnly)
                    .toggleStyle(.switch)
                    .controlSize(.small)
                    .help("Show only agents working right now")

                HStack(spacing: 12) {
                    list
                        .frame(width: 330)
                        .frame(maxHeight: .infinity)
                        .joeGlassRect(16)
                    detailPane
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .joeGlassRect(16)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
        .task { await poll() }
        .onChange(of: kind) { Task { await load() } }
    }

    // MARK: list

    @ViewBuilder
    private var list: some View {
        switch runs {
        case .loading:
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        case .failed(let e):
            ContentUnavailableView("Couldn't load agents", systemImage: "person.3.sequence",
                                   description: Text(e))
        case .loaded(let all) where filtered(all).isEmpty:
            ContentUnavailableView("Nothing running", systemImage: "person.3.sequence",
                                   description: Text("Research, background agents and scheduled tasks appear here."))
        case .loaded(let all):
            ScrollView {
                LazyVStack(spacing: 8) {
                    ForEach(nested(filtered(all)), id: \.run.id) { item in
                        row(item.run, all: all)
                            .padding(.leading, item.depth > 0 ? 18 : 0)
                    }
                }
                .padding(12)
            }
        }
    }

    private func row(_ run: AgentRun, all: [AgentRun]) -> some View {
        let isSelected = selectedID == run.id
        return Button {
            selectedID = run.id
            Task { await loadDetail(run.id) }
        } label: {
            HStack(alignment: .top, spacing: 9) {
                statusDot(run)
                    .padding(.top, 4)
                VStack(alignment: .leading, spacing: 3) {
                    Text(run.title.isEmpty ? run.kindLabel : run.title)
                        .font(.system(size: 12.5, weight: isSelected ? .semibold : .regular))
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                    HStack(spacing: 5) {
                        Image(systemName: run.kindIcon).font(.system(size: 9))
                        Text(run.kindLabel)
                        Text("·")
                        Text(run.live && !run.progress.isEmpty ? run.progress : run.status)
                            .lineLimit(1)
                    }
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
                    if run.parentID.isEmpty, case let kids = all.filter({ $0.parentID == run.id }), !kids.isEmpty {
                        Label("\(kids.count) subagent\(kids.count == 1 ? "" : "s") · \(kids.filter(\.live).count) running", systemImage: "arrow.triangle.branch")
                            .font(.system(size: 10)).foregroundStyle(.secondary)
                    }
                    if !run.parentID.isEmpty {
                        Text("spawned by \(parentName(run, in: all))")
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    Text(relativeDate(run.finishedAt.isEmpty ? run.createdAt : run.finishedAt))
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                }
                Spacer(minLength: 0)
                if run.live {
                    ProgressView().controlSize(.small)
                }
            }
            .padding(.vertical, 7)
            .padding(.horizontal, 9)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(isSelected ? AnyShapeStyle(.quaternary.opacity(0.5)) : AnyShapeStyle(.clear),
                        in: RoundedRectangle(cornerRadius: 9))
        }
        .buttonStyle(.plain)
    }

    private func statusDot(_ run: AgentRun) -> some View {
        Circle()
            .fill(colour(run.status))
            .frame(width: 7, height: 7)
            .help(run.status)
    }

    private func colour(_ status: String) -> Color {
        switch status {
        case "running": return .yellow
        case "waiting": return .orange        // a coding agent waiting for approval
        case "done": return .green
        case "failed": return .red
        case "cancelled": return .secondary
        default: return .secondary.opacity(0.6)       // queued
        }
    }

    // MARK: detail

    @ViewBuilder
    private var detailPane: some View {
        if let run = detail {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    header(run)
                    if !run.error.isEmpty {
                        Label(run.error, systemImage: "exclamationmark.triangle")
                            .font(.system(size: 11.5))
                            .foregroundStyle(run.status == "failed" ? .red : .secondary)
                    }
                    if let note = cancelNote {
                        Label(note, systemImage: "stop.circle")
                            .font(.system(size: 11.5))
                            .foregroundStyle(.secondary)
                    }
                    if run.result.isEmpty {
                        Text(run.live ? "No output yet — still working."
                                      : "This run produced no output.")
                            .font(.system(size: 12))
                            .foregroundStyle(.secondary)
                    } else {
                        MarkdownText(text: run.result)
                    }
                    if let msgs = run.messages, !msgs.isEmpty {
                        Divider().padding(.vertical, 2)
                        Text("Transcript — \(msgs.count) message\(msgs.count == 1 ? "" : "s")")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(.secondary)
                        ForEach(msgs.prefix(12)) { m in
                            VStack(alignment: .leading, spacing: 2) {
                                Text(m.role.capitalized)
                                    .font(.system(size: 10, weight: .semibold))
                                    .foregroundStyle(.tertiary)
                                Text(m.content.prefix(600))
                                    .font(.system(size: 11.5))
                                    .textSelection(.enabled)
                            }
                        }
                    } else if !run.sessionID.isEmpty {
                        Text("No stored transcript for this run — its chat has been deleted.")
                            .font(.system(size: 11))
                            .foregroundStyle(.tertiary)
                    }
                }
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .overlay(alignment: .topTrailing) {
                if detailLoading { ProgressView().controlSize(.small).padding(10) }
            }
        } else {
            ContentUnavailableView("Select an agent", systemImage: "sidebar.right",
                                   description: Text("Its full output and transcript show here."))
        }
    }

    private func header(_ run: AgentRun) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: run.kindIcon).foregroundStyle(.secondary)
                Text(run.title.isEmpty ? run.kindLabel : run.title)
                    .font(.system(size: 15, weight: .semibold))
                Spacer(minLength: 8)
                if run.kind == "coding" && run.status == "waiting" {
                    Button {
                        Task { await decide(run, approve: true) }
                    } label: {
                        Label("Approve", systemImage: "checkmark.circle")
                    }
                    .buttonStyle(.borderless)
                    .help("Approve the permission prompt in that terminal")
                    Button {
                        Task { await decide(run, approve: false) }
                    } label: {
                        Label("Deny", systemImage: "xmark.circle")
                    }
                    .buttonStyle(.borderless)
                    .help("Deny it (Esc in that terminal)")
                }
                if run.live {
                    Button {
                        Task { await cancel(run) }
                    } label: {
                        Label("Stop", systemImage: "stop.fill")
                    }
                    .buttonStyle(.borderless)
                    .help("Ask this agent to stop")
                }
                if !run.sessionID.isEmpty {
                    Button {
                        store.select(run.sessionID)
                    } label: {
                        Label("Open chat", systemImage: "bubble.left.and.bubble.right")
                    }
                    .buttonStyle(.borderless)
                    .help("Open this agent's chat")
                }
            }
            HStack(spacing: 6) {
                statusDot(run)
                Text(run.live && !run.progress.isEmpty ? "\(run.status) — \(run.progress)" : run.status)
                if !run.model.isEmpty {
                    Text("·"); Text(run.model)
                }
                if run.resultChars > 0 {
                    Text("·"); Text("\(run.resultChars) chars")
                }
                Text("·"); Text(relativeDate(run.finishedAt.isEmpty ? run.createdAt : run.finishedAt))
            }
            .font(.system(size: 11))
            .foregroundStyle(.secondary)
        }
    }

    // MARK: data

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

    /// Poll only while something is live — an idle panel makes no requests.
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

    private func decide(_ run: AgentRun, approve: Bool) async {
        do {
            let out = try await APIClient.shared.decideCoding(id: run.id, approve: approve)
            if (out["applied"] as? Bool) == true {
                cancelNote = approve ? "Approved." : "Denied."
            } else {
                cancelNote = (out["detail"] as? String) ?? "Sent — it will apply as soon as the prompt is on screen."
            }
            await load()
        } catch {
            cancelNote = error.localizedDescription
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
