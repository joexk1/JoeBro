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
    @State private var customRepo = ""
    @State private var agent = "claude"
    @State private var busy = false
    @State private var error: String?

    private var folders: [String] {
        var seen = Set<String>()
        return sessions.filter { $0.host == host && !$0.repo.isEmpty && !$0.repo.hasPrefix("/tmp/") && !$0.repo.contains("/scratchpad") }.map(\.repo).filter { seen.insert($0).inserted }
    }
    private var folder: String { repo == "__other" ? customRepo.trimmingCharacters(in: .whitespaces) : repo }
    private var resumable: [Session] { sessions.filter { $0.host == host && $0.repo == folder && !$0.resumeID.isEmpty } }

    var body: some View {
        VStack(alignment: .leading, spacing: JB.s4) {
            Text("New coding agent").font(.jb(17, .semibold))

            field("Device") {
                Picker("", selection: $host) {
                    ForEach(hosts) { h in Text(h.up == false ? h.label + " (offline)" : h.label).tag(h.id) }
                }.labelsHidden()
            }
            field("Folder") {
                VStack(alignment: .leading, spacing: 6) {
                    Picker("", selection: $repo) {
                        ForEach(folders, id: \.self) { f in Text(shortPath(f)).tag(f) }
                        Text("Other folder…").tag("__other")
                    }.labelsHidden()
                    if repo == "__other" {
                        TextField("/path/to/project", text: $customRepo).textFieldStyle(.roundedBorder).font(.jb(13))
                    }
                }
            }
            field("Agent") {
                Picker("", selection: $agent) {
                    Text("Claude Code").tag("claude"); Text("Codex").tag("codex"); Text("OpenCode").tag("opencode")
                }.pickerStyle(.segmented).labelsHidden()
            }

            Button { Task { await start(resume: nil) } } label: {
                Label("Start a new session", systemImage: "plus").frame(maxWidth: .infinity)
            }
            .buttonStyle(.jbPrimary)
            .disabled(busy || host.isEmpty || folder.isEmpty)

            if !resumable.isEmpty {
                Text("Resume").font(.jb(12, .semibold)).foregroundStyle(JB.ink2)
                ScrollView {
                    VStack(spacing: 4) {
                        ForEach(resumable) { s in
                            Button { Task { await start(resume: s.resumeID) } } label: {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(s.summary.isEmpty ? "Session" : s.summary).font(.jb(13, .medium)).foregroundStyle(JB.ink).lineLimit(2)
                                    Text([s.agent, s.branch, relative(s.at)].filter { !$0.isEmpty }.joined(separator: " · "))
                                        .font(.jb(11.5)).foregroundStyle(JB.ink3)
                                }
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.horizontal, 10).padding(.vertical, 8)
                                .jbHoverRow(selected: false)
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .disabled(busy)
                        }
                    }
                }
                .frame(maxHeight: 220)
            }

            if let error { Text(error).font(.jb(12)).foregroundStyle(JB.danger) }
            HStack {
                if busy { JBSpinner() }
                Spacer()
                Button("Cancel") { dismiss() }.buttonStyle(.jbGhost).keyboardShortcut(.cancelAction)
            }
        }
        .padding(JB.s5)
        .frame(width: 440)
        .task { await load() }
        .onChange(of: host) { repo = folders.first ?? "__other" }
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
        repo = folders.first ?? "__other"
    }

    private func start(resume: String?) async {
        busy = true; error = nil
        defer { busy = false }
        var body: [String: Any] = ["repo": folder, "host": host, "agent": agent]
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
