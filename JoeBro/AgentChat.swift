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
            if let note { Text(note).font(.jb(10.5)).foregroundStyle(JB.ink3).lineLimit(1) }
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
    @State private var mic = MicRecorder()
    @State private var sending = false
    @State private var echoes: [(text: String, via: String)] = []
    @State private var error: String?
    @State private var showScreen = false
    @State private var screen = ""
    @FocusState private var focused: Bool

    private var messages: [AgentMessage] { run.messages ?? [] }

    private enum Item { case message(AgentMessage), tools([String]) }
    /// Consecutive tool lines fold into one row.
    private var items: [Item] {
        var out: [Item] = []
        for m in messages {
            if !["user", "assistant", "approval"].contains(m.role) {
                if case .tools(let l) = out.last { out[out.count - 1] = .tools(l + [m.content]) } else { out.append(.tools([m.content])) }
            } else { out.append(.message(m)) }
        }
        return out
    }
    private var canSend: Bool { run.kind == "coding" && run.status != "ended" }
    private var working: Bool { run.live && run.status != "waiting" }

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        ForEach(Array(items.enumerated()), id: \.offset) { i, item in
                            switch item {
                            case .message(let m): bubble(m, last: i == items.count - 1)
                            case .tools(let lines): AgentToolGroup(lines: lines, running: working && i == items.count - 1)
                            }
                        }
                        // Local echo only until the agent's own feed shows the message (no duplicates).
                        ForEach(Array(echoes.enumerated()).filter { e in !messages.contains { $0.content.contains(e.element.text) } },
                                id: \.offset) { _, e in userCard(e.text, tag: e.via) }
                        if working {
                            HStack(spacing: 6) { JBSpinner(); JBStreamCursor() }
                        }
                        if run.status == "waiting" && !messages.contains(where: { $0.role == "approval" }) {
                            approvalCard("needs your approval")
                        }
                        if showScreen { screenView }
                        Color.clear.frame(height: 1).id("end")
                    }
                    .padding(.horizontal, 24).padding(.vertical, 16)
                    .frame(maxWidth: 860, alignment: .leading)   // same column as a JoeBro chat
                    .frame(maxWidth: .infinity)
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
            // Same as a JoeBro reply: plain text, a readable card over photos/scenes.
            MarkdownText(text: m.content).textSelection(.enabled).foregroundStyle(JB.ink)
                .frame(maxWidth: .infinity, alignment: .leading)
                .modifier(AgentReplyCard())
        case "approval": approvalCard(m.content, active: last && run.status == "waiting")
        default:
            HStack(spacing: 6) {
                JBStatusDot(color: JB.ink3, size: 5)
                Text(m.content).font(.jb(11)).foregroundStyle(JB.ink2).lineLimit(2)
            }
        }
    }

    /// Same bubble as your messages in a JoeBro chat (right-aligned), with "Queued" under it until delivered.
    private func userCard(_ text: String, tag: String?) -> some View {
        HStack {
            Spacer(minLength: 80)
            VStack(alignment: .trailing, spacing: 4) {
                Text(text)
                    .font(.jb(14)).foregroundStyle(JB.ink).lineSpacing(3)
                    .textSelection(.enabled)
                    .padding(.horizontal, 14).padding(.vertical, 10)
                    .background(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).fill(JB.panelFill)
                        .overlay(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).fill(JB.raised)))
                    .overlay(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).strokeBorder(JB.line, lineWidth: 1))
                if let tag { Text(tag.capitalized).font(.jb(11)).foregroundStyle(JB.ink3) }
            }
        }
    }

    private func approvalCard(_ text: String, active: Bool = true) -> some View {
        VStack(alignment: .leading, spacing: JB.s2) {
            HStack(spacing: 6) { JBStatusDot(color: JB.warn, live: active, size: 6); Text("Needs you").jbEyebrow() }
            Text(text).font(.jb(11.5)).foregroundStyle(JB.ink)
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

    /// Same look as the JoeBro composer.
    private var composer: some View {
        VStack(alignment: .leading, spacing: 6) {
            VStack(alignment: .leading, spacing: 8) {
                TextField("Message \(run.title.isEmpty ? "agent" : run.title)", text: $draft, axis: .vertical)
                    .textFieldStyle(.plain).font(.jb(14)).lineLimit(1...8)
                    .focused($focused)
                    .onSubmit { Task { await send() } }
                HStack(spacing: 6) {
                    Spacer()
                    Button {
                        mic.toggle { t in draft = draft.isEmpty ? t : draft + " " + t }
                    } label: {
                        if mic.isTranscribing {
                            JBSpinner()
                        } else {
                            Image(systemName: mic.isRecording ? "mic.fill" : "mic")
                                .foregroundStyle(mic.isRecording ? JB.danger : JB.ink2)
                                .symbolEffect(.pulse, isActive: mic.isRecording)
                        }
                    }
                    .buttonStyle(JBIconButtonStyle(size: 26))
                    .help(mic.isRecording ? "Stop recording" : "Voice input")
                    Button { Task { await send() } } label: { Image(systemName: "arrow.up") }
                        .buttonStyle(JBIconButtonStyle(size: 30, active: !draft.isEmpty))
                        .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || sending)
                        .keyboardShortcut(.return, modifiers: .command)
                }
            }
            .padding(.horizontal, 14).padding(.top, 12).padding(.bottom, 8)
            .background {
                RoundedRectangle(cornerRadius: JB.rPanel, style: .continuous).fill(JB.readFill)
                    .overlay(RoundedRectangle(cornerRadius: JB.rPanel, style: .continuous)
                        .strokeBorder(focused ? JB.accent.opacity(0.7) : JB.line, lineWidth: 1))
            }
            .animation(JB.fade, value: focused)
            if let error { Text(error).font(.jb(11)).foregroundStyle(JB.danger) }
        }
        .frame(maxWidth: 860)
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 24).padding(.bottom, 12)
    }

    private func send() async {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !sending else { return }
        sending = true
        draft = ""          // clear at once: no selected-text flash, no double send
        // Shown immediately, labelled the same way the terminal shows it: queued while the agent works.
        echoes.append((text, working ? "queued" : "sending"))
        let idx = echoes.count - 1
        defer { sending = false }
        do {
            let how = try await APIClient.shared.codingSend(id: run.id, text: text)
            if echoes.indices.contains(idx) { echoes[idx].via = how == "terminal" && !working ? "sent" : "queued" }
            error = nil
            await onChanged()
        } catch {
            if echoes.indices.contains(idx) { echoes.remove(at: idx) }
            draft = text; self.error = error.localizedDescription
        }
    }
}


