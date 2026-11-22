import SwiftUI

/// Collapsible reasoning section.
struct ThinkingSection: View {
    let thinking: String
    let inProgress: Bool
    var seconds: Double? = nil
    var label: String = "thinking"
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                withAnimation(JB.snap) { expanded.toggle() }
            } label: {
                HStack(spacing: 6) {
                    if inProgress {
                        JBScrambleText(text: "thinking", active: true, font: .jb(11.5))
                    } else {
                        Text(label)
                            .font(.jb(11.5))
                            .foregroundStyle(JB.ink3)
                        if let seconds {
                            Text(String(format: "%.1fs", seconds))
                                .font(.jb(11))
                                .foregroundStyle(JB.ink3)
                        }
                        Image(systemName: "chevron.right")
                            .font(.jb(7, .bold))
                            .foregroundStyle(JB.ink3)
                            .rotationEffect(.degrees(expanded ? 90 : 0))
                    }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if expanded || inProgress {
                Text(thinking)
                    .font(.jb(12))
                    .foregroundStyle(JB.ink2)
                    .lineSpacing(2)
                    .textSelection(.enabled)
                    .padding(.leading, 10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .overlay(alignment: .leading) { Rectangle().fill(JB.line).frame(width: 1) }
                    .transition(.opacity)
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
            ToolActivityView(calls: message.activityTools.isEmpty ? [message] : message.activityTools)
        case .text:
            if message.isUser {
                userBubble
            } else {
                assistantBubble.modifier(ReadableOnBackdrop())
            }
        }
    }

    /// Older turns were collapsed server-side — all the user needs to know.
    private var compactedPill: some View {
        HStack(spacing: 10) {
            Rectangle().fill(JB.line).frame(height: 1)
            Text("compacted")
                .font(.jb(11))
                .foregroundStyle(JB.ink3)
            Rectangle().fill(JB.line).frame(height: 1)
        }
        .padding(.vertical, 4)
    }

    private var userBubble: some View {
        HStack {
            Spacer(minLength: 80)
            VStack(alignment: .trailing, spacing: 4) {
                if !message.attachments.isEmpty {
                    AttachmentChipsView(attachments: message.attachments)
                }
                if !message.content.isEmpty {
                    // No .textSelection here — it installs the system text menu
                    // (Look Up / Translate / …) over our Copy/Resend/Unsend menu.
                    Text(message.content)
                        .font(.jb(14))
                        .foregroundStyle(JB.ink)
                        .lineSpacing(3)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .background(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).fill(JB.panelFill)
                            .overlay(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).fill(JB.raised)))
                        .overlay(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).strokeBorder(JB.line, lineWidth: 1))
                        .jbContextMenu {
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
                    HStack(spacing: 0) {
                        ChatCopyButton(text: message.content)
                        Button { store.resend(message) } label: { Image(systemName: "arrow.clockwise") }
                            .buttonStyle(JBIconButtonStyle(size: 24))
                            .help("Retry")
                        Button { store.beginEdit(message) } label: { Image(systemName: "pencil") }
                            .buttonStyle(JBIconButtonStyle(size: 24))
                            .help("Edit")
                    }
                    .opacity(hovering ? 1 : 0)
                    .animation(JB.fade, value: hovering)
                }
            }
        }
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
    }

    private var assistantBubble: some View {
        let liveThinking = message.isStreaming
            && message.content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        let split = message.thinking.map {
            ThinkingSplit(thinking: $0, body: message.content, inProgress: liveThinking)
        } ?? extractThinking(message.content)
        // Live frames strip tool/DSML markup on the fly (matching the renderer
        // does the same every frame); finalized messages are already clean.
        let renderedBody = message.isStreaming ? stripToolArtifacts(split.body) : split.body
        return VStack(alignment: .leading, spacing: 8) {
            if let model = message.modelName {
                HStack(spacing: 6) {
                    ProviderLogoView(model: model, size: 11)
                        .foregroundStyle(JB.ink3)
                    Text(model.components(separatedBy: "/").last ?? model)
                        .font(.jb(11))
                        .foregroundStyle(JB.ink3)
                }
            }

            if !split.thinking.isEmpty {
                ThinkingSection(thinking: split.thinking,
                                inProgress: split.inProgress && message.isStreaming,
                                seconds: message.thinkingTime)
            }

            if message.content.isEmpty && message.isStreaming && split.thinking.isEmpty {
                ThinkingDots()
            } else if !split.body.isEmpty || split.thinking.isEmpty {
                MarkdownText(text: renderedBody)
                if message.isStreaming && !renderedBody.isEmpty {
                    JBStreamCursor()
                }
                WorkspaceActionButtons(text: renderedBody)
            }

            if !message.webSources.isEmpty {
                webSourcesView
            }

            if !message.memoriesUsed.isEmpty {
                MemoriesUsedView(memories: message.memoriesUsed)
            }

            if !message.pluginsUsed.isEmpty {
                PluginsUsedView(plugins: message.pluginsUsed)
            }

            if !message.skillsUsed.isEmpty {
                SkillsUsedView(skills: message.skillsUsed)
            }

            if !message.attachments.isEmpty {
                AttachmentChipsView(attachments: message.attachments)
            }

            if !message.isStreaming && !message.content.isEmpty {
                HStack(spacing: 8) {
                    HStack(spacing: 0) {
                        ChatCopyButton(text: message.content)
                    }
                    .opacity(hovering ? 1 : 0)
                    .animation(JB.fade, value: hovering)
                    if let metrics = message.metrics {
                        Text(metrics)
                    }
                    if let ctx = message.contextPercent {
                        HStack(spacing: 4) {
                            ZStack {
                                Circle().stroke(JB.line, lineWidth: 1.6)
                                Circle()
                                    .trim(from: 0, to: min(ctx / 100, 1))
                                    .stroke(ctx > 85 ? JB.danger : JB.ink3,
                                            style: StrokeStyle(lineWidth: 1.6, lineCap: .round))
                                    .rotationEffect(.degrees(-90))
                            }
                            .frame(width: 10, height: 10)
                            Text("\(Int(ctx))%")
                        }
                        .help("Context window used")
                    }
                }
                .font(.jb(10.5))
                .foregroundStyle(JB.ink3)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 4)
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
        .jbContextMenu {
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
                        HStack(spacing: 6) {
                            Text("\(i + 1)")
                                .font(.jb(10, .medium))
                                .foregroundStyle(JB.accent)
                            Text(src.title)
                                .font(.jb(11))
                                .foregroundStyle(JB.ink2)
                                .lineLimit(1)
                                .frame(maxWidth: 180, alignment: .leading)
                        }
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .jbSurface(.raised, radius: 6)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }
}

private let mdLinkRegex = try? NSRegularExpression(pattern: #"\[([^\]]+)\]\(([^)]+)\)"#)

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
                                .font(.jb(11))
                                .foregroundStyle(JB.ink2)
                        }
                        .buttonStyle(.plain)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .jbSurface(.raised, radius: 6)
                        .jbHoverRow(radius: 6)
                    }
                }
            }
        }
    }

    private var workspaceActions: [Action] {
        guard let re = mdLinkRegex else { return [] }
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

    // Deliberately no store.sessions lookup: the poller reassigns that array
    // every 4s, which would re-run this regex scan in every visible bubble.
    // The "session-" prefix already covers what the backend actually emits.
    private func isWorkspaceTarget(_ target: String) -> Bool {
        ["research-", "document-", "event-", "task-", "note-", "session-"].contains { target.hasPrefix($0) }
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


struct ThinkingDots: View {
    var body: some View {
        JBScrambleText(text: "thinking", active: true, font: .jb(11.5))
            .padding(.vertical, 4)
    }
}

/// Collapsed "N things used" disclosure shared by plugins, skills, memories.
private struct ChatUsedList: View {
    let glyph: JBGlyph
    let title: String
    let items: [String]
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Button {
                withAnimation(JB.snap) { expanded.toggle() }
            } label: {
                HStack(spacing: 5) {
                    JBIcon(glyph, size: 11)
                    Text(title)
                        .font(.jb(10.5))
                    Image(systemName: "chevron.right")
                        .font(.jb(7, .bold))
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                }
                .foregroundStyle(JB.ink3)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if expanded {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    Text(item)
                        .font(.jb(11))
                        .foregroundStyle(JB.ink2)
                        .padding(.leading, 16)
                }
                .transition(.opacity)
            }
        }
    }
}

