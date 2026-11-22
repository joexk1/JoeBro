import SwiftUI

/// The one memory surface: what you told JoeBro (You), what every agent learned about a
/// project (Projects), what applies everywhere (Global), plus the plain-text Files the phone agent reads.
/// Everything is one store (user, project and global scopes of Shared Memory).
struct SharedMemoryPanel: View {
    private enum Scope: String, CaseIterable { case all = "All", you = "You", projects = "Projects", global = "Global", files = "Files" }
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
            case .all, .files: true
            case .you: $0.scope == "user"
            case .global: $0.scope == "global"
            case .projects: $0.scope == "project"
            }
        }.sorted { ($0.isPinned ? 1 : 0, $0.strengthValue) > ($1.isPinned ? 1 : 0, $1.strengthValue) }
    }

    var body: some View {
        ExtrasShell(title: "Memory", glyph: .memory) {
            if scope != .files { PanelSearchField(text: $search, open: $searchOpen, placeholder: "Search memory") }
            ExtrasIconButton(symbol: "plus", help: "Add to memory") { adding = true }
            ExtrasIconButton(symbol: "arrow.clockwise", help: "Refresh") { Task { await load() } }
        } content: {
            VStack(spacing: JB.s3) {
                HStack(spacing: JB.s2) {
                    ForEach(Scope.allCases, id: \.self) { s in
                        Button(s.rawValue) { withAnimation(JB.snap) { scope = s } }
                            .buttonStyle(JBButtonStyle(kind: scope == s ? .secondary : .ghost, compact: true))
                    }
                    Spacer()
                    if scope != .files {
                        if projects.count > 2, scope == .all || scope == .projects {
                            Picker("", selection: $project) {
                                Text("Every project").tag("__all")
                                ForEach(projects.filter { $0.key != "global" && $0.key != "user" }) { Text($0.label).tag($0.key) }
                            }.labelsHidden().fixedSize()
                        }
                        Button("Archived") { showArchived.toggle() }.buttonStyle(JBButtonStyle(kind: showArchived ? .secondary : .ghost, compact: true))
                    }
                    if let note { Text(note).font(.jbMono(11)).foregroundStyle(JB.danger).lineLimit(1) }
                }
                if scope == .files {
                    PhoneMemoryView()
                } else {
                    HStack(spacing: JB.s3) {
                        ScrollView {
                            LazyVStack(spacing: 6) {
                                if shown.isEmpty { JBEmptyState(art: .inbox, text: "Nothing here yet").frame(minHeight: 240) }
                                ForEach(shown) { f in row(f).transition(.opacity) }
                            }
                            .padding(JB.s2)
                            .animation(JB.smooth, value: shown.map(\.id))
                        }
                        .frame(width: 430).frame(maxHeight: .infinity).jbSurface(.panel)
                        detail.frame(maxWidth: .infinity, maxHeight: .infinity).jbSurface(.panel)
                    }
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
        MemoryRow(fact: f, selected: selectedID == f.id) { selectedID = f.id }
            .contextMenu {
                Button(f.isPinned ? "Unpin" : "Pin") { act { try await APIClient.shared.memoryPin(f.id, pinned: !f.isPinned) } }
                if f.isArchived { Button("Restore") { act { try await APIClient.shared.memoryRestore(f.id) } } }
                else { Button("Archive", role: .destructive) { act { try await APIClient.shared.memoryArchive(f.id) } } }
            }
    }

    @ViewBuilder private var detail: some View {
        if let f = facts.first(where: { $0.id == selectedID }) {
            MemoryDetail(fact: f) { Task { await load() } }.id(f.id)
        } else {
            JBEmptyState(art: .search, text: "Pick a memory")
        }
    }

    private func act(_ work: @escaping () async throws -> Void) {
        Task { do { try await work(); await load() } catch { note = error.localizedDescription } }
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

private struct MemoryRow: View {
    let fact: MemFact
    let selected: Bool
    let tap: () -> Void
    var body: some View {
        Button(action: tap) {
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 6) {
                    Text(((fact.topic ?? "").isEmpty ? fact.kindLabel : (fact.topic ?? "")).lowercased()).font(.jbMono(10.5)).foregroundStyle(JB.ink3)
                    Spacer()
                    if fact.isPinned { Circle().fill(JB.accent).frame(width: 5, height: 5) }
                    Text(relativeDateISO(fact.updatedAt)).font(.jbMono(10.5)).foregroundStyle(JB.ink3)
                }
                Text(fact.text).font(.jb(12.5)).foregroundStyle(JB.ink).multilineTextAlignment(.leading).lineLimit(4)
                HStack(spacing: 8) {
                    Capsule().fill(JB.line).frame(width: 36, height: 2)
                        .overlay(alignment: .leading) { Capsule().fill(JB.accent.opacity(0.8)).frame(width: 36 * fact.strengthValue, height: 2) }
                    if !fact.provenanceLine.isEmpty { Text(fact.provenanceLine).font(.jbMono(10.5)).foregroundStyle(JB.ink3).lineLimit(1) }
                }
            }
            .padding(10).frame(maxWidth: .infinity, alignment: .leading)
            .jbHoverRow(selected: selected)
            .opacity(fact.isArchived ? 0.5 : 1)
        }.buttonStyle(.plain)
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
    @FocusState private var focus: Bool
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: JB.s3) {
                TextEditor(text: $text).font(.jb(13)).foregroundStyle(JB.ink).frame(minHeight: 110).scrollContentBackground(.hidden)
                    .padding(6).jbSurface(.raised, radius: JB.rControl)
                HStack(spacing: JB.s2) {
                    TextField("Topic", text: $topic).focused($focus).jbField(focused: focus)
                    Picker("", selection: $kind) { ForEach(["fact", "decision", "gotcha", "preference", "handoff", "learned"], id: \.self) { Text($0).tag($0) } }
                        .labelsHidden().frame(width: 130)
                }
                VStack(alignment: .leading, spacing: 4) {
                    meta("by", fact.provenanceLine.isEmpty ? "unknown" : fact.provenanceLine)
                    meta("strength", "\(Int(fact.strengthValue * 100))%")
                    meta("recalled", "\(fact.recalls ?? 0)x")
                    meta("updated", relativeDateISO(fact.updatedAt))
                }
                HStack(spacing: JB.s2) {
                    Button("Save") { run { try await APIClient.shared.memoryEdit(fact.id, text: text, topic: topic, kind: kind) } }.buttonStyle(JBButtonStyle(kind: .primary, compact: true))
                    Button(fact.isPinned ? "Unpin" : "Pin") { run { try await APIClient.shared.memoryPin(fact.id, pinned: !fact.isPinned) } }.buttonStyle(JBButtonStyle(kind: .secondary, compact: true))
                    if fact.isArchived { Button("Restore") { run { try await APIClient.shared.memoryRestore(fact.id) } }.buttonStyle(JBButtonStyle(kind: .secondary, compact: true)) }
                    else { Button("Archive") { run { try await APIClient.shared.memoryArchive(fact.id) } }.buttonStyle(JBButtonStyle(kind: .danger, compact: true)) }
                }
                if let error { Text(error).font(.jbMono(11)).foregroundStyle(JB.danger) }
                if history.count > 1 {
                    JBSectionHeader("Earlier", count: history.count - 1)
                    ForEach(history.dropLast()) { Text($0.text).font(.jb(11.5)).foregroundStyle(JB.ink3) }
                }
            }.padding(JB.s4)
        }
        .task { text = fact.text; topic = fact.topic ?? ""; kind = fact.kind ?? "fact"; history = (try? await APIClient.shared.memoryHistory(fact.id)) ?? [] }
    }
    private func meta(_ k: String, _ v: String) -> some View {
        HStack(spacing: 8) { Text(k).jbEyebrow().frame(width: 70, alignment: .leading); Text(v).font(.jbMono(11.5)).foregroundStyle(JB.ink2) }
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
    @FocusState private var focus: Bool
    var body: some View {
        VStack(spacing: 0) {
            ExtrasSheetHeader(title: "Add to memory") {
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction).buttonStyle(JBButtonStyle(kind: .ghost, compact: true))
                Button("Add") { Task {
                    do { try await APIClient.shared.memoryAdd(project: target, text: text, kind: kind); saved(); dismiss() } catch { self.error = error.localizedDescription }
                } }.keyboardShortcut(.defaultAction).buttonStyle(JBButtonStyle(kind: .primary, compact: true)).disabled(text.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            VStack(alignment: .leading, spacing: JB.s3) {
                TextField("What should every agent know?", text: $text, axis: .vertical).lineLimit(2...8).focused($focus).jbField(focused: focus)
                HStack(spacing: JB.s3) {
                    ExtrasField(label: "Applies to") {
                        Picker("", selection: $target) {
                            Text("You").tag("user"); Text("Everywhere").tag("global")
                            ForEach(projects.filter { $0.key != "global" && $0.key != "user" }) { Text($0.label).tag($0.key) }
                        }.labelsHidden()
                    }
                    ExtrasField(label: "Kind") {
                        Picker("", selection: $kind) { ForEach(["fact", "decision", "gotcha", "preference"], id: \.self) { Text($0).tag($0) } }.labelsHidden()
                    }
                }
                if let error { Text(error).font(.jbMono(11)).foregroundStyle(JB.danger) }
            }.padding(JB.s4)
        }.frame(width: 480).background(JB.surface).onAppear { focus = true; if current != "__all" { target = current } }
    }
}
