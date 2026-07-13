import Foundation
import SwiftUI

// MARK: - Sessions

struct ChatSessionInfo: Identifiable, Decodable, Hashable {
    let id: String
    var name: String
    var model: String?
    var archived: Bool?
    var folder: String?
    var createdAt: FlexibleString?
    var mode: String?
    var isImportant: Bool?
    var sortOrder: Double?

    enum CodingKeys: String, CodingKey {
        case id, name, model, archived, folder, mode
        case createdAt = "created_at"
        case isImportant = "is_important"
        case sortOrder = "sort_order"
    }
}

// MARK: - Models / endpoints

struct ModelsResponse: Decodable {
    var items: [ModelEndpointItem]
}

struct ModelEndpointItem: Decodable {
    var url: String
    var models: [String]
    var modelsDisplay: [String]?
    var modelsExtra: [String]?
    var modelsExtraDisplay: [String]?
    var endpointId: String?
    var endpointName: String?
    var category: String?
    var offline: Bool?

    enum CodingKeys: String, CodingKey {
        case url, models, category, offline
        case modelsDisplay = "models_display"
        case modelsExtra = "models_extra"
        case modelsExtraDisplay = "models_extra_display"
        case endpointId = "endpoint_id"
        case endpointName = "endpoint_name"
    }
}

/// One pickable model, flattened from the endpoint list.
struct ModelChoice: Identifiable, Hashable {
    var modelID: String
    var display: String
    var endpointName: String
    var endpointURL: String
    var endpointID: String
    var category: String
    var offline: Bool

    var id: String { modelID + "@" + endpointID }

    /// "(mac)" / "(pc)" style tag matching the web picker's _localTag().
    var localTag: String {
        let n = endpointName.lowercased()
        if n.contains("macbook") || n.contains(" m1") || n.contains(" m2") || n.contains(" m3") || n.contains(" m4") { return "mac" }
        if n.contains(" pc") || n.contains("rtx") || n.contains("gtx") { return "pc" }
        return ""
    }

    var displayWithTag: String {
        let tag = localTag
        return tag.isEmpty ? display : "\(display) (\(tag))"
    }
}

// MARK: - History

struct HistoryResponse: Decodable {
    var history: [HistoryMessage]
    var model: String?
    var name: String?
}

struct HistoryMessage: Decodable {
    var role: String
    var content: String
    var metadata: JSONValue?

    enum CodingKeys: String, CodingKey { case role, content, metadata }

    init(from decoder: Decoder) throws {
        // NEVER throw — one odd history entry must not blank the whole chat.
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else {
            role = "assistant"
            content = (try? decoder.singleValueContainer().decode(String.self)) ?? ""
            metadata = nil
            return
        }
        role = (try? c.decode(String.self, forKey: .role)) ?? "assistant"
        metadata = try? c.decode(JSONValue.self, forKey: .metadata)
        // content is usually a string, but vision turns hold a LIST of
        // content blocks ([{type: text|image_url, ...}]) — a strict String
        // decode here used to 86 the whole conversation.
        if let s = try? c.decode(String.self, forKey: .content) {
            content = s
        } else if let v = try? c.decode(JSONValue.self, forKey: .content) {
            if let blocks = v.arrayValue {
                content = blocks
                    .compactMap { $0["text"]?.stringValue ?? $0.stringValue }
                    .joined(separator: "\n")
            } else {
                content = v.displayText
            }
        } else {
            content = ""
        }
    }
}

// MARK: - Model providers (logo monograms)

struct ProviderBrand {
    let initial: String
    let color: Color

    /// Provider identity derived from the model id — the same heuristic
    /// the web UI uses for its inline provider icons.
    static func from(model: String) -> ProviderBrand {
        let m = model.lowercased()
        if m.contains("deepseek") { return .init(initial: "D", color: Color(red: 0.30, green: 0.42, blue: 1.0)) }
        if m.contains("glm") || m.contains("zai") { return .init(initial: "Z", color: Color(red: 0.36, green: 0.33, blue: 0.87)) }
        if m.contains("gpt") || m.contains("openai") || m.hasPrefix("o1") || m.hasPrefix("o3") || m.contains("oss") {
            return .init(initial: "O", color: Color(red: 0.10, green: 0.65, blue: 0.55)) }
        if m.contains("claude") { return .init(initial: "C", color: Color(red: 0.85, green: 0.47, blue: 0.34)) }
        if m.contains("gemini") || m.contains("gemma") { return .init(initial: "G", color: Color(red: 0.26, green: 0.52, blue: 0.96)) }
        if m.contains("qwen") { return .init(initial: "Q", color: Color(red: 0.55, green: 0.36, blue: 0.96)) }
        if m.contains("llama") { return .init(initial: "L", color: Color(red: 0.0, green: 0.50, blue: 0.95)) }
        if m.contains("mistral") || m.contains("mixtral") { return .init(initial: "M", color: Color(red: 0.99, green: 0.44, blue: 0.13)) }
        if m.contains("grok") { return .init(initial: "X", color: Color(white: 0.25)) }
        if m.contains("kimi") || m.contains("moonshot") { return .init(initial: "K", color: Color(red: 0.15, green: 0.15, blue: 0.2)) }
        return .init(initial: String(model.prefix(1)).uppercased(), color: Color.gray)
    }
}

