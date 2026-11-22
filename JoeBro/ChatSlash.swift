import SwiftUI
import AppKit

extension Notification.Name {
    /// Object: String. Replaces the composer text (edit-last-message, chips).
    static let chatFillComposer = Notification.Name("JoeBro.chatFillComposer")
    /// Opens the photo picker for the window backdrop.
    static let jbPickBackdropPhoto = Notification.Name("JoeBro.pickBackdropPhoto")
}

enum ChatKey { case up, down, enter, tab, escape }

/// One row in the slash list: a built-in command or a skill.
struct ChatSlashItem: Identifiable, Equatable {
    /// action runs at once; menu opens its options; option is one choice of a menu.
    enum Kind: Equatable { case action, skill, menu, option }
    let name: String
    var label: String = ""
    var kind: Kind = .action
    var parent: String = ""
    var value: String = ""
    var current = false
    var id: String { "\(kind):\(parent):\(name)" }
}

/// Built-in commands + skills, fetched lazily and cached for a minute.
@MainActor @Observable
final class ChatSlashSource {
    static let shared = ChatSlashSource()
    private(set) var skills: [ChatSlashItem] = []
    private var loadedAt: Date?
    private var loading = false

    static let builtins: [ChatSlashItem] = [
        .init(name: "new", label: "new chat"),
        .init(name: "background", label: "scene, photo or plain", kind: .menu),
        .init(name: "palette", label: "accent colour", kind: .menu),
        .init(name: "panels", label: "panel opacity", kind: .menu),
        .init(name: "open", label: "go to a panel", kind: .menu),
        .init(name: "settings"),
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

    /// Choices for a menu command, with the current one marked.
    @MainActor static func options(for menu: String) -> [ChatSlashItem] {
        let look = JBAppearance.shared
        switch menu {
        case "background":
            var out: [ChatSlashItem] = [
                .init(name: "plain", label: "palette ground", kind: .option, parent: menu, value: "", current: look.backdrop == .palette),
                .init(name: "photo", label: "choose a picture…", kind: .option, parent: menu, value: "photo", current: look.backdrop == .photo),
            ]
            let scenes = JBBackdropLibrary.bundledScenes.map { ($0, $0) }
                + JBBackdropLibrary.userBackdrops().map { ($0.lastPathComponent, $0.lastPathComponent) }
            for (id, _) in scenes {
                out.append(.init(name: id.replacingOccurrences(of: " ", with: "-"), label: JBBackdropLibrary.displayName(id),
                                 kind: .option, parent: menu, value: id,
                                 current: look.backdrop == .scene && look.sceneID == id))
            }
            return out
        case "palette":
            return JBPalette.allCases.map {
                .init(name: $0.rawValue, label: $0.label, kind: .option, parent: menu, value: $0.rawValue, current: look.palette == $0)
            }
        case "panels":
            return [40, 60, 80, 100].map { v in
                .init(name: "\(v)", label: v == 100 ? "solid" : "\(v)% opaque", kind: .option, parent: menu, value: "\(v)",
                      current: abs(look.solidity * 100 - Double(v)) < 5)
            }
        case "open":
            let tabs: [WorkspaceTab] = [.chat, .agents, .tasks, .nightShift, .memory, .calendar, .email, .inbox,
                                        .research, .skills, .tools]
            return tabs.map { .init(name: $0.title.lowercased().replacingOccurrences(of: " ", with: "-"), label: $0.title,
                                    kind: .option, parent: menu, value: $0.rawValue) }
        default: return []
        }
    }

    @MainActor func matches(_ query: String) -> [ChatSlashItem] {
        let parts = query.split(separator: " ", maxSplits: 1, omittingEmptySubsequences: false)
        if parts.count == 2 {
            let menu = String(parts[0]).lowercased(), q = String(parts[1]).lowercased()
            let opts = Self.options(for: menu)
            if q.isEmpty { return opts }
            return opts.filter { $0.name.lowercased().hasPrefix(q) }
                + opts.filter { !$0.name.lowercased().hasPrefix(q) && ($0.name + " " + $0.label).lowercased().contains(q) }
        }
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
        if item.kind == .menu { return "/\(item.name) " }
        if item.kind == .option {
            let look = JBAppearance.shared
            switch item.parent {
            case "background":
                if item.value.isEmpty {
                    look.backdrop = .palette
                } else if item.value == "photo" {
                    NotificationCenter.default.post(name: .jbPickBackdropPhoto, object: nil)
                } else {
                    look.sceneID = item.value; look.backdrop = .scene; look.palette = .auto
                }
            case "palette": if let p = JBPalette(rawValue: item.value) { look.palette = p }
            case "panels": look.solidity = max(0.35, (Double(item.value) ?? 100) / 100)
            case "open": if let t = WorkspaceTab(rawValue: item.value) { activeTab = t }
            default: break
            }
            return ""
        }
        switch item.name {
        case "settings": NSApp.sendAction(Selector(("showSettingsWindow:")), to: nil, from: nil)
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
            if item.kind == .option {
                Text(item.label.isEmpty ? item.name : item.label)
                    .font(.jb(13, .medium))
                    .foregroundStyle(JB.ink)
            } else {
                Text("/" + item.name)
                    .font(.jb(12.5, .medium))
                    .foregroundStyle(JB.ink)
            }
            if item.kind == .skill && item.label.isEmpty {
                JBChip(text: "skill")
            } else if item.kind != .option, !item.label.isEmpty {
                Text(item.label)
                    .font(.jb(11))
                    .foregroundStyle(JB.ink3)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
            if item.current { Image(systemName: "checkmark").font(.jb(11, .semibold)).foregroundStyle(JB.accent) }
            if item.kind == .menu { Image(systemName: "chevron.right").font(.jb(10, .semibold)).foregroundStyle(JB.ink3) }
            else if selected { JBKbd("↩") }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .jbHoverRow(selected: selected)
        .contentShape(Rectangle())
    }
}
