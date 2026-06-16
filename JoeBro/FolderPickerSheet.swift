import SwiftUI

/// Native folder binding for the local production app.
/// The selected path is stored on the local backend; file reads/writes happen
/// on this Mac and never require a remote bridge.
struct FolderPickerSheet: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss

    @State private var selectedURL: URL?
    @State private var error: String?
    @State private var binding = false

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Bind Project Folder")
                    .font(.system(size: 14, weight: .semibold))
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button {
                    bind()
                } label: {
                    if binding { ProgressView().controlSize(.small) }
                    else { Text("Bind Folder") }
                }
                .buttonStyle(.glassProminent)
                .tint(Color.accentColor)
                .disabled(binding || selectedURL == nil)
            }
            .padding(14)
            Divider()

            VStack(spacing: 18) {
                Image(systemName: "folder.badge.plus")
                    .font(.system(size: 44, weight: .light))
                    .foregroundStyle(Color.accentColor)
                Text(selectedURL?.path ?? "Choose a local project folder")
                    .font(.system(size: 12, design: selectedURL == nil ? .default : .monospaced))
                    .foregroundStyle(selectedURL == nil ? .secondary : .primary)
                    .multilineTextAlignment(.center)
                    .lineLimit(4)
                    .frame(maxWidth: .infinity)
                Button {
                    chooseFolder()
                } label: {
                    Label("Choose Folder...", systemImage: "folder")
                }
                .buttonStyle(.glassProminent)
                .tint(Color.accentColor)
                Text("JoeBro reads and writes this folder directly with the access you just granted — nothing leaves your Mac.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 360)
                if let error {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.red)
                }
            }
            .padding(28)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(width: 480, height: 360)
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose"
        if panel.runModal() == .OK {
            selectedURL = panel.url
        }
    }

    private func bind() {
        guard let url = selectedURL else { return }
        binding = true
        error = nil
        Task {
            defer { binding = false }
            do {
                store.saveWorkdirBookmark(for: url)
                try await store.bindWorkdir(url.path)
                dismiss()
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}
