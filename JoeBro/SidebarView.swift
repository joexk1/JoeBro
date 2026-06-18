import SwiftUI
import AppKit

struct SidebarView: View {
    @Environment(AppStore.self) private var store
    @State private var chatsExpanded = true
    @State private var workspaceExpanded = UserDefaults.standard.object(forKey: "workspaceExpanded") == nil
        ? true : UserDefaults.standard.bool(forKey: "workspaceExpanded")
    @State private var renameTarget: ChatSessionInfo?
    @State private var renameText = ""
    @State private var showGlassTuner = false
    @State private var showLocalInfo = false

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
                        ForEach(store.filteredSessions) { session in
                            SessionRow(session: session,
                                       isSelected: store.activeTab == .chat && session.id == store.selectedSessionID,
                                       isWorking: store.workingSessions.contains(session.id),
                                       hasDocAlert: store.docAlertSessions.contains(session.id))
                                .onTapGesture { store.select(session.id) }
                                .contextMenu {
                                    Button(session.isImportant == true ? "Unpin" : "Pin") { store.togglePin(session.id) }
                                    Button("Rename") { renameTarget = session }
                                    Button("Delete", role: .destructive) { store.deleteSession(session.id) }
                                }
                        }
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
    }

    private func sectionLabel(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 10.5, weight: .semibold))
            .foregroundStyle(.tertiary)
            .textCase(.uppercase)
            .padding(.horizontal, 10)
            .padding(.top, 12)
            .padding(.bottom, 3)
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
