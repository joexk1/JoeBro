import SwiftUI

/// Collapsible reasoning section — "View thinking process".
struct ThinkingSection: View {
    let thinking: String
    let inProgress: Bool
    var seconds: Double? = nil
    var label: String = "thinking process"
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                withAnimation(.spring(duration: 0.25)) { expanded.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "brain")
                        .font(.system(size: 10))
                    Text(inProgress ? "Thinking…" : (expanded ? "Hide \(label)" : "View \(label)"))
                        .font(.system(size: 11, weight: .medium))
                    if let seconds, !inProgress {
                        Text(String(format: "%.1fs", seconds))
                            .font(.system(size: 10))
                            .foregroundStyle(.tertiary)
                    }
                    if inProgress {
                        ProgressView().controlSize(.mini)
                    } else {
                        Image(systemName: "chevron.right")
                            .font(.system(size: 7, weight: .bold))
                            .rotationEffect(.degrees(expanded ? 90 : 0))
                    }
                }
                .foregroundStyle(.secondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if expanded || inProgress {
                Text(thinking)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .lineSpacing(2)
                    .textSelection(.enabled)
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 10))
            }
        }
    }
}

struct MessageBubbleView: View, Equatable {
    @Environment(AppStore.self) private var store
    let message: Message
    @State private var hovering = false

    // Equatable conformance lets SwiftUI skip re-rendering the (potentially
    // hundreds of) unchanged rows on every streaming flush — only the row
    // whose Message actually changed re-evaluates.
    static func == (lhs: MessageBubbleView, rhs: MessageBubbleView) -> Bool {
        lhs.message == rhs.message
    }

    var body: some View {
        switch message.kind {
        case .compacted:
            compactedPill
        case .tool:
            ToolRowView(message: message)
        case .text:
            if message.isUser {
                userBubble
            } else {
                assistantBubble
            }
        }
    }

    /// Older turns were collapsed server-side — all the user needs to know.
    private var compactedPill: some View {
        HStack {
            Spacer()
            HStack(spacing: 6) {
                Image(systemName: "arrow.down.right.and.arrow.up.left")
                    .font(.system(size: 9, weight: .semibold))
                Text("Compacted chat")
                    .font(.system(size: 11, weight: .medium, design: .rounded))
            }
            .foregroundStyle(.secondary)
            .padding(.horizontal, 12)
            .padding(.vertical, 5)
            .joeGlassCapsule()
            Spacer()
        }
        .padding(.vertical, 2)
    }

    private var userBubble: some View {
        HStack {
            Spacer(minLength: 80)
            VStack(alignment: .trailing, spacing: 6) {
                if !message.attachments.isEmpty {
                    AttachmentChipsView(attachments: message.attachments)
                }
                if !message.content.isEmpty {
                    // No .textSelection here — it installs the system text menu
                    // (Look Up / Translate / …) over our Copy/Resend/Unsend menu.
                    Text(message.content)
                        .lineSpacing(3)
                        .padding(.horizontal, 15)
                        .padding(.vertical, 10)
                        .joeCard(17)
                        .background(Color.accentColor.opacity(0.30), in: RoundedRectangle(cornerRadius: 17))
                        .overlay(RoundedRectangle(cornerRadius: 17).strokeBorder(.white.opacity(0.14), lineWidth: 1))
                        .contextMenu {
                            Button {
                                NSPasteboard.general.clearContents()
                                NSPasteboard.general.setString(message.content, forType: .string)
                            } label: {
                                Label("Copy", systemImage: "doc.on.doc")
                            }
                            Button {
                                store.resend(message)
                            } label: {
                                Label("Resend", systemImage: "arrow.clockwise")
                            }
                            .disabled(store.isStreaming)
                            Button(role: .destructive) {
                                store.unsend(message)
                            } label: {
                                Label("Unsend", systemImage: "trash")
                            }
                        }
                }
            }
        }
    }

