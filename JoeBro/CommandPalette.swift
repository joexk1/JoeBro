import SwiftUI
import AppKit

extension Notification.Name {
    static let jbOpenPalette = Notification.Name("jbOpenPalette")
    static let jbToggleSidebar = Notification.Name("jbToggleSidebar")
}

/// The panel order behind ⌘1–9 and the palette.
enum JBPanelOrder {
    static let jumpTabs: [WorkspaceTab] = [.chat, .email, .calendar, .memory, .agents, .nightShift, .tasks, .skills, .tools]
    static let paletteTabs: [WorkspaceTab] = jumpTabs + [.notes, .research, .inbox, .aiCheck]

    static func shortcut(for tab: WorkspaceTab) -> String? {
        jumpTabs.firstIndex(of: tab).map { "⌘\($0 + 1)" }
    }
}

private struct PaletteItem: Identifiable {
    enum Kind { case chat, panel, action }
    let id: String
    let title: String
    let icon: String
    var meta: String? = nil
    var kbd: String? = nil
    let kind: Kind
    let run: () -> Void
}

/// Floating ⌘K palette: panels, recent chats and actions with fuzzy filtering.
struct CommandPalette: View {
    @Environment(AppStore.self) private var store
    @Binding var isPresented: Bool
    var toggleSidebar: () -> Void
    @State private var query = ""
    @State private var selected = 0
    @FocusState private var focused: Bool

    private var items: [PaletteItem] {
        let close = { isPresented = false }
        var chats: [PaletteItem] = store.sessions.prefix(40).map { s in
            PaletteItem(id: "chat-" + s.id, title: s.name, icon: "bubble.left", meta: nil, kind: .chat) {
                close(); store.select(s.id)
            }
        }
        if query.isEmpty { chats = Array(chats.prefix(6)) }
        let panels: [PaletteItem] = JBPanelOrder.paletteTabs.map { tab in
            PaletteItem(id: "tab-" + tab.rawValue, title: tab.title, icon: tab.icon,
                        kbd: JBPanelOrder.shortcut(for: tab), kind: .panel) {
                close(); store.activeTab = tab
            }
        }
        let actions: [PaletteItem] = [
            PaletteItem(id: "new", title: "New chat", icon: "plus", kbd: "⌘N", kind: .action) {
                close(); store.newChat()
            },
            PaletteItem(id: "settings", title: "Settings", icon: "gearshape", kbd: "⌘,", kind: .action) {
                close(); NSApp.sendAction(Selector(("showSettingsWindow:")), to: nil, from: nil)
            },
            PaletteItem(id: "sidebar", title: "Toggle sidebar", icon: "sidebar.left", kbd: "⌘\\", kind: .action) {
                close(); toggleSidebar()
            },
        ]
        if query.isEmpty { return chats + actions + panels }
        let all = chats + panels + actions
        return all.compactMap { item -> (PaletteItem, Int)? in
            Self.score(query, item.title).map { (item, $0) }
        }
        .sorted { $0.1 > $1.1 }
        .map(\.0)
    }

    /// Subsequence match: higher is better; nil when the query isn't contained.
    private static func score(_ q: String, _ text: String) -> Int? {
        let q = Array(q.lowercased()), t = Array(text.lowercased())
        var qi = 0, score = 0, prev = -2
        for (i, ch) in t.enumerated() where qi < q.count && ch == q[qi] {
            score += 1
            if i == prev + 1 { score += 3 }
            if i == 0 || t[i - 1] == " " { score += 4 }
            prev = i
            qi += 1
        }
        guard qi == q.count else { return nil }
        return score * 10 - t.count
    }

    var body: some View {
        let list = items
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(JB.ink3)
                TextField("Jump to a panel or chat", text: $query)
                    .textFieldStyle(.plain)
                    .font(.jb(15))
                    .focused($focused)
                    .onSubmit { run(list) }
                    .onKeyPress(.downArrow) { move(1, count: list.count); return .handled }
                    .onKeyPress(.upArrow) { move(-1, count: list.count); return .handled }
                    .onKeyPress(.escape) { isPresented = false; return .handled }
                JBKbd("esc")
            }
            .padding(.horizontal, 14)
            .frame(height: 48)
            Rectangle().fill(JB.line).frame(height: 1)
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 1) {
                        if list.isEmpty {
                            Text("No matches")
                                .font(.jb(13))
                                .foregroundStyle(JB.ink3)
                                .padding(.vertical, 20)
                        }
                        ForEach(Array(list.enumerated()), id: \.element.id) { idx, item in
                            row(item, selected: idx == selected)
                                .id(item.id)
                                .onTapGesture { item.run() }
                        }
                    }
                    .padding(6)
                }
                .frame(maxHeight: 360)
                .onChange(of: selected) { _, new in
                    if list.indices.contains(new) { proxy.scrollTo(list[new].id) }
                }
            }
        }
        .frame(width: 560)
        .jbFloating(radius: JB.rCard + 4)
        .onAppear { focused = true }
        .onChange(of: query) { _, _ in selected = 0 }
    }

    private func row(_ item: PaletteItem, selected: Bool) -> some View {
        HStack(spacing: 10) {
            JBSymbol(name: item.icon, size: 14)
                .foregroundStyle(selected ? JB.ink : JB.ink2)
                .frame(width: 20)
            Text(item.title)
                .font(.jb(13))
                .foregroundStyle(JB.ink)
                .lineLimit(1)
            Spacer(minLength: 8)
            if let kbd = item.kbd { JBKbd(kbd) }
            else if item.kind == .chat { Text("chat").font(.jb(10.5)).foregroundStyle(JB.ink3) }
        }
        .padding(.horizontal, 10)
        .frame(height: 32)
        .contentShape(Rectangle())
        .jbHoverRow(selected: selected)
    }

    private func move(_ d: Int, count: Int) {
        guard count > 0 else { return }
        selected = (selected + d + count) % count
    }

    private func run(_ list: [PaletteItem]) {
        guard list.indices.contains(selected) else { return }
        list[selected].run()
    }
}
