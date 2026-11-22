import SwiftUI

/// Which coding agent is open in the chat area, plus the live list the sidebar shows.
/// Coding agents live alongside chats: pick one in the sidebar (or the Agents tab)
/// and its conversation takes the chat area, looking exactly like a JoeBro chat.
@MainActor @Observable
final class AgentNav {
    static let shared = AgentNav()
    var openID: String?
    var runs: [AgentRun] = []          // coding agents worth showing (live or recent)
    private var polling = false

    func open(_ id: String) { openID = id }
    func close() { openID = nil }

    func startPolling() {
        guard !polling else { return }
        polling = true
        Task {
            while polling {
                await refresh()
                try? await Task.sleep(for: .seconds(runs.contains { $0.live } ? 6 : 20))
            }
        }
    }

    func refresh() async {
        guard let env = try? await APIClient.shared.agents(kind: "coding") else { return }
        // Exactly the backend's rule, so the sidebar, the iPhone's Chats and both Agents pages agree:
        // top-level coding agents still open in their terminal (subagents live inside their parent).
        runs = env.agents.filter { $0.kind == "coding" && $0.parentID.isEmpty && ($0.open || $0.live || $0.status == "waiting") }
    }
}

/// The chat-area view of one coding agent.
struct CodingAgentConversation: View {
    let agentID: String
    @Binding var sidebarVisible: Bool
    @State private var run: AgentRun?
    @State private var stopping = false

    var body: some View {
        VStack(spacing: 12) {
            header
            if let run {
                AgentChatThread(run: run) { await load() }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                Spacer()
                JBSpinner()
                Spacer()
            }
        }
        .task(id: agentID) {
            run = nil
            while !Task.isCancelled {
                await load()
                try? await Task.sleep(for: .seconds(run?.live == true ? 3 : 10))
            }
        }
    }

    private func load() async {
        if let r = try? await APIClient.shared.agentDetail(id: agentID) { run = r }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Button { sidebarVisible.toggle() } label: { Image(systemName: "sidebar.left") }
                .buttonStyle(JBIconButtonStyle(size: 26))
                .help("Toggle sidebar")
            JBIcon(.coding, size: 13).foregroundStyle(JB.ink2)
            Text(run?.title.isEmpty == false ? run!.title : "Coding agent")
                .font(.jb(14, .semibold)).foregroundStyle(JB.ink).lineLimit(1)
            if let run { statusChip(run) }
            Spacer()
            if let run, run.live {
                Button(stopping ? "Stopping…" : "Stop") {
                    stopping = true
                    Task { _ = try? await APIClient.shared.cancelAgent(id: run.id); await load(); stopping = false }
                }
                .buttonStyle(.jbSecondary)
                .disabled(stopping)
            }
        }
        .padding(.horizontal, 12).frame(height: 46)
        .jbSurface(.chrome, radius: JB.rPanel)
    }

    private func statusChip(_ r: AgentRun) -> some View {
        let waiting = r.status == "waiting"
        let color = waiting || r.status == "limited" ? JB.warn : r.live ? JB.live : JB.ink3
        return HStack(spacing: 5) {
            JBStatusDot(color: color, live: r.live, size: 6)
            Text(waiting ? "Needs you" : r.live ? "Working" : r.status == "limited" ? "Hit usage limit" : r.status.capitalized)
                .font(.jb(12, .medium)).foregroundStyle(color)
        }
    }
}

/// Sidebar section: coding agents as conversations.
struct SidebarCodingAgents: View {
    @Environment(AppStore.self) private var store
    @State private var nav = AgentNav.shared

    var body: some View {
        if !nav.runs.isEmpty {
            VStack(alignment: .leading, spacing: 1) {
                ForEach(nav.runs) { r in
                    let sel = store.activeTab == .chat && nav.openID == r.id
                    Button {
                        nav.open(r.id)
                        store.activeTab = .chat
                    } label: {
                        HStack(spacing: 9) {
                            JBStatusDot(color: r.status == "waiting" ? JB.warn : r.live ? JB.live : JB.ink3,
                                        live: r.live, size: 6)
                                .frame(width: 14)
                            Text(r.title.isEmpty ? "Coding agent" : r.title)
                                .font(.jb(13)).foregroundStyle(JB.ink).lineLimit(1)
                            Spacer(minLength: 4)
                            if r.status == "waiting" {
                                Text("Needs you").font(.jb(11, .medium)).foregroundStyle(JB.warn)
                            }
                        }
                        .padding(.horizontal, 10).frame(height: 30)
                        .jbHoverRow(selected: sel)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }
}