/// A JoeBro reply's readable card over a photo or animated scene (none on the plain palette).
struct AgentReplyCard: ViewModifier {
    @State private var look = JBAppearance.shared
    func body(content: Content) -> some View {
        if look.backdrop == .palette {
            content
        } else {
            content
                .padding(.horizontal, 14).padding(.vertical, 10)
                .background(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).fill(JB.readFill))
                .overlay(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).strokeBorder(JB.line, lineWidth: 1))
        }
    }
}


/// Tool calls sit on a small readable card (panel fill + hairline) so they stay legible over any background.
struct ToolCard: ViewModifier {
    func body(content: Content) -> some View {
        content
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(RoundedRectangle(cornerRadius: JB.rControl + 1, style: .continuous).fill(JB.readFill))
            .overlay(RoundedRectangle(cornerRadius: JB.rControl + 1, style: .continuous).strokeBorder(JB.line, lineWidth: 1))
    }
}

/// A run of a coding agent's tool calls, collapsed to one line (the latest), like JoeBro's tool rows.
struct AgentToolGroup: View {
    let lines: [String]
    var running = false
    @State private var expanded = false
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button { withAnimation(JB.snap) { expanded.toggle() } } label: {
                HStack(spacing: 8) {
                    JBStatusDot(color: running ? JB.live : JB.ink3, live: running, size: 6)
                    Text(lines.last ?? "").font(.jb(12)).foregroundStyle(JB.ink2).lineLimit(1).truncationMode(.tail)
                    if lines.count > 1 { Text("\(lines.count)").font(.jb(11)).foregroundStyle(JB.ink3) }
                    Image(systemName: "chevron.right").font(.jb(7, .bold)).foregroundStyle(JB.ink3)
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                }
                .frame(minHeight: 22)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if expanded {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(Array(lines.enumerated()), id: \.offset) { _, l in
                        Text(l).font(.jbMono(11)).foregroundStyle(JB.ink2).lineLimit(3).textSelection(.enabled)
                    }
                }
                .padding(.leading, 14)
                .transition(.opacity)
            }
        }
        .modifier(ToolCard())
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
