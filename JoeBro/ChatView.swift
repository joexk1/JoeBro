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

                ZStack(alignment: .bottom) {
                    if store.messages.isEmpty {
                        emptyState
                    } else {
                        messageList
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)

                ComposerView()
            }
            .onDrop(of: [.fileURL, .image], isTargeted: $dropTargeted) { providers in
                store.attachProviders(providers)
            }
            .overlay {
                if dropTargeted {
                    VStack(spacing: 10) {
                        Image(systemName: "paperclip.badge.ellipsis")
                            .font(.system(size: 34, weight: .light))
                            .foregroundStyle(Color.accentColor)
                        Text("Drop to attach")
                            .font(.system(size: 14, weight: .semibold, design: .rounded))
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(.ultraThinMaterial)
                    .overlay(
                        RoundedRectangle(cornerRadius: 18)
                            .strokeBorder(Color.accentColor.opacity(0.7), style: StrokeStyle(lineWidth: 2, dash: [8, 6]))
                            .padding(6)
                    )
                    .clipShape(RoundedRectangle(cornerRadius: 18))
                    .transition(.opacity)
                    .allowsHitTesting(false)
                }
            }
            .animation(.spring(duration: 0.25), value: dropTargeted)
            .overlay(alignment: .bottom) {
                if let p = store.pendingPermission {
                    CommandPermissionPrompt(prompt: p) { store.respondPermission($0) }
                        .padding(.bottom, 90)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                }
            }
            .animation(.spring(duration: 0.28), value: store.pendingPermission?.id)

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
        .onChange(of: store.selectedSessionID) { visibleCount = 60 }
        .sheet(isPresented: $renaming) {
            VStack(spacing: 14) {
                Text("Rename Chat")
                    .font(.system(size: 14, weight: .semibold))
                TextField("Chat name", text: $renameText)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 280)
                    .onSubmit(doRename)
                HStack {
                    Button("Cancel") { renaming = false }
                        .keyboardShortcut(.cancelAction)
                    Button("Rename", action: doRename)
                        .buttonStyle(.glassProminent)
                        .tint(Color.accentColor)
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
                    .font(.system(size: 13, weight: .medium))
            }
            .buttonStyle(.borderless)
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
                        .font(.system(size: 14, weight: .semibold, design: .rounded))
                        .lineLimit(1)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 8, weight: .bold))
                        .foregroundStyle(.secondary)
                }
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .disabled(store.selectedSessionID == nil)

            if !store.messages.isEmpty {
                Text("\(store.messages.count)")
                    .font(.system(size: 11, weight: .medium, design: .rounded))
                    .foregroundStyle(.tertiary)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background(.quaternary.opacity(0.4), in: Capsule())
                    .help("Messages in this chat")
            }

            if store.compacting || exportingPDF {
                ProgressView().controlSize(.small)
            }

            Spacer()

            if !store.openDocs.isEmpty && !store.editorVisible {
                Button {
                    store.editorVisible = true
                } label: {
                    Label("\(store.openDocs.count)", systemImage: "doc.text")
                        .font(.system(size: 12, weight: .medium))
                }
                .buttonStyle(.borderless)
                .help("Show editor")
            }

            if let err = store.loadError {
                HStack(spacing: 6) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 10))
                        .foregroundStyle(.orange)
                    Text(err)
                        .font(.system(size: 11))
                        .lineLimit(1)
                        .frame(maxWidth: 320)
                    Button {
                        store.loadError = nil
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.borderless)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 5)
                .glassEffect(.regular.tint(.orange.opacity(0.15)), in: .capsule)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .joeGlassCapsule()
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
        VStack(spacing: 14) {
            Spacer()
            Image(systemName: "sparkles")
                .font(.system(size: 42, weight: .light))
                .foregroundStyle(Color.accentColor.gradient)
            Text("What can I help with?")
                .font(.system(.title2, design: .rounded, weight: .semibold))
            if let model = store.selectedModel {
                HStack(spacing: 6) {
                    ProviderLogoView(model: model.modelID, size: 12)
                        .foregroundStyle(.secondary)
                    Text(model.displayWithTag)
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 5)
                .joeGlassCapsule()
            }
            Spacer()
        }
    }

    private var messageList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                // Eager VStack: LazyVStack + defaultScrollAnchor(.bottom)
                // left the viewport empty until first scroll (rows above the
                // anchor never materialized). Equatable rows keep it cheap.
                VStack(spacing: 12) {
                    // Big chats render only the recent tail — eager-rendering
                    // hundreds of markdown bubbles at once was the open-chat lag.
                    if store.messages.count > visibleCount {
                        Button {
                            visibleCount += 100
                        } label: {
                            Label("Show earlier messages (\(store.messages.count - visibleCount) more)",
                                  systemImage: "arrow.up.circle")
                                .font(.system(size: 12, weight: .medium))
                        }
                        .buttonStyle(.borderless)
                        .padding(.top, 6)
                    }
                    ForEach(store.messages.suffix(visibleCount)) { message in
                        MessageBubbleView(message: message)
                            .equatable()
                            .id(message.id)
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .padding(.horizontal, 18)
                .padding(.top, 8)
                .frame(maxWidth: 860)
                .frame(maxWidth: .infinity)
            }
            .scrollIndicators(.never)
            .defaultScrollAnchor(.bottom)
            // Hard clip + edge fade so message cards never visually collide
            // with the glass composer below / header above.
            .clipShape(RoundedRectangle(cornerRadius: 16))
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
                        withAnimation(.spring(duration: 0.35)) {
                            proxy.scrollTo("bottom", anchor: .bottom)
                        }
                    } label: {
                        Image(systemName: "arrow.down")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(.primary)
                            .frame(width: 34, height: 34)
                            .contentShape(Circle())
                    }
                    .buttonStyle(.plain)
                    .background(.ultraThinMaterial, in: Circle())
                    .overlay(Circle().strokeBorder(.white.opacity(0.18), lineWidth: 1))
                    .shadow(color: .black.opacity(0.2), radius: 10, y: 4)
                    .padding(.bottom, 12)
                    .transition(.scale.combined(with: .opacity))
                    .help("Jump to latest")
                }
            }
            .animation(.spring(duration: 0.25), value: nearBottom)
        }
    }
}

/// Claude-Code-style approval card for a command the agent wants to run in
/// Full Access mode (shown when "Ask before running commands" is on).
struct CommandPermissionPrompt: View {
    let prompt: PendingPermission
    let respond: (String) -> Void   // "allow" | "deny" | "always"

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 7) {
                Image(systemName: "terminal.fill")
                    .font(.system(size: 12))
                    .foregroundStyle(Color.orange)
                Text("Run \(prompt.tool) command?")
                    .font(.system(size: 13, weight: .semibold))
            }
            Text(prompt.command)
                .font(.system(size: 12, design: .monospaced))
                .foregroundStyle(.secondary)
                .lineLimit(4)
                .textSelection(.enabled)
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.black.opacity(0.25), in: RoundedRectangle(cornerRadius: 8))
            HStack(spacing: 8) {
                Button("Deny") { respond("deny") }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button("Always allow this session") { respond("always") }
                Button("Allow") { respond("allow") }
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
            }
            .font(.system(size: 12))
        }
        .padding(14)
        .frame(width: 380)
        .joeGlassRect(14)   // real Liquid Glass, consistent with the rest of the app
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.orange.opacity(0.45), lineWidth: 1))
        .shadow(color: .black.opacity(0.3), radius: 20, y: 8)
    }
}