// MARK: - Model endpoints (settings)

struct EndpointInfo: Decodable, Identifiable, Hashable {
    var id: String
    var name: String?
    var category: String?
    var baseUrl: String?
    var enabled: Bool?

    enum CodingKeys: String, CodingKey {
        case id, name, category, enabled
        case baseUrl = "base_url"
    }
}

// MARK: - Chat messages (UI model)

struct Attachment: Identifiable, Hashable {
    var id: String
    var name: String
    var mime: String
}

enum MessageKind: Equatable {
    case text
    case tool
    case compacted   // "Compacted chat" pill — older turns collapsed
}

struct WebSource: Hashable {
    var title: String
    var url: String
}

struct Message: Identifiable, Equatable {
    let id = UUID()
    var dbID: String?         // server-side message id (for unsend)
    var role: String          // "user" | "assistant" | "tool"
    var kind: MessageKind = .text
    var content: String
    // Tool rows (kind == .tool)
    var toolName: String?
    var command: String?
    var toolOutput: String?
    var exitCode: Int?
    var toolDone = false
    // Text extras
    var attachments: [Attachment] = []
    var webSources: [WebSource] = []
    var modelName: String?
    var metrics: String?
    var contextPercent: Double?
    var thinking: String?          // reasoning text (metadata on history, parsed live)
    var thinkingTime: Double?
    var isIntermediate = false     // agent round narration — collapsed by default
    var memoriesUsed: [String] = []
    var pluginsUsed: [String] = []
    var skillsUsed: [String] = []
    var isStreaming = false

    var isUser: Bool { role == "user" }
}

