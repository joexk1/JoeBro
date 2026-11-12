import Foundation

// Night Shift (task queue + morning review) and Shared Memory (project facts every
// coding agent reads and writes). Shapes follow reports/feature-api.md. Everything is
// optional or defaulted so a backend that adds a field never breaks the screen.

struct NightDiffFile: Decodable, Hashable { let path: String; let added: Int?; let removed: Int? }
struct NightDiff: Decodable, Hashable {
    let files: Int?, insertions: Int?, deletions: Int?
    let fileList: [NightDiffFile]?
    enum CodingKeys: String, CodingKey { case files, insertions, deletions, fileList = "file_list" }
}
struct NightTests: Decodable, Hashable {
    let command: String?, passed: Bool?, durationS: Double?, outputTail: String?
    enum CodingKeys: String, CodingKey { case command, passed, durationS = "duration_s", outputTail = "output_tail" }
}
struct NightResult: Decodable, Hashable {
    let summary: String?, learned: [String]?, commits: Int?, durationS: Double?, costUSD: Double?
    let screenshot: Bool?, diff: NightDiff?, tests: NightTests?
    enum CodingKeys: String, CodingKey {
        case summary, learned, commits, screenshot, diff, tests
        case durationS = "duration_s", costUSD = "cost_usd"
    }
}
struct NightFollowup: Decodable, Hashable { let text: String? }

struct NightTask: Decodable, Identifiable, Hashable {
    let id: String
    let title: String?, repoName: String?, repo: String?, prompt: String?
    let agent: String?, model: String?, host: String?
    let status: String?, branch: String?, error: String?
    let windowStart: String?, windowEnd: String?, testCommand: String?, onApprove: String?
    let maxMinutes: Int?, position: Int?
    let followups: [NightFollowup]?
    let result: NightResult?
    enum CodingKeys: String, CodingKey {
        case id, title, repo, prompt, agent, model, host, status, branch, error, position, followups, result
        case repoName = "repo_name", windowStart = "window_start", windowEnd = "window_end"
        case testCommand = "test_command", onApprove = "on_approve", maxMinutes = "max_minutes"
    }
    var name: String { (title?.isEmpty == false ? title : nil) ?? String((prompt ?? "Task").prefix(48)) }
    var state: String { status ?? "queued" }
    var needsReview: Bool { state == "review" || (state == "failed" && result != nil) }
    var isOpen: Bool { state == "queued" || state == "running" }
    var agentLabel: String {
        switch agent ?? "any" { case "claude": "Claude Code"; case "codex": "Codex"; case "opencode": "OpenCode"; default: "Any agent" }
    }
}

struct NightConfig: Decodable, Hashable {
    var enabled: Bool?, windowStart: String?, windowEnd: String?, defaultAgent: String?
    enum CodingKeys: String, CodingKey { case enabled, windowStart = "window_start", windowEnd = "window_end", defaultAgent = "default_agent" }
}

struct MemProvenance: Decodable, Hashable { let agent: String?, model: String?, host: String?, source: String? }
struct MemFact: Decodable, Identifiable, Hashable {
    let id: String
    let project: String?, projectName: String?, scope: String?, topic: String?, kind: String?
    let text: String, status: String?
    let pinned: Bool?, strength: Double?, recalls: Int?
    let updatedAt: String?, createdAt: String?
    let provenance: MemProvenance?
    enum CodingKeys: String, CodingKey {
        case id, project, scope, topic, kind, text, status, pinned, strength, recalls, provenance
        case projectName = "project_name", updatedAt = "updated_at", createdAt = "created_at"
    }
    var isArchived: Bool { status == "archived" }
    var isPinned: Bool { pinned == true }
    var strengthValue: Double { min(max(strength ?? 0.5, 0), 1) }
    var kindLabel: String { (kind ?? "fact").capitalized }
    var kindSymbol: String {
        switch kind ?? "fact" {
        case "decision": "checkmark.seal"
        case "gotcha": "exclamationmark.triangle"
        case "preference": "heart"
        case "handoff": "arrow.right.circle"
        case "learned": "lightbulb"
        default: "text.quote"
        }
    }
    /// "Claude Code · opus · mac · MCP"
    var provenanceLine: String {
        guard let p = provenance else { return "" }
        let agent: String? = switch p.agent ?? "" {
        case "claude": "Claude Code"; case "codex": "Codex"; case "opencode": "OpenCode"
        case "app": "JoeBro app"; case "joebro": "JoeBro"; case "": nil; default: p.agent
        }
        let source: String? = switch p.source ?? "" {
        case "mcp": "MCP"; case "night_shift": "Night Shift"; case "consolidation": "consolidated"; case "app": nil; case "": nil; default: p.source
        }
        return [agent, p.model, p.host, source].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
    }
}
struct MemProject: Decodable, Identifiable, Hashable {
    let key: String, name: String?
    let active: Int?, archived: Int?
    var id: String { key }
    var label: String { key == "global" ? "All projects (global)" : (name ?? key) }
}

