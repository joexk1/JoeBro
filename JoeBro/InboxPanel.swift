import SwiftUI

/// Notices from the phone agent, tasks and agents land here. Tap to mark read.
struct InboxPanel: View {
    @Environment(AppStore.self) private var store
    @State private var items: Loadable<[InboxItem]> = .loading
    @State private var selection: Set<String> = []
    @State private var confirmClear = false

    var body: some View {
        PanelChrome(title: "Inbox", icon: "tray") {
            Button {
                Task { await load() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            if case .loaded(let list) = items, !list.isEmpty {
                Button("Select all") {
                    withAnimation(.spring(duration: 0.25)) { selection = Set(list.map(\.id)) }
                }
                .buttonStyle(.borderless)
                .font(.system(size: 11))
                Button("Clear all") { confirmClear = true }
                    .buttonStyle(.borderless)
                    .font(.system(size: 11))
            }
        } content: {
            ZStack {
                switch items {
                case .loading:
                    ProgressView()
                case .failed(let e):
                    ContentUnavailableView("Couldn't load the inbox", systemImage: "tray", description: Text(e))
                case .loaded(let list) where list.isEmpty:
                    ContentUnavailableView("Nothing here", systemImage: "tray",
                                           description: Text("Notices from your phone agent, tasks and agents show up here."))
                case .loaded(let list):
                    VStack(spacing: 0) {
                        if !selection.isEmpty {
                            SelectionBar(total: list.count, allIDs: list.map(\.id), selection: $selection) { ids in
                                Task { await APIClient.shared.deleteInbox(ids: ids); await load() }
                            }
                        }
                        ScrollView {
                            LazyVStack(spacing: 8) {
                                ForEach(list) { row($0) }
                            }
                            .padding(12)
                        }
                    }
                    .animation(.spring(duration: 0.25), value: selection.isEmpty)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .joeGlassRect(16)
        }
        .task { await load() }
        .confirmationDialog("Delete every notification?", isPresented: $confirmClear) {
            Button("Clear all", role: .destructive) {
                Task { await APIClient.shared.clearInbox(); await load() }
            }
        }
    }

    private func row(_ n: InboxItem) -> some View {
        SkillRowShell(id: n.id, checked: selection.contains(n.id),
                      selecting: !selection.isEmpty,
                      onToggleCheck: { toggleSelection(n.id, in: &selection) }) {
            Image(systemName: n.icon)
                .font(.system(size: 13))
                .foregroundStyle(n.read ? Color.secondary : Color.accentColor)
                .frame(width: 24)
            VStack(alignment: .leading, spacing: 3) {
                Text(n.title.isEmpty ? "Notice" : n.title)
                    .font(.system(size: 13, weight: n.read ? .regular : .semibold))
                if !n.body.isEmpty {
                    Text(n.body)
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
                Text(relativeDate(n.createdAt))
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
            }
            Spacer()
        }
        .contentShape(Rectangle())
        .onTapGesture {
            guard !n.read else { return }
            Task { await APIClient.shared.markInboxRead(n.id); await load() }
        }
        .contextMenu {
            Button("Delete", role: .destructive) {
                Task { await APIClient.shared.deleteInbox(ids: [n.id]); await load() }
            }
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
