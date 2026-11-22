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
            .buttonStyle(.jbIcon)
        } content: {
            VStack(spacing: 10) {
                launcherBar

                if let run = store.researchRun {
                    ResearchProgressView(run: run) { store.cancelResearch() }
                        .id(run.id)
                        .frame(height: 96)
                        .jbSurface(.panel, radius: JB.rPanel)
                        .transition(.opacity.combined(with: .move(edge: .top)))
                }

                if hasReports {
                    HStack(spacing: 12) {
                        listColumn
                            .frame(width: 300)
                            .jbSurface(.panel, radius: JB.rPanel)
                        readerColumn
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                            .jbSurface(.panel, radius: JB.rPanel)
                    }
                } else {
                    listColumn
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .jbSurface(.panel, radius: JB.rPanel)
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
            JBIcon(.research, size: 15)
                .foregroundStyle(JB.accent)
            TextField("Research anything", text: $query)
                .textFieldStyle(.plain)
                .font(.jb(13))
                .onSubmit(start)
            ModelPickerMenu(selection: .some(researchModel ?? store.selectedModel),
                            onPick: { researchModel = $0 })
            Button("Research", action: start)
            .buttonStyle(JBButtonStyle(kind: .primary, compact: true))
            .disabled(query.trimmingCharacters(in: .whitespaces).isEmpty || store.researchRun != nil)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .jbSurface(.raised, radius: JB.rCard)
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
                PanelLoading()
            case .failed(let e):
                PanelFailure(title: "Couldn't load reports", detail: e)
            case .loaded(let list) where list.isEmpty:
                PanelEmpty(art: .search, text: "No research yet")
            case .loaded(let list):
                VStack(spacing: 0) {
                    if !selection.isEmpty {
                        SelectionBar(total: list.count, allIDs: list.map(\.id), selection: $selection) { ids in
                            Task {
                                for id in ids {
                                    await APIClient.shared.researchDelete(id: id)
                                    if selected?.id == id { selected = nil; detail = nil }
                                }
                                await load()
                            }
                        }
                    }
                    ScrollView {
                        LazyVStack(spacing: 6) {
                            ForEach(list) { item in
                                row(item)
                            }
                        }
                        .padding(8)
                        .animation(JB.smooth, value: list.map(\.id))
                    }
                }
                .animation(JB.snap, value: selection.isEmpty)
            }
        }
    }



    private func row(_ item: ResearchItem) -> some View {
        SkillRowShell(id: item.id, checked: selection.contains(item.id),
                      selecting: !selection.isEmpty,
                      onToggleCheck: { toggleSelection(item.id, in: &selection) }) {
            VStack(alignment: .leading, spacing: 3) {
                Text(item.query)
                    .font(.jb(12.5, selected?.id == item.id ? .semibold : .regular))
                    .foregroundStyle(JB.ink)
                    .lineLimit(2)
                HStack(spacing: 8) {
                    if let cat = item.category, !cat.isEmpty {
                        JBChip(text: cat)
                    }
                    if let n = item.sourceCount { Text("\(n) sources") }
                    if let done = item.completedAt, done > 0 {
                        Text(Date(timeIntervalSince1970: done).formatted(date: .abbreviated, time: .omitted))
                    }
                }
                .font(.jb(10.5))
                .foregroundStyle(JB.ink3)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Spacer(minLength: 0)
        }
        .overlay(alignment: .leading) {
            if selected?.id == item.id {
                Capsule().fill(JB.accent).frame(width: 2).padding(.vertical, 8).offset(x: -1)
            }
        }
        .contentShape(Rectangle())
        .onTapGesture { selection.isEmpty ? open(item) : toggleSelection(item.id, in: &selection) }
        .jbContextMenu {
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
            for (i, s) in sources.enumerated() {
                guard let url = s["url"]?.stringValue else { continue }
                md += "\n\(i + 1). [\(s["title"]?.stringValue ?? url)](\(url))"
            }
        }
        return md
    }

    /// Copy the entire report to the clipboard.
    private func copyReport(_ item: ResearchItem) async {
        let text = await reportMarkdown(item)
        await MainActor.run { JBPasteboard.copy(text) }
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

    private func reportText(_ d: JSONValue, _ item: ResearchItem) -> String {
        let r = d["report"]?.stringValue ?? d["summary"]?.stringValue ?? d["result"]?.stringValue ?? ""
        return "# \(item.query)\n\n" + r
    }

    @ViewBuilder
    private var readerColumn: some View {
        if let item = selected {
            if let detail {
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        HStack(alignment: .top, spacing: 8) {
                            Text(item.query)
                                .font(.jb(20, .semibold))
                                .tracking(-0.3)
                                .frame(maxWidth: .infinity, alignment: .leading)
                            CopyCheckButton(text: reportText(detail, item))
                        }
                        let report = detail["report"]?.stringValue
                            ?? detail["summary"]?.stringValue
                            ?? detail["result"]?.stringValue
                            ?? ""
                        if report.isEmpty {
                            Text("Empty report").font(.jb(12)).foregroundStyle(JB.ink3)
                        } else {
                            MarkdownText(text: report)
                        }
                        if let sources = detail["sources"]?.arrayValue, !sources.isEmpty {
                            Divider().overlay(JB.line)
                            JBSectionHeader("Sources", count: sources.count)
                            ForEach(Array(sources.prefix(30).enumerated()), id: \.offset) { i, s in
                                if let url = s["url"]?.stringValue {
                                    Link(destination: URL(string: url) ?? URL(string: "about:blank")!) {
                                        HStack(spacing: 6) {
                                            Text("\(i + 1).")
                                                .font(.jb(10.5))
                                                .foregroundStyle(JB.ink3)
                                            Text(s["title"]?.stringValue ?? url)
                                                .font(.jb(12))
                                                .foregroundStyle(JB.ink2)
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
                PanelLoading()
            }
        } else {
            PanelEmpty(art: .files, text: "Select a report")
        }
    }
}

// MARK: - Running research

struct ResearchProgressView: View {
    let run: ResearchRun
    let onFinished: () -> Void   // Cancel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                JBStatusDot(live: true, size: 6)
                Text(run.phase).jbEyebrow()
                if !run.message.isEmpty {
                    Text(run.message)
                        .font(.jb(11))
                        .foregroundStyle(JB.ink3)
                        .lineLimit(1)
                }
                Spacer()
                JBSpinner()
            }
            Text(run.query)
                .font(.jb(14, .semibold))
                .lineLimit(2)
            Capsule().fill(JB.line).frame(height: 3)
                .overlay(alignment: .leading) {
                    GeometryReader { geo in
                        Capsule().fill(JB.live)
                            .frame(width: geo.size.width * progressFraction)
                            .animation(JB.smooth, value: progressFraction)
                    }
                }
            HStack {
                Text(timeString(run.elapsed))
                if let avg = run.avgDuration { Text("/ ~\(timeString(avg))") }
                Spacer()
                Button("Cancel") { onFinished() }
                    .buttonStyle(JBButtonStyle(kind: .ghost, compact: true))
            }
            .font(.jb(10.5))
            .foregroundStyle(JB.ink3)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
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