struct PluginsUsedView: View {
    let plugins: [String]
    var body: some View {
        ChatUsedList(glyph: .plugin, title: "\(plugins.count) plugin\(plugins.count == 1 ? "" : "s")", items: plugins)
    }
}

struct SkillsUsedView: View {
    let skills: [String]
    var body: some View {
        ChatUsedList(glyph: .skills, title: "\(skills.count) skill\(skills.count == 1 ? "" : "s")", items: skills)
    }
}

struct MemoriesUsedView: View {
    let memories: [String]
    var body: some View {
        ChatUsedList(glyph: .memory, title: "\(memories.count) memor\(memories.count == 1 ? "y" : "ies")", items: memories)
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
    // MCP tools are "mcp_<serverid>__<tool>" — show the tool part, not the id.
    if name.hasPrefix("mcp_"), let sep = name.range(of: "__") {
        let tool = String(name[sep.upperBound...])
        return tool.split(separator: "_").map { String($0).capitalized }.joined(separator: " ")
    }
    return name.split(separator: "_").map { String($0).capitalized }.joined(separator: " ")
}

/// Copy affordance: swaps to a check for 1.2s.
struct ChatCopyButton: View {
    let text: String
    @State private var copied = false

    var body: some View {
        Button {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
            withAnimation(JB.snap) { copied = true }
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
                withAnimation(JB.snap) { copied = false }
            }
        } label: {
            Image(systemName: copied ? "checkmark" : "doc.on.doc")
                .foregroundStyle(copied ? JB.live : JB.ink2)
                .contentTransition(.symbolEffect(.replace))
        }
        .buttonStyle(JBIconButtonStyle(size: 24))
        .help("Copy")
    }
}

