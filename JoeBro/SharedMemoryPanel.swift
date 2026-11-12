import SwiftUI

/// The one memory screen: what you told JoeBro (You), what every agent learned about a
/// project (Projects) and what applies everywhere (Global), each fact with who wrote it.
struct SharedMemoryPanel: View {
    private enum Scope: String, CaseIterable { case all = "All", you = "You", projects = "Projects", global = "Global" }
    @State private var scope = Scope.all
    @State private var projects: [MemProject] = []
    @State private var project = "__all"
    @State private var facts: [MemFact] = []
    @State private var search = ""
    @State private var searchOpen = false
    @State private var showArchived = false
    @State private var selectedID: String?
    @State private var adding = false
    @State private var note: String?

    private var shown: [MemFact] {
        facts.filter {
            switch scope {
            case .all: true
            case .you: $0.scope == "user"
            case .global: $0.scope == "global"
            case .projects: $0.scope == "project"
            }
        }.sorted { ($0.isPinned ? 1 : 0, $0.strengthValue) > ($1.isPinned ? 1 : 0, $1.strengthValue) }
    }

    var body: some View {
        PanelChrome(title: "Memory", icon: "brain.head.profile") {
            PanelSearchField(text: $search, open: $searchOpen, placeholder: "Search memory")
            Button { adding = true } label: { Image(systemName: "plus") }.buttonStyle(.borderless).help("Add to memory")
            Button { Task { await load() } } label: { Image(systemName: "arrow.clockwise") }.buttonStyle(.borderless).help("Refresh")
        } content: {
            VStack(spacing: 10) {
                HStack(spacing: 12) {
                    Picker("", selection: $scope) { ForEach(Scope.allCases, id: \.self) { Text($0.rawValue).tag($0) } }
                        .pickerStyle(.segmented).labelsHidden().frame(maxWidth: 360)
                    Picker("Project", selection: $project) {
                        Text("Every project").tag("__all")
                        ForEach(projects.filter { $0.key != "global" && $0.key != "user" }) { Text($0.label).tag($0.key) }
                    }.frame(maxWidth: 220)
                    Toggle("Archived", isOn: $showArchived).toggleStyle(.switch).tint(.green).controlSize(.small)
                }
                if let note { Text(note).font(.system(size: 11.5)).foregroundStyle(.secondary) }
                HStack(spacing: 12) {
                    ScrollView {
                        LazyVStack(spacing: 8) {
                            if shown.isEmpty { ContentUnavailableView("Nothing here yet", systemImage: "brain", description: Text("Agents add facts as they work. Add your own with +.")).padding(.top, 40) }
                            ForEach(shown) { row($0) }
                        }.padding(12)
                    }.frame(width: 430).frame(maxHeight: .infinity).joeGlassRect(16)
                    detail.frame(maxWidth: .infinity, maxHeight: .infinity).joeGlassRect(16)
                }
            }
        }
        .task { await load() }
        .onChange(of: search) { Task { await load() } }
        .onChange(of: project) { Task { await load() } }
        .onChange(of: showArchived) { Task { await load() } }
        .sheet(isPresented: $adding) { MemoryAddSheet(projects: projects, current: project) { Task { await load() } } }
    }

    private func row(_ f: MemFact) -> some View {
        Button { selectedID = f.id } label: {
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 5) {
                    Image(systemName: f.kindSymbol).font(.system(size: 10)).foregroundStyle(Color.accentColor)
                    Text(f.kindLabel + ((f.topic ?? "").isEmpty ? "" : " · " + (f.topic ?? ""))).font(.system(size: 10.5, weight: .semibold)).foregroundStyle(.secondary)
                    Spacer()
                    if f.isPinned { Image(systemName: "pin.fill").font(.system(size: 9)).foregroundStyle(.orange) }
                }
                Text(f.text).font(.system(size: 12.5)).multilineTextAlignment(.leading).lineLimit(4)
                HStack(spacing: 6) {
                    ProgressView(value: f.strengthValue).frame(width: 46).controlSize(.mini)
                    Text(f.provenanceLine).lineLimit(1)
                    Spacer()
                    Text(relativeDateISO(f.updatedAt))
                }.font(.system(size: 10)).foregroundStyle(.secondary)
            }
            .padding(9).frame(maxWidth: .infinity, alignment: .leading)
            .background(selectedID == f.id ? AnyShapeStyle(.quaternary.opacity(0.6)) : AnyShapeStyle(.clear), in: RoundedRectangle(cornerRadius: 9))
            .opacity(f.isArchived ? 0.55 : 1)
        }.buttonStyle(.plain)
    }

    @ViewBuilder private var detail: some View {
        if let f = facts.first(where: { $0.id == selectedID }) {
            MemoryDetail(fact: f) { Task { await load() } }.id(f.id)
        } else {
            ContentUnavailableView("Pick a memory", systemImage: "brain", description: Text("See where it came from, edit it, pin it or archive it."))
        }
    }

    private func load() async {
        do {
            if projects.isEmpty { projects = try await APIClient.shared.memoryProjects() }
            facts = try await APIClient.shared.memoryFacts(project: project, status: showArchived ? "all" : "active", query: search)
            note = nil
        } catch where error.isNotFound { facts = []; note = nil
        } catch { note = error.localizedDescription }
    }
}

