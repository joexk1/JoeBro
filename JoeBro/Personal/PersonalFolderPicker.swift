// PERSONAL add-on: bind a chat to a folder on this Mac or on the Pi (Nextcloud).
#if PERSONAL
import SwiftUI

/// Bind a chat to a folder on either THIS MAC or the PI (where Nextcloud lives).
/// The backend runs on the Pi; a Mac-bound chat routes the agent's file tools to
/// this Mac's bridge, a Pi-bound chat runs them on the Pi itself.
struct PersonalFolderPicker: View {
    @Environment(AppStore.self) private var store
    @Environment(\.dismiss) private var dismiss

    enum Host: String { case pi, mac }
    @State private var host: Host = .pi
    static let nextcloudRoot = "/mnt/nextcloud/data/joe/files"   // Pi Nextcloud files

    // This Mac
    @State private var macURL: URL?
    // Pi (remote browse)
    @State private var piPath = ""
    @State private var piEntries: [FSEntry] = []
    @State private var loading = false

    @State private var error: String?
    @State private var binding = false

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Bind Project Folder").font(.system(size: 14, weight: .semibold))
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button { bind() } label: {
                    if binding { ProgressView().controlSize(.small) } else { Text("Bind Folder") }
                }
                .buttonStyle(.glassProminent)
                .tint(Color.accentColor)
                .disabled(binding || !canBind)
            }
            .padding(14)
            Divider()

            Picker("", selection: $host) {
                Text("Pi · Nextcloud").tag(Host.pi)
                Text("This Mac").tag(Host.mac)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(.horizontal, 14)
            .padding(.top, 12)

            Group {
                if host == .mac { macPane } else { piPane }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)

            if let error {
                Text(error).font(.caption).foregroundStyle(.red).padding(.bottom, 8)
            }
        }
        .frame(width: 520, height: 440)
        .task(id: host) { if host == .pi && piEntries.isEmpty { await loadPi(Self.nextcloudRoot) } }
    }

    private var canBind: Bool {
        host == .mac ? macURL != nil : !piPath.isEmpty
    }

    // MARK: This Mac
    private var macPane: some View {
        VStack(spacing: 16) {
            Image(systemName: "laptopcomputer").font(.system(size: 40, weight: .light))
                .foregroundStyle(Color.accentColor)
            Text(macURL?.path ?? "Choose a folder on this Mac")
                .font(.system(size: 12, design: macURL == nil ? .default : .monospaced))
                .foregroundStyle(macURL == nil ? .secondary : .primary)
                .multilineTextAlignment(.center).lineLimit(4)
            Button { chooseMacFolder() } label: { Label("Choose Folder…", systemImage: "folder") }
                .buttonStyle(.glassProminent).tint(Color.accentColor)
            Text("The agent works on this Mac through its bridge. The Mac must be on Tailscale and running its bridge agent.")
                .font(.caption).foregroundStyle(.secondary)
                .multilineTextAlignment(.center).frame(maxWidth: 380)
        }
        .padding(24)
    }

    // MARK: Pi browser
    private var piPane: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Button { Task { await loadPi(parentOf(piPath)) } } label: { Image(systemName: "chevron.up") }
                    .buttonStyle(.borderless).disabled(piPath.isEmpty || piPath == "/")
                Text(piPath.isEmpty ? "~" : piPath)
                    .font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                if loading { ProgressView().controlSize(.small) }
            }
            .padding(.horizontal, 14).padding(.vertical, 8)
            Divider().opacity(0.4)
            ScrollView {
                LazyVStack(spacing: 2) {
                    ForEach(piEntries) { e in
                        Button { Task { await loadPi(join(piPath, e.name)) } } label: {
                            HStack(spacing: 8) {
                                Image(systemName: "folder.fill").foregroundStyle(Color.accentColor.opacity(0.8))
                                Text(e.name).font(.system(size: 12.5)); Spacer()
                                Image(systemName: "chevron.right").font(.system(size: 9)).foregroundStyle(.tertiary)
                            }
                            .padding(.horizontal, 12).padding(.vertical, 7)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                    if piEntries.isEmpty && !loading {
                        Text("No sub-folders").font(.caption).foregroundStyle(.tertiary).padding(.top, 20)
                    }
                }
                .padding(8)
            }
            Text("Binds this folder on the Pi (where Nextcloud is mounted). The agent works on the Pi.")
                .font(.caption).foregroundStyle(.secondary).padding(8)
        }
    }

    private func chooseMacFolder() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose"
        if panel.runModal() == .OK { macURL = panel.url }
    }

    private func loadPi(_ path: String) async {
        loading = true; error = nil
        defer { loading = false }
        do {
            let r = try await APIClient.shared.fsBrowse(path: path, device: nil)  // nil = Pi host
            piPath = r.path
            piEntries = r.entries.filter(\.isDir).sorted { $0.name.lowercased() < $1.name.lowercased() }
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func parentOf(_ p: String) -> String {
        guard !p.isEmpty, p != "/" else { return p }
        let parent = (p as NSString).deletingLastPathComponent
        return parent.isEmpty ? "/" : parent
    }

    private func join(_ base: String, _ name: String) -> String {
        base.isEmpty ? name : (base as NSString).appendingPathComponent(name)
    }

    private func bind() {
        binding = true; error = nil
        Task {
            defer { binding = false }
            do {
                if host == .mac {
                    guard let url = macURL else { return }
                    store.saveWorkdirBookmark(for: url)            // local access for the app's own file views
                    try await store.bindWorkdir(url.path, device: "macbook")
                } else {
                    try await store.bindWorkdir(piPath, device: nil)   // Pi-local (host_id empty)
                }
                dismiss()
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}
#endif
