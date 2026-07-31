import SwiftUI

// MARK: - Brain (memory)

struct BrainPanel: View {
    @State private var memories: Loadable<[MemoryEntry]> = .loading
    @State private var search = ""
    @State private var searchOpen = false
    @State private var editing: MemoryEntry?
    @State private var selection: Set<String> = []
    // AppStorage so map/list choice survives tab switches (the panel is recreated).
    @AppStorage("brainMapMode") private var mapMode = true

    var body: some View {
        PanelChrome(title: "Brain", icon: "brain.head.profile") {
            Button {
                withAnimation(.spring(duration: 0.25)) { mapMode.toggle() }
            } label: {
                Image(systemName: mapMode ? "list.bullet" : "map")
            }
            .buttonStyle(.borderless)
            .help(mapMode ? "List view" : "Map view")
            PanelSearchField(text: $search, open: $searchOpen, placeholder: "Search memories")
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
                        } else if mapMode {
                            MemoryMapView(memories: shown,
                                          onEdit: { editing = $0 },
                                          reload: { await load() })
                        } else {
                            VStack(spacing: 0) {
                                if !selection.isEmpty {
                                    SelectionBar(total: shown.count, allIDs: shown.map(\.id), selection: $selection) { ids in
                                        Task {
                                            for id in ids { try? await APIClient.shared.deleteMemory(id: id) }
                                            await load()
                                        }
                                    }
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



    private func memoryCard(_ m: MemoryEntry) -> some View {
        SkillRowShell(id: m.id, checked: selection.contains(m.id),
                      selecting: !selection.isEmpty,
                      onToggleCheck: { toggleSelection(m.id, in: &selection) }) {
            Image(systemName: m.pinned == true ? "pin.fill" : "sparkle")
                .font(.system(size: 11))
                .foregroundStyle(m.pinned == true ? Color.accentColor : .secondary)
                .padding(.top, 2)
            VStack(alignment: .leading, spacing: 4) {
                Text(m.displayText)
                    .font(.system(size: 13))
                    .textSelection(.enabled)
                HStack(spacing: 8) {
                    Text(m.effectiveCategory)
                        .font(.system(size: 10, weight: .medium))
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2)
                        .background(.quaternary.opacity(0.6), in: Capsule())
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

// MARK: - Memory map (obsidian-style graph)

@MainActor @Observable
private final class MemoryGraph {
    struct Node: Identifiable {
        let memory: MemoryEntry
        var pos: CGPoint
        var vel = CGVector(dx: 0, dy: 0)
        let id: String
        let label: String
        var degree = 0   // connection count — hubs draw bigger
    }

    var nodes: [Node] = []
    // rest = spring rest length: shorter = conceptually closer.
    var edges: [(a: Int, b: Int, rest: CGFloat)] = []
    var draggingID: String?
    private(set) var settled = false

    /// Rebuild for a new memory list, keeping positions of surviving nodes so
    /// search/reload doesn't scatter the layout.
    func rebuild(_ mems: [MemoryEntry]) {
        var old: [String: CGPoint] = [:]
        for n in nodes { old[n.id] = n.pos }
        nodes = mems.enumerated().map { i, m in
            let angle = Double(i) / Double(max(mems.count, 1)) * 2 * .pi
            let ring = 120.0 + Double(i % 5) * 30
            let t = m.displayText
            return Node(memory: m,
                        pos: old[m.id] ?? CGPoint(x: cos(angle) * ring, y: sin(angle) * ring),
                        id: m.id,
                        label: t.count > 22 ? String(t.prefix(22)) + "…" : t)
        }
        // Same-category nodes chain together (O(n), not a clique); pairs sharing
        // 2+ significant words connect by relevance. Boilerplate words the
        // memory extractor uses everywhere ("user prefers…") don't count.
        let stop: Set<Substring> = ["user", "users", "prefer", "prefers", "preferred",
            "likes", "liked", "wants", "wanted", "with", "that", "this", "from",
            "have", "uses", "used", "using", "avoid", "avoids", "them", "they",
            "their", "when", "which", "would", "should", "about", "into", "more",
            "also", "very", "then", "than", "over", "only", "some", "such", "does",
            "doesn", "will", "being", "because"]
        var e: [(a: Int, b: Int, rest: CGFloat)] = []
        var lastOfCat: [String: Int] = [:]
        let words: [Set<Substring>] = mems.map {
            Set($0.displayText.lowercased()
                .split(whereSeparator: { !$0.isLetter && !$0.isNumber })
                .filter { $0.count > 3 && !stop.contains($0) })
        }
        // Category-only kinship is the loosest tie → longest line.
        for (i, m) in mems.enumerated() {
            let cat = m.effectiveCategory
            if let prev = lastOfCat[cat] { e.append((a: prev, b: i, rest: 190)) }
            lastOfCat[cat] = i
        }
        // More shared words = conceptually closer = shorter line.
        outer: for i in mems.indices {
            for j in (i + 1)..<mems.count {
                let overlap = words[i].intersection(words[j]).count
                if overlap >= 2 {
                    e.append((a: i, b: j, rest: max(60, 200 - CGFloat(overlap) * 35)))
                    if e.count > 600 { break outer }   // ponytail: edge cap; prune by weight if maps get huge
                }
            }
        }
        edges = e
        for (a, b, _) in e {
            nodes[a].degree += 1
            nodes[b].degree += 1
        }
        settled = false
    }

    func poke() { settled = false }

    /// Run the sim to rest in one go — the map is static between reloads/drags.
    /// Declares settled even if the energy threshold was never hit: big graphs
    /// can jitter forever, and "settled" also gates label drawing.
    func settle(maxSteps: Int = 600) {
        for _ in 0..<maxSteps where !settled { tick() }
        settled = true
    }

    /// One force-directed step: pairwise repulsion, edge springs, gentle pull
    /// to origin. ponytail: O(n²) repulsion — fine for a personal memory store.
    func tick() {
        guard !settled, !nodes.isEmpty else { return }
        var force = [CGVector](repeating: CGVector(dx: 0, dy: 0), count: nodes.count)
        for i in nodes.indices {
            for j in (i + 1)..<nodes.count {
                let dx = nodes[i].pos.x - nodes[j].pos.x
                let dy = nodes[i].pos.y - nodes[j].pos.y
                let d2 = max(dx * dx + dy * dy, 40)
                let d = sqrt(d2)
                let rep = 16000 / d2
                force[i].dx += dx / d * rep; force[i].dy += dy / d * rep
                force[j].dx -= dx / d * rep; force[j].dy -= dy / d * rep
            }
            force[i].dx -= nodes[i].pos.x * 0.015
            force[i].dy -= nodes[i].pos.y * 0.015
        }
        for (a, b, rest) in edges {
            let dx = nodes[b].pos.x - nodes[a].pos.x
            let dy = nodes[b].pos.y - nodes[a].pos.y
            let d = max(sqrt(dx * dx + dy * dy), 1)
            let pull = (d - rest) * 0.015
            force[a].dx += dx / d * pull; force[a].dy += dy / d * pull
            force[b].dx -= dx / d * pull; force[b].dy -= dy / d * pull
        }
        var maxV = 0.0
        for i in nodes.indices where nodes[i].id != draggingID {
            nodes[i].vel.dx = (nodes[i].vel.dx + force[i].dx) * 0.66
            nodes[i].vel.dy = (nodes[i].vel.dy + force[i].dy) * 0.66
            nodes[i].pos.x += nodes[i].vel.dx
            nodes[i].pos.y += nodes[i].vel.dy
            maxV = max(maxV, abs(nodes[i].vel.dx) + abs(nodes[i].vel.dy))
        }
        if maxV < 0.08, draggingID == nil { settled = true }
    }
}

private struct MemoryMapView: View {
    let memories: [MemoryEntry]
    let onEdit: (MemoryEntry) -> Void
    let reload: () async -> Void

    @State private var graph = MemoryGraph()
    @State private var scale: CGFloat = 1
    @State private var pan: CGSize = .zero
    @State private var pinchScale: CGFloat = 1
    @State private var panDrag: CGSize = .zero
    @State private var hoveredID: String?
    @State private var relax: Task<Void, Never>?
    @Environment(\.colorScheme) private var colorScheme

    private var zoom: CGFloat { scale * pinchScale }

    var body: some View {
        GeometryReader { geo in
            let center = CGPoint(x: geo.size.width / 2, y: geo.size.height / 2)
            ZStack {
                // Everything visible draws in ONE canvas pass — per-node SwiftUI
                // views (materials, shadows) made dragging laggy.
                Canvas { ctx, _ in
                    drawEdges(ctx, center: center)
                    drawNodes(ctx, center: center)
                }
                .contentShape(Rectangle())
                .gesture(
                    DragGesture()
                        .onChanged { panDrag = $0.translation }
                        .onEnded { v in
                            pan.width += v.translation.width
                            pan.height += v.translation.height
                            panDrag = .zero
                        }
                )
                // Invisible, near-free hit targets for hover / right-click / drag.
                ForEach(graph.nodes) { node in
                    hitTarget(node, center: center)
                }
                // Labels are persistent views (never unmounted, just faded) so
                // their glyph layout is cached — drawing them in the canvas
                // re-laid-out every string on every frame.
                ForEach(graph.nodes) { node in
                    Text(node.label)
                        .font(.system(size: 9))
                        .foregroundStyle(.secondary)
                        .opacity(zoom > 0.55 || hoveredID == node.id ? 1 : 0)
                        .position(labelPoint(node, center: center))
                        .allowsHitTesting(false)
                }
            }
            .coordinateSpace(name: "memMap")
        }
        .simultaneousGesture(
            MagnifyGesture()
                .onChanged { pinchScale = $0.magnification }
                .onEnded { v in
                    scale = max(scale * v.magnification, 0.1)   // zoom in is unlimited
                    pinchScale = 1
                }
        )
        .overlay(alignment: .bottomTrailing) { zoomControls }
        .overlay(alignment: .bottomLeading) { hoverCard }
        .clipped()
        .task(id: memories.map(\.id)) {
            graph.rebuild(memories)
            graph.settle()
        }
        .onDisappear { relax?.cancel() }
    }

    private func drawEdges(_ ctx: GraphicsContext, center: CGPoint) {
        for (a, b, rest) in graph.edges {
            var path = Path()
            path.move(to: screen(graph.nodes[a].pos, center: center))
            path.addLine(to: screen(graph.nodes[b].pos, center: center))
            let hot = hoveredID != nil &&
                (graph.nodes[a].id == hoveredID || graph.nodes[b].id == hoveredID)
            // Stronger tie (shorter rest) also draws heavier, so strength
            // reads even when the layout can't hit ideal lengths.
            let strength = (200 - rest) / 140   // 0 weak … 1 strong
            ctx.stroke(path,
                       with: .color(hot ? Color.accentColor
                                        : Color.secondary.opacity(0.2 + 0.25 * strength)),
                       lineWidth: hot ? 1.6 : 0.7 + strength)
        }
    }

    private func drawNodes(_ ctx: GraphicsContext, center: CGPoint) {
        let base = colorScheme == .dark ? Color.white.opacity(0.14) : Color.black.opacity(0.08)
        for node in graph.nodes {
            let m = node.memory
            let hovered = hoveredID == m.id
            let pinned = m.pinned == true
            let p = screen(node.pos, center: center)
            let r = radius(node, hovered: hovered)
            let circle = Path(ellipseIn: CGRect(x: p.x - r, y: p.y - r, width: r * 2, height: r * 2))
            ctx.fill(circle, with: .color(base))
            ctx.fill(circle, with: .color(Color.accentColor.opacity(pinned ? 0.4 : hovered ? 0.25 : 0.1)))
            ctx.stroke(circle,
                       with: .color(hovered ? Color.accentColor : Color.white.opacity(0.35)),
                       lineWidth: hovered ? 1.6 : 0.8)
        }
    }

    private func screen(_ p: CGPoint, center: CGPoint) -> CGPoint {
        CGPoint(x: center.x + p.x * zoom + pan.width + panDrag.width,
                y: center.y + p.y * zoom + pan.height + panDrag.height)
    }

    private func world(_ p: CGPoint, center: CGPoint) -> CGPoint {
        CGPoint(x: (p.x - center.x - pan.width - panDrag.width) / zoom,
                y: (p.y - center.y - pan.height - panDrag.height) / zoom)
    }

    /// Well-connected memories draw bigger, obsidian-style; pins add a bump.
    private func radius(_ node: MemoryGraph.Node, hovered: Bool) -> CGFloat {
        (5 + min(CGFloat(node.degree), 9) * 0.8 + (node.memory.pinned == true ? 2 : 0))
            * (hovered ? 1.2 : 1)
    }

    private func labelPoint(_ node: MemoryGraph.Node, center: CGPoint) -> CGPoint {
        let p = screen(node.pos, center: center)
        return CGPoint(x: p.x, y: p.y + radius(node, hovered: hoveredID == node.id) + 9)
    }

    // Interactions attach BEFORE .position — a positioned view's frame fills
    // the whole map, so hover/right-click after it would hit every node at once.
    private func hitTarget(_ node: MemoryGraph.Node, center: CGPoint) -> some View {
        let m = node.memory
        return Color.clear
            .frame(width: 28, height: 28)
            .contentShape(Circle())
            .onHover { h in
                if h { hoveredID = m.id } else if hoveredID == m.id { hoveredID = nil }
            }
            .contextMenu {
                Button("Edit…") { onEdit(m) }
                Button(m.pinned == true ? "Unpin" : "Pin") {
                    Task { await APIClient.shared.pinMemory(id: m.id); await reload() }
                }
                Button("Delete", role: .destructive) {
                    Task { try? await APIClient.shared.deleteMemory(id: m.id); await reload() }
                }
            }
            .gesture(
                DragGesture(minimumDistance: 1, coordinateSpace: .named("memMap"))
                    .onChanged { v in
                        relax?.cancel()
                        graph.draggingID = m.id
                        if let idx = graph.nodes.firstIndex(where: { $0.id == m.id }) {
                            graph.nodes[idx].pos = world(v.location, center: center)
                            graph.nodes[idx].vel = CGVector(dx: 0, dy: 0)
                            graph.poke()
                            graph.tick()   // neighbours pull along, one step per drag event
                        }
                    }
                    .onEnded { _ in
                        graph.draggingID = nil
                        graph.poke()
                        // Relax by ticking frames: nodes and edges move in lockstep.
                        // 2 sim steps per 30fps frame — converges before the frame
                        // budget runs out, so no trailing settle-jump.
                        relax = Task {
                            for _ in 0..<60 where !graph.settled && !Task.isCancelled {
                                graph.tick()
                                graph.tick()
                                try? await Task.sleep(for: .milliseconds(33))
                            }
                            if !Task.isCancelled { graph.settle() }
                        }
                    }
            )
            .position(screen(node.pos, center: center))
    }

    private var zoomControls: some View {
        HStack(spacing: 12) {
            Button {
                scale = max(scale / 1.25, 0.1)
            } label: {
                Image(systemName: "minus.magnifyingglass")
            }
            .help("Zoom out")
            Button {
                scale = 1; pan = .zero
            } label: {
                Image(systemName: "scope")
            }
            .help("Reset view")
            Button {
                scale *= 1.25
            } label: {
                Image(systemName: "plus.magnifyingglass")
            }
            .help("Zoom in")
        }
        .buttonStyle(.borderless)
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .joeGlassCapsule()
        .padding(10)
    }

    @ViewBuilder
    private var hoverCard: some View {
        if let m = graph.nodes.first(where: { $0.id == hoveredID })?.memory {
            VStack(alignment: .leading, spacing: 5) {
                Text(m.displayText)
                    .font(.system(size: 12))
                    .lineLimit(6)
                HStack(spacing: 8) {
                    Text(m.effectiveCategory)
                        .font(.system(size: 10, weight: .medium))
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2)
                        .background(.quaternary.opacity(0.6), in: Capsule())
                    Text(relativeDate(m.createdAt))
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                }
            }
            .padding(10)
            .frame(maxWidth: 260, alignment: .leading)
            .joeGlassRect(10)
            .padding(10)
            .allowsHitTesting(false)
            .transition(.opacity)
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
