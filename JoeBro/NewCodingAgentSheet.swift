import SwiftUI

/// Start a coding agent on any linked computer, like opening a terminal there:
/// device → folder → new session or resume one of its recent sessions.
struct NewCodingAgentSheet: View {
    var onStarted: () -> Void
    @Environment(\.dismiss) private var dismiss

    private struct Session: Identifiable, Hashable {
        let id: String, repo: String, host: String, agent: String, summary: String, branch: String, at: String, resumeID: String
    }

    @State private var hosts: [NightHost] = []
    @State private var sessions: [Session] = []
    @State private var host = ""
    @State private var repo = ""
    @State private var query = ""
    @State private var agent = "claude"
    @State private var busy = false
    @State private var error: String?

    private var folders: [String] {
        var seen = Set<String>()
        return sessions.filter { $0.host == host && !$0.repo.isEmpty && !$0.repo.hasPrefix("/tmp/") && !$0.repo.contains("/scratchpad") }.map(\.repo).filter { seen.insert($0).inserted }
    }
    private var folder: String { repo.trimmingCharacters(in: .whitespaces) }
    /// Recent folders, filtered by what's typed: never more than six rows.
    private var matches: [String] {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        let list = q.isEmpty ? folders : folders.filter { $0.lowercased().contains(q) }
        return Array(list.prefix(6))
    }
    private var typedPath: String? {
        let q = query.trimmingCharacters(in: .whitespaces)
        return (q.hasPrefix("/") || q.hasPrefix("~")) && !folders.contains(q) ? q : nil
    }
    private var resumable: [Session] { sessions.filter { $0.host == host && $0.repo == folder && !$0.resumeID.isEmpty } }

    @State private var permission = "edits"
    @State private var prompt = ""

    var body: some View {
        VStack(alignment: .leading, spacing: JB.s4) {
            Text("New coding agent").font(.jb(17, .semibold)).tracking(-0.2)

            HStack(spacing: JB.s3) {
                field("Device") {
                    select(hosts.first { $0.id == host }.map { $0.up == false ? $0.label + " (offline)" : $0.label } ?? "Choose") {
                        ForEach(hosts) { h in option(h.up == false ? h.label + " (offline)" : h.label, on: h.id == host) { host = h.id } }
                    }
                }
                field("Agent") {
                    select(["claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"][agent] ?? agent) {
                        option("Claude Code", on: agent == "claude") { agent = "claude" }
                        option("Codex", on: agent == "codex") { agent = "codex" }
                        option("OpenCode", on: agent == "opencode") { agent = "opencode" }
                    }
                }
                field("Permissions") {
                    select(["ask": "Ask first", "edits": "Accept edits", "full": "Full access"][permission] ?? permission) {
                        option("Ask first", on: permission == "ask") { permission = "ask" }
                        option("Accept edits", on: permission == "edits") { permission = "edits" }
                        option("Full access", on: permission == "full") { permission = "full" }
                    }
                }
            }

            field("Folder") {
                VStack(spacing: 0) {
                    HStack(spacing: 8) {
                        Image(systemName: "magnifyingglass").font(.jb(12)).foregroundStyle(JB.ink3)
                        TextField("Search recent folders or paste a path", text: $query)
                            .textFieldStyle(.plain).font(.jb(13))
                            .onSubmit { if let p = typedPath { repo = p } else if let f = matches.first { repo = f } }
                    }
                    .padding(.horizontal, 10).frame(height: 34)
                    Rectangle().fill(JB.line).frame(height: 1)
                    VStack(spacing: 1) {
                        if let p = typedPath { folderRow(p, title: "Use this path") }
                        ForEach(matches, id: \.self) { f in folderRow(f) }
                        if matches.isEmpty && typedPath == nil {
                            Text(folders.isEmpty ? "No recent folders on this device. Paste a path." : "No match. Paste a full path.")
                                .font(.jb(12)).foregroundStyle(JB.ink3)
                                .frame(maxWidth: .infinity, alignment: .leading).padding(10)
                        }
                    }
                    .padding(4)
                }
                .background(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).fill(JB.raised))
                .overlay(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).strokeBorder(JB.line, lineWidth: 1))
            }

            field("First message") {
                TextField("Optional", text: $prompt, axis: .vertical)
                    .textFieldStyle(.plain).font(.jb(13)).lineLimit(1...3)
                    .padding(.horizontal, 10).padding(.vertical, 8)
                    .jbField(focused: false)
            }

