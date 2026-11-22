import SwiftUI

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
            .buttonStyle(.jbIcon)
            Button {
                showNew = true
            } label: {
                Image(systemName: "plus")
            }
            .buttonStyle(.jbIcon)
            .help("New note")
        } content: {
            Group {
                switch notes {
                case .loading:
                    PanelLoading()
                case .failed(let e):
                    PanelFailure(title: "Couldn't load notes", detail: e)
                case .loaded(let list) where list.isEmpty:
                    PanelEmpty(art: .files, text: "No notes yet")
                case .loaded(let list):
                    ScrollView {
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 240), spacing: 10)], spacing: 10) {
                            ForEach(list) { note in
                                noteCard(note)
                            }
                        }
                        .padding(12)
                        .animation(JB.smooth, value: list.map(\.id))
                    }
                }
            }
            .jbSurface(.panel, radius: JB.rPanel)
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
                        .font(.jb(10))
                        .foregroundStyle(JB.accent)
                }
                Text(note.title?.isEmpty == false ? note.title! : "Untitled")
                    .font(.jb(13, .semibold))
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
                                .font(.jb(12))
                                .foregroundStyle(item.done == true ? JB.accent : JB.ink3)
                            Text(item.text ?? "")
                                .font(.jb(12))
                                .strikethrough(item.done == true)
                                .foregroundStyle(item.done == true ? JB.ink3 : JB.ink)
                                .lineLimit(1)
                            Spacer()
                        }
                    }
                    .buttonStyle(JBPressStyle())
                }
            } else if let content = note.content, !content.isEmpty {
                Text(content)
                    .font(.jb(12))
                    .foregroundStyle(JB.ink2)
                    .lineLimit(6)
            }
            if let due = note.dueDate {
                Label(relativeDate(due), systemImage: "alarm")
                    .font(.jbMono(10.5))
                    .foregroundStyle(JB.ink3)
            }
        }
        .panelCard()
        .jbHoverActions {
            Button { Task { await APIClient.shared.pinNote(id: note.id); await load() } } label: {
                Image(systemName: note.pinned == true ? "pin.slash" : "pin")
            }
            .buttonStyle(.jbIcon).help(note.pinned == true ? "Unpin" : "Pin")
            Button { Task { await APIClient.shared.archiveNote(id: note.id); await load() } } label: {
                Image(systemName: "trash")
            }
            .buttonStyle(.jbIcon).help("Delete")
        }
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
                Text("New note").font(.jb(14, .semibold))
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction).buttonStyle(.jbGhost)
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
                .buttonStyle(.jbPrimary)
                .disabled(title.isEmpty && content.isEmpty && items.allSatisfy(\.isEmpty))
            }
            .padding(14)
            Rectangle().fill(JB.line).frame(height: 1)
            Form {
                TextField("Title", text: $title)
                Toggle("Checklist", isOn: $checklist).jbToggle()
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


