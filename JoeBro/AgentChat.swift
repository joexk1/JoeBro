import SwiftUI

// Coding agents as chats: transcript, composer, approvals. Used by AgentsPanel and the Inbox.

extension APIClient {
    /// Type an instruction into a coding agent (terminal now, or queued for its next turn).
    func codingSend(id: String, text: String, mode: String = "auto") async throws -> String {
        let out = try await sendJSON("api/coding-agents/\(id)/send", body: ["text": text, "sender": "app", "mode": mode])
        return (out["delivered"] as? String) ?? "queued"
    }

    /// Switch the active model on any coding agent. Uses the dedicated backend route when available,
    /// falling back to `/model <model>` in the agent's terminal on older backends.
    func codingSetModel(id: String, model: String, harness: String = "") async throws -> String {
        do {
            let out = try await sendJSON("api/coding-agents/\(id)/model", body: ["model": model])
            return (out["delivered"] as? String) ?? "terminal"
        } catch {
            let mode = harness == "opencode" ? "queue" : "terminal"
            return try await codingSend(id: id, text: "/model " + model, mode: mode)
        }
    }

    /// The agent's terminal screen (or its last hook lines when it has no controllable terminal).
    func codingScreen(id: String, lines: Int = 40) async throws -> String {
        let out = try await sendJSON("api/coding-agents/\(id)/screen?lines=\(lines)", method: "GET")
        if let s = out["screen"] as? String { return s }
        if let l = out["lines"] as? [String] { return l.joined(separator: "\n") }
        return ""
    }
}

extension APIClient {
    /// Pick an option of the agent's on-screen question/menu (or type free text). Returns the next one, if any.
    func codingAnswer(id: String, key: String?, text: String?) async throws -> AgentPrompt? {
        var body: [String: Any] = [:]
        if let key { body["key"] = key }
        if let text { body["text"] = text }
        let out = try await sendJSON("api/coding-agents/\(id)/answer", body: body)
        guard let p = out["prompt"], !(p is NSNull),
              let data = try? JSONSerialization.data(withJSONObject: p) else { return nil }
        return try? JSONDecoder().decode(AgentPrompt.self, from: data)
    }
}

/// The question a coding agent is asking, as buttons: one per option,
/// plus a text field when it takes a typed answer. Multi-question forms step through in place.
struct AgentPromptCard: View {
    let agentID: String
    let prompt: AgentPrompt
    var onDone: () async -> Void = {}
    @State private var shown: AgentPrompt?
    @State private var typed = ""
    @State private var busy = false
    @State private var note: String?
    @FocusState private var typing: Bool

    private var p: AgentPrompt { shown ?? prompt }

