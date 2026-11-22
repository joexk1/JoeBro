import SwiftUI

struct ChatView: View {
    @Environment(AppStore.self) private var store
    @Binding var sidebarVisible: Bool
    @State private var nearBottom = true
    @State private var renaming = false
    @State private var renameText = ""
    @State private var pdfPreview: URL?
    @State private var exportingPDF = false
    @State private var editorWidth: CGFloat = max(380, min(700, CGFloat(UserDefaults.standard.double(forKey: "editorWidth") == 0 ? 480 : UserDefaults.standard.double(forKey: "editorWidth"))))
    @State private var visibleCount = 60
    @State private var dragStartWidth: CGFloat?
    @State private var dropTargeted = false
    @State private var rowsSettled = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        GeometryReader { geo in
        // ONE editor pane, always trailing. If the chat column can keep its
        // ~720pt working width beside it, the chat pads over (split feel);
        // otherwise the pane overlays. Single view identity either way, so
        // editor state survives windowed ↔ full-screen transitions.
        let minChat: CGFloat = 720
        let paneWidth = min(editorWidth, max(340, geo.size.width - 60))
        let paneShown = store.editorVisible && !store.editorDetached
        let splits = paneShown && (geo.size.width - paneWidth - 12) >= minChat
        ZStack(alignment: .trailing) {
        HStack(spacing: 12) {
            VStack(spacing: 12) {
                header

                // One ComposerView slot for both states, so draft text and
                // focus survive the first send. Empty: banner + composer
                // centred; otherwise the transcript fills the column.
                if store.messages.isEmpty {
                    Spacer(minLength: 0)
                    emptyState
                } else {
                    messageList
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }

                ComposerView()
                    .frame(maxWidth: 860)
                    .frame(maxWidth: .infinity)

                if store.messages.isEmpty {
                    Spacer(minLength: 0)
                }
            }
            .animation(JB.smooth, value: store.messages.isEmpty)
            .onDrop(of: [.fileURL, .image], isTargeted: $dropTargeted) { providers in
                store.attachProviders(providers)
            }
            .overlay {
                if dropTargeted {
                    VStack(spacing: 10) {
                        Image(systemName: "paperclip")
                            .font(.jb(28, .light))
                            .foregroundStyle(JB.accent)
                        Text("Drop to attach")
                            .font(.jb(12))
                            .foregroundStyle(JB.ink2)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(JB.panelFill)
                    .overlay(
                        RoundedRectangle(cornerRadius: JB.rPanel, style: .continuous)
                            .strokeBorder(JB.accent.opacity(0.7), style: StrokeStyle(lineWidth: 1.5, dash: [6, 5]))
                            .padding(6)
                    )
                    .clipShape(RoundedRectangle(cornerRadius: JB.rPanel, style: .continuous))
                    .transition(.opacity)
                    .allowsHitTesting(false)
                }
            }
            .animation(JB.snap, value: dropTargeted)
            .overlay(alignment: .bottom) {
                if let p = store.pendingPermission {
                    CommandPermissionPrompt(prompt: p) { store.respondPermission($0) }
                        .padding(.bottom, 90)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                }
            }
            .animation(JB.snap, value: store.pendingPermission?.id)

        }
        .padding(.trailing, splits ? paneWidth + 12 : 0)

        // The one true editor pane
        if paneShown {
            EditorPane()
                .frame(width: paneWidth)
                .overlay(alignment: .leading) {
                    // Grab the left edge to resize
                    Rectangle()
                        .fill(.clear)
                        .frame(width: 9)
                        .contentShape(Rectangle())
                        .onHover { inside in
                            if inside { NSCursor.resizeLeftRight.push() }
                            else { NSCursor.pop() }
                        }
                        .gesture(
                            DragGesture(coordinateSpace: .global)
                                .onChanged { v in
                                    if dragStartWidth == nil { dragStartWidth = editorWidth }
                                    editorWidth = max(340, min(geo.size.width - 60,
                                                               (dragStartWidth ?? editorWidth) - v.translation.width))
                                }
                                .onEnded { _ in
                                    dragStartWidth = nil
                                    UserDefaults.standard.set(Double(editorWidth), forKey: "editorWidth")
                                }
                        )
                }
                .shadow(color: .black.opacity(splits ? 0.16 : 0.35), radius: splits ? 18 : 24, x: splits ? 0 : -6)
                .transition(.move(edge: .trailing).combined(with: .opacity))
        }
        }
        .animation(.spring(duration: 0.25), value: splits)
        }
        .animation(.spring(duration: 0.32), value: store.editorVisible)
        .onChange(of: store.selectedSessionID) {
            visibleCount = 60
            // Opening a chat should not animate its whole history in.
            rowsSettled = false
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { rowsSettled = true }
        }
        .task {
            try? await Task.sleep(for: .milliseconds(600))
            rowsSettled = true
        }
        .sheet(isPresented: $renaming) {
            VStack(spacing: 14) {
                Text("Rename chat")
                    .font(.jb(14, .medium))
                    .foregroundStyle(JB.ink)
                TextField("Chat name", text: $renameText)
                    .jbField()
                    .frame(width: 280)
                    .onSubmit(doRename)
                HStack {
                    Button("Cancel") { renaming = false }
                        .buttonStyle(.jbGhost)
                        .keyboardShortcut(.cancelAction)
                    Button("Rename", action: doRename)
                        .buttonStyle(.jbPrimary)
                        .disabled(renameText.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .padding(24)
        }
        .sheet(item: Binding(
            get: { pdfPreview.map { ChatPDFWrap(url: $0) } },
            set: { pdfPreview = $0?.url }
        )) { wrap in
            QuickLookSheet(url: wrap.url)
        }
        .sheet(item: Binding(
            get: { store.quickLookURL.map { ChatPDFWrap(url: $0) } },
            set: { store.quickLookURL = $0?.url }
        )) { wrap in
            QuickLookSheet(url: wrap.url)
        }
    }

    private func doRename() {
        if let sid = store.selectedSessionID {
            store.renameSession(sid, to: renameText)
        }
        renaming = false
    }

    private var header: some View {
        HStack(spacing: 10) {
            Button {
                sidebarVisible.toggle()
            } label: {
                Image(systemName: "sidebar.left")
            }
            .buttonStyle(JBIconButtonStyle(size: 26))
            .help("Toggle sidebar")

            // Title dropdown — Rename / Compact / Copy / PDF / Save to Documents
            Menu {
                Button {
                    renameText = currentTitle
                    renaming = true
                } label: {
                    Label("Rename", systemImage: "pencil")
                }
                Button {
                    store.compactChat()
                } label: {
                    Label("Compact Chat", systemImage: "arrow.down.right.and.arrow.up.left")
                }
                .disabled(store.messages.count < 6)
                Button {
                    store.copyChat()
                } label: {
                    Label("Copy Chat", systemImage: "doc.on.doc")
                }
                Button {
                    exportingPDF = true
                    Task {
                        pdfPreview = await store.exportChatPDF()
                        exportingPDF = false
                    }
                } label: {
                    Label("PDF", systemImage: "doc.richtext")
                }
                Button {
                    store.saveChatToDocuments()
                } label: {
                    Label("Save to Documents", systemImage: "doc.badge.plus")
                }
            } label: {
                HStack(spacing: 5) {
                    Text(currentTitle)
                        .font(.jb(14, .medium))
                        .foregroundStyle(JB.ink)
                        .lineLimit(1)
                    Image(systemName: "chevron.down")
                        .font(.jb(8, .semibold))
                        .foregroundStyle(JB.ink3)
                }
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .disabled(store.selectedSessionID == nil)

            if !store.messages.isEmpty {
                Text("\(store.messages.count)")
                    .font(.jb(11))
                    .foregroundStyle(JB.ink3)
                    .help("Messages in this chat")
            }

            if store.compacting || exportingPDF {
                JBSpinner()
            }

            Spacer()

            if !store.openDocs.isEmpty && !store.editorVisible {
                Button {
                    store.editorVisible = true
                } label: {
                    Label("\(store.openDocs.count)", systemImage: "doc.text")
                        .font(.jb(11.5))
                        .foregroundStyle(JB.ink2)
                }
                .buttonStyle(.plain)
                .help("Show editor")
            }

            if let err = store.loadError {
                HStack(spacing: 6) {
                    Image(systemName: "exclamationmark.triangle")
                        .font(.jb(10))
                        .foregroundStyle(JB.warn)
                    Text(err)
                        .font(.jb(11))
                        .foregroundStyle(JB.ink2)
                        .lineLimit(1)
                        .frame(maxWidth: 320)
                    Button {
                        store.loadError = nil
                    } label: {
                        Image(systemName: "xmark")
                            .font(.jb(9, .semibold))
                            .foregroundStyle(JB.ink3)
                    }
                    .buttonStyle(.plain)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 5)
                .background(Capsule().fill(JB.warn.opacity(0.14)))
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .jbSurface(.chrome, radius: JB.rPanel)
    }

    private struct ChatPDFWrap: Identifiable {
        let url: URL
        var id: String { url.path }
    }

    private var currentTitle: String {
        if let sid = store.selectedSessionID,
           let s = store.sessions.first(where: { $0.id == sid }) {
            return s.name
        }
        return "New Chat"
    }

    private var emptyState: some View {
        VStack(spacing: 22) {
            JBWordmark(dot: 3.4)
            Text("What should we work on today?")
                .font(.jb(15))
                .foregroundStyle(JB.ink2)
        }
        .frame(maxWidth: .infinity)
        .padding(.bottom, 28)
        .transition(.opacity)
    }

    private func isOn(_ name: String) -> Bool {
        switch name {
        case "agent": return store.agentMode
        case "chat": return !store.agentMode
        case "think": return store.thinkingOn
        default: return !store.thinkingOn
        }
    }

    private var groupedMessages: [Message] {
        var rows: [Message] = []
        var activityIndex: Int?
        for message in store.messages {
            if message.isUser || message.kind == .compacted { activityIndex = nil }
            if message.kind == .tool {
                if let index = activityIndex {
                    rows[index].activityTools.append(message)
                } else {
                    var activity = message
                    activity.activityTools = [message]
                    activityIndex = rows.count
                    rows.append(activity)
                }
            } else { rows.append(message) }
        }
        return rows
    }

    private var messageList: some View {
        let rows = groupedMessages
        return ScrollViewReader { proxy in
            ScrollView {
                // Eager VStack: LazyVStack + defaultScrollAnchor(.bottom)
                // left the viewport empty until first scroll (rows above the
                // anchor never materialized). Equatable rows keep it cheap.
                VStack(spacing: 12) {
                    // Big chats render only the recent tail — eager-rendering
                    // hundreds of markdown bubbles at once was the open-chat lag.
                    if rows.count > visibleCount {
                        Button {
                            visibleCount += 100
                        } label: {
                            Label("\(rows.count - visibleCount) earlier",
                                  systemImage: "arrow.up")
                                .font(.jb(11.5))
                                .foregroundStyle(JB.ink2)
                        }
                        .buttonStyle(.plain)
                        .padding(.top, 6)
                    }
                    ForEach(rows.suffix(visibleCount)) { message in
                        MessageBubbleView(message: message)
                            .equatable()
                            .id(message.id)
                            .transition(.opacity.combined(with: .offset(y: 6)))
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .padding(.horizontal, 18)
                .padding(.top, 8)
                .animation(rowsSettled && !reduceMotion ? JB.smooth : nil, value: rows.count)
                .frame(maxWidth: 860)
                .frame(maxWidth: .infinity)
            }
            .scrollIndicators(.never)
            .defaultScrollAnchor(.bottom)
            .clipShape(RoundedRectangle(cornerRadius: JB.rPanel, style: .continuous))
            .onScrollGeometryChange(for: Bool.self) { geo in
                geo.contentOffset.y + geo.containerSize.height >= geo.contentSize.height - 80
            } action: { _, isNear in
                nearBottom = isNear
            }
            .onChange(of: store.messages.count) {
                proxy.scrollTo("bottom", anchor: .bottom)
            }
            .onChange(of: store.selectedSessionID) {
                // History lands async; jump once layout settles
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) {
                    proxy.scrollTo("bottom", anchor: .bottom)
                }
            }
            // Opening/closing the editor pane reflows the chat column —
            // restore the bottom anchor instead of landing mid-transcript.
            .onChange(of: store.editorVisible) {
                if nearBottom {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.36) {
                        proxy.scrollTo("bottom", anchor: .bottom)
                    }
                }
            }
            .overlay(alignment: .bottom) {
                if !nearBottom {
                    Button {
                        withAnimation(JB.smooth) {
                            proxy.scrollTo("bottom", anchor: .bottom)
                        }
                    } label: {
                        Image(systemName: "arrow.down")
                    }
                    .buttonStyle(JBIconButtonStyle(size: 30))
                    .jbFloating(radius: JB.rControl + 2)
                    .padding(.bottom, 12)
                    .transition(.opacity.combined(with: .offset(y: 4)))
                    .help("Jump to latest")
                }
            }
            .animation(JB.snap, value: nearBottom)
        }
    }
}

/// Claude-Code-style approval card for a command the agent wants to run in
/// Full Access mode (shown when "Ask before running commands" is on).
struct CommandPermissionPrompt: View {
    let prompt: PendingPermission
    let respond: (String) -> Void   // "allow" | "deny" | "always"

    // Doc-write tools reach this gate only for files NOT open in the editor
    // (open ones are approved via the in-editor diff banner instead).
    private var isDocEdit: Bool { prompt.tool.contains("document") }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                if isDocEdit {
                    Image(systemName: "pencil.line")
                        .font(.jb(12))
                        .foregroundStyle(JB.warn)
                } else {
                    JBIcon(.coding, size: 14)
                        .foregroundStyle(JB.warn)
                }
                Text(isDocEdit ? "Let the AI edit this file?" : "Run \(prompt.tool) command?")
                    .font(.jb(13, .medium))
                    .foregroundStyle(JB.ink)
            }
            Text(prompt.command)
                .font(.jb(12))
                .foregroundStyle(JB.ink2)
                .lineLimit(4)
                .textSelection(.enabled)
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .jbSurface(.raised, radius: JB.rControl)
            HStack(spacing: 8) {
                Button("Deny") { respond("deny") }
                    .buttonStyle(.jbGhost)
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button("Always allow this session") { respond("always") }
                    .buttonStyle(.jbSecondary)
                Button("Allow") { respond("allow") }
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.jbPrimary)
            }
        }
        .padding(14)
        .frame(width: 380)
        .jbFloating()
        .overlay(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).strokeBorder(JB.warn.opacity(0.45), lineWidth: 1))
    }
}
