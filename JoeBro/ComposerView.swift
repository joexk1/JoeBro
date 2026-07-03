import SwiftUI
import UniformTypeIdentifiers

struct ComposerView: View {
    @Environment(AppStore.self) private var store
    @State private var text = ""
    @State private var showFileImporter = false
    @State private var mic = MicRecorder()
    @State private var showPermissions = false
    @State private var inputHeight: CGFloat = 22

    var body: some View {
        @Bindable var store = store
        VStack(spacing: 8) {
            if !store.pendingAttachments.isEmpty {
                attachmentChips
            }

            // Row 1 — input + model picker (matches the web composer)
            HStack(alignment: .top, spacing: 10) {
                ZStack(alignment: .topLeading) {
                    if text.isEmpty {
                        Text("Message…")
                            .font(.system(size: 14))
                            .foregroundStyle(.tertiary)
                            .padding(.top, 2)
                            .padding(.leading, 5)
                            .allowsHitTesting(false)
                    }
                    ChatInputTextView(text: $text, height: $inputHeight, onSend: send)
                        .frame(height: inputHeight)
                }
                .padding(.vertical, 4)

            }

            // Row 2 — toggles left, mode switches + mic + attach right
            HStack(spacing: 8) {
                LitToggle(icon: "magnifyingglass", isOn: $store.useWeb, help: "Web search (agent mode)")
                    .disabled(!store.agentMode)
                    .opacity(store.agentMode ? 1 : 0.35)
                LitToggle(icon: "apple.terminal", isOn: $store.allowBash, help: "Allow terminal commands (agent mode)")
                    .disabled(!store.agentMode)
                    .opacity(store.agentMode ? 1 : 0.35)

                Button {
                    showPermissions.toggle()
                } label: {
                    Image(systemName: "lock.fill")
                        .font(.system(size: 12))
                        .foregroundStyle(
                            store.permissionMode == "sandbox" ? Color.secondary :
                            store.permissionMode == "readonly" ? Color.orange : Color.red)
                        .contentTransition(.symbolEffect(.replace))
                }
                .buttonStyle(.borderless)
                .help("Agent file access mode")
                .popover(isPresented: $showPermissions, arrowEdge: .top) {
                    VStack(alignment: .leading, spacing: 2) {
                        permissionRow("sandbox", "Bound folder", "folder.badge.gearshape", .secondary)
                        permissionRow("readonly", "Read-only", "eye", .orange)
                        permissionRow("full", "Full access", "exclamationmark.shield", .red)
                        if store.permissionMode == "full" {
                            Divider().padding(.vertical, 3)
                            Toggle(isOn: $store.askBeforeCommands) {
                                Label("Ask before running commands", systemImage: "hand.raised.fill")
                                    .font(.system(size: 11.5))
                            }
                            .toggleStyle(.switch)
                            .controlSize(.mini)
                            .padding(.horizontal, 6)
                            .frame(width: 220)
                            .transition(.opacity.combined(with: .move(edge: .top)))
                        }
                    }
                    .padding(8)
                    .animation(.spring(duration: 0.25), value: store.permissionMode)
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
                        ProgressView().controlSize(.small)
                    } else {
                        Image(systemName: mic.isRecording ? "mic.fill" : "mic")
                            .foregroundStyle(mic.isRecording ? Color.red : Color.secondary)
                            .symbolEffect(.pulse, isActive: mic.isRecording)
                    }
                }
                .buttonStyle(.borderless)
                .help(mic.isRecording ? "Stop recording" : "Voice input")

                Button {
                    showFileImporter = true
                } label: {
                    Image(systemName: "plus")
                        .font(.system(size: 13, weight: .medium))
                }
                .buttonStyle(.borderless)
                .help("Attach files")

                sendButton
            }
            .buttonStyle(.borderless)
            .controlSize(.small)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
        .joeGlassRectInteractive(22)
        .shadow(color: .black.opacity(0.14), radius: 14, y: 6)
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
    }

    private func permissionRow(_ mode: String, _ title: String, _ icon: String, _ color: Color) -> some View {
        Button {
            withAnimation(.spring(duration: 0.25)) { store.permissionMode = mode }
            showPermissions = false
        } label: {
            HStack(spacing: 8) {
                Circle().fill(color).frame(width: 7, height: 7)
                Image(systemName: icon)
                    .font(.system(size: 11))
                    .frame(width: 16)
                Text(title)
                    .font(.system(size: 12))
                Spacer()
                if store.permissionMode == mode {
                    Image(systemName: "checkmark")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(Color.accentColor)
                }
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .frame(width: 160, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private var attachmentChips: some View {
        HStack(spacing: 6) {
            ForEach(store.pendingAttachments, id: \.id) { f in
                HStack(spacing: 5) {
                    Image(systemName: "paperclip")
                        .font(.system(size: 10))
                    Text(f.name)
                        .font(.system(size: 11.5))
                        .lineLimit(1)
                    Button {
                        store.pendingAttachments.removeAll { $0.id == f.id }
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.borderless)
                }
                .padding(.horizontal, 9)
                .padding(.vertical, 5)
                .background(.quaternary.opacity(0.5), in: Capsule())
            }
            Spacer()
        }
    }

    @ViewBuilder
    private var sendButton: some View {
        if store.isStreaming {
            Button {
                store.stopStreaming()
            } label: {
                Image(systemName: "stop.fill")
                    .font(.system(size: 12, weight: .bold))
                    .frame(width: 28, height: 28)
            }
            .buttonStyle(.glassProminent)
            .tint(.red)
            .help("Stop generating")
        } else {
            Button(action: send) {
                Image(systemName: "arrow.up")
                    .font(.system(size: 13, weight: .bold))
                    .frame(width: 28, height: 28)
            }
            .buttonStyle(.glassProminent)
            .tint(Color.accentColor)
            .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && store.pendingAttachments.isEmpty)
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
        .background(.quaternary.opacity(0.4), in: Capsule())
        .fixedSize()
    }

    private func segment(_ label: String, active: Bool, action: @escaping () -> Void) -> some View {
        Button(action: { withAnimation(.spring(duration: 0.28)) { action() } }) {
            Text(label)
                .lineLimit(1)
                .fixedSize()
                .font(.system(size: 11.5, weight: active ? .semibold : .regular))
                .foregroundStyle(active ? .primary : .secondary)
                .padding(.horizontal, 11)
                .padding(.vertical, 4)
                .background {
                    if active {
                        Capsule()
                            .fill(.ultraThinMaterial)
                            .overlay(Capsule().strokeBorder(.white.opacity(0.18), lineWidth: 1))
                            .matchedGeometryEffect(id: "thumb", in: ns)
                    }
                }
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
    }
}

/// Toggle button that visibly lights up — accent icon + tinted capsule when
/// on (Toggle(.button) + borderless was swallowing the on-state entirely).
struct LitToggle: View {
    let icon: String
    @Binding var isOn: Bool
    let help: String

    var body: some View {
        Button {
            withAnimation(.spring(duration: 0.2)) { isOn.toggle() }
        } label: {
            Image(systemName: icon)
                .font(.system(size: 12, weight: isOn ? .semibold : .regular))
                .foregroundStyle(isOn ? Color.accentColor : Color.secondary)
                .frame(width: 26, height: 22)
                .background(
                    Capsule().fill(isOn ? AnyShapeStyle(Color.accentColor.opacity(0.22)) : AnyShapeStyle(.clear))
                )
        }
        .buttonStyle(.borderless)
        .help(help)
    }
}