    private var assistantBubble: some View {
        let liveThinking = message.isStreaming
            && message.content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        let split = message.thinking.map {
            ThinkingSplit(thinking: $0, body: message.content, inProgress: liveThinking)
        } ?? extractThinking(message.content)
        return VStack(alignment: .leading, spacing: 8) {
            // Model header — provider logo + name, like the web UI
            if let model = message.modelName {
                HStack(spacing: 6) {
                    ProviderLogoView(model: model, size: 13)
                        .foregroundStyle(Color.accentColor)
                    Text(model.components(separatedBy: "/").last ?? model)
                        .font(.system(size: 11, weight: .semibold, design: .rounded))
                        .foregroundStyle(Color.accentColor)
                }
            }

            if !split.thinking.isEmpty {
                ThinkingSection(thinking: split.thinking,
                                inProgress: split.inProgress && message.isStreaming,
                                seconds: message.thinkingTime,
                                label: "thinking process")
            }

            if message.content.isEmpty && message.isStreaming && split.thinking.isEmpty {
                ThinkingDots()
            } else if !split.body.isEmpty || split.thinking.isEmpty {
                // Live frames strip tool/DSML markup on the fly (matching the
                // renderer does the same every frame); finalized messages
                // are already clean.
                MarkdownText(text: message.isStreaming ? stripToolArtifacts(split.body) : split.body)
                WorkspaceActionButtons(text: message.isStreaming ? stripToolArtifacts(split.body) : split.body)
            }

            if !message.memoriesUsed.isEmpty {
                MemoriesUsedView(memories: message.memoriesUsed)
            }

            if !message.attachments.isEmpty {
                AttachmentChipsView(attachments: message.attachments)
            }

            if message.metrics != nil || message.contextPercent != nil {
                HStack(spacing: 8) {
                    if let metrics = message.metrics {
                        Text(metrics)
                    }
                    if let ctx = message.contextPercent {
                        HStack(spacing: 3) {
                            ZStack {
                                Circle()
                                    .stroke(.quaternary, lineWidth: 1.6)
                                Circle()
                                    .trim(from: 0, to: min(ctx / 100, 1))
                                    .stroke(ctx > 85 ? Color.red.opacity(0.8) : Color.secondary,
                                            style: StrokeStyle(lineWidth: 1.6, lineCap: .round))
                                    .rotationEffect(.degrees(-90))
                            }
                            .frame(width: 11, height: 11)
                            Text("\(Int(ctx))%")
                        }
                        .help("Context window used")
                    }
                }
                .font(.system(size: 10.5))
                .foregroundStyle(.tertiary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 16)
        .padding(.vertical, 13)
        .joeCard(17)
        .overlay(RoundedRectangle(cornerRadius: 17).strokeBorder(.white.opacity(0.10), lineWidth: 1))
        // Hover copy button — reliable full-response copy. (Right-click is
        // swallowed by the selectable text's own macOS menu over glyphs.)
        .overlay(alignment: .topTrailing) {
            if hovering && !message.content.isEmpty && !message.isStreaming {
                CopyResponseButton(text: message.content).padding(8)
            }
        }
        .onHover { hovering = $0 }
        .contentShape(RoundedRectangle(cornerRadius: 17))
        .contextMenu {
            Button {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(message.content, forType: .string)
            } label: {
                Label("Copy Full Response", systemImage: "doc.on.doc")
            }
            .disabled(message.content.isEmpty)
        }
        .environment(\.openURL, OpenURLAction { url in
            // Workspace tools emit [label](#kind-id) anchors — route them
            // to the matching panel instead of the browser.
            let s = url.absoluteString.removingPercentEncoding ?? url.absoluteString
            guard s.hasPrefix("#") || s.hasPrefix("about:") || url.scheme == nil else { return .systemAction }
            var body = s
            if body.hasPrefix("about:") { body = String(body.dropFirst("about:".count)) }
            if body.hasPrefix("#") { body = String(body.dropFirst()) }
            func id(after prefix: String) -> String? {
                body.hasPrefix(prefix) ? String(body.dropFirst(prefix.count)) : nil
            }
            if let rid = id(after: "research-") {
                store.pendingResearchID = rid
                store.activeTab = .research
            } else if let did = id(after: "document-") {
                store.openDocumentByID(did)
            } else if let uid = id(after: "event-") {
                store.pendingEventID = uid
                store.activeTab = .calendar
            } else if id(after: "task-") != nil {
                store.activeTab = .tasks
            } else if id(after: "note-") != nil {
                store.activeTab = .notes
            } else if let sid = id(after: "session-") {
                store.select(sid)
            } else if store.sessions.contains(where: { $0.id == body }) {
                store.select(body)
            } else {
                return .systemAction
            }
            return .handled
        })
    }

    private var webSourcesView: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(Array(message.webSources.prefix(8).enumerated()), id: \.offset) { i, src in
                    Link(destination: URL(string: src.url) ?? URL(string: "about:blank")!) {
                        HStack(spacing: 5) {
                            Text("\(i + 1)")
                                .font(.system(size: 9, weight: .bold))
                                .frame(width: 14, height: 14)
                                .background(Color.accentColor.opacity(0.25), in: Circle())
                            Text(src.title)
                                .font(.system(size: 11))
                                .lineLimit(1)
                                .frame(maxWidth: 180, alignment: .leading)
                        }
                        .padding(.horizontal, 8)
                        .padding(.vertical, 5)
                        .background(.ultraThinMaterial, in: Capsule())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }
}

private struct WorkspaceActionButtons: View {
    @Environment(AppStore.self) private var store
    let text: String

    private struct Action: Identifiable {
        let id = UUID()
        var label: String
        var target: String

        var icon: String {
            if target.hasPrefix("research-") { return "antenna.radiowaves.left.and.right" }
            if target.hasPrefix("document-") { return "doc.text" }
            if target.hasPrefix("event-") { return "calendar" }
            if target.hasPrefix("task-") { return "clock.badge.checkmark" }
            if target.hasPrefix("note-") { return "note.text" }
            if target.hasPrefix("session-") { return "bubble.left.and.bubble.right" }
            return "arrow.right.circle"
        }
    }

    var body: some View {
        let actions = workspaceActions
        if !actions.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 7) {
                    ForEach(actions) { action in
                        Button {
                            open(action.target)
                        } label: {
                            Label(action.label, systemImage: action.icon)
                                .font(.system(size: 11, weight: .medium))
                        }
                        .buttonStyle(.plain)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .joeGlassCapsuleInteractive()
                    }
                }
            }
        }
    }

