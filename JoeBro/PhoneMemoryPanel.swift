import SwiftUI

/// Plain Markdown memory files the phone agent reads, shown as the "Files" view of Memory.
struct PhoneMemoryView: View {
    @State private var files: [String] = []
    @State private var selected: String?
    @State private var text = ""
    @State private var loadedText = ""
    @State private var error: String?
    @State private var newName = ""
    @State private var showNew = false
    @State private var saveTask: Task<Void, Never>?

    var body: some View {
        HStack(spacing: JB.s3) {
            VStack(spacing: 0) {
                ScrollView {
                    LazyVStack(spacing: 2) {
                        ForEach(files, id: \.self) { f in
                            Button { selected = f } label: {
                                Text(f).font(.jbMono(12)).foregroundStyle(selected == f ? JB.ink : JB.ink2)
                                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 10).padding(.vertical, 6)
                                    .jbHoverRow(selected: selected == f)
                            }.buttonStyle(.plain)
                        }
                    }.padding(6)
                }
                HStack {
                    ExtrasIconButton(symbol: "plus", help: "New file") { showNew = true }
                    Spacer()
                    ExtrasIconButton(symbol: "trash", help: "Delete file") { delete() }.disabled(selected == nil || selected == "about-me")
                }.padding(6)
            }
            .frame(width: 190).jbSurface(.panel)

            VStack(spacing: 6) {
                if selected == nil {
                    JBEmptyState(art: .files, text: "Pick a file")
                } else {
                    TextEditor(text: $text)
                        .font(.jbMono(12.5)).foregroundStyle(JB.ink)
                        .scrollContentBackground(.hidden)
                        .padding(8)
                        .onChange(of: text) { scheduleSave() }
                }
                if let error { Text(error).font(.jbMono(11)).foregroundStyle(JB.danger).padding([.horizontal, .bottom], 10) }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .jbSurface(.panel)
        }
        .task { await reload() }
        .onChange(of: selected) { old, new in
            saveTask?.cancel()
            if let old, text != loadedText { let t = text; Task { try? await APIClient.shared.museWrite(old, content: t) } }
            Task { await open(new) }
        }
        .onDisappear {
            saveTask?.cancel()
            if let s = selected, text != loadedText { let t = text; Task { try? await APIClient.shared.museWrite(s, content: t) } }
        }
        .alert("New memory file", isPresented: $showNew) {
            TextField("Name", text: $newName)
            Button("Create") { create() }
            Button("Cancel", role: .cancel) { newName = "" }
        }
    }

    private func reload() async {
        do {
            files = try await APIClient.shared.museFiles()
            if selected == nil { selected = files.first(where: { $0 == "about-me" }) ?? files.first }
        } catch { self.error = error.localizedDescription }
    }

    private func open(_ name: String?) async {
        guard let name else { text = ""; loadedText = ""; return }
        let c = (try? await APIClient.shared.museRead(name)) ?? ""
        loadedText = c
        text = c
    }

    /// Debounced autosave (800ms after the last keystroke).
    private func scheduleSave() {
        guard let name = selected, text != loadedText else { return }
        saveTask?.cancel()
        let t = text
        saveTask = Task {
            try? await Task.sleep(for: .milliseconds(800))
            guard !Task.isCancelled else { return }
            do {
                try await APIClient.shared.museWrite(name, content: t)
                loadedText = t
                error = nil
            } catch { self.error = error.localizedDescription }
        }
    }

    private func create() {
        let n = newName.trimmingCharacters(in: .whitespaces)
        newName = ""
        guard !n.isEmpty else { return }
        Task {
            do {
                try await APIClient.shared.museCreate(n)
                await reload()
                selected = files.first { $0 == n || $0 == n + ".md" } ?? selected
            } catch { self.error = error.localizedDescription }
        }
    }

    private func delete() {
        guard let s = selected else { return }
        selected = nil
        Task {
            try? await APIClient.shared.museDelete(s)
            await reload()
        }
    }
}
