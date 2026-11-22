import SwiftUI

extension Notification.Name {
    /// Object: String. Replaces the composer text (edit-last-message, chips).
    static let chatFillComposer = Notification.Name("JoeBro.chatFillComposer")
}

enum ChatKey { case up, down, enter, tab, escape }

/// One row in the slash list: a built-in command or a skill.
struct ChatSlashItem: Identifiable, Equatable {
    enum Kind: Equatable { case action, skill }
    let name: String
    var label: String = ""
    var kind: Kind = .action
    var id: String { (kind == .skill ? "skill:" : "cmd:") + name }
}

/// Built-in commands + skills, fetched lazily and cached for a minute.
@MainActor @Observable
final class ChatSlashSource {
    static let shared = ChatSlashSource()
    private(set) var skills: [ChatSlashItem] = []
    private var loadedAt: Date?
    private var loading = false

    static let builtins: [ChatSlashItem] = [
        .init(name: "new"),
        .init(name: "compact", label: "shrink context"),
        .init(name: "copy", label: "chat to clipboard"),
        .init(name: "save", label: "to Documents"),
        .init(name: "think", label: "extended thinking"),
        .init(name: "fast"),
        .init(name: "agent"),
        .init(name: "chat"),
    ]

    func refresh() {
        if let t = loadedAt, Date().timeIntervalSince(t) < 60 { return }
        guard !loading else { return }
        loading = true
        Task {
            defer { loading = false }
            guard let v: JSONValue = try? await APIClient.shared.getJSON("api/skills") else { return }
            let list = v["skills"]?.arrayValue ?? []
            skills = list.compactMap { s in
                let status = s["status"]?.stringValue ?? "active"
                guard status != "disabled" && status != "archived" else { return nil }
                guard let n = s["name"]?.stringValue ?? s["id"]?.stringValue else { return nil }
                return ChatSlashItem(name: n.replacingOccurrences(of: " ", with: "-"),
                                     label: s["description"]?.stringValue ?? "", kind: .skill)
            }
            loadedAt = Date()
        }
    }

    func matches(_ query: String) -> [ChatSlashItem] {
        let q = query.lowercased()
        let all = Self.builtins + skills
        if q.isEmpty { return all }
        let starts = all.filter { $0.name.lowercased().hasPrefix(q) }
        let contains = all.filter { !$0.name.lowercased().hasPrefix(q) && $0.name.lowercased().contains(q) }
        return starts + contains
    }
}

extension AppStore {
    /// Runs a built-in command. Returns the composer text to leave behind.
    func runChatSlash(_ item: ChatSlashItem) -> String {
        if item.kind == .skill { return "Use the \(item.name) skill: " }
        switch item.name {
        case "new": newChat()
        case "compact": compactChat()
        case "copy": copyChat()
        case "save": saveChatToDocuments()
        case "think": thinkingOn = true
        case "fast": thinkingOn = false
        case "agent": agentMode = true
        case "chat": agentMode = false
        default: break
        }
        return ""
    }

    /// Puts a user message back in the composer as a new draft. Nothing is
    /// deleted; sending it adds a new message.
    func beginEdit(_ message: Message) {
        NotificationCenter.default.post(name: .chatFillComposer, object: message.content)
    }
}

/// Floating suggestion list above the composer.
struct ChatSlashPopover: View {
    let items: [ChatSlashItem]
    let selected: Int
    var onHover: (Int) -> Void
    var onPick: (ChatSlashItem) -> Void

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(spacing: 1) {
                    ForEach(Array(items.enumerated()), id: \.element.id) { i, item in
                        row(item, selected: i == selected)
                            .id(item.id)
                            .onHover { if $0 { onHover(i) } }
                            .onTapGesture { onPick(item) }
                    }
                }
                .padding(5)
            }
            .scrollIndicators(.never)
            .frame(maxHeight: 232)
            .fixedSize(horizontal: false, vertical: true)
            .onChange(of: selected) {
                if items.indices.contains(selected) { proxy.scrollTo(items[selected].id) }
            }
        }
        .frame(width: 340)
        .jbFloating()
    }

    private func row(_ item: ChatSlashItem, selected: Bool) -> some View {
        HStack(spacing: 8) {
            Text("/" + item.name)
                .font(.jbMono(12.5, .medium))
                .foregroundStyle(JB.ink)
            if item.kind == .skill && item.label.isEmpty {
                JBChip(text: "skill")
            } else if !item.label.isEmpty {
                Text(item.label)
                    .font(.jbMono(11))
                    .foregroundStyle(JB.ink3)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
            if selected { JBKbd("↩") }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .jbHoverRow(selected: selected)
        .contentShape(Rectangle())
    }
}