    private var workspaceActions: [Action] {
        guard let re = try? NSRegularExpression(pattern: #"\[([^\]]+)\]\(([^)]+)\)"#) else { return [] }
        let ns = text as NSString
        var out: [Action] = []
        for match in re.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
            guard match.numberOfRanges >= 3 else { continue }
            let label = ns.substring(with: match.range(at: 1))
            var target = ns.substring(with: match.range(at: 2)).removingPercentEncoding ?? ns.substring(with: match.range(at: 2))
            if target.hasPrefix("about:") { target = String(target.dropFirst("about:".count)) }
            if target.hasPrefix("#") { target = String(target.dropFirst()) }
            if isWorkspaceTarget(target) {
                out.append(Action(label: label, target: target))
            }
        }
        return out
    }

    private func isWorkspaceTarget(_ target: String) -> Bool {
        ["research-", "document-", "event-", "task-", "note-", "session-"].contains { target.hasPrefix($0) }
            || store.sessions.contains(where: { $0.id == target })
    }

    private func open(_ target: String) {
        func id(after prefix: String) -> String? {
            target.hasPrefix(prefix) ? String(target.dropFirst(prefix.count)) : nil
        }
        if let rid = id(after: "research-") {
            store.pendingResearchID = rid
            store.activeTab = .research
        } else if let did = id(after: "document-") {
            store.openDocumentByID(did)
        } else if let uid = id(after: "event-") {
            store.pendingEventID = uid
            store.activeTab = .calendar
        } else if id(after: "task-") != nil {
            store.activeTab = .tasks
        } else if id(after: "note-") != nil {
            store.activeTab = .notes
        } else if let sid = id(after: "session-") {
            store.select(sid)
        } else if store.sessions.contains(where: { $0.id == target }) {
            store.select(target)
        }
    }
}

// MARK: - Tool row (collapsible, like the web agent thread)

