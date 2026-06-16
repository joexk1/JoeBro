import SwiftUI

// MARK: - Brain (memory)

struct BrainPanel: View {
    @State private var memories: Loadable<[MemoryEntry]> = .loading
    @State private var search = ""
    @State private var editing: MemoryEntry?
    @State private var selection: Set<String> = []

    var body: some View {
        PanelChrome(title: "Brain", icon: "brain.head.profile") {
            TextField("Search memories", text: $search)
                .textFieldStyle(.plain)
                .font(.system(size: 12))
                .frame(width: 180)
                .padding(.horizontal, 10)
                .padding(.vertical, 5)
                .background(.quaternary.opacity(0.5), in: Capsule())
            Button {
                Task { await load() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
        } content: {
            VStack(spacing: 10) {
                QuickAddBar(placeholder: "Remember something new…", actionIcon: "plus.circle.fill") { add($0) }

                ZStack {
                    switch memories {
                    case .loading:
                        ProgressView()
                    case .failed(let e):
                        ContentUnavailableView("Couldn't load memories", systemImage: "brain", description: Text(e))
                    case .loaded(let list):
                        let shown = filtered(list)
                        if shown.isEmpty {
                            ContentUnavailableView("No memories", systemImage: "brain")
                        } else {
                            VStack(spacing: 0) {
                                if !selection.isEmpty {
                                    selectionBar(total: shown.count, allIDs: shown.map(\.id))
                                }
                                ScrollView {
                                    LazyVStack(spacing: 8) {
                                        ForEach(shown) { m in
                                            memoryCard(m)
                                        }
                                    }
                                    .padding(12)
                                }
                            }
                            .animation(.spring(duration: 0.25), value: selection.isEmpty)
                        }
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .joeGlassRect(16)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
        .task { await load() }
        .sheet(item: Binding(
            get: { editing.map { EditableMemory(entry: $0) } },
            set: { if $0 == nil { editing = nil } }
        )) { wrap in
            EditMemorySheet(entry: wrap.entry) { await load() }
        }
    }

    private func filtered(_ list: [MemoryEntry]) -> [MemoryEntry] {
        guard !search.isEmpty else { return list }
        return list.filter { $0.displayText.localizedCaseInsensitiveContains(search) }
    }

    private func load() async {
        do { memories = .loaded(try await APIClient.shared.memories()) }
        catch { memories = .failed(error.localizedDescription) }
    }

    private func add(_ text: String) {
        guard !text.isEmpty else { return }
        Task {
            try? await APIClient.shared.addMemory(text: text, category: "fact")
            await load()
        }
    }

    private func selectionBar(total: Int, allIDs: [String]) -> some View {
        HStack(spacing: 10) {
            Text("\(selection.count) selected")
                .font(.system(size: 11, weight: .semibold))
            Button(selection.count == total ? "None" : "All") {
                withAnimation(.spring(duration: 0.25)) {
                    selection = selection.count == total ? [] : Set(allIDs)
                }
            }
            .buttonStyle(.borderless)
            .font(.system(size: 11))
            Spacer()
            Button {
                let ids = selection
                selection = []
                Task {
                    for id in ids { try? await APIClient.shared.deleteMemory(id: id) }
                    await load()
                }
            } label: {
                Image(systemName: "trash").font(.system(size: 12))
            }
            .buttonStyle(.borderless)
            .help("Delete selected")
            Button {
                withAnimation(.spring(duration: 0.25)) { selection = [] }
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.borderless)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color.accentColor.opacity(0.10))
        .transition(.move(edge: .top).combined(with: .opacity))
    }

    private func toggleSelect(_ id: String) {
        withAnimation(.spring(duration: 0.2)) {
            if selection.contains(id) { selection.remove(id) } else { selection.insert(id) }
        }
    }

    private func memoryCard(_ m: MemoryEntry) -> some View {
        SkillRowShell(id: m.id, checked: selection.contains(m.id),
                      selecting: !selection.isEmpty,
                      onToggleCheck: { toggleSelect(m.id) }) {
            Image(systemName: m.pinned == true ? "pin.fill" : "sparkle")
                .font(.system(size: 11))
                .foregroundStyle(m.pinned == true ? Color.accentColor : .secondary)
                .padding(.top, 2)
            VStack(alignment: .leading, spacing: 4) {
                Text(m.displayText)
                    .font(.system(size: 13))
                    .textSelection(.enabled)
                HStack(spacing: 8) {
                    if let cat = m.category {
                        Text(cat)
                            .font(.system(size: 10, weight: .medium))
                            .padding(.horizontal, 7)
                            .padding(.vertical, 2)
                            .background(.quaternary.opacity(0.6), in: Capsule())
                    }
                    Text(relativeDate(m.createdAt))
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                }
            }
            Spacer()
        }
        .contentShape(Rectangle())
        .contextMenu {
            Button("Edit…") { editing = m }
            Button(m.pinned == true ? "Unpin" : "Pin") {
                Task { await APIClient.shared.pinMemory(id: m.id); await load() }
            }
            Button("Delete", role: .destructive) {
                Task { try? await APIClient.shared.deleteMemory(id: m.id); await load() }
            }
        }
    }
}

// MARK: - Notes

struct NotesPanel: View {
    @State private var notes: Loadable<[Note]> = .loading
    @State private var showNew = false

    var body: some View {
        PanelChrome(title: "Notes", icon: "note.text") {
            Button {
                Task { await load() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            Button {
                showNew = true
            } label: {
                Image(systemName: "plus")
            }
            .buttonStyle(.borderless)
            .help("New note")
        } content: {
            Group {
                switch notes {
                case .loading:
                    ProgressView().frame(maxHeight: .infinity)
                case .failed(let e):
                    ContentUnavailableView("Couldn't load notes", systemImage: "note.text", description: Text(e))
                case .loaded(let list) where list.isEmpty:
                    ContentUnavailableView("No notes yet", systemImage: "note.text")
                case .loaded(let list):
                    ScrollView {
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 240), spacing: 10)], spacing: 10) {
                            ForEach(list) { note in
                                noteCard(note)
                            }
                        }
                        .padding(12)
                    }
                }
            }
            .joeGlassRect(16)
        }
        .task { await load() }
        .sheet(isPresented: $showNew) {
            NewNoteSheet { await load() }
        }
    }

    private func load() async {
        do { notes = .loaded(try await APIClient.shared.notes()) }
        catch { notes = .failed(error.localizedDescription) }
    }

    private func noteCard(_ note: Note) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                if note.pinned == true {
                    Image(systemName: "pin.fill")
                        .font(.system(size: 9))
                        .foregroundStyle(Color.accentColor)
                }
                Text(note.title?.isEmpty == false ? note.title! : "Untitled")
                    .font(.system(size: 13, weight: .semibold))
                    .lineLimit(1)
                Spacer()
            }
            if let items = note.items, !items.isEmpty {
                ForEach(Array(items.enumerated()), id: \.offset) { i, item in
                    Button {
                        Task {
                            await APIClient.shared.toggleNoteItem(noteID: note.id, index: i)
                            await load()
                        }
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: item.done == true ? "checkmark.circle.fill" : "circle")
                                .font(.system(size: 12))
                                .foregroundStyle(item.done == true ? Color.accentColor : .secondary)
                            Text(item.text ?? "")
                                .font(.system(size: 12))
                                .strikethrough(item.done == true)
                                .foregroundStyle(item.done == true ? .secondary : .primary)
                                .lineLimit(1)
                            Spacer()
                        }
                    }
                    .buttonStyle(.borderless)
                }
            } else if let content = note.content, !content.isEmpty {
                Text(content)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .lineLimit(6)
            }
            if let due = note.dueDate {
                Label(relativeDate(due), systemImage: "alarm")
                    .font(.system(size: 10))
                    .foregroundStyle(.tertiary)
            }
        }
        .panelCard()
        .hoverBounce()
        .contextMenu {
            Button(note.pinned == true ? "Unpin" : "Pin") {
                Task { await APIClient.shared.pinNote(id: note.id); await load() }
            }
            Button("Delete", role: .destructive) {
                Task { await APIClient.shared.archiveNote(id: note.id); await load() }
            }
        }
    }
}

