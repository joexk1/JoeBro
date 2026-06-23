import SwiftUI
import AppKit
import UniformTypeIdentifiers

struct SidebarView: View {
    @Environment(AppStore.self) private var store
    @State private var chatsExpanded = true
    @State private var workspaceExpanded = UserDefaults.standard.object(forKey: "workspaceExpanded") == nil
        ? true : UserDefaults.standard.bool(forKey: "workspaceExpanded")
    @State private var renameTarget: ChatSessionInfo?
    @State private var renameText = ""
    @State private var showGlassTuner = false
    @State private var showLocalInfo = false
    // Collapsed project folders, persisted across launches.
    @State private var collapsedFolders: Set<String> =
        Set(UserDefaults.standard.stringArray(forKey: "collapsedChatFolders") ?? [])
    // Pinned project folders, floated to the top of the list, persisted.
    @State private var pinnedFolders: Set<String> =
        Set(UserDefaults.standard.stringArray(forKey: "pinnedChatFolders") ?? [])
    // The chat currently being dragged, and where it will drop (insertion line).
    @State private var dragging: String?
    @State private var dropEdge: DropEdge?
    // "Rename Folder" prompt.
    @State private var renameFolderTarget: FolderRenameTarget?
    @State private var renameFolderText = ""
    // "New Folder…" prompt — moves this chat into a freshly named folder.
    @State private var newFolderTarget: ChatSessionInfo?
    @State private var newFolderText = ""

    var body: some View {
        @Bindable var store = store
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Image(systemName: "sparkles")
                    .foregroundStyle(Color.accentColor)
                Text("JoeBro")
                    .font(.system(.title3, design: .rounded, weight: .bold))
                Spacer()
                Button {
                    store.newChat()
                } label: {
                    Image(systemName: "square.and.pencil")
                        .font(.system(size: 14, weight: .semibold))
                }
                .buttonStyle(.borderless)
                .help("New chat (⌘N)")
            }
            .padding(.top, 14)
            .padding(.horizontal, 16)
            .padding(.bottom, 10)

            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                    .font(.system(size: 12))
                TextField("Search chats", text: $store.sessionSearch)
                    .textFieldStyle(.plain)
                    .font(.system(size: 13))
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .background(.quaternary.opacity(0.5), in: Capsule())
            .padding(.horizontal, 12)
            .padding(.bottom, 8)