/// Strips raw tool-call artifacts the model narrates into its text —
/// the native equivalent of the web UI's stripToolBlocks(). Tool calls
/// get their own rows in the transcript, so the JSON blobs are noise.
func stripToolArtifacts(_ text: String) -> String {
    var s = text
    let patterns = [
        #"\[TOOL_CALL\][\s\S]*?(\[/TOOL_CALL\]|$)"#,
        #"```(?:web_search|read_file|write_file|create_document|edit_document|update_document|bash)\s*\n[\s\S]*?(```|$)"#,
        #"```(?:manage_documents|chat_with_model|create_session|list_sessions|send_to_session|pipeline|manage_session|ask_teacher|list_models|download_model|serve_model|list_served_models|stop_served_model|list_downloads|cancel_download|search_hf_models|list_cached_models|list_serve_presets|serve_preset|adopt_served_model|list_cookbook_servers|app_api)\s*\n[\s\S]*?(```|$)"#,
        #"<(?:\w+:)?(?:tool_call|function_call)>[\s\S]*?(</(?:\w+:)?(?:tool_call|function_call)>|$)"#,
        #"<invoke\s+name=['\"][^'\"]*['\"]>[\s\S]*?(</invoke>|$)"#,
        #"\(running\s+[\w_]+\)"#,
        // DeepSeek DSML tool-call markup (fullwidth ｜ pipes)
        #"<\s*[｜|]+\s*DSML\s*[｜|]+\s*tool_calls\s*>[\s\S]*?(<\s*/\s*[｜|]+\s*DSML\s*[｜|]+\s*tool_calls\s*>|$)"#,
        #"<\s*/?\s*[｜|]+\s*DSML\s*[｜|]+[^>]*>"#,
    ]
    for p in patterns {
        s = s.replacingOccurrences(of: p, with: "", options: .regularExpression)
    }
    // Drop paragraphs that are bare JSON objects (streamed tool arguments).
    // Fenced ```json blocks are untouched — they start with a backtick.
    let paragraphs = s.components(separatedBy: "\n\n").filter { para in
        let t = para.trimmingCharacters(in: .whitespacesAndNewlines)
        return !(t.hasPrefix("{") && t.hasSuffix("}") && t.contains("\""))
    }
    s = paragraphs.joined(separator: "\n\n")
    s = s.replacingOccurrences(of: #"\n{3,}"#, with: "\n\n", options: .regularExpression)
    return s.trimmingCharacters(in: .whitespacesAndNewlines)
}

let obsoleteBackendToolNames: Set<String> = [
    "manage_documents",
    "chat_with_model", "create_session", "list_sessions", "send_to_session", "pipeline",
    "manage_session", "ask_teacher", "list_models",
    "download_model", "serve_model", "list_served_models", "stop_served_model",
    "list_downloads", "cancel_download", "search_hf_models", "list_cached_models",
    "list_serve_presets", "serve_preset", "adopt_served_model", "list_cookbook_servers",
    "app_api", "api_call",
]

func isObsoleteBackendTool(_ raw: String?) -> Bool {
    obsoleteBackendToolNames.contains((raw ?? "").lowercased())
}

/// Splits a stored assistant message into text segments + tool rows, so
/// historical agent chats render their actions exactly like live ones.
/// The DB stores the model's raw narration (tool-call JSON inline); live
/// streams get real tool_start events, history has to reconstruct them.
func splitHistorySegments(_ content: String) -> [Message] {
    let patterns: [(pattern: String, nameGroup: Int?)] = [
        // DSML invoke blocks: <｜DSML｜invoke name="bash">…</｜DSML｜invoke>
        (#"<\s*[｜|]+\s*DSML\s*[｜|]+\s*invoke\s+name="([\w.-]+)"[\s\S]*?(?:<\s*/\s*[｜|]+\s*DSML\s*[｜|]+\s*invoke\s*>|$)"#, 1),
        (#"\(running\s+([\w_]+)\)\s*\n?\{[\s\S]*?\}(?=\s*(?:\n\n|\n\(running|$))"#, 1),
        (#"\(running\s+([\w_]+)\)"#, 1),
        (#"\[TOOL_CALL\][\s\S]*?(?:\[/TOOL_CALL\]|$)"#, nil),
        (#"```(web_search|read_file|write_file|create_document|edit_document|update_document|bash)\s*\n[\s\S]*?(?:```|$)"#, 1),
    ]

    var matches: [(range: NSRange, name: String, command: String)] = []
    let ns = content as NSString
    for (pattern, nameGroup) in patterns {
        guard let re = try? NSRegularExpression(pattern: pattern) else { continue }
        for m in re.matches(in: content, range: NSRange(location: 0, length: ns.length)) {
            // Skip if inside an already-claimed range (patterns overlap by design)
            if matches.contains(where: { NSIntersectionRange($0.range, m.range).length > 0 }) { continue }
            let block = ns.substring(with: m.range)
            var name = "tool"
            if let g = nameGroup, g < m.numberOfRanges, m.range(at: g).location != NSNotFound {
                name = ns.substring(with: m.range(at: g))
            } else if let nameMatch = block.range(of: #""(?:tool|name|action)"\s*:\s*"([\w_]+)""#, options: .regularExpression) {
                name = String(block[nameMatch]).components(separatedBy: "\"").dropLast(1).last ?? "tool"
            }
            if isObsoleteBackendTool(name) { continue }
            // Command preview: DSML parameter value when present, else the block body
            var command = block
            if let pr = block.range(of: #"parameter\s+name="command"[^>]*>"#, options: .regularExpression) {
                let after = String(block[pr.upperBound...])
                command = after.components(separatedBy: "<").first ?? after
            } else {
                command = block
                    .replacingOccurrences(of: #"^\(running\s+[\w_]+\)\s*"#, with: "", options: .regularExpression)
                    .replacingOccurrences(of: #"\[/?TOOL_CALL\]"#, with: "", options: .regularExpression)
                    .replacingOccurrences(of: #"<\s*/?\s*[｜|]+\s*DSML\s*[｜|]+[^>]*>"#, with: " ", options: .regularExpression)
            }
            command = command.trimmingCharacters(in: .whitespacesAndNewlines)
            matches.append((m.range, name, String(command.prefix(400))))
        }
    }
    matches.sort { $0.range.location < $1.range.location }

    var out: [Message] = []
    var cursor = 0
    func appendText(_ s: String) {
        let cleaned = stripToolArtifacts(s)
        guard !cleaned.isEmpty else { return }
        out.append(Message(role: "assistant", content: cleaned))
    }
    for m in matches {
        if m.range.location > cursor {
            appendText(ns.substring(with: NSRange(location: cursor, length: m.range.location - cursor)))
        }
        var row = Message(role: "tool", kind: .tool, content: "")
        row.toolName = m.name
        row.command = m.command
        row.toolDone = true
        out.append(row)
        cursor = max(cursor, m.range.location + m.range.length)
    }
    if cursor < ns.length {
        appendText(ns.substring(from: cursor))
    }
    if out.isEmpty {
        appendText(content)
    }
    return out
}

/// Decodes a value that may arrive as a string OR a number (e.g. created_at
/// is an ISO string on some rows and an epoch float on others).
struct FlexibleString: Decodable, Hashable {
    let value: String?

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if let s = try? c.decode(String.self) { value = s }
        else if let d = try? c.decode(Double.self) { value = String(d) }
        else { value = nil }
    }
}

// MARK: - Upload

struct UploadResponse: Decodable {
    var files: [UploadedFile]
}

struct UploadedFile: Decodable {
    var id: String
    var name: String
    var mime: String
    var size: Int?
}

// MARK: - Streaming events

enum StreamEvent {
    case delta(String)
    case thinkingDelta(String)
    case toolStart(tool: String, command: String)
    case toolOutput(tool: String, output: String, exitCode: Int?)
    case agentStep
    case modelInfo(String)
    case metrics(String, contextPercent: Double?)
    case webSources([WebSource])
    case docStreamOpen(title: String, language: String)
    case docStreamDelta(content: String)
    case docUpdate(id: String, title: String, language: String?, content: String, version: Int?)
    case docSuggestions(docID: String, suggestions: [DocSuggestion])
    case researchStarted(id: String)
    case memoriesUsed([String])
    case pluginsUsed([String])
    case skillsUsed([String])
    case permissionRequest(id: String, tool: String, command: String)
    case error(String)
    case done
}

/// A live deep-research run. Owned by AppStore so it outlives the panel view
/// (leaving the tab must never stop or restart it).
@Observable
final class ResearchRun: Identifiable {
    let id: String
    let query: String
    var phase = "planning"
    var message = ""
    var elapsed: TimeInterval = 0
    var avgDuration: Double?
    init(id: String, query: String) { self.id = id; self.query = query }
}

/// An active "allow this command?" prompt (Claude-Code style) in full mode.
struct PendingPermission: Identifiable {
    let id: String        // backend request_id
    let tool: String      // "bash" | "python"
    let command: String
}

/// Sentinel for the on-device Apple Intelligence model in the picker.
let appleIntelligenceID = "apple-intelligence"

extension ModelChoice {
    static let appleIntelligence = ModelChoice(
        modelID: appleIntelligenceID,
        display: "Apple Intelligence (basic, on-device)",
        endpointName: "On-device",
        endpointURL: "",
        endpointID: "apple",
        category: "local",
        offline: false)

    var isAppleIntelligence: Bool { modelID == appleIntelligenceID }
}

// MARK: - Editor (co-editing pane)

struct EditorDoc: Identifiable {
    var id: String
    var title: String
    var language: String
    var content: String
    var version: Int = 1
    var dirty = false
    var aiWriting = false          // AI is streaming into this doc
    var suggestions: [DocSuggestion] = []
    var pendingAIContent: String?  // AI edit awaiting user approval
    var localFileURL: URL?         // binary docs (PDFs) — rendered, not edited
    var localPath: String?         // bound-folder text file — saves write to disk
    var richDoc = false            // .doc/.docx — native rich-text editor owns load/save
    var docHeader: String?         // .docx header text (rendered around the page)
    var docFooter: String?         // .docx footer text
    var docFootnotes: String?      // .docx footnotes text (display-only band)
    var reloadNonce = 0            // bumped to force the rich .docx editor to re-read from disk after an AI edit
    var spreadsheet = false        // .csv/.xlsx — content is CSV, rendered as an editable grid
    var xlsxURL: URL?              // .xlsx-backed: saves convert CSV->xlsx and write here
    var flashcards = false         // .apkg — content is Q/A text, rendered as a deck
    var apkgURL: URL?              // .apkg-backed: saves convert text->apkg and write here
}

struct DocSuggestion: Identifiable, Hashable {
    var id: String
    var find: String
    var replace: String
    var reason: String
}

// MARK: - Workdir (chat-bound folder)

struct WorkdirInfo: Decodable {
    var workdir: String?
}

struct WorkdirListResponse: Decodable {
    var entries: [WorkdirEntry]
    var path: String?
}

struct WorkdirEntry: Decodable, Identifiable, Hashable {
    var name: String
    var type: String   // "dir" | "file"
    var size: Int?

    var id: String { name }
    var isDir: Bool { type == "dir" }
}

// MARK: - Workspace tabs

struct Plugin: Identifiable, Codable, Hashable {
    let id: String
    var name: String
    var kind: String
    var repoPath: String?
    var description: String?
    var isEnabled: Bool?
    var source: String?
    var permissionMode: String?

    enum CodingKeys: String, CodingKey {
        case id, name, kind, description, source
        case repoPath = "repo_path"
        case isEnabled = "is_enabled"
        case permissionMode = "permission_mode"
    }
}

struct PluginsResponse: Codable { var plugins: [Plugin] }

enum WorkspaceTab: String, CaseIterable, Identifiable {
    case chat, email, calendar, brain, notes, tasks, skills, tools, aiCheck, research

    var id: String { rawValue }

    var title: String {
        switch self {
        case .chat: return "Chats"
        case .email: return "Email"
        case .calendar: return "Calendar"
        case .brain: return "Brain"
        case .notes: return "Notes"
        case .tasks: return "Tasks"
        case .skills: return "Skills"
        case .tools: return "Tools"
        case .aiCheck: return "AI Check"
        case .research: return "Deep Research"
        }
    }

    var icon: String {
        switch self {
        case .chat: return "bubble.left.and.bubble.right"
        case .email: return "envelope"
        case .calendar: return "calendar"
        case .brain: return "brain.head.profile"
        case .notes: return "note.text"
        case .tasks: return "clock.badge.checkmark"
        case .skills: return "graduationcap"
        case .tools: return "wrench.and.screwdriver"
        case .aiCheck: return "checkmark.shield"
        case .research: return "antenna.radiowaves.left.and.right"
        }
    }
}

// MARK: - API Tools (custom callable APIs)

struct ToolsResponse: Decodable {
    var tools: [APITool]
}

struct APITool: Decodable, Identifiable, Hashable {
    var id: String
    var name: String
    var funcName: String?
    var baseURL: String?
    var hasKey: Bool?
    var method: String?
    var description: String?
    var isEnabled: Bool?

    enum CodingKeys: String, CodingKey {
        case id, name, method, description
        case funcName = "func_name"
        case baseURL = "base_url"
        case hasKey = "has_key"
        case isEnabled = "is_enabled"
    }
}

// MARK: - MCP servers (Model Context Protocol, stdio transport)

struct MCPServersResponse: Decodable {
    var servers: [MCPServer]
}

struct MCPServer: Decodable, Identifiable, Hashable {
    var id: String
    var name: String
    var command: String
    var args: String
    var enabled: Bool
    var tools: [String]
    var error: String?
    var permissionMode: String?

    enum CodingKeys: String, CodingKey {
        case id, name, command, args, enabled, tools, error
        case permissionMode = "permission_mode"
    }
}

// MARK: - Email

struct EmailListResponse: Decodable {
    var emails: [EmailSummary]
    var total: Int?
    var error: String?
}

struct EmailSummary: Decodable, Identifiable, Hashable {
    var uid: String
    var subject: String?
    var fromName: String?
    var fromAddress: String?
    var date: String?
    var isRead: Bool?
    var isFlagged: Bool?
    var hasAttachments: Bool?
    var cachedSummary: String?

    var id: String { uid }

    enum CodingKeys: String, CodingKey {
        case uid, subject, date
        case fromName = "from_name"
        case fromAddress = "from_address"
        case isRead = "is_read"
        case isFlagged = "is_flagged"
        case hasAttachments = "has_attachments"
        case cachedSummary = "cached_summary"
    }
}

struct EmailDetail: Decodable {
    var uid: String?
    var messageId: String?
    var subject: String?
    var fromName: String?
    var fromAddress: String?
    var to: String?
    var cc: String?
    var date: String?
    var references: String?
    var body: String?
    var bodyHtml: String?
    var attachments: [EmailAttachmentInfo]?
    var error: String?

    enum CodingKeys: String, CodingKey {
        case uid, subject, to, cc, date, body, references, attachments, error
        case messageId = "message_id"
        case fromName = "from_name"
        case fromAddress = "from_address"
        case bodyHtml = "body_html"
    }
}

struct EmailAttachmentInfo: Decodable, Identifiable, Hashable {
    var filename: String?
    var size: Int?
    var index: Int?
    var contentType: String?

    var id: String { "\(index ?? 0)-\(filename ?? "")" }

    enum CodingKeys: String, CodingKey {
        case filename, size, index
        case contentType = "content_type"
    }
}

struct FoldersResponse: Decodable {
    var folders: [String]
}

// MARK: - Calendar

struct EventsResponse: Decodable {
    var events: [CalEvent]
}

struct CalEvent: Decodable, Identifiable, Hashable {
    var uid: String
    var summary: String
    var dtstart: String
    var dtend: String
    var allDay: Bool?
    var location: String?
    var description: String?
    var calendarId: String?
    var color: String?

    var id: String { uid }

    enum CodingKeys: String, CodingKey {
        case uid, summary, dtstart, dtend, location, description, color
        case allDay = "all_day"
        case calendarId = "calendar_id"
    }

    var startDate: Date? { parseISO(dtstart) }
    var endDate: Date? { parseISO(dtend) }
}

struct EmailSourceInfo: Decodable, Identifiable, Hashable {
    var id: String
    var display: String?
    var type: String?
}

struct IntegrationStatus: Decodable {
    var configured: Bool
    var type: String?
    var display: String?
    var macosAuthorized: Bool?
    var sources: [EmailSourceInfo]?

    enum CodingKeys: String, CodingKey {
        case configured, type, display, sources
        case macosAuthorized = "macos_authorized"
    }
}

/// Backend dates come in several flavours: full ISO with/without Z,
/// or bare "yyyy-MM-dd" for all-day events.
func parseISO(_ s: String) -> Date? {
    let iso = ISO8601DateFormatter()
    iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let d = iso.date(from: s) { return d }
    iso.formatOptions = [.withInternetDateTime]
    if let d = iso.date(from: s) { return d }
    let df = DateFormatter()
    df.locale = Locale(identifier: "en_US_POSIX")
    for fmt in ["yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd"] {
        df.dateFormat = fmt
        if let d = df.date(from: s) { return d }
    }
    return nil
}

// MARK: - Memory (Brain)

struct LearnSkillsStatus: Decodable {
    var status: String
    var detail: String?
    var error: String?
    var skills: [String]?
}

struct MemoryResponse: Decodable {
    var memory: [MemoryEntry]
}

struct MemoryEntry: Decodable, Identifiable {
    var rawId: JSONValue?
    var text: String?
    var content: String?
    var category: String?
    var createdAt: String?
    var pinned: Bool?

    var id: String {
        if case .string(let s)? = rawId { return s }
        if case .number(let n)? = rawId { return String(Int(n)) }
        return text ?? UUID().uuidString
    }

    var displayText: String { text ?? content ?? "" }

    /// Stored category when meaningful; otherwise a keyword guess — the
    /// auto-extractor files most memories under "auto"/"general".
    /// ponytail: keyword buckets; swap for an LLM pass if these misfile too much
    var effectiveCategory: String {
        if let c = category?.lowercased(), !c.isEmpty, c != "auto", c != "general" { return c }
        let t = " " + displayText.lowercased() + " "
        if ["prefer", "likes ", "like ", "wants ", "style", "tone", "fan of", "avoid",
            "favourite", "favorite", "informal", "emoji"].contains(where: t.contains) { return "preference" }
        if ["build", "writing", "working on", "project", "essay", "site ", "app ",
            "server", "designing", "creating", "developing"].contains(where: t.contains) { return "project" }
        if ["friend", "mum ", "dad ", "brother", "sister", "wife", "girlfriend",
            "boyfriend", "partner", "colleague", " name is"].contains(where: t.contains) { return "person" }
        if ["meeting", "deadline", "appointment", "birthday", "event",
            "tomorrow", "next week"].contains(where: t.contains) { return "event" }
        if ["owns", "has a ", "has an ", "lives", "works at", "studies",
            "student", "is a "].contains(where: t.contains) { return "about" }
        return "fact"
    }

    enum CodingKeys: String, CodingKey {
        case rawId = "id"
        case text, content, category, pinned
        case createdAt = "created_at"
    }
}

// MARK: - Notes

struct NotesResponse: Decodable {
    var notes: [Note]
}

struct Note: Decodable, Identifiable {
    var id: String
    var title: String?
    var content: String?
    var items: [NoteChecklistItem]?
    var noteType: String?
    var color: String?
    var pinned: Bool?
    var dueDate: String?

    enum CodingKeys: String, CodingKey {
        case id, title, content, items, color, pinned
        case noteType = "note_type"
        case dueDate = "due_date"
    }
}

struct NoteChecklistItem: Decodable, Hashable {
    var text: String?
    var done: Bool?
}

// MARK: - Documents

struct DocsLibraryResponse: Decodable {
    var documents: [DocSummary]
    var total: Int?
}

struct DocSummary: Decodable, Identifiable, Hashable {
    var id: String
    var title: String?
    var language: String?
    var updatedAt: String?
    var versionCount: Int?
    var archived: Bool?

    enum CodingKeys: String, CodingKey {
        case id, title, language, archived
        case updatedAt = "updated_at"
        case versionCount = "version_count"
    }
}

struct DocDetail: Decodable {
    var id: String
    var title: String?
    var language: String?
    var currentContent: String?
    var versionCount: Int?

    enum CodingKeys: String, CodingKey {
        case id, title, language
        case currentContent = "current_content"
        case versionCount = "version_count"
    }
}

// MARK: - Personal files

struct PersonalFilesResponse: Decodable {
    var files: [PersonalFile]
}

struct PersonalFile: Decodable, Identifiable, Hashable {
    var name: String
    var size: Int?
    var path: String?

    var id: String { path ?? name }
}

// MARK: - Gallery

struct GalleryLibraryResponse: Decodable {
    var items: [GalleryItem]
    var total: Int?
}

struct GalleryItem: Decodable, Identifiable, Hashable {
    var id: String
    var filename: String?
    var prompt: String?
    var favorite: Bool?
    var createdAt: String?

    enum CodingKeys: String, CodingKey {
        case id, filename, prompt, favorite
        case createdAt = "created_at"
    }
}

// MARK: - Loose JSON (for message metadata etc.)

enum JSONValue: Decodable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else if let o = try? c.decode([String: JSONValue].self) { self = .object(o) }
        else { self = .null }
    }

    subscript(key: String) -> JSONValue? {
        if case .object(let o) = self { return o[key] }
        return nil
    }

    var stringValue: String? {
        if case .string(let s) = self { return s }
        return nil
    }

    var arrayValue: [JSONValue]? {
        if case .array(let a) = self { return a }
        return nil
    }

    var doubleValue: Double? {
        if case .number(let n) = self { return n }
        if case .string(let s) = self { return Double(s) }
        return nil
    }

    var intValue: Int? { doubleValue.map { Int($0) } }

    var boolValue: Bool? {
        if case .bool(let b) = self { return b }
        return nil
    }

    /// Best-effort display string for string-or-dict payload leaves.
    var displayText: String {
        if let s = stringValue { return s }
        for key in ["text", "headline", "summary", "title", "detail"] {
            if let s = self[key]?.stringValue { return s }
        }
        return ""
    }
}

// MARK: - AI Check (ZeroGPT)

struct AICheckResult: Decodable {
    var aiPercent: Double
    var humanPercent: Double?
    var aiWords: Int?
    var totalWords: Int?
    var flaggedSentences: [String]?
    var provider: String?

    enum CodingKeys: String, CodingKey {
        case aiPercent = "ai_percent"
        case humanPercent = "human_percent"
        case aiWords = "ai_words"
        case totalWords = "total_words"
        case flaggedSentences = "flagged_sentences"
        case provider
    }
}


// MARK: - Deep Research

struct ResearchLibraryResponse: Decodable {
    var research: [ResearchItem]
    var total: Int?
}

struct ResearchItem: Decodable, Identifiable, Hashable {
    var id: String
    var query: String
    var category: String?
    var sourceCount: Int?
    var status: String?
    var duration: FlexibleString?
    var completedAt: Double?

    enum CodingKeys: String, CodingKey {
        case id, query, category, status, duration
        case sourceCount = "source_count"
        case completedAt = "completed_at"
    }
}


// MARK: - Empty-segment detection (the "weird dashes" between tool rows)

/// True when stripped content is just punctuation/dash debris and should
/// not get its own card in the transcript.
func isDebrisContent(_ s: String) -> Bool {
    let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
    if t.isEmpty { return true }
    return t.range(of: #"^[\s\-—–_*•·.:,;>#`~|]+$"#, options: .regularExpression) != nil
}

// MARK: - Thinking extraction (<think>…</think> reasoning models)

struct ThinkingSplit {
    var thinking: String
    var body: String
    var inProgress: Bool   // unterminated <think> — model is still reasoning
}

func extractThinking(_ content: String) -> ThinkingSplit {
    let openTags = ["<think>", "<thinking>", "<｜think｜>"]
    let closeTags = ["</think>", "</thinking>", "</｜think｜>"]
    var body = content
    var thinkingParts: [String] = []
    var inProgress = false

    while true {
        let openings = openTags.compactMap { tag in body.range(of: tag).map { (tag, $0) } }
        guard let open = openings.min(by: { $0.1.lowerBound < $1.1.lowerBound }) else { break }
        let afterOpen = open.1.upperBound
        var bestClose: Range<String.Index>?
        for tag in closeTags {
            if let close = body.range(of: tag, range: afterOpen..<body.endIndex),
               bestClose == nil || close.lowerBound < bestClose!.lowerBound {
                bestClose = close
            }
        }
        if let close = bestClose {
            thinkingParts.append(String(body[afterOpen..<close.lowerBound]).trimmingCharacters(in: .whitespacesAndNewlines))
            body.removeSubrange(open.1.lowerBound..<close.upperBound)
        } else {
            thinkingParts.append(String(body[afterOpen...]).trimmingCharacters(in: .whitespacesAndNewlines))
            body.removeSubrange(open.1.lowerBound..<body.endIndex)
            inProgress = true
            break
        }
    }

    return ThinkingSplit(
        thinking: thinkingParts.filter { !$0.isEmpty }.joined(separator: "\n\n"),
        body: body.trimmingCharacters(in: .whitespacesAndNewlines),
        inProgress: inProgress)
}

// MARK: - Email documents (To/Subject/---/body shape)

struct EmailDocFields {
    var to = ""
    var cc = ""
    var subject = ""
    var inReplyTo = ""
    var references = ""
    var body = ""

    /// Parse the email-doc format: header lines, then ---, then body.
    static func parse(_ content: String) -> EmailDocFields? {
        guard content.range(of: #"(?im)^To:\s*"#, options: .regularExpression) != nil,
              content.range(of: #"(?im)^Subject:\s*"#, options: .regularExpression) != nil
        else { return nil }
        var f = EmailDocFields()
        let parts = content.components(separatedBy: "\n---\n")
        let header = parts[0]
        f.body = parts.count > 1 ? parts.dropFirst().joined(separator: "\n---\n") : ""
        for line in header.components(separatedBy: "\n") {
            let lower = line.lowercased()
            func val(_ prefix: String) -> String {
                String(line.dropFirst(prefix.count)).trimmingCharacters(in: .whitespaces)
            }
            if lower.hasPrefix("to:") { f.to = val("to:") }
            else if lower.hasPrefix("cc:") { f.cc = val("cc:") }
            else if lower.hasPrefix("subject:") { f.subject = val("subject:") }
            else if lower.hasPrefix("in-reply-to:") { f.inReplyTo = val("in-reply-to:") }
            else if lower.hasPrefix("references:") { f.references = val("references:") }
            else if !line.trimmingCharacters(in: .whitespaces).isEmpty && parts.count == 1 {
                // No --- separator: everything after headers is body
                f.body += (f.body.isEmpty ? "" : "\n") + line
            }
        }
        return f
    }

    func serialize() -> String {
        var head = "To: \(to)"
        if !cc.isEmpty { head += "\nCc: \(cc)" }
        head += "\nSubject: \(subject)"
        if !inReplyTo.isEmpty { head += "\nIn-Reply-To: \(inReplyTo)" }
        if !references.isEmpty { head += "\nReferences: \(references)" }
        return head + "\n---\n" + body
    }
}

// MARK: - Scheduled tasks

struct TasksResponse: Decodable {
    var tasks: [TaskItem]
}

struct TaskItem: Decodable, Identifiable, Hashable {
    var id: String
    var name: String?
    var prompt: String?
    var taskType: String?
    var schedule: String?
    var scheduledTime: String?
    var nextRun: String?
    var lastRun: String?
    var status: String?
    var permissionMode: String?
    var repeatDay: Int?

    enum CodingKeys: String, CodingKey {
        case id, name, prompt, schedule, status
        case taskType = "task_type"
        case scheduledTime = "scheduled_time"
        case nextRun = "next_run"
        case lastRun = "last_run"
        case permissionMode = "permission_mode"
        case repeatDay = "repeat_day"
    }
}

// MARK: - Filesystem browse (folder picker)

struct FSEntry: Identifiable, Hashable {
    var name: String
    var isDir: Bool
    var id: String { name }
}

// MARK: - Endpoint model visibility

struct EndpointModelInfo: Decodable, Identifiable {
    var id: String
    var display: String?
    var isHidden: Bool?

    enum CodingKeys: String, CodingKey {
        case id, display
        case isHidden = "is_hidden"
    }
}
