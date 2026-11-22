import SwiftUI
import AppKit
import UniformTypeIdentifiers

struct ComposerView: View {
    @Environment(AppStore.self) private var store
    @State private var text = ""
    @State private var showFileImporter = false
    @State private var mic = MicRecorder()
    @State private var showPermissions = false
    @State private var inputHeight: CGFloat = 22
    @State private var focused = false
    @State private var slashIndex = 0
    @State private var slashDismissed = false
    private let slash = ChatSlashSource.shared

    private var hasContent: Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !store.pendingAttachments.isEmpty
    }

    /// Suggestions while the text is a single "/token" at the start.
    private var slashItems: [ChatSlashItem] {
        guard !slashDismissed, text.hasPrefix("/"), !text.contains(where: \.isNewline),
              text.filter({ $0 == " " }).count <= 1 else { return [] }
        if text.contains(" "), ChatSlashSource.options(for: String(text.dropFirst().prefix(while: { $0 != " " })).lowercased()).isEmpty { return [] }
        return slash.matches(String(text.dropFirst()))
    }

    var body: some View {
        @Bindable var store = store
        let items = slashItems
        VStack(spacing: 8) {
            if !store.pendingAttachments.isEmpty {
                attachmentChips
            }

            ZStack(alignment: .topLeading) {
                if text.isEmpty {
                    Text("Message")
                        .font(.jb(14))
                        .foregroundStyle(JB.ink3)
                        .padding(.top, 2)
                        .padding(.leading, 7)   // NSTextView's lineFragmentPadding: caret sits before the text, not on it
                        .allowsHitTesting(false)
                }
                ChatInputTextView(text: $text, height: $inputHeight, focused: $focused,
                                  onKey: handleKey, onSend: send)
                    .frame(height: inputHeight)
            }
            .padding(.vertical, 4)

            HStack(spacing: 6) {
                LitToggle(icon: "magnifyingglass", isOn: $store.useWeb, help: "Web search (agent mode)")
                    .disabled(!store.agentMode)
                    .opacity(store.agentMode ? 1 : 0.35)
                LitToggle(glyph: .coding, isOn: $store.allowBash, help: "Allow terminal commands (agent mode)")
                    .disabled(!store.agentMode)
                    .opacity(store.agentMode ? 1 : 0.35)

                Button {
                    showPermissions.toggle()
                } label: {
                    Image(systemName: "lock")
                        .foregroundStyle(
                            store.permissionMode == "sandbox" ? JB.ink2 :
                            store.permissionMode == "readonly" ? JB.warn : JB.danger)
                }
                .buttonStyle(JBIconButtonStyle(size: 26))
                .help("Agent file access mode")
                .popover(isPresented: $showPermissions, arrowEdge: .top) {
                    VStack(alignment: .leading, spacing: 2) {
                        permissionRow("sandbox", "Bound folder", "folder", JB.ink2)
                        permissionRow("readonly", "Read-only", "eye", JB.warn)
                        permissionRow("full", "Full access", "exclamationmark.triangle", JB.danger)
                        if store.permissionMode == "full" {
                            Rectangle().fill(JB.line).frame(height: 1).padding(.vertical, 4)
                            Toggle(isOn: $store.askBeforeCommands) {
                                Text("Ask before running commands")
                                    .font(.jb(11.5))
                                    .foregroundStyle(JB.ink)
                            }
                            .toggleStyle(JBSwitchStyle())
                            .controlSize(.mini)
                            .padding(.horizontal, 6)
                            .frame(width: 220)
                            .transition(.opacity.combined(with: .move(edge: .top)))
                        }
                    }
                    .padding(8)
                    .animation(JB.snap, value: store.permissionMode)
                    // Force the popover body to re-evaluate the instant the mode
                    // changes (popover content otherwise snapshots on present).
                    .id(store.permissionMode)
                }

                Spacer()

                SlideSwitch(left: "Think", right: "Fast", isLeft: $store.thinkingOn)
                    .help("Extended thinking vs fast replies")

                SlideSwitch(left: "Agent", right: "Chat", isLeft: $store.agentMode)

                ModelPickerMenu()

                Button {
                    mic.toggle { transcript in
                        text = text.isEmpty ? transcript : text + " " + transcript
                    }
                } label: {
                    if mic.isTranscribing {
                        JBSpinner()
                    } else {
                        Image(systemName: mic.isRecording ? "mic.fill" : "mic")
                            .foregroundStyle(mic.isRecording ? JB.danger : JB.ink2)
                            .symbolEffect(.pulse, isActive: mic.isRecording)
                    }
                }
                .buttonStyle(JBIconButtonStyle(size: 26))
                .help(mic.isRecording ? "Stop recording" : "Voice input")

                Button {
                    showFileImporter = true
                } label: {
                    Image(systemName: "paperclip")
                }
                .buttonStyle(JBIconButtonStyle(size: 26))
                .help("Attach files")

                sendButton
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
        .background {
            RoundedRectangle(cornerRadius: JB.rPanel, style: .continuous).fill(JB.readFill)
                .overlay(RoundedRectangle(cornerRadius: JB.rPanel, style: .continuous)
                    .strokeBorder(focused ? JB.accent.opacity(0.7) : JB.line, lineWidth: 1))
        }
        .animation(JB.fade, value: focused)
        .overlay(alignment: .top) {
            if !items.isEmpty {
                ChatSlashPopover(items: items, selected: min(slashIndex, items.count - 1),
                                 onHover: { slashIndex = $0 }, onPick: accept)
                    .alignmentGuide(.top) { $0[.bottom] + 8 }
                    .transition(.opacity.combined(with: .offset(y: 2)))
            }
        }
        .animation(JB.snap, value: items.isEmpty)
        .zIndex(1)
        .fileImporter(isPresented: $showFileImporter,
                      allowedContentTypes: [.item],
                      allowsMultipleSelection: true) { result in
            if case .success(let urls) = result {
                for url in urls {
                    let scoped = url.startAccessingSecurityScopedResource()
                    store.attach(url)
                    if scoped { url.stopAccessingSecurityScopedResource() }
                }
            }
        }
        .onDrop(of: [.fileURL, .image], isTargeted: nil) { providers in
            store.attachProviders(providers)
        }
        .onChange(of: store.agentMode) {
            if !store.agentMode {
                store.allowBash = false
            }
            store.persistChatMode()
        }
        .onChange(of: text) {
            slashIndex = 0
            if !text.hasPrefix("/") { slashDismissed = false }
            if text.hasPrefix("/") { slash.refresh() }
        }
        .onReceive(NotificationCenter.default.publisher(for: .jbPickBackdropPhoto)) { _ in
            DispatchQueue.main.async { pickBackdropPhoto() }
        }
        .onReceive(NotificationCenter.default.publisher(for: .chatFillComposer)) { n in
            if let s = n.object as? String { text = s }
        }
    }

    // MARK: Keys and slash

    private func handleKey(_ key: ChatKey) -> Bool {
        let items = slashItems
        if !items.isEmpty {
            let i = min(slashIndex, items.count - 1)
            switch key {
            case .up: slashIndex = (i - 1 + items.count) % items.count; return true
            case .down: slashIndex = (i + 1) % items.count; return true
            case .enter, .tab: accept(items[i]); return true
            case .escape: slashDismissed = true; return true
            }
        }
        switch key {
        case .escape:
            guard store.isStreaming else { return false }
            store.stopStreaming()
            return true
        case .up:
            guard text.isEmpty, !store.isStreaming,
                  let last = store.messages.last(where: { $0.isUser }) else { return false }
            store.beginEdit(last)
            return true
        default:
            return false
        }
    }

    private func pickBackdropPhoto() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.image]
        panel.canChooseDirectories = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        store.setWallpaper(from: url)
        JBAppearance.shared.backdrop = .photo
        JBAppearance.shared.palette = .auto
    }

    private func accept(_ item: ChatSlashItem) {
        text = store.runChatSlash(item)
        slashIndex = 0
        slashDismissed = false
    }

    private func permissionRow(_ mode: String, _ title: String, _ icon: String, _ color: Color) -> some View {
        Button {
            withAnimation(JB.snap) { store.permissionMode = mode }
            showPermissions = false
        } label: {
            HStack(spacing: 8) {
                Circle().fill(color).frame(width: 7, height: 7)
                Image(systemName: icon)
                    .font(.jb(11))
                    .foregroundStyle(JB.ink2)
                    .frame(width: 16)
                Text(title)
                    .font(.jb(12))
                    .foregroundStyle(JB.ink)
                Spacer()
                if store.permissionMode == mode {
                    Image(systemName: "checkmark")
                        .font(.jb(10, .bold))
                        .foregroundStyle(JB.accent)
                }
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .frame(width: 160, alignment: .leading)
            .jbHoverRow(selected: false)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private var attachmentChips: some View {
        HStack(spacing: 6) {
            ForEach(store.pendingAttachments, id: \.id) { f in
                HStack(spacing: 5) {
                    Image(systemName: "paperclip")
                        .font(.jb(10))
                        .foregroundStyle(JB.ink3)
                    Text(f.name)
                        .font(.jb(11))
                        .foregroundStyle(JB.ink2)
                        .lineLimit(1)
                    Button {
                        store.pendingAttachments.removeAll { $0.id == f.id }
                    } label: {
                        Image(systemName: "xmark")
                            .font(.jb(8, .semibold))
                            .foregroundStyle(JB.ink3)
                    }
                    .buttonStyle(.plain)
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(RoundedRectangle(cornerRadius: 5, style: .continuous).fill(JB.hover))
                .overlay(RoundedRectangle(cornerRadius: 5, style: .continuous).strokeBorder(JB.line, lineWidth: 1))
                .transition(.opacity)
            }
            Spacer()
        }
        .animation(JB.snap, value: store.pendingAttachments.count)
    }

    @ViewBuilder
    private var sendButton: some View {
        if store.isStreaming {
            Button {
                store.stopStreaming()
            } label: {
                Image(systemName: "stop.fill")
                    .foregroundStyle(JB.danger)
            }
            .buttonStyle(JBIconButtonStyle(size: 28))
            .help("Stop (esc)")
        } else {
            Button(action: send) {
                Image(systemName: "arrow.up")
                    .fontWeight(.semibold)
            }
            .buttonStyle(JBIconButtonStyle(size: 28, active: hasContent))
            .disabled(!hasContent)
            .keyboardShortcut(.return, modifiers: .command)
            .help("Send (⏎)")
        }
    }

    private func send() {
        guard !store.isStreaming else { return }
        let t = text
        text = ""
        store.send(t)
    }
}

/// Sliding two-option switch — thumb glides between labels.
struct SlideSwitch: View {
    let left: String
    let right: String
    @Binding var isLeft: Bool
    @Namespace private var ns

    var body: some View {
        HStack(spacing: 0) {
            segment(left, active: isLeft) { isLeft = true }
            segment(right, active: !isLeft) { isLeft = false }
        }
        .padding(2)
        .background(RoundedRectangle(cornerRadius: JB.rControl, style: .continuous).fill(JB.hover.opacity(0.6)))
        .fixedSize()
    }

    private func segment(_ label: String, active: Bool, action: @escaping () -> Void) -> some View {
        Button(action: { withAnimation(JB.snap) { action() } }) {
            Text(label)
                .lineLimit(1)
                .fixedSize()
                .font(.jb(11, active ? .medium : .regular))
                .foregroundStyle(active ? JB.ink : JB.ink3)
                .padding(.horizontal, 9)
                .padding(.vertical, 3.5)
                .background {
                    if active {
                        RoundedRectangle(cornerRadius: JB.rControl - 2, style: .continuous)
                            .fill(JB.raised)
                            .overlay(RoundedRectangle(cornerRadius: JB.rControl - 2, style: .continuous).strokeBorder(JB.line, lineWidth: 1))
                            .matchedGeometryEffect(id: "thumb", in: ns)
                    }
                }
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

/// Toggle button that lights up in the accent when on.
struct LitToggle: View {
    var icon: String? = nil
    var glyph: JBGlyph? = nil
    @Binding var isOn: Bool
    let help: String

    var body: some View {
        Button {
            withAnimation(JB.snap) { isOn.toggle() }
        } label: {
            Group {
                if let glyph { JBIcon(glyph, size: 14) } else if let icon { Image(systemName: icon) }
            }
            .foregroundStyle(isOn ? JB.accent : JB.ink2)
        }
        .buttonStyle(JBIconButtonStyle(size: 26, active: isOn))
        .help(help)
    }
}