private struct MemoryDetail: View {
    let fact: MemFact
    let changed: () -> Void
    @State private var text = ""
    @State private var topic = ""
    @State private var kind = "fact"
    @State private var history: [MemFact] = []
    @State private var error: String?
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                TextEditor(text: $text).font(.system(size: 13)).frame(minHeight: 110).scrollContentBackground(.hidden)
                    .padding(6).background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))
                HStack {
                    TextField("Topic", text: $topic).textFieldStyle(.roundedBorder)
                    Picker("Kind", selection: $kind) { ForEach(["fact", "decision", "gotcha", "preference", "handoff", "learned"], id: \.self) { Text($0.capitalized).tag($0) } }.frame(width: 190)
                }
                Group {
                    LabeledContent("Written by", value: fact.provenanceLine.isEmpty ? "Unknown" : fact.provenanceLine)
                    LabeledContent("Strength", value: "\(Int(fact.strengthValue * 100))%")
                    LabeledContent("Recalled", value: "\(fact.recalls ?? 0) times")
                    LabeledContent("Updated", value: relativeDateISO(fact.updatedAt))
                }.font(.system(size: 12))
                HStack {
                    Button("Save") { run { try await APIClient.shared.memoryEdit(fact.id, text: text, topic: topic, kind: kind) } }.buttonStyle(.borderedProminent)
                    Button(fact.isPinned ? "Unpin" : "Pin") { run { try await APIClient.shared.memoryPin(fact.id, pinned: !fact.isPinned) } }
                    if fact.isArchived { Button("Restore") { run { try await APIClient.shared.memoryRestore(fact.id) } } }
                    else { Button("Archive", role: .destructive) { run { try await APIClient.shared.memoryArchive(fact.id) } } }
                }.controlSize(.small)
                if let error { Text(error).foregroundStyle(.red).font(.caption) }
                if history.count > 1 {
                    Divider()
                    Text("Earlier versions").font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                    ForEach(history.dropLast()) { Text($0.text).font(.system(size: 11.5)).foregroundStyle(.secondary) }
                }
            }.padding(16)
        }
        .task { text = fact.text; topic = fact.topic ?? ""; kind = fact.kind ?? "fact"; history = (try? await APIClient.shared.memoryHistory(fact.id)) ?? [] }
    }
    private func run(_ work: @escaping () async throws -> Void) {
        Task { do { try await work(); changed() } catch { self.error = error.localizedDescription } }
    }
}

private struct MemoryAddSheet: View {
    let projects: [MemProject]
    let current: String
    let saved: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var target = "user"
    @State private var kind = "fact"
    @State private var error: String?
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Add to memory").font(.headline)
            TextField("What should every agent know?", text: $text, axis: .vertical).lineLimit(2...8).textFieldStyle(.roundedBorder)
            HStack {
                Picker("Applies to", selection: $target) {
                    Text("You").tag("user"); Text("Everywhere").tag("global")
                    ForEach(projects.filter { $0.key != "global" && $0.key != "user" }) { Text($0.label).tag($0.key) }
                }
                Picker("Kind", selection: $kind) { ForEach(["fact", "decision", "gotcha", "preference"], id: \.self) { Text($0.capitalized).tag($0) } }
            }
            if let error { Text(error).foregroundStyle(.red).font(.caption) }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Add") { Task {
                    do { try await APIClient.shared.memoryAdd(project: target, text: text, kind: kind); saved(); dismiss() } catch { self.error = error.localizedDescription }
                } }.keyboardShortcut(.defaultAction).disabled(text.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }.padding(20).frame(width: 480).onAppear { if current != "__all" { target = current } }
    }
}
