import SwiftUI

/// Muse-style memory files the phone agent reads: plain Markdown, one file each.
/// Separate from the Brain by design.
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
        HStack(spacing: 10) {
            VStack(spacing: 6) {
                List(files, id: \.self, selection: $selected) { f in
                    Label(f, systemImage: "doc.text").font(.system(size: 12))
                }
                .scrollContentBackground(.hidden)
                HStack {
                    Button { showNew = true } label: { Label("New file", systemImage: "plus") }
                    Spacer()
                    Button(role: .destructive) { delete() } label: { Image(systemName: "trash") }
                        .disabled(selected == nil || selected == "about-me")
                }
                .buttonStyle(.borderless)
                .font(.system(size: 11))
                .padding(.horizontal, 8)
                .padding(.bottom, 6)
            }
            .frame(width: 170)
            .joeGlassRect(16)

            VStack(spacing: 6) {
                if selected == nil {
                    ContentUnavailableView("Pick a file", systemImage: "doc.text")
                } else {
                    TextEditor(text: $text)
                        .font(.system(size: 12.5, design: .monospaced))
                        .scrollContentBackground(.hidden)
                        .padding(8)
                        .onChange(of: text) { scheduleSave() }
                }
                if let error {
                    Text(error).font(.system(size: 11)).foregroundStyle(.red)
                }
                Text("These are plain Markdown files in JoeBro's data folder. The phone agent reads about-me every time and the other files only when relevant.")
                    .font(.system(size: 10.5))
                    .foregroundStyle(.tertiary)
                    .padding([.horizontal, .bottom], 10)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .joeGlassRect(16)
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