struct ToolRowView: View {
    let message: Message
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.spring(duration: 0.25)) { expanded.toggle() }
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: toolIcon)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(Color.accentColor)
                    Text(prettyToolName(message.toolName).uppercased())
                        .font(.system(size: 10.5, weight: .bold, design: .rounded))
                        .tracking(0.6)
                    if message.toolDone {
                        Text(failed ? "failed" : "done")
                            .font(.system(size: 10.5))
                            .foregroundStyle(failed ? Color.red : Color.secondary)
                    } else {
                        ProgressView()
                            .controlSize(.mini)
                    }
                    if !expanded, let cmd = message.command, !cmd.isEmpty {
                        Text(cmd)
                            .font(.system(size: 10.5, design: .monospaced))
                            .foregroundStyle(.tertiary)
                            .lineLimit(1)
                    }
                    Spacer()
                    Image(systemName: "chevron.right")
                        .font(.system(size: 8, weight: .bold))
                        .foregroundStyle(.tertiary)
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)

            if expanded {
                VStack(alignment: .leading, spacing: 6) {
                    if let cmd = message.command, !cmd.isEmpty {
                        Text(cmd)
                            .font(.system(size: 11, design: .monospaced))
                            .textSelection(.enabled)
                            .padding(8)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(.black.opacity(0.18), in: RoundedRectangle(cornerRadius: 8))
                    }
                    if let out = message.toolOutput, !out.isEmpty {
                        ScrollView {
                            Text(out.count > 4000 ? String(out.prefix(4000)) + "\n…" : out)
                                .font(.system(size: 11, design: .monospaced))
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .frame(maxHeight: 220)
                        .padding(8)
                        .background(.black.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
                    }
                }
                .padding(.horizontal, 12)
                .padding(.bottom, 10)
            }
        }
        .joeCard(13)
        .overlay(RoundedRectangle(cornerRadius: 13).strokeBorder(.white.opacity(0.10), lineWidth: 1))
        .padding(.leading, 4)
    }

    private var failed: Bool {
        if let code = message.exitCode, code != 0 { return true }
        return false
    }

    private var toolIcon: String {
        switch (message.toolName ?? "").lowercased() {
        case "bash": return "apple.terminal"
        case "read", "read_file": return "doc.text.magnifyingglass"
        case "write", "write_file", "update_document", "edit_document", "create_document": return "square.and.pencil"
        case "web_search": return "magnifyingglass"
        case "generate_image": return "photo"
        case let t where t.contains("email"): return "envelope"
        case let t where t.contains("calendar") || t.contains("note"): return "calendar"
        case let t where t.contains("memory"): return "brain.head.profile"
        default: return "hammer"
        }
    }
}

struct ThinkingDots: View {
    @State private var phase = 0

    var body: some View {
        HStack(spacing: 4) {
            ForEach(0..<3, id: \.self) { i in
                Circle()
                    .fill(.secondary)
                    .frame(width: 6, height: 6)
                    .opacity(phase == i ? 1 : 0.35)
            }
        }
        .padding(.vertical, 6)
        .task {
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(280))
                phase = (phase + 1) % 3
            }
        }
    }
}


/// "🧠 N memories" — which Brain entries informed this reply.
struct MemoriesUsedView: View {
    let memories: [String]
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Button {
                withAnimation(.spring(duration: 0.25)) { expanded.toggle() }
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: "brain.head.profile")
                        .font(.system(size: 10))
                    Text("\(memories.count) memor\(memories.count == 1 ? "y" : "ies") used")
                        .font(.system(size: 10.5, weight: .medium))
                    Image(systemName: "chevron.right")
                        .font(.system(size: 7, weight: .bold))
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                }
                .foregroundStyle(.secondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if expanded {
                ForEach(Array(memories.enumerated()), id: \.offset) { _, m in
                    HStack(alignment: .top, spacing: 5) {
                        Image(systemName: "sparkle")
                            .font(.system(size: 8))
                            .foregroundStyle(Color.accentColor)
                            .padding(.top, 2)
                        Text(m)
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                    }
                }
                .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
    }
}

/// Raw tool identifiers (edit_document) read like plumbing — show humane
/// names in the UI and fall back to de-underscored title case.
func prettyToolName(_ raw: String?) -> String {
    let known: [String: String] = [
        "bash": "Terminal", "python": "Python", "read_file": "Read File",
        "write_file": "Write File",
        "create_document": "Create Document", "edit_document": "Edit Document",
        "update_document": "Update Document", "suggest_document": "Suggest Edits",
        "manage_memory": "Memory",
        "manage_tasks": "Tasks", "manage_calendar": "Calendar",
        "manage_notes": "Notes", "manage_skills": "Skills",
        "manage_research": "Research Library",
        "trigger_research": "Deep Research", "web_search": "Web Search",
        "generate_image": "Generate Image",
        "skill": "Skill",
        "read_webpage": "Read Webpage",
    ]
    let name = (raw ?? "tool").lowercased()
    if let nice = known[name] { return nice }
    return name.split(separator: "_").map { String($0).capitalized }.joined(separator: " ")
}

/// Hover-reveal "copy full response" affordance — the reliable copy path,
/// since selectable text steals right-clicks over its glyphs on macOS.
struct CopyResponseButton: View {
    let text: String
    @State private var copied = false

    var body: some View {
        Button {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
            withAnimation(.spring(duration: 0.25)) { copied = true }
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.4) {
                withAnimation { copied = false }
            }
        } label: {
            Image(systemName: copied ? "checkmark" : "doc.on.doc")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(copied ? Color.green : Color.secondary)
                .frame(width: 26, height: 26)
                .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 8))
                .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(.white.opacity(0.12), lineWidth: 1))
                .scaleEffect(copied ? 1.12 : 1)
        }
        .buttonStyle(.plain)
        .help("Copy full response")
    }
}