/// "3d ago" from an ISO timestamp; empty when it does not parse.
func relativeDateISO(_ iso: String?) -> String {
    guard let iso, !iso.isEmpty else { return "" }
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let g = ISO8601DateFormatter()
    let plain = DateFormatter(); plain.locale = Locale(identifier: "en_US_POSIX"); plain.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
    guard let d = f.date(from: iso) ?? g.date(from: iso) ?? plain.date(from: String(iso.prefix(19))) else { return "" }
    let out = RelativeDateTimeFormatter(); out.unitsStyle = .short
    return out.localizedString(for: d, relativeTo: .now)
}


extension APIClient {
    /// Route that resumes a coding session on a linked machine (start_coding_session).
    /// Not in reports/feature-api.md yet: change this one constant when the backend names it.
    static let resumeCodingRoute = "api/coding-agents/start"

    private struct TasksEnv: Decodable { let tasks: [NightTask]; let config: NightConfig? }
    private struct DiffEnv: Decodable { let diff: String? }
    private struct FactsEnv: Decodable { let facts: [MemFact] }
    private struct ProjectsEnv: Decodable { let projects: [MemProject] }
    private struct HistoryEnv: Decodable { let history: [MemFact]? }
    private func seg(_ id: String) -> String { id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(CharacterSet(charactersIn: "/?#%"))) ?? id }

    func nightShift() async throws -> (tasks: [NightTask], config: NightConfig?) {
        let r: TasksEnv = try await getJSON("api/night-shift"); return (r.tasks, r.config)
    }
    func nightAdd(_ body: [String: Any]) async throws { _ = try await sendJSON("api/night-shift", body: body) }
    func nightReorder(_ ids: [String]) async throws { _ = try await sendJSON("api/night-shift/reorder", body: ["ids": ids]) }
    func nightCancel(_ id: String) async throws { _ = try await sendJSON("api/night-shift/\(seg(id))/cancel", body: [:]) }
    func nightRun(_ id: String) async throws { _ = try await sendJSON("api/night-shift/\(seg(id))/run", body: [:]) }
    func nightConfig(enabled: Bool) async throws { _ = try await sendJSON("api/night-shift/config", method: "PUT", body: ["enabled": enabled]) }
    func nightDecide(_ id: String, decision: String, mode: String? = nil, text: String? = nil) async throws {
        var b: [String: Any] = ["decision": decision]
        if let mode { b["mode"] = mode }
        if let text { b["text"] = text }
        _ = try await sendJSON("api/night-shift/\(seg(id))/decide", body: b)
    }
    func nightDiff(_ id: String) async throws -> String {
        let r: DiffEnv = try await getJSON("api/night-shift/\(seg(id))/diff"); return r.diff ?? ""
    }
    func resumeCoding(_ run: AgentRun) async throws {
        _ = try await sendJSON(Self.resumeCodingRoute, body: ["resume": run.id, "session_id": run.sessionID, "title": run.title])
    }

    func memoryProjects() async throws -> [MemProject] { let r: ProjectsEnv = try await getJSON("api/shared-memory/projects"); return r.projects }
    func memoryFacts(project: String, status: String, query: String) async throws -> [MemFact] {
        var q = ["status": status, "limit": "200"]
        if project != "__all" { q["project"] = project }
        if !query.isEmpty { q["q"] = query }
        let r: FactsEnv = try await getJSON("api/shared-memory", query: q); return r.facts
    }
    func memoryHistory(_ id: String) async throws -> [MemFact] { let r: HistoryEnv = try await getJSON("api/shared-memory/\(seg(id))"); return r.history ?? [] }
    func memoryEdit(_ id: String, text: String, topic: String, kind: String) async throws {
        _ = try await sendJSON("api/shared-memory/\(seg(id))", method: "PUT", body: ["text": text, "topic": topic, "kind": kind])
    }
    func memoryPin(_ id: String, pinned: Bool) async throws { _ = try await sendJSON("api/shared-memory/\(seg(id))/pin", body: ["pinned": pinned]) }
    func memoryArchive(_ id: String) async throws { _ = try await sendJSON("api/shared-memory/\(seg(id))/archive", body: [:]) }
    func memoryRestore(_ id: String) async throws { _ = try await sendJSON("api/shared-memory/\(seg(id))/restore", body: [:]) }
    func memoryAdd(project: String, text: String, kind: String) async throws {
        var b: [String: Any] = ["text": text, "kind": kind, "source": "app", "agent": "app"]
        if project != "__all" { b["project"] = project }
        _ = try await sendJSON("api/shared-memory", body: b)
    }
}
