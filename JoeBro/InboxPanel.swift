import SwiftUI

/// Notices from the phone agent, tasks and agents.
struct InboxPanel: View {
    @Environment(AppStore.self) private var store
    @State private var items: Loadable<[InboxItem]> = .loading
    @State private var selection: Set<String> = []
    @State private var hidden: Set<String> = []
    @State private var undoTask: Task<Void, Never>?
    @State private var undoText: String?

    var body: some View {
        PanelChrome(title: "Inbox", icon: "tray") {
            Button { Task { await load() } } label: { Image(systemName: "arrow.clockwise") }
                .buttonStyle(.jbIcon)
            if case .loaded(let list) = items, !list.isEmpty {
                Button("Select all") {
                    withAnimation(JB.snap) { selection = Set(list.map(\.id)) }
                }
                .buttonStyle(JBButtonStyle(kind: .ghost, compact: true))
                Button("Clear all") {
                    withAnimation(JB.smooth) { hidden.formUnion(list.map(\.id)) }
                    Task {
                        await APIClient.shared.markInboxRead(nil)
                        await APIClient.shared.clearInbox()
                        await load()
                        hidden = []
                    }
                }
                    .buttonStyle(JBButtonStyle(kind: .ghost, compact: true))
            }
        } content: {
            ZStack(alignment: .bottom) {
                switch items {
                case .loading:
                    PanelLoading()
                case .failed(let e):
                    PanelFailure(title: "Couldn't load the inbox", detail: e)
                case .loaded(let all):
                    let list = all.filter { !hidden.contains($0.id) }
                    if list.isEmpty {
                        PanelEmpty(art: .done, text: "Nothing here")
                    } else {
                        VStack(spacing: 0) {
                            if !selection.isEmpty {
                                SelectionBar(total: list.count, allIDs: list.map(\.id), selection: $selection) { ids in
                                    remove(ids, label: "Deleted \(ids.count)")
                                }
                            }
                            ScrollView {
                                LazyVStack(spacing: 8) {
                                    ForEach(list) { row($0) }
                                }
                                .padding(12)
                                .animation(JB.smooth, value: list.map(\.id))
                            }
                        }
                        .animation(JB.snap, value: selection.isEmpty)
                    }
                }
                if let undoText {
                    UndoStrip(text: undoText) { undo() }
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .jbSurface(.panel, radius: JB.rPanel)
        }
        .task { await load() }
    }

    private func row(_ n: InboxItem) -> some View {
        SkillRowShell(id: n.id, checked: selection.contains(n.id),
                      selecting: !selection.isEmpty,
                      onToggleCheck: { toggleSelection(n.id, in: &selection) }) {
            Image(systemName: n.icon)
                .font(.jb(13))
                .foregroundStyle(n.read ? JB.ink3 : JB.accent)
                .frame(width: 24)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    if !n.read { Circle().fill(JB.accent).frame(width: 6, height: 6) }
                    Text(n.title.isEmpty ? "Notice" : n.title)
                        .font(.jb(13, n.read ? .regular : .semibold))
                        .foregroundStyle(n.read ? JB.ink2 : JB.ink)
                }
                if !n.body.isEmpty {
                    Text(n.body)
                        .font(.jb(12))
                        .foregroundStyle(JB.ink2)
                        .textSelection(.enabled)
                }
                if n.kind == "coding_approval", !n.source.isEmpty, !n.read,
                   let d = parseISO(n.createdAt ?? ""), Date().timeIntervalSince(d) < 15 * 60 {
                    AgentApprovalButtons(agentID: n.source) {
                        await APIClient.shared.markInboxRead(n.id); await load()
                    }
                    .padding(.top, 2)
                }
                Text(relativeDate(n.createdAt))
                    .font(.jb(10.5))
                    .foregroundStyle(JB.ink3)
            }
            Spacer()
        }
        .jbHoverActions {
            Button { remove([n.id], label: "Deleted") } label: { Image(systemName: "trash") }
                .buttonStyle(.jbIcon).help("Delete")
        }
        .contentShape(Rectangle())
        .onTapGesture {
            guard !n.read else { return }
            Task { await APIClient.shared.markInboxRead(n.id); await load() }
        }
        .transition(.opacity.combined(with: .scale(scale: 0.98)))
    }

    /// Hide now, delete for real after the undo window.
    private func remove(_ ids: [String], label: String) {
        undoTask?.cancel()
        flush()
        withAnimation(JB.smooth) { hidden.formUnion(ids); selection.subtract(ids); undoText = label }
        undoTask = Task {
            try? await Task.sleep(for: .seconds(4))
            guard !Task.isCancelled else { return }
            flush()
        }
    }

    private func undo() {
        undoTask?.cancel()
        withAnimation(JB.smooth) { hidden = []; undoText = nil }
    }

    private func flush() {
        let ids = Array(hidden)
        withAnimation(JB.smooth) { undoText = nil }
        guard !ids.isEmpty else { return }
        Task {
            await APIClient.shared.deleteInbox(ids: ids)
            await load()
            hidden.subtract(ids)
        }
    }

    private func load() async {
        do {
            let list = try await APIClient.shared.inbox()
            items = .loaded(list)
            store.inboxUnread = list.filter { !$0.read }.count
        } catch { items = .failed(error.localizedDescription) }
    }
}