            ScrollView {
                VStack(alignment: .leading, spacing: 2) {
                    // Chats
                    sectionHeader("Chats", expanded: $chatsExpanded)
                    if chatsExpanded {
                        // Project folders first (collapsible), then ungrouped chats.
                        let grouped = groupedSessions(store.filteredSessions)
                        ForEach(grouped.folders, id: \.name) { group in
                            folderHeader(group.name, count: group.sessions.count)
                            if !collapsedFolders.contains(group.name) {
                                ForEach(group.sessions) { session in
                                    sessionRow(session, indented: true)
                                }
                                // Bottom-of-folder drop catcher: a chat dropped just
                                // below the last chat stays IN this folder instead of
                                // escaping into the ungrouped list below.
                                Color.clear
                                    .frame(maxWidth: .infinity, minHeight: 12)
                                    .contentShape(Rectangle())
                                    .onDrop(of: [.text], delegate: MoveDropDelegate(
                                        dragging: $dragging,
                                        commit: { store.moveSession($0, toFolder: group.name, sortOrder: folderBottomOrder(group.name)) }))
                            }
                        }
                        VStack(alignment: .leading, spacing: 2) {
                            ForEach(grouped.ungrouped) { session in
                                sessionRow(session, indented: false)
                            }
                            if grouped.ungrouped.isEmpty && !grouped.folders.isEmpty {
                                Text("Drop here to remove from a folder")
                                    .font(.system(size: 11))
                                    .foregroundStyle(.tertiary)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(.horizontal, 10)
                                    .padding(.vertical, 10)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .onDrop(of: [.text], delegate: MoveDropDelegate(
                            dragging: $dragging,
                            commit: { store.moveSession($0, toFolder: nil, sortOrder: ungroupedTopOrder()) }))
                    }

                    // Bound project folder — visible on fresh chats too, so
                    // you can bind a folder before the first message
                    if store.activeTab == .chat {
                        FileTreeSection()
                    }

                    sectionHeader("Workspace", expanded: Binding(
                        get: { workspaceExpanded },
                        set: { workspaceExpanded = $0; UserDefaults.standard.set($0, forKey: "workspaceExpanded") }
                    ))
                    if workspaceExpanded {
                        Group {
                            navRow(.email)
                            navRow(.calendar)
                            navRow(.brain)
                            navRow(.tasks)
                            navRow(.skills)
                            navRow(.tools)
                            navRow(.research)
                            navRow(.aiCheck)
                        }
                        .transition(.opacity.combined(with: .move(edge: .top)))
                    }
                }
                .padding(.horizontal, 8)
                .padding(.bottom, 8)
                .animation(.spring(duration: 0.28), value: workspaceExpanded)
                .animation(.spring(duration: 0.28), value: chatsExpanded)
                .animation(.spring(duration: 0.28), value: collapsedFolders)
                .animation(.spring(duration: 0.28), value: pinnedFolders)
                .animation(.spring(duration: 0.26), value: store.filteredSessions.map(\.id))
            }

            Divider().opacity(0.4)
            HStack(spacing: 8) {
                Button {
                    showLocalInfo.toggle()
                } label: {
                    Image(systemName: "lock.laptopcomputer")
                        .font(.system(size: 18))
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.borderless)
                .help("Local only")
                .popover(isPresented: $showLocalInfo, arrowEdge: .top) {
                    Text("Local only — everything runs and stays on this Mac.")
                        .font(.system(size: 12))
                        .padding(12)
                }
                Spacer()
                Button {
                    showGlassTuner.toggle()
                } label: {
                    Image(systemName: "circle.lefthalf.filled")
                        .font(.system(size: 13))
                }
                .buttonStyle(.borderless)
                .help("Glass opacity")
                .popover(isPresented: $showGlassTuner, arrowEdge: .top) {
                    GlassTunerPopover()
                }
                SettingsLink {
                    Image(systemName: "gearshape")
                        .font(.system(size: 13))
                }
                .buttonStyle(.borderless)
                .help("Settings (⌘,)")
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
        }
        .sheet(item: $renameTarget) { session in
            VStack(spacing: 14) {
                Text("Rename Chat")
                    .font(.system(size: 14, weight: .semibold))
                TextField("Chat name", text: $renameText)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 280)
                    .onSubmit {
                        store.renameSession(session.id, to: renameText)
                        renameTarget = nil
                    }
                HStack {
                    Button("Cancel") { renameTarget = nil }
                        .keyboardShortcut(.cancelAction)
                    Button("Rename") {
                        store.renameSession(session.id, to: renameText)
                        renameTarget = nil
                    }
                    .buttonStyle(.glassProminent)
                    .tint(Color.accentColor)
                    .disabled(renameText.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .padding(24)
            .onAppear { renameText = session.name }
        }
        .sheet(item: $newFolderTarget) { session in
            VStack(spacing: 14) {
                Text("New Project Folder")
                    .font(.system(size: 14, weight: .semibold))
                TextField("Folder name", text: $newFolderText)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 280)
                    .onSubmit {
                        store.setFolder(session.id, folder: newFolderText)
                        newFolderTarget = nil
                    }
                HStack {
                    Button("Cancel") { newFolderTarget = nil }
                        .keyboardShortcut(.cancelAction)
                    Button("Create") {
                        store.setFolder(session.id, folder: newFolderText)
                        newFolderTarget = nil
                    }
                    .buttonStyle(.glassProminent)
                    .tint(Color.accentColor)
                    .disabled(newFolderText.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .padding(24)
            .onAppear { newFolderText = "" }
        }
        .sheet(item: $renameFolderTarget) { target in
            VStack(spacing: 14) {
                Text("Rename Project Folder")
                    .font(.system(size: 14, weight: .semibold))
                TextField("Folder name", text: $renameFolderText)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 280)
                    .onSubmit { commitFolderRename(target.name) }
                HStack {
                    Button("Cancel") { renameFolderTarget = nil }
                        .keyboardShortcut(.cancelAction)
                    Button("Rename") { commitFolderRename(target.name) }
                        .buttonStyle(.glassProminent)
                        .tint(Color.accentColor)
                        .disabled(renameFolderText.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .padding(24)
        }
    }

    // MARK: - Project-folder grouping

    private struct FolderGroup { var name: String; var sessions: [ChatSessionInfo] }

    /// Split the (already pin-sorted, search-filtered) sessions into project
    /// folders + ungrouped chats, preserving the incoming order in each.
    private func groupedSessions(_ list: [ChatSessionInfo]) -> (folders: [FolderGroup], ungrouped: [ChatSessionInfo]) {
        var order: [String] = []
        var byFolder: [String: [ChatSessionInfo]] = [:]
        var ungrouped: [ChatSessionInfo] = []
        for s in list {
            let f = s.folder?.trimmingCharacters(in: .whitespaces) ?? ""
            if f.isEmpty {
                ungrouped.append(s)
            } else {
                if byFolder[f] == nil { order.append(f) }
                byFolder[f, default: []].append(s)
            }
        }
        let folders = order
            .sorted { a, b in
                // Pinned folders float to the top, then alphabetical.
                let pa = pinnedFolders.contains(a), pb = pinnedFolders.contains(b)
                if pa != pb { return pa }
                return a.localizedCaseInsensitiveCompare(b) == .orderedAscending
            }
            .map { FolderGroup(name: $0, sessions: byFolder[$0] ?? []) }
        return (folders, ungrouped)
    }

    private func toggleFolder(_ name: String) {
        if collapsedFolders.contains(name) { collapsedFolders.remove(name) }
        else { collapsedFolders.insert(name) }
        UserDefaults.standard.set(Array(collapsedFolders), forKey: "collapsedChatFolders")
    }

    private func togglePinFolder(_ name: String) {
        if pinnedFolders.contains(name) { pinnedFolders.remove(name) }
        else { pinnedFolders.insert(name) }
        UserDefaults.standard.set(Array(pinnedFolders), forKey: "pinnedChatFolders")
    }

    /// Apply a folder rename and carry the collapsed/pinned state to the new name.
    private func commitFolderRename(_ old: String) {
        let new = renameFolderText.trimmingCharacters(in: .whitespaces)
        renameFolderTarget = nil
        guard !new.isEmpty, new != old else { return }
        if collapsedFolders.remove(old) != nil {
            collapsedFolders.insert(new)
            UserDefaults.standard.set(Array(collapsedFolders), forKey: "collapsedChatFolders")
        }
        if pinnedFolders.remove(old) != nil {
            pinnedFolders.insert(new)
            UserDefaults.standard.set(Array(pinnedFolders), forKey: "pinnedChatFolders")
        }
        store.renameFolder(old, to: new)
    }

    // Drag-reorder helpers: fractional sort_order lets a chat slot between two
    // others without renumbering the rest.
    private func sessionsInFolder(_ folder: String?) -> [ChatSessionInfo] {
        let key = folder?.trimmingCharacters(in: .whitespaces) ?? ""
        return store.sessions
            .filter { ($0.folder?.trimmingCharacters(in: .whitespaces) ?? "") == key }
            .sorted { ($0.sortOrder ?? 0) < ($1.sortOrder ?? 0) }
    }
    /// Order value placing a dragged chat just above `target`.
    private func orderBefore(_ target: ChatSessionInfo) -> Double {
        let group = sessionsInFolder(target.folder)
        let targetOrder = target.sortOrder ?? 0
        guard let idx = group.firstIndex(where: { $0.id == target.id }), idx > 0 else {
            return targetOrder - 1
        }
        let prev = group[idx - 1].sortOrder ?? (targetOrder - 2)
        return (prev + targetOrder) / 2
    }
    private func folderTopOrder(_ folder: String) -> Double {
        (sessionsInFolder(folder).compactMap { $0.sortOrder }.min() ?? 0) - 1
    }
    private func folderBottomOrder(_ folder: String) -> Double {
        (sessionsInFolder(folder).compactMap { $0.sortOrder }.max() ?? 0) + 1
    }
    private func ungroupedTopOrder() -> Double {
        (sessionsInFolder(nil).compactMap { $0.sortOrder }.min() ?? 0) - 1
    }
    /// Order value placing a dragged chat just below `target`.
    private func orderAfter(_ target: ChatSessionInfo) -> Double {
        let group = sessionsInFolder(target.folder)
        let targetOrder = target.sortOrder ?? 0
        guard let idx = group.firstIndex(where: { $0.id == target.id }), idx < group.count - 1 else {
            return targetOrder + 1
        }
        let next = group[idx + 1].sortOrder ?? (targetOrder + 2)
        return (targetOrder + next) / 2
    }
    private var dropLine: some View {
        RoundedRectangle(cornerRadius: 1.5)
            .fill(Color.accentColor)
            .frame(height: 2.5)
            .padding(.horizontal, 6)
    }

    private func folderHeader(_ name: String, count: Int) -> some View {
        let collapsed = collapsedFolders.contains(name)
        return Button {
            withAnimation(.spring(duration: 0.25)) { toggleFolder(name) }
        } label: {
            HStack(spacing: 6) {
                Image(systemName: "chevron.right")
                    .font(.system(size: 8, weight: .bold))
                    .foregroundStyle(.tertiary)
                    .rotationEffect(.degrees(collapsed ? 0 : 90))
                Image(systemName: "folder.fill")
                    .font(.system(size: 11))
                    .foregroundStyle(Color.accentColor.opacity(0.8))
                Text(name)
                    .font(.system(size: 12, weight: .semibold))
                    .lineLimit(1)
                Text("\(count)")
                    .font(.system(size: 10.5))
                    .foregroundStyle(.tertiary)
                if pinnedFolders.contains(name) {
                    Image(systemName: "pin.fill")
                        .font(.system(size: 8))
                        .foregroundStyle(Color.accentColor.opacity(0.7))
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .contentShape(Rectangle())
        }
        .buttonStyle(.borderless)
        .contextMenu {
            Button("Rename Folder") {
                renameFolderText = name
                renameFolderTarget = FolderRenameTarget(name: name)
            }
            Button(pinnedFolders.contains(name) ? "Unpin Folder" : "Pin Folder") {
                togglePinFolder(name)
            }
        }
        .onDrop(of: [.text], delegate: MoveDropDelegate(
            dragging: $dragging,
            commit: { store.moveSession($0, toFolder: name, sortOrder: folderTopOrder(name)) }))
    }

    /// One chat row with the shared context menu (pin / rename / folder / delete).
    @ViewBuilder
    private func sessionRow(_ session: ChatSessionInfo, indented: Bool) -> some View {
        SessionRow(session: session,
                   isSelected: store.activeTab == .chat && session.id == store.selectedSessionID,
                   isWorking: store.workingSessions.contains(session.id),
                   hasDocAlert: store.docAlertSessions.contains(session.id))
            .padding(.leading, indented ? 14 : 0)
            .onTapGesture { store.select(session.id) }
            .contextMenu {
                Button(session.isImportant == true ? "Unpin" : "Pin") { store.togglePin(session.id) }
                Button("Rename") { renameTarget = session }
                Menu("Add to Project Folder") {
                    ForEach(store.folderNames, id: \.self) { name in
                        Button(name) { store.setFolder(session.id, folder: name) }
                    }
                    if !store.folderNames.isEmpty { Divider() }
                    Button("New Folder…") {
                        newFolderText = ""
                        newFolderTarget = session
                    }
                }
                if let folder = session.folder, !folder.isEmpty {
                    Button("Remove from Folder") { store.setFolder(session.id, folder: "") }
                }
                Button("Delete", role: .destructive) { store.deleteSession(session.id) }
            }
            .opacity(dragging == session.id ? 0.4 : 1)
            .overlay(alignment: .top) {
                if dragging != nil, dropEdge == DropEdge(id: session.id, below: false) { dropLine }
            }
            .overlay(alignment: .bottom) {
                if dragging != nil, dropEdge == DropEdge(id: session.id, below: true) { dropLine }
            }
            .background {
                GeometryReader { geo in
                    Color.clear.onDrop(of: [.text], delegate: RowReorderDelegate(
                        height: geo.size.height,
                        targetID: session.id,
                        dragging: $dragging,
                        dropEdge: $dropEdge,
                        commit: { dragged, below in
                            let order = below ? orderAfter(session) : orderBefore(session)
                            store.moveSession(dragged, toFolder: session.folder, sortOrder: order)
                        }))
                }
            }
            .onDrag {
                dragging = session.id
                dropEdge = nil
                return NSItemProvider(object: session.id as NSString)
            }
    }

    private func sectionHeader(_ text: String, expanded: Binding<Bool>) -> some View {
        Button {
            withAnimation(.spring(duration: 0.25)) { expanded.wrappedValue.toggle() }
        } label: {
            HStack(spacing: 4) {
                Text(text)
                    .font(.system(size: 10.5, weight: .semibold))
                    .foregroundStyle(.tertiary)
                    .textCase(.uppercase)
                Image(systemName: "chevron.right")
                    .font(.system(size: 7, weight: .bold))
                    .foregroundStyle(.tertiary)
                    .rotationEffect(.degrees(expanded.wrappedValue ? 90 : 0))
                Spacer()
            }
        }
        .buttonStyle(.borderless)
        .padding(.horizontal, 10)
        .padding(.top, 12)
        .padding(.bottom, 3)
    }

    private func navRow(_ tab: WorkspaceTab) -> some View {
        NavRowView(tab: tab, isSelected: store.activeTab == tab,
                   showDot: (tab == .email && store.emailDot) || (tab == .research && store.researchDot),
                   bounce: tab == .email && store.emailBounce,
                   working: tab == .research && store.researchRun != nil) {
            store.activeTab = tab
        }
    }
}

private struct NavRowView: View {
    @Environment(AppStore.self) var store
    let tab: WorkspaceTab
    let isSelected: Bool
    let showDot: Bool
    var bounce = false
    var working = false
    let action: () -> Void
    @State private var hovering = false
    @State private var bounceY: CGFloat = 0

    var body: some View {
        HStack(spacing: 9) {
            Image(systemName: tab.icon)
                .font(.system(size: 13))
                .foregroundStyle(isSelected ? Color.accentColor : .secondary)
                .frame(width: 20)
            Text(tab.title)
                .font(.system(size: 13, weight: isSelected ? .semibold : .regular))
            Spacer()
            if working {
                ProgressView()
                    .controlSize(.small)
                    .scaleEffect(0.6)
                    .frame(width: 12, height: 12)
            }
            if showDot && !working {
                Circle()
                    .fill(Color.accentColor)
                    .frame(width: 6, height: 6)
                    .offset(y: bounceY)
                    .transition(.scale.combined(with: .opacity))
            }
        }
        .animation(.spring(duration: 0.3), value: showDot)
        .onChange(of: bounce, initial: true) { _, on in
            if on {
                withAnimation(.easeInOut(duration: 0.45).repeatForever(autoreverses: true)) {
                    bounceY = -4
                }
            } else {
                withAnimation(.spring(duration: 0.25)) { bounceY = 0 }
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(
            RoundedRectangle(cornerRadius: 9)
                .fill(isSelected ? AnyShapeStyle(Color.accentColor.opacity(0.18))
                      : hovering ? AnyShapeStyle(.quaternary.opacity(0.55)) : AnyShapeStyle(.clear))
        )
        .scaleEffect(hovering ? 1.02 : 1)
        .contentShape(RoundedRectangle(cornerRadius: 9))
        .onHover { h in
            withAnimation(.spring(duration: 0.22)) { hovering = h }
        }
        .onTapGesture(perform: action)
    }
}

// MARK: - Bound-folder file tree

struct FileTreeSection: View {
    @Environment(AppStore.self) private var store
    @State private var showBind = false
    @State private var filesExpanded = UserDefaults.standard.object(forKey: "filesExpanded") == nil
        ? true : UserDefaults.standard.bool(forKey: "filesExpanded")

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 4) {
                Button {
                    withAnimation(.spring(duration: 0.28)) {
                        filesExpanded.toggle()
                        UserDefaults.standard.set(filesExpanded, forKey: "filesExpanded")
                    }
                } label: {
                    HStack(spacing: 4) {
                        Text("Files")
                            .font(.system(size: 10.5, weight: .semibold))
                            .foregroundStyle(.tertiary)
                            .textCase(.uppercase)
                        Image(systemName: "chevron.right")
                            .font(.system(size: 7, weight: .bold))
                            .foregroundStyle(.tertiary)
                            .rotationEffect(.degrees(filesExpanded ? 90 : 0))
                    }
                }
                .buttonStyle(.borderless)
                if let wd = store.workdirPath {
                    Text("· \((wd as NSString).lastPathComponent)")
                        .font(.system(size: 10.5))
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
                Spacer()
                if store.workdirPath != nil {
                    Button {
                        store.refreshTree()
                    } label: {
                        Image(systemName: "arrow.clockwise")
                            .font(.system(size: 9))
                            .foregroundStyle(.tertiary)
                    }
                    .buttonStyle(.borderless)
                }
            }
            .padding(.horizontal, 10)
            .padding(.top, 12)
            .padding(.bottom, 3)

            if filesExpanded && store.workdirPath == nil {
                // Empty state: bind a local project folder.
                Button {
                    showBind = true
                } label: {
                    HStack(spacing: 7) {
                        Image(systemName: "folder.badge.plus")
                            .font(.system(size: 12))
                        Text("Bind to a folder")
                            .font(.system(size: 12, weight: .medium))
                        Spacer()
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 8)
                    .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 9))
                }
                .buttonStyle(.borderless)
                .padding(.horizontal, 8)
                .sheet(isPresented: $showBind) {
                    FolderPickerSheet()
                }
            }

            if filesExpanded && store.workdirPath != nil {
                FileTreeLevel(sub: "", depth: 0)
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .animation(.spring(duration: 0.28), value: filesExpanded)
    }


}

private struct FileTreeLevel: View {
    @Environment(AppStore.self) private var store
    let sub: String
    let depth: Int

    var body: some View {
        let entries = (store.treeEntries[sub] ?? []).sorted {
            if $0.isDir != $1.isDir { return $0.isDir }
            return $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending
        }
        ForEach(entries) { entry in
            let path = sub.isEmpty ? entry.name : sub + "/" + entry.name
            FileTreeRow(entry: entry, path: path, depth: depth)
            if entry.isDir && store.expandedDirs.contains(path) {
                FileTreeLevel(sub: path, depth: depth + 1)
            }
        }
    }
}

private struct FileTreeRow: View {
    @Environment(AppStore.self) private var store
    let entry: WorkdirEntry
    let path: String
    let depth: Int
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 6) {
            if entry.isDir {
                Image(systemName: "chevron.right")
                    .font(.system(size: 7, weight: .bold))
                    .foregroundStyle(.tertiary)
                    .rotationEffect(.degrees(store.expandedDirs.contains(path) ? 90 : 0))
                Image(systemName: "folder.fill")
                    .font(.system(size: 11))
                    .foregroundStyle(Color.accentColor.opacity(0.8))
            } else {
                Image(systemName: fileIcon)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .padding(.leading, 13)
            }
            Text(entry.name)
                .font(.system(size: 12))
                .lineLimit(1)
            Spacer(minLength: 0)
        }
        .padding(.vertical, 4)
        .padding(.leading, 10 + CGFloat(depth) * 14)
        .padding(.trailing, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 7)
                .fill(hovering ? AnyShapeStyle(.quaternary.opacity(0.5)) : AnyShapeStyle(.clear))
        )
        .contentShape(RoundedRectangle(cornerRadius: 7))
        .scaleEffect(hovering ? 1.02 : 1)
        .onHover { h in
            withAnimation(.spring(duration: 0.22)) { hovering = h }
        }
        .onTapGesture {
            if entry.isDir {
                withAnimation(.spring(duration: 0.22)) { store.toggleDir(path) }
            } else {
                store.openWorkdirFile(sub: path)
            }
        }
        .contextMenu {
            Button("Rename…") { promptRename() }
            Button("Delete", role: .destructive) {
                store.deleteWorkdirFile(sub: path)
            }
        }
    }

    /// Native rename prompt (works for files and folders).
    private func promptRename() {
        let alert = NSAlert()
        alert.messageText = "Rename \(entry.isDir ? "folder" : "file")"
        alert.informativeText = entry.name
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        input.stringValue = entry.name
        alert.accessoryView = input
        alert.addButton(withTitle: "Rename")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = input
        if alert.runModal() == .alertFirstButtonReturn {
            store.renameWorkdirFile(sub: path, newName: input.stringValue)
        }
    }

    private var fileIcon: String {
        switch (entry.name as NSString).pathExtension.lowercased() {
        case "md", "markdown": return "doc.richtext"
        case "pdf": return "doc.fill"
        case "png", "jpg", "jpeg", "webp", "gif": return "photo"
        case "py", "js", "ts", "swift", "sh", "json", "html", "css": return "chevron.left.forwardslash.chevron.right"
        default: return "doc.text"
        }
    }
}

private struct SessionRow: View {
    let session: ChatSessionInfo
    let isSelected: Bool
    var isWorking = false
    var hasDocAlert = false
    @State private var hovering = false
    @State private var bounceY: CGFloat = 0

    var body: some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text(session.name)
                    .font(.system(size: 13, weight: isSelected ? .semibold : .regular))
                    .lineLimit(1)
                if let model = session.model, !model.isEmpty {
                    HStack(spacing: 4) {
                        ProviderLogoView(model: model, size: 9)
                            .foregroundStyle(.tertiary)
                        Text(model.components(separatedBy: "/").last ?? model)
                            .font(.system(size: 10.5))
                            .foregroundStyle(.tertiary)
                            .lineLimit(1)
                    }
                }
            }
            Spacer(minLength: 0)
            if hasDocAlert && !isWorking {
                Circle()
                    .fill(Color.accentColor)
                    .frame(width: 6, height: 6)
                    .offset(y: bounceY)
                    .transition(.scale.combined(with: .opacity))
                    .help("The AI edited a document — open this chat to review")
            }
            if isWorking {
                ProgressView()
                    .controlSize(.small)
                    .scaleEffect(0.6)
                    .frame(width: 14, height: 14)
                    .help("Working…")
            }
            if session.isImportant == true {
                Image(systemName: "pin.fill")
                    .font(.system(size: 8))
                    .foregroundStyle(Color.accentColor.opacity(0.8))
            }
            if session.mode == "agent" {
                Image(systemName: "hammer.fill")
                    .font(.system(size: 9))
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 9)
                .fill(isSelected
                      ? AnyShapeStyle(Color.accentColor.opacity(0.22))
                      : hovering ? AnyShapeStyle(.quaternary.opacity(0.6)) : AnyShapeStyle(.clear))
        )
        .scaleEffect(hovering ? 1.02 : 1)
        .contentShape(RoundedRectangle(cornerRadius: 9))
        .animation(.spring(duration: 0.3), value: hasDocAlert)
        .onChange(of: hasDocAlert, initial: true) { _, on in
            if on {
                withAnimation(.easeInOut(duration: 0.45).repeatForever(autoreverses: true)) {
                    bounceY = -4
                }
            } else {
                withAnimation(.spring(duration: 0.25)) { bounceY = 0 }
            }
        }
        .onHover { h in
            withAnimation(.spring(duration: 0.22)) { hovering = h }
        }
    }
}


private struct DropEdge: Equatable { let id: String; let below: Bool }

/// Reorder drop for a chat row: tracks whether the cursor is over the top or
/// bottom half (so you can drop ABOVE or BELOW any chat — an insertion line
/// shows where), uses MOVE semantics (no green "+"), commits once on drop.
private struct RowReorderDelegate: DropDelegate {
    let height: CGFloat
    let targetID: String
    @Binding var dragging: String?
    @Binding var dropEdge: DropEdge?
    let commit: (String, Bool) -> Void
    func validateDrop(info: DropInfo) -> Bool { dragging != nil }
    func dropUpdated(info: DropInfo) -> DropProposal? {
        guard dragging != nil else { dropEdge = nil; return nil }
        let edge = DropEdge(id: targetID, below: info.location.y > height / 2)
        if dropEdge != edge { dropEdge = edge }
        return DropProposal(operation: .move)
    }
    func dropExited(info: DropInfo) {
        if dropEdge?.id == targetID { dropEdge = nil }
    }
    func performDrop(info: DropInfo) -> Bool {
        guard let d = dragging else { return false }
        commit(d, info.location.y > height / 2)
        dragging = nil
        dropEdge = nil
        return true
    }
}

/// Drop a chat onto a folder header (file it) or the ungrouped zone (ungroup it).
/// MOVE semantics (no green "+"); commits once on drop.
private struct MoveDropDelegate: DropDelegate {
    @Binding var dragging: String?
    let commit: (String) -> Void
    func validateDrop(info: DropInfo) -> Bool { dragging != nil }
    func dropUpdated(info: DropInfo) -> DropProposal? { DropProposal(operation: .move) }
    func performDrop(info: DropInfo) -> Bool {
        if let d = dragging { commit(d) }
        dragging = nil
        return true
    }
}

private struct FolderRenameTarget: Identifiable {
    let id = UUID()
    let name: String
}