/// Kept for callers of the old name.
struct CopyResponseButton: View {
    let text: String
    var body: some View { ChatCopyButton(text: text) }
}

/// Turn-level steps: one mono line with a status dot, click to expand.
private struct ToolActivityView: View {
    let calls: [Message]
    @State private var expanded = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private var running: Bool { calls.contains { !$0.toolDone } }
    private var failed: Bool { calls.contains { ($0.exitCode ?? 0) != 0 } }
    private var summary: String {
        if failed && !running { return "Finished with an issue" }
        return toolActivityText(calls.last(where: { !$0.toolDone })?.toolName ?? calls.last?.toolName ?? "", running: running)
    }
    private var groups: [Message] {
        var rows: [Message] = []
        for message in calls {
            if let prior = rows.last, prior.toolName == message.toolName {
                var group = prior
                group.groupedToolCount += 1
                if prior.groupedToolCount == 1 {
                    group.toolOutput = [prior.command, prior.toolOutput].compactMap { $0 }.joined(separator: "\n")
                }
                group.command = nil
                group.toolOutput = (group.toolOutput ?? "") + "\n\nCall \(group.groupedToolCount)\n" + [message.command, message.toolOutput].compactMap { $0 }.joined(separator: "\n")
                group.toolDone = prior.toolDone && message.toolDone
                if (message.exitCode ?? 0) != 0 { group.exitCode = message.exitCode }
                rows[rows.count - 1] = group
            } else { rows.append(message) }
        }
        return rows
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                withAnimation(reduceMotion ? nil : JB.snap) { expanded.toggle() }
            } label: {
                HStack(spacing: 8) {
                    JBStatusDot(color: failed && !running ? JB.danger : (running ? JB.live : JB.ink3), live: running, size: 6)
                    Text(summary)
                        .font(.jb(11.5))
                        .foregroundStyle(JB.ink2)
                        .lineLimit(1)
                    Text("\(calls.count)")
                        .font(.jb(11))
                        .foregroundStyle(JB.ink3)
                    Image(systemName: "chevron.right")
                        .font(.jb(7, .bold))
                        .foregroundStyle(JB.ink3)
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                }
                .frame(minHeight: 22, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(summary), \(calls.count) steps")
            .accessibilityValue(expanded ? "Expanded" : "Collapsed")
            if expanded {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(groups) { message in ToolActivityStep(message: message) }
                }
                .padding(.leading, 10)
                .overlay(alignment: .leading) { Rectangle().fill(JB.line).frame(width: 1) }
                .transition(.opacity)
            }
        }
        .modifier(ToolCard())
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct ToolActivityStep: View {
    let message: Message
    @State private var expanded = false
    private var failed: Bool { (message.exitCode ?? 0) != 0 }
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button {
                withAnimation(JB.snap) { expanded.toggle() }
            } label: {
                HStack(spacing: 8) {
                    JBStatusDot(color: failed ? JB.danger : (message.toolDone ? JB.ink3 : JB.live), live: !message.toolDone, size: 5)
                    Text(prettyToolName(message.toolName) + (message.groupedToolCount > 1 ? " (\(message.groupedToolCount))" : ""))
                        .font(.jb(11.5))
                        .foregroundStyle(JB.ink2)
                        .lineLimit(1)
                    if !expanded, let cmd = message.command, !cmd.isEmpty {
                        Text(cmd)
                            .font(.jb(11))
                            .foregroundStyle(JB.ink3)
                            .lineLimit(1)
                    }
                    Spacer(minLength: 0)
                }
                .padding(.vertical, 2)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if expanded {
                VStack(alignment: .leading, spacing: 6) {
                    if let command = message.command, !command.isEmpty {
                        Text(command).font(.jb(11)).foregroundStyle(JB.ink).textSelection(.enabled)
                    }
                    if let output = message.toolOutput, !output.isEmpty {
                        ScrollView {
                            Text(output).font(.jb(11)).foregroundStyle(JB.ink2).textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .frame(maxHeight: 220)
                    }
                }
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .jbSurface(.raised, radius: JB.rControl)
                .transition(.opacity)
            }
        }
    }
}

/// Human activity text beside the steps disclosure; never expose raw arguments.
private func toolActivityText(_ raw: String, running: Bool) -> String {
    let name = raw.components(separatedBy: "__").last!.lowercased()
    if name.contains("spotify") && name.contains("search") { return running ? "Searching Spotify" : "Searched Spotify" }
    if name.contains("email") || name.contains("mail") {
        if name.contains("send") || name.contains("reply") { return running ? "Sending email" : "Sent email" }
        return running ? "Reading email" : "Checked email"
    }
    if name.contains("weather") { return running ? "Checking the weather" : "Checked the weather" }
    if name.contains("plan") { return running ? "Updating the plan" : "Updated the plan" }
    if name.contains("reminder") { return running ? "Working with reminders" : "Checked reminders" }
    if name.contains("calendar") || name.contains("event") { return running ? "Working with the calendar" : "Checked the calendar" }
    if name.contains("memory") || name.hasPrefix("muse_") { return running ? "Working with memory" : "Finished memory steps" }
    if name.contains("read") || name.contains("inspect") { return running ? "Reading information" : "Read information" }
    if name.contains("search") || name.contains("find") { return running ? "Searching" : "Finished searching" }
    if name.contains("create") || name.contains("write") || name.contains("edit") { return running ? "Working on changes" : "Finished change steps" }
    if name.contains("agent") { return running ? "Working with agents" : "Checked agents" }
    if name == "bash" || name == "python" { return running ? "Running a command" : "Finished command steps" }
    return running ? "Working" : "Finished steps"
}


/// Over a photo or animated backdrop, assistant text sits on a card that
/// follows the panel-opacity slider; on the plain ground it stays bare.
private struct ReadableOnBackdrop: ViewModifier {
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
