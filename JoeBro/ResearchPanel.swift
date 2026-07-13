import SwiftUI

/// Deep Research — launch runs, watch them live, read every report.
struct ResearchPanel: View {
    @Environment(AppStore.self) private var store
    // (deep-link handled in load())
    @State private var items: Loadable<[ResearchItem]> = .loading
    @State private var selected: ResearchItem?
    @State private var detail: JSONValue?
    @State private var query = ""
    @State private var researchModel: ModelChoice?
    @State private var selection: Set<String> = []

    var body: some View {
        PanelChrome(title: "Deep Research", icon: "antenna.radiowaves.left.and.right") {
            Button {
                Task { await load() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
        } content: {
            VStack(spacing: 10) {
                launcherBar

                if let run = store.researchRun {
                    ResearchProgressView(run: run) { store.cancelResearch() }
                        .id(run.id)
                        .joeGlassRect(16)
                        .frame(height: 190)
                }

                if hasReports {
                    HStack(spacing: 12) {
                        listColumn
                            .frame(width: 300)
                            .joeGlassRect(16)
                        readerColumn
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                            .joeGlassRect(16)
                    }
                } else {
                    listColumn
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .joeGlassRect(16)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        }
        .task { await load() }
        // When the store-owned run finishes (researchRun clears), refresh the
        // library so the new report appears.
        .onChange(of: store.researchRun?.id) { _, newID in
            if newID == nil { Task { await load() } }
        }
    }

    private var hasReports: Bool {
        if case .loaded(let list) = items {
            return !list.isEmpty
        }
        return false
    }

    private var launcherBar: some View {
        HStack(spacing: 8) {
            Image(systemName: "sparkle.magnifyingglass")
                .font(.system(size: 13))
                .foregroundStyle(Color.accentColor)
            TextField("Research anything...", text: $query)
                .textFieldStyle(.plain)
                .font(.system(size: 13))
                .onSubmit(start)
            Picker("", selection: Binding(
                get: { (researchModel ?? store.selectedModel)?.id ?? "" },
                set: { id in researchModel = store.models.first { $0.id == id } }
            )) {
                if researchModel == nil && store.selectedModel == nil {
                    Text("Model").tag("")
                }
                ModelPickerItems(models: store.models)
            }
            .labelsHidden()
            .frame(width: 190)
            Button(action: start) {
                Label("Research", systemImage: "bolt.horizontal")
                    .font(.system(size: 12, weight: .semibold))
            }
            .buttonStyle(.glassProminent)
            .tint(Color.accentColor)
            .controlSize(.small)
            .disabled(query.trimmingCharacters(in: .whitespaces).isEmpty || store.researchRun != nil)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .joeGlassCapsuleInteractive()
    }

    private func start() {
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty, store.researchRun == nil else { return }
        query = ""
        let model = researchModel ?? store.selectedModel
        // The run + polling live on the store, so it keeps going across tabs.
        store.startResearch(query: q, endpointID: model?.endpointID, model: model?.modelID)
    }

    private func load() async {
        do { items = .loaded(try await APIClient.shared.researchLibrary()) }
        catch { items = .failed(error.localizedDescription) }
        // Adopt a still-running job (e.g. one the agent started from chat) so the
        // panel shows live progress — only WATCH it, the store never re-triggers.
        if store.researchRun == nil,
           let running = try? await APIClient.shared.researchActive(),
           let first = running.first {
            store.adoptResearch(id: first.id, query: first.query)
        }
        // Chat anchor deep-link: open that report (or watch it if running)
        if let target = store.pendingResearchID {
            store.pendingResearchID = nil
            if case .loaded(let list) = items, let item = list.first(where: { $0.id == target }) {
                open(item)
            }
        }
    }

    private var listColumn: some View {
        Group {
            switch items {
            case .loading:
                ProgressView().frame(maxHeight: .infinity)
            case .failed(let e):
                ContentUnavailableView("Couldn't load reports", systemImage: "antenna.radiowaves.left.and.right.slash", description: Text(e))
            case .loaded(let list) where list.isEmpty:
                ContentUnavailableView("No research yet", systemImage: "doc.text.magnifyingglass",
                                       description: Text("Ask the agent to research something."))
            case .loaded(let list):
                VStack(spacing: 0) {
                    if !selection.isEmpty {
                        selectionBar(total: list.count, allIDs: list.map(\.id))
                    }
                    ScrollView {
                        LazyVStack(spacing: 6) {
                            ForEach(list) { item in
                                row(item)
                            }
                        }
                        .padding(8)
                    }
                }
                .animation(.spring(duration: 0.25), value: selection.isEmpty)
            }
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
                    for id in ids {
                        await APIClient.shared.researchDelete(id: id)
                        if selected?.id == id { selected = nil; detail = nil }
                    }
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

    private func row(_ item: ResearchItem) -> some View {
        SkillRowShell(id: item.id, checked: selection.contains(item.id),
                      selecting: !selection.isEmpty,
                      onToggleCheck: { toggleSelect(item.id) }) {
            VStack(alignment: .leading, spacing: 3) {
                Text(item.query)
                    .font(.system(size: 12.5, weight: selected?.id == item.id ? .semibold : .regular))
                    .lineLimit(2)
                HStack(spacing: 8) {
                    if let cat = item.category, !cat.isEmpty {
                        Text(cat)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 1)
                            .background(.quaternary.opacity(0.6), in: Capsule())
                    }
                    if let n = item.sourceCount { Text("\(n) sources") }
                    if let done = item.completedAt, done > 0 {
                        Text(Date(timeIntervalSince1970: done).formatted(date: .abbreviated, time: .omitted))
                    }
                }
                .font(.system(size: 10))
                .foregroundStyle(.tertiary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Spacer(minLength: 0)
        }
        .contentShape(Rectangle())
        .onTapGesture { selection.isEmpty ? open(item) : toggleSelect(item.id) }
        .contextMenu {
            Button("Copy Report") {
                Task { await copyReport(item) }
            }
            Button("Download Markdown") {
                Task { await downloadMarkdown(item) }
            }
            Button("Delete", role: .destructive) {
                Task {
                    await APIClient.shared.researchDelete(id: item.id)
                    if selected?.id == item.id { selected = nil; detail = nil }
                    await load()
                }
            }
        }
    }

    /// The full report markdown (title + body), shared by Copy and Download.
    private func reportMarkdown(_ item: ResearchItem) async -> String {
        let detail = try? await APIClient.shared.researchDetail(id: item.id)
        let report = detail?["report"]?.stringValue
            ?? detail?["summary"]?.stringValue
            ?? detail?["result"]?.stringValue ?? ""
        var md = "# \(item.query)\n\n" + report
        if let sources = detail?["sources"]?.arrayValue, !sources.isEmpty {
            md += "\n\n## Sources\n"
            for (i, s) in sources.enumerated() where s["url"]?.stringValue != nil {
                let url = s["url"]!.stringValue!
                md += "\n\(i + 1). [\(s["title"]?.stringValue ?? url)](\(url))"
            }
        }
        return md
    }

    /// Copy the entire report to the clipboard.
    private func copyReport(_ item: ResearchItem) async {
        let text = await reportMarkdown(item)
        await MainActor.run {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
        }
    }

    /// Save the full report as a .md named after the research query.
    private func downloadMarkdown(_ item: ResearchItem) async {
        let text = await reportMarkdown(item)
        await MainActor.run {
            let bad = CharacterSet(charactersIn: "/\\:?%*|\"<>")
            let name = String(item.query.components(separatedBy: bad)
                .joined(separator: " ").prefix(120))
                .trimmingCharacters(in: .whitespaces)
            let panel = NSSavePanel()
            panel.nameFieldStringValue = (name.isEmpty ? "research" : name) + ".md"
            if panel.runModal() == .OK, let url = panel.url {
                try? text.write(to: url, atomically: true, encoding: .utf8)
            }
        }
    }

    private func open(_ item: ResearchItem) {
        selected = item
        detail = nil
        Task {
            detail = try? await APIClient.shared.researchDetail(id: item.id)
        }
    }

    // (progress view lives below)

    @ViewBuilder
    private var readerColumn: some View {
        if let item = selected {
            if let detail {
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        Text(item.query)
                            .font(.system(.title3, design: .rounded, weight: .bold))
                        let report = detail["report"]?.stringValue
                            ?? detail["summary"]?.stringValue
                            ?? detail["result"]?.stringValue
                            ?? ""
                        if report.isEmpty {
                            Text("This report has no readable summary.")
                                .foregroundStyle(.secondary)
                        } else {
                            MarkdownText(text: report)
                        }
                        if let sources = detail["sources"]?.arrayValue, !sources.isEmpty {
                            Divider().opacity(0.4)
                            Text("Sources (\(sources.count))")
                                .font(.system(size: 12, weight: .semibold))
                                .foregroundStyle(.secondary)
                            ForEach(Array(sources.prefix(30).enumerated()), id: \.offset) { i, s in
                                if let url = s["url"]?.stringValue {
                                    Link(destination: URL(string: url) ?? URL(string: "about:blank")!) {
                                        HStack(spacing: 6) {
                                            Text("\(i + 1).")
                                                .font(.system(size: 10.5, weight: .bold))
                                                .foregroundStyle(.tertiary)
                                            Text(s["title"]?.stringValue ?? url)
                                                .font(.system(size: 11.5))
                                                .lineLimit(1)
                                        }
                                    }
                                    .buttonStyle(.plain)
                                }
                            }
                        }
                    }
                    .padding(16)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        } else {
            ContentUnavailableView("Select a report", systemImage: "doc.text.magnifyingglass")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

// MARK: - Liquid-glass researching animation

struct ResearchProgressView: View {
    let run: ResearchRun
    let onFinished: () -> Void   // Cancel

    private var phaseIcon: String {
        switch run.phase {
        case "planning": return "list.bullet.clipboard"
        case "searching": return "magnifyingglass"
        case "reading": return "book"
        case "analyzing": return "brain.head.profile"
        case "writing": return "square.and.pencil"
        default: return "sparkles"
        }
    }

    var body: some View {
        HStack(spacing: 24) {
            // Pulsing concentric glass rings around the phase icon
            TimelineView(.animation(minimumInterval: 1.0 / 30.0)) { context in
                let t = context.date.timeIntervalSinceReferenceDate
                ZStack {
                    ForEach(0..<3, id: \.self) { ring in
                        let phase = (t * 0.7 + Double(ring) / 3).truncatingRemainder(dividingBy: 1)
                        Circle()
                            .stroke(Color.accentColor.opacity(0.55 * (1 - phase)), lineWidth: 2)
                            .frame(width: 40 + 90 * phase, height: 40 + 90 * phase)
                    }
                    Circle()
                        .fill(.ultraThinMaterial)
                        .frame(width: 56, height: 56)
                        .overlay(Circle().strokeBorder(.white.opacity(0.25), lineWidth: 1))
                    Image(systemName: phaseIcon)
                        .font(.system(size: 19, weight: .medium))
                        .foregroundStyle(Color.accentColor)
                        .contentTransition(.symbolEffect(.replace))
                }
            }
            .frame(width: 140, height: 140)

            VStack(alignment: .leading, spacing: 7) {
                Text(run.query)
                    .font(.system(size: 13.5, weight: .semibold))
                    .lineLimit(2)
                HStack(spacing: 6) {
                    Text(run.phase.capitalized)
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(Color.accentColor)
                    if !run.message.isEmpty {
                        Text("· " + run.message)
                            .font(.system(size: 11.5))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                }
                // Elapsed vs the historical average duration
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(.quaternary)
                        Capsule()
                            .fill(LinearGradient(colors: [Color.accentColor.opacity(0.7), Color.accentColor],
                                                 startPoint: .leading, endPoint: .trailing))
                            .frame(width: geo.size.width * progressFraction)
                    }
                }
                .frame(height: 5)
                HStack {
                    Text(timeString(run.elapsed))
                    if let avg = run.avgDuration {
                        Text("· usually ~\(timeString(avg))")
                    }
                    Spacer()
                    Button("Cancel") { onFinished() }
                        .buttonStyle(.borderless)
                        .font(.system(size: 11))
                }
                .font(.system(size: 10.5))
                .foregroundStyle(.tertiary)
            }
            .padding(.trailing, 16)
        }
        .padding(.horizontal, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var progressFraction: Double {
        guard let avg = run.avgDuration, avg > 0 else {
            return min(run.elapsed / 300, 0.95)
        }
        return min(run.elapsed / avg, 0.97)
    }

    private func timeString(_ s: TimeInterval) -> String {
        String(format: "%d:%02d", Int(s) / 60, Int(s) % 60)
    }
}