            if !resumable.isEmpty {
                field("Or resume a session here") {
                    VStack(spacing: 1) {
                        ForEach(resumable.prefix(3)) { s in
                            Button { Task { await start(resume: s.resumeID) } } label: {
                                HStack(spacing: 10) {
                                    Image(systemName: "arrow.uturn.forward").font(.jb(11)).foregroundStyle(JB.ink3).frame(width: 16)
                                    Text(s.summary.isEmpty ? "Session" : s.summary).font(.jb(13)).foregroundStyle(JB.ink).lineLimit(1)
                                    Spacer(minLength: 8)
                                    Text(relative(s.at)).font(.jb(11.5)).foregroundStyle(JB.ink3)
                                }
                                .padding(.horizontal, 8).frame(height: 30)
                                .jbHoverRow(selected: false)
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .disabled(busy)
                        }
                    }
                }
            }

            if let error { Text(error).font(.jb(12)).foregroundStyle(JB.danger) }
            HStack(spacing: JB.s2) {
                if busy { JBSpinner(); Text("Starting…").font(.jb(12)).foregroundStyle(JB.ink2) }
                Spacer()
                Button("Cancel") { dismiss() }.buttonStyle(.jbGhost).keyboardShortcut(.cancelAction)
                Button("Start") { Task { await start(resume: nil) } }
                    .buttonStyle(.jbPrimary)
                    .keyboardShortcut(.defaultAction)
                    .disabled(busy || host.isEmpty || folder.isEmpty)
            }
        }
        .padding(JB.s5)
        .frame(width: 540)
        .task { await load() }
        .onChange(of: host) { repo = folders.first ?? ""; query = "" }
    }

    private func select<C: View>(_ title: String, @ViewBuilder _ items: @escaping () -> C) -> some View {
        JBMenu(content: items) {
            HStack(spacing: 6) {
                Text(title).font(.jb(13)).foregroundStyle(JB.ink).lineLimit(1)
                Spacer(minLength: 4)
                Image(systemName: "chevron.down").font(.jb(9, .semibold)).foregroundStyle(JB.ink3)
            }
            .padding(.horizontal, 10).frame(height: 32)
            .jbField(focused: false)
        }
    }

    private func option(_ title: String, on: Bool, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack {
                Text(title)
                Spacer(minLength: 16)
                if on { Image(systemName: "checkmark").font(.jb(11, .semibold)).foregroundStyle(JB.accent) }
            }
        }
    }

    private func folderRow(_ f: String, title: String? = nil) -> some View {
        let sel = repo == f
        return Button { repo = f } label: {
            HStack(spacing: 10) {
                Image(systemName: sel ? "folder.fill" : "folder").font(.jb(13)).foregroundStyle(sel ? JB.accent : JB.ink3).frame(width: 18)
                Text(title ?? (f as NSString).lastPathComponent).font(.jb(13, .medium)).foregroundStyle(JB.ink).lineLimit(1)
                Text(f).font(.jb(11.5)).foregroundStyle(JB.ink3).lineLimit(1).truncationMode(.head)
                Spacer(minLength: 4)
                if sel { Image(systemName: "checkmark").font(.jb(11, .semibold)).foregroundStyle(JB.accent) }
            }
            .padding(.horizontal, 8).frame(height: 30)
            .jbHoverRow(selected: sel)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func field<C: View>(_ title: String, @ViewBuilder _ content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.jb(12, .semibold)).foregroundStyle(JB.ink2)
            content()
        }
    }

    private func shortPath(_ p: String) -> String {
        let parts = p.split(separator: "/")
        return parts.count > 2 ? "…/" + parts.suffix(2).joined(separator: "/") : p
    }

    private func relative(_ iso: String) -> String {
        guard let d = parseISO(iso) else { return "" }
        return d.formatted(.relative(presentation: .named))
    }

    private func load() async {
        hosts = await APIClient.shared.nightHosts().filter { $0.up != false || true }
        if let v: JSONValue = try? await APIClient.shared.getJSON("api/shared-memory/sessions", query: ["limit": "50"]) {
            sessions = (v["sessions"]?.arrayValue ?? []).map { s in
                Session(id: s["id"]?.stringValue ?? UUID().uuidString, repo: s["repo"]?.stringValue ?? "",
                        host: s["host"]?.stringValue ?? "", agent: s["agent"]?.stringValue ?? "",
                        summary: s["summary"]?.stringValue ?? "", branch: s["branch"]?.stringValue ?? "",
                        at: s["at"]?.stringValue ?? "", resumeID: s["resume_id"]?.stringValue ?? "")
            }
        }
        // Default to the computer you used last.
        host = sessions.first(where: { s in hosts.contains { $0.id == s.host } })?.host ?? hosts.first { $0.label == "Mac" }?.id ?? hosts.first?.id ?? ""
        repo = folders.first ?? ""
    }

    private func start(resume: String?) async {
        busy = true; error = nil
        defer { busy = false }
        var body: [String: Any] = ["repo": folder, "host": host, "agent": agent, "permission": permission]
        if resume == nil, !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { body["prompt"] = prompt }
        body["resume"] = resume ?? false
        do {
            let r = try await APIClient.shared.sendJSON("api/coding-agents/start", body: body)
            if (r["ok"] as? Bool) == false { error = r["detail"] as? String ?? "Couldn't start"; return }
            onStarted(); dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
