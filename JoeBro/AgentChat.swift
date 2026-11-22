import SwiftUI

// Coding agents as chats: transcript, composer, approvals. Used by AgentsPanel and the Inbox.

extension APIClient {
    /// Type an instruction into a coding agent (terminal now, or queued for its next turn).
    func codingSend(id: String, text: String) async throws -> String {
        let out = try await sendJSON("api/coding-agents/\(id)/send", body: ["text": text, "sender": "app"])
        return (out["delivered"] as? String) ?? "queued"
    }

    /// The agent's terminal screen (or its last hook lines when it has no controllable terminal).
    func codingScreen(id: String, lines: Int = 40) async throws -> String {
        let out = try await sendJSON("api/coding-agents/\(id)/screen?lines=\(lines)", method: "GET")
        if let s = out["screen"] as? String { return s }
        if let l = out["lines"] as? [String] { return l.joined(separator: "\n") }
        return ""
    }
}

/// Approve / deny a coding agent's pending permission prompt. Reports the outcome in place.
struct AgentApprovalButtons: View {
    let agentID: String
    var onDone: () async -> Void = {}
    @State private var busy = false
    @State private var note: String?

    var body: some View {
        HStack(spacing: JB.s2) {
            Button("Approve") { Task { await decide(true) } }.buttonStyle(.jbPrimary)
            Button("Deny") { Task { await decide(false) } }.buttonStyle(.jbDanger)
            if busy { JBSpinner() }
            if let note { Text(note).font(.jbMono(10.5)).foregroundStyle(JB.ink3).lineLimit(1) }
        }
        .disabled(busy)
    }

    private func decide(_ approve: Bool) async {
        busy = true
        defer { busy = false }
        do {
            let out = try await APIClient.shared.decideCoding(id: agentID, approve: approve)
            note = (out["applied"] as? Bool) == true ? (approve ? "approved" : "denied")
                : ((out["detail"] as? String) ?? "pending")
            await onDone()
        } catch { note = error.localizedDescription }
    }
}

/// Chat-style view of a coding agent: user prompts as raised cards, replies as plain text,
/// tool calls as one mono line, and a composer that talks to the agent.
struct AgentChatThread: View {
    let run: AgentRun
    var onChanged: () async -> Void = {}
    @State private var draft = ""
    @State private var sending = false
    @State private var echoes: [(text: String, via: String)] = []
    @State private var error: String?
    @State private var showScreen = false
    @State private var screen = ""
    @FocusState private var focused: Bool

    private var messages: [AgentMessage] { run.messages ?? [] }
    private var canSend: Bool { run.kind == "coding" && run.status != "ended" }
    private var working: Bool { run.live && run.status != "waiting" }

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: JB.s3) {
                        ForEach(Array(messages.enumerated()), id: \.offset) { i, m in
                            bubble(m, last: i == messages.count - 1)
                        }
                        ForEach(Array(echoes.enumerated()), id: \.offset) { _, e in userCard(e.text, tag: e.via) }
                        if working {
                            HStack(spacing: 6) { JBSpinner(); JBStreamCursor() }
                        }
                        if run.status == "waiting" && !messages.contains(where: { $0.role == "approval" }) {
                            approvalCard("needs your approval")
                        }
                        if showScreen { screenView }
                        Color.clear.frame(height: 1).id("end")
                    }
                    .padding(JB.s4)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .onChange(of: messages.count + echoes.count) { _, _ in
                    withAnimation(JB.smooth) { proxy.scrollTo("end", anchor: .bottom) }
                }
                .onAppear { proxy.scrollTo("end", anchor: .bottom) }
            }
            if canSend { composer }
        }
        .task(id: showScreen) {
            guard showScreen else { return }
            while !Task.isCancelled {
                screen = (try? await APIClient.shared.codingScreen(id: run.id)) ?? screen
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    @ViewBuilder private func bubble(_ m: AgentMessage, last: Bool) -> some View {
        switch m.role {
        case "user": userCard(m.content.replacingOccurrences(of: "[from app] ", with: ""), tag: nil)
        case "assistant":
            MarkdownText(text: m.content).textSelection(.enabled).foregroundStyle(JB.ink)
                .frame(maxWidth: .infinity, alignment: .leading)
        case "approval": approvalCard(m.content, active: last && run.status == "waiting")
        default:
            HStack(spacing: 6) {
                JBStatusDot(color: JB.ink3, size: 5)
                Text(m.content).font(.jbMono(11)).foregroundStyle(JB.ink2).lineLimit(2)
            }
        }
    }

    private func userCard(_ text: String, tag: String?) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(text).font(.jbBody).foregroundStyle(JB.ink).textSelection(.enabled)
            if let tag { Text(tag).jbEyebrow() }
        }
        .padding(JB.s3)
        .frame(maxWidth: .infinity, alignment: .leading)
        .jbSurface(.raised, radius: JB.rCard)
    }

    private func approvalCard(_ text: String, active: Bool = true) -> some View {
        VStack(alignment: .leading, spacing: JB.s2) {
            HStack(spacing: 6) { JBStatusDot(color: JB.warn, live: active, size: 6); Text("Needs you").jbEyebrow() }
            Text(text).font(.jbMono(11.5)).foregroundStyle(JB.ink)
            if active { AgentApprovalButtons(agentID: run.id, onDone: onChanged) }
        }
        .padding(JB.s3)
        .frame(maxWidth: .infinity, alignment: .leading)
        .jbSurface(.raised, radius: JB.rCard)
        .overlay(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).strokeBorder(JB.warn.opacity(active ? 0.5 : 0), lineWidth: 1))
    }

    private var screenView: some View {
        Text(screen.isEmpty ? " " : screen)
            .font(.jbMono(10.5)).foregroundStyle(JB.ink2).textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(JB.s3).jbSurface(.raised, radius: JB.rCard)
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .bottom, spacing: JB.s2) {
                TextField("Message \(run.title.isEmpty ? "agent" : run.title)", text: $draft, axis: .vertical)
                    .textFieldStyle(.plain).font(.jbBody).lineLimit(1...6)
                    .focused($focused)
                    .padding(.horizontal, JB.s3).padding(.vertical, 8)
                    .jbField(focused: focused)
                    .onSubmit { Task { await send() } }
                Button { showScreen.toggle() } label: { Image(systemName: "terminal") }
                    .buttonStyle(JBIconButtonStyle(size: 30, active: showScreen)).help("Terminal")
                Button { Task { await send() } } label: { Image(systemName: "arrow.up") }
                    .buttonStyle(JBIconButtonStyle(size: 30, active: !draft.isEmpty))
                    .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || sending)
                    .keyboardShortcut(.return, modifiers: .command)
            }
            if let error { Text(error).font(.jbMono(10.5)).foregroundStyle(JB.danger) }
        }
        .padding(JB.s3)
        .overlay(alignment: .top) { Rectangle().fill(JB.line).frame(height: 1) }
    }

    private func send() async {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !sending else { return }
        sending = true
        defer { sending = false }
        do {
            let how = try await APIClient.shared.codingSend(id: run.id, text: text)
            echoes.append((text, how == "terminal" ? "sent" : "queued for its next turn"))
            draft = ""; error = nil
            await onChanged()
        } catch { self.error = error.localizedDescription }
    }
}