struct NewNoteSheet: View {
    let onSaved: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var content = ""
    @State private var checklist = false
    @State private var items: [String] = [""]

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("New Note").font(.system(size: 14, weight: .semibold))
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Add") {
                    Task {
                        try? await APIClient.shared.createNote(
                            title: title,
                            content: checklist ? "" : content,
                            items: checklist ? items.filter { !$0.isEmpty } : [])
                        await onSaved()
                        dismiss()
                    }
                }
                .buttonStyle(.glassProminent)
                .tint(Color.accentColor)
                .disabled(title.isEmpty && content.isEmpty && items.allSatisfy(\.isEmpty))
            }
            .padding(14)
            Divider()
            Form {
                TextField("Title", text: $title)
                Toggle("Checklist", isOn: $checklist)
                if checklist {
                    ForEach(items.indices, id: \.self) { i in
                        TextField("Item \(i + 1)", text: $items[i])
                            .onSubmit { if i == items.count - 1 { items.append("") } }
                    }
                    Button("Add item") { items.append("") }
                } else {
                    TextField("Content", text: $content, axis: .vertical)
                        .lineLimit(4...10)
                }
            }
            .formStyle(.grouped)
        }
        .frame(width: 420, height: 400)
    }
}


private struct EditableMemory: Identifiable {
    let entry: MemoryEntry
    var id: String { entry.id }
}

struct EditMemorySheet: View {
    let entry: MemoryEntry
    let onSaved: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var category = "fact"
    @State private var error: String?

    private let categories = ["fact", "preference", "person", "project", "event", "skill", "other"]

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Edit Memory").font(.system(size: 14, weight: .semibold))
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Save") { save() }
                    .buttonStyle(.glassProminent)
                    .tint(Color.accentColor)
                    .disabled(text.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            .padding(14)
            Divider()
            Form {
                TextField("Memory", text: $text, axis: .vertical)
                    .lineLimit(3...8)
                Picker("Category", selection: $category) {
                    ForEach(categories, id: \.self) { Text($0.capitalized).tag($0) }
                }
                if let error {
                    Text(error).font(.caption).foregroundStyle(.red)
                }
            }
            .formStyle(.grouped)
        }
        .frame(width: 420, height: 280)
        .onAppear {
            text = entry.displayText
            if let c = entry.category, categories.contains(c) { category = c }
        }
    }

    private func save() {
        Task {
            do {
                try await APIClient.shared.updateMemory(id: entry.id, text: text, category: category)
                await onSaved()
                dismiss()
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}