    var body: some View {
        VStack(alignment: .leading, spacing: JB.s2) {
            if !p.header.isEmpty { Text(p.header).jbEyebrow() }
            if !p.question.isEmpty { Text(p.question).font(.jb(13, .medium)).foregroundStyle(JB.ink).fixedSize(horizontal: false, vertical: true) }
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(p.options.filter { !$0.freeform }.enumerated()), id: \.element) { i, o in
                    Button { Task { await answer(key: o.key.isEmpty ? o.label : o.key, text: nil) } } label: {
                        HStack(alignment: .firstTextBaseline, spacing: JB.s2) {
                            Text(o.key.isEmpty ? "\(i + 1)" : o.key).font(.jbMono(10.5)).foregroundStyle(JB.ink3).frame(width: 14)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(o.label).font(.jb(12.5)).foregroundStyle(JB.ink).lineLimit(3)
                                if !o.description.isEmpty && o.description != o.label {
                                    Text(o.description).font(.jb(11)).foregroundStyle(JB.ink3).lineLimit(4)
                                }
                            }
                            Spacer(minLength: 0)
                        }
                        .padding(.horizontal, JB.s2).padding(.vertical, 6)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(JBMenuRowStyle())
                }
            }
            if p.options.contains(where: \.freeform) {
                HStack(spacing: JB.s2) {
                    TextField("Type your own answer", text: $typed)
                        .textFieldStyle(.plain).font(.jb(12.5)).focused($typing)
                        .onSubmit { send() }
                        .padding(.horizontal, JB.s2).padding(.vertical, 6)
                        .jbField(focused: typing)
                    Button("Send") { send() }.buttonStyle(.jbSecondary)
                        .disabled(typed.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            HStack(spacing: 6) {
                if busy { JBSpinner() }
                if let note { Text(note).font(.jb(10.5)).foregroundStyle(JB.ink3).lineLimit(2) }
            }
        }
        .disabled(busy)
        .onChange(of: prompt) { _, _ in shown = nil }
    }

    private func send() {
        let t = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        Task { await answer(key: nil, text: t) }
    }

    private func answer(key: String?, text: String?) async {
        busy = true; note = nil
        defer { busy = false }
        do {
            let next = try await APIClient.shared.codingAnswer(id: agentID, key: key, text: text)
            typed = ""
            if let next { shown = next } else { note = "Answered"; await onDone() }
        } catch { note = error.localizedDescription }
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
    var readOnly = false        // a finished session's overview: read it, then Resume
    var onChanged: () async -> Void = {}
    @State private var draft = ""
    @State private var mic = MicRecorder()
    @State private var slashIndex = 0
    private let slash = ChatSlashSource.shared

    /// Harness slash commands (typed into the agent like any message) plus your skills.
    private var agentCommands: [ChatSlashItem] {
        switch run.harness {
        case "agy":
            return [
                .init(name: "model", label: "switch model"),
                .init(name: "effort", label: "set reasoning effort (low|medium|high|xhigh)"),
                .init(name: "compact", label: "summarise the conversation so far"),
                .init(name: "plan", label: "draft an implementation plan"),
                .init(name: "diff", label: "view current changes"),
                .init(name: "learn", label: "capture rules or skills from this session"),
                .init(name: "usage", label: "display model quota usage"),
            ]
        case "codex":
            return [
                .init(name: "model", label: "switch model"),
                .init(name: "compact", label: "summarise the conversation so far"),
                .init(name: "review", label: "review the current changes"),
                .init(name: "diff", label: "view current diff"),
                .init(name: "new", label: "start a new session"),
            ]
        case "opencode":
            return [
                .init(name: "models", label: "choose model"),
                .init(name: "compact", label: "summarise the conversation so far"),
                .init(name: "new", label: "start a new session"),
            ]
        default:
            return [
                .init(name: "compact", label: "summarise the conversation so far"),
                .init(name: "clear", label: "start fresh"),
                .init(name: "model", label: "switch model"),
                .init(name: "review", label: "review the current changes"),
                .init(name: "cost", label: "token usage"),
                .init(name: "memory", label: "edit memory files"),
                .init(name: "init", label: "write a CLAUDE.md for this repo"),
            ]
        }
    }
    private var slashItems: [ChatSlashItem] {
        guard draft.hasPrefix("/"), !draft.contains(" "), !draft.contains(where: \.isNewline) else { return [] }
        let q = String(draft.dropFirst()).lowercased()
        let cmds = agentCommands.filter { q.isEmpty || $0.name.hasPrefix(q) }
        return cmds + slash.matches(q).filter { $0.kind == .skill }
    }
    @State private var sending = false
    @State private var switchingModel = false
    @State private var pickedModel: String?
    @State private var customModelOpen = false
    @State private var customModelText = ""
    @State private var echoes: [(text: String, via: String)] = []
    @State private var error: String?
    @State private var showScreen = false
    @State private var screen = ""
    @State private var inputFocused = false
    @State private var inputHeight: CGFloat = 22
    private var focused: Bool { inputFocused }

    private var messages: [AgentMessage] { run.messages ?? [] }

    private enum Item { case message(AgentMessage), tools([String]) }
    /// Consecutive tool lines fold into one row.
    private var items: [Item] {
        var out: [Item] = []
        for m in messages {
            if !["user", "assistant", "approval", "notice", "event"].contains(m.role) {
                if case .tools(let l) = out.last { out[out.count - 1] = .tools(l + [m.content]) } else { out.append(.tools([m.content])) }
            } else { out.append(.message(m)) }
        }
        return out
    }
    private var canSend: Bool { !readOnly && run.kind == "coding" && run.status != "ended" }
    private var working: Bool { run.live && run.status != "waiting" }

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(Array(items.enumerated()), id: \.offset) { i, item in
                            switch item {
                            case .message(let m): bubble(m, last: i == items.count - 1).padding(.vertical, 7)
                            case .tools(let lines): AgentToolGroup(lines: lines, running: working && i == items.count - 1)
                            }
                        }
                        // Local echo only until the agent's own feed shows the message (no duplicates).
                        ForEach(Array(echoes.enumerated()).filter { e in !messages.contains { $0.role == "user" && sameMessage($0.content, e.element.text) } },
                                id: \.offset) { _, e in userCard(e.text, tag: e.via).padding(.vertical, 7) }
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
        case "user": userCard(handoverStripped(m.content), tag: nil)
        case "assistant":
            // Same as a JoeBro reply: plain text, a readable card over photos/scenes.
            MarkdownText(text: m.content).textSelection(.enabled).foregroundStyle(JB.ink)
                .frame(maxWidth: .infinity, alignment: .leading)
                .modifier(AgentReplyCard())
        case "approval": approvalCard(m.content, active: last && run.status == "waiting")
        case "notice":   // usage limit / API error: the agent stopped and why
            HStack(spacing: 8) {
                Image(systemName: "exclamationmark.triangle.fill").font(.jb(11)).foregroundStyle(JB.warn)
                Text(m.content).font(.jb(12.5, .medium)).foregroundStyle(JB.ink)
            }
            .padding(.horizontal, 12).padding(.vertical, 8)
            .background(RoundedRectangle(cornerRadius: JB.rControl, style: .continuous).fill(JB.warn.opacity(0.12)))
            .overlay(RoundedRectangle(cornerRadius: JB.rControl, style: .continuous).strokeBorder(JB.warn.opacity(0.35), lineWidth: 1))
            .frame(maxWidth: .infinity, alignment: .leading)
        case "event":    // harness happenings (background task done, /command, compacted): a quiet centred line
            HStack(spacing: 8) {
                Rectangle().fill(JB.line).frame(height: 1)
                Text(m.content).font(.jb(11.5)).foregroundStyle(JB.ink3).lineLimit(1).fixedSize()
                Rectangle().fill(JB.line).frame(height: 1)
            }
            .frame(maxWidth: .infinity)
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
                if let tag { Text(tag.capitalized).font(.jb(11, .medium)).foregroundStyle(JB.ink2) }
            }
        }
    }

    private func approvalCard(_ text: String, active: Bool = true) -> some View {
        VStack(alignment: .leading, spacing: JB.s2) {
            HStack(spacing: 6) { JBStatusDot(color: JB.warn, live: active, size: 6); Text("Needs you").jbEyebrow() }
            if active, let prompt = run.prompt, !prompt.options.isEmpty {
                AgentPromptCard(agentID: run.id, prompt: prompt, onDone: onChanged)
            } else {
                Text(text.replacingOccurrences(of: "Question: ", with: "")).font(.jb(11.5)).foregroundStyle(JB.ink)
                if active { AgentApprovalButtons(agentID: run.id, onDone: onChanged) }
            }
        }
        .padding(JB.s3)
        .frame(maxWidth: .infinity, alignment: .leading)
        // Waiting on you: always solid, so the options read over any wallpaper.
        .background(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).fill(JB.surface.opacity(active ? 0.96 : 0)))
        .jbSurface(.raised, radius: JB.rCard)
        .overlay(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).strokeBorder(JB.warn.opacity(active ? 0.5 : 0), lineWidth: 1))
    }

    private var screenView: some View {
        Text(screen.isEmpty ? " " : screen)
            .font(.jbMono(10.5)).foregroundStyle(JB.ink2).textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(JB.s3).jbSurface(.raised, radius: JB.rCard)
    }

    private var activeModel: String { pickedModel ?? run.model }

    private var modelOptions: [CodingModelOption] {
        CodingAgentModelPresets.options(for: run.harness, current: activeModel, server: run.availableModels)
    }

    private func isCurrentModel(_ id: String) -> Bool {
        let cur = activeModel.lowercased()
        guard !cur.isEmpty else { return false }
        return cur == id.lowercased() || cur.contains(id.lowercased())
    }

    private func switchModel(to model: String) async {
        switchingModel = true
        defer { switchingModel = false }
        do {
            _ = try await APIClient.shared.codingSetModel(id: run.id, model: model, harness: run.harness)
            pickedModel = model
            error = nil
            await onChanged()
        } catch {
            self.error = "Model change needs the agent idle in a Kitty/tmux terminal: " + error.localizedDescription
        }
    }

    /// Model picker for every linked coding agent (Claude Code, Antigravity, Codex, OpenCode).
    private var agentModelMenu: some View {
        JBMenu {
            ForEach(modelOptions) { m in
                Button { Task { await switchModel(to: m.id) } } label: {
                    HStack {
                        Text(m.label)
                        Spacer(minLength: 16)
                        if isCurrentModel(m.id) { Image(systemName: "checkmark").font(.jb(11, .semibold)).foregroundStyle(JB.accent) }
                    }
                }
            }
            Divider()
            Button("Custom model…") {
                customModelText = activeModel
                customModelOpen = true
            }
        } label: {
            HStack(spacing: 5) {
                ProviderLogoView(model: activeModel.isEmpty ? run.harness : activeModel, size: 11)
                if switchingModel { JBSpinner() }
                Text(CodingAgentModelPresets.displayLabel(activeModel, in: modelOptions))
                    .font(.jb(11.5)).foregroundStyle(JB.ink2).lineLimit(1)
                Image(systemName: "chevron.down").font(.jb(8, .semibold)).foregroundStyle(JB.ink3)
            }
            .padding(.horizontal, 6).padding(.vertical, 4)
        }
        .disabled(switchingModel)
        .alert("Switch model", isPresented: $customModelOpen) {
            TextField("Model id (e.g. provider/model)", text: $customModelText)
            Button("Switch") {
                let m = customModelText.trimmingCharacters(in: .whitespacesAndNewlines)
                if !m.isEmpty { Task { await switchModel(to: m) } }
            }
            Button("Cancel", role: .cancel) { }
        } message: {
            Text("Enter the model slug to pass to /model.")
        }
    }

    /// Same look as the JoeBro composer.
    private var composer: some View {
        VStack(alignment: .leading, spacing: 6) {
            VStack(alignment: .leading, spacing: 8) {
                // The JoeBro composer's own text view: ⇧↩ new line, ↑ recalls your last message, slash keys.
                ZStack(alignment: .topLeading) {
                    if draft.isEmpty {
                        Text("Message \(run.title.isEmpty ? "agent" : run.title)")
                            .font(.jb(14)).foregroundStyle(JB.ink3)
                            .padding(.top, 2).padding(.leading, 7)
                            .allowsHitTesting(false)
                    }
                    ChatInputTextView(text: $draft, height: $inputHeight, focused: $inputFocused,
                                      onKey: agentKey, onSend: submit)
                        .frame(height: inputHeight)
                }
                HStack(spacing: 6) {
                    if !run.harness.isEmpty { agentModelMenu }
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
                    Button { submit() } label: { Image(systemName: "arrow.up") }
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
            .overlay(alignment: .topLeading) {
                let items = slashItems
                if !items.isEmpty {
                    ChatSlashPopover(items: items, selected: min(slashIndex, items.count - 1),
                                     onHover: { slashIndex = $0 }, onPick: { draft = "/" + $0.name + " " })
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(height: 0, alignment: .bottom)
                        .offset(y: -8)
                }
            }
            .onChange(of: draft) { slashIndex = 0; if draft.hasPrefix("/") { slash.refresh() } }
            if let error { Text(error).font(.jb(11)).foregroundStyle(JB.danger) }
        }
        .frame(maxWidth: 860)
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 24).padding(.bottom, 12)
    }

    private func agentKey(_ key: ChatKey) -> Bool {
        let items = slashItems
        if !items.isEmpty {
            let i = min(slashIndex, items.count - 1)
            switch key {
            case .up: slashIndex = (i - 1 + items.count) % items.count; return true
            case .down: slashIndex = (i + 1) % items.count; return true
            case .enter, .tab: draft = "/" + items[i].name + " "; return true
            case .escape: draft = ""; return true
            }
        }
        if key == .up, draft.isEmpty {
            // Recall the last thing you sent this agent.
            let last = echoes.last?.text ?? messages.last(where: { $0.role == "user" }).map { handoverStripped($0.content) }
            if let last { draft = last; return true }
        }
        return false
    }

    /// Clears the box in the same event as Return (no select-all flash), then sends.
    private func submit() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !sending else { return }
        draft = ""
        Task { await send(text) }
    }

    private func send(_ text: String) async {
        guard !sending else { return }
        sending = true
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


/// Default model presets per coding-agent harness, merged with any server-reported options and the active model.
enum CodingAgentModelPresets {
    static let defaults: [String: [CodingModelOption]] = [
        "claude": [
            .init(id: "opus", label: "Opus"),
            .init(id: "sonnet", label: "Sonnet"),
            .init(id: "haiku", label: "Haiku"),
            .init(id: "default", label: "Default"),
        ],
        "agy": [
            .init(id: "gemini-4-neon-high", label: "Gemini 4 Neon (High)"),
            .init(id: "gemini-4-neon-medium", label: "Gemini 4 Neon (Medium)"),
            .init(id: "gemini-4-carbon-high", label: "Gemini 4 Carbon (High)"),
            .init(id: "gemini-4-hydrogen-high", label: "Gemini 4 Hydrogen (High)"),
            .init(id: "gemini-pro-orchestrator-neon", label: "Orchestrator (Neon)"),
            .init(id: "claude-opus-5-5-high", label: "Claude Opus 5.5 (High)"),
            .init(id: "claude-sonnet-5-5-high", label: "Claude Sonnet 5.5 (High)"),
        ],
        "codex": [
            .init(id: "gpt-6.2-sol", label: "GPT-6.2 Sol"),
            .init(id: "gpt-6.2-astra", label: "GPT-6.2 Astra"),
            .init(id: "gpt-6.1-terra", label: "GPT-6.1 Terra"),
            .init(id: "gpt-5-codex", label: "GPT-5 Codex"),
            .init(id: "gpt-5", label: "GPT-5"),
        ],
        "opencode": [
            .init(id: "qwen3-coder:30b", label: "Qwen3 Coder 30B"),
            .init(id: "llama3.3:70b", label: "Llama 3.3 70B"),
            .init(id: "deepseek-v3.2", label: "DeepSeek V3.2"),
        ],
    ]

    static func options(for harness: String, current: String, server: [CodingModelOption]?) -> [CodingModelOption] {
        var seen = Set<String>()
        var out: [CodingModelOption] = []
        func add(_ m: CodingModelOption) {
            let k = m.id.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            guard !k.isEmpty, seen.insert(k).inserted else { return }
            out.append(m)
        }
        let cur = current.trimmingCharacters(in: .whitespacesAndNewlines)
        if !cur.isEmpty {
            let preset = (server ?? defaults[harness] ?? []).first { $0.id.lowercased() == cur.lowercased() }
            add(preset ?? CodingModelOption(id: cur))
        }
        for m in server ?? [] { add(m) }
        for m in defaults[harness] ?? [] { add(m) }
        return out
    }

    static func displayLabel(_ model: String, in options: [CodingModelOption]) -> String {
        guard !model.isEmpty else { return "Model" }
        if let hit = options.first(where: { $0.id.lowercased() == model.lowercased() }) { return hit.label }
        return model.components(separatedBy: "/").last ?? model
    }
}

/// "Message from app via JoeBro: hi" / "[from app] hi" -> "hi" (how a delivered message is logged).
func handoverStripped(_ s: String) -> String {
    var t = s.replacingOccurrences(of: "[from app] ", with: "")
    if let r = t.range(of: #"^Message from [^:\n]{1,40}: "#, options: .regularExpression) { t.removeSubrange(r) }
    return t
}


/// Delivered-yet check: the feed collapses whitespace and clips long text, so compare normalised prefixes.
func sameMessage(_ feed: String, _ sent: String) -> Bool {
    func norm(_ s: String) -> String { handoverStripped(s).split(whereSeparator: \.isWhitespace).joined(separator: " ").lowercased() }
    let a = norm(feed), b = norm(sent)
    guard !a.isEmpty, !b.isEmpty else { return false }
    let n = min(80, a.count, b.count)
    return a.contains(b.prefix(n)) || b.hasPrefix(a.prefix(n))
}
