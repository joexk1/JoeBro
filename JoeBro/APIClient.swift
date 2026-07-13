import Foundation

enum APIError: LocalizedError {
    case http(Int, String)
    case badResponse

    var errorDescription: String? {
        switch self {
        case .http(let code, let detail): return detail.isEmpty ? "HTTP \(code)" : detail
        case .badResponse: return "Unexpected server response"
        }
    }
}

/// Thin async client for the bundled local JoeBro backend.
final class APIClient {
    static let shared = APIClient()

    var baseURL: URL {
        let raw = UserDefaults.standard.string(forKey: "serverURL") ?? "http://127.0.0.1:8765"
        return URL(string: raw) ?? URL(string: "http://127.0.0.1:8765")!
    }

    private let session: URLSession = {
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 120
        cfg.timeoutIntervalForResource = 600
        // Default is 6/host. A slow IMAP fetch or an open chat SSE stream would
        // otherwise queue the quick data calls (sessions/models/history) behind
        // them, so the whole app appears to "not load". Give the fast loads headroom.
        cfg.httpMaximumConnectionsPerHost = 12
        cfg.httpCookieStorage = .shared
        // Never serve API responses from cache. The backend is live state
        // (models, sessions, email); URLSession's default heuristic caching was
        // serving a stale empty /api/models from before endpoints were configured,
        // leaving the model picker permanently empty until the cache expired.
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        cfg.urlCache = nil
        return URLSession(configuration: cfg)
    }()

    private func request(_ path: String, method: String = "GET") -> URLRequest {
        // appendingPathComponent percent-encodes "?" into the PATH (…/sessions%3Fdevice=…),
        // which the backend can't route — "Sessions: not found". Split off any query
        // string and attach it as a real query instead.
        let url: URL
        if let q = path.firstIndex(of: "?") {
            let base = baseURL.appendingPathComponent(String(path[..<q]))
            var comps = URLComponents(url: base, resolvingAgainstBaseURL: false)
            comps?.percentEncodedQuery = String(path[path.index(after: q)...])
            url = comps?.url ?? base
        } else {
            url = baseURL.appendingPathComponent(path)
        }
        var req = URLRequest(url: url)
        req.httpMethod = method
        // Minutes west of UTC, matching JS getTimezoneOffset() — backend
        // uses this so calendar/notes tools work in the user's timezone.
        req.setValue(String(-TimeZone.current.secondsFromGMT() / 60), forHTTPHeaderField: "X-TZ-Offset")
        return req
    }

    private func check(_ data: Data, _ resp: URLResponse) throws {
        guard let http = resp as? HTTPURLResponse else { throw APIError.badResponse }
        guard (200..<300).contains(http.statusCode) else {
            var detail = ""
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let d = (obj["detail"] as? String) ?? (obj["error"] as? String) {
                detail = d
            }
            throw APIError.http(http.statusCode, detail)
        }
    }

    // MARK: Sessions

    func sessions() async throws -> [ChatSessionInfo] {
        let (data, resp) = try await session.data(for: request("api/sessions"))
        try check(data, resp)
        return try JSONDecoder().decode([ChatSessionInfo].self, from: data)
    }

    func createSession(name: String, model: String, endpointURL: String, endpointID: String) async throws -> ChatSessionInfo {
        var req = request("api/session", method: "POST")
        req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        req.httpBody = formEncode([
            "name": name,
            "model": model,
            "endpoint_url": endpointURL,
            "endpoint_id": endpointID,
            "skip_validation": "true",
        ])
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
        return try JSONDecoder().decode(ChatSessionInfo.self, from: data)
    }

    func deleteSession(_ id: String) async throws {
        let (data, resp) = try await session.data(for: request("api/session/\(id)", method: "DELETE"))
        try check(data, resp)
    }

    func renameSession(_ id: String, name: String) async throws {
        var req = request("api/session/\(id)", method: "PATCH")
        req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        req.httpBody = formEncode(["name": name])
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
    }

    func setSessionMode(_ id: String, mode: String) async throws {
        var req = request("api/session/\(id)", method: "PATCH")
        req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        req.httpBody = formEncode(["mode": mode])
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
    }

    /// Set (or, with an empty string, clear) the project folder a chat is
    /// grouped under in the sidebar.
    func setSessionFolder(_ id: String, folder: String) async throws {
        var req = request("api/session/\(id)", method: "PATCH")
        req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        req.httpBody = formEncode(["folder": folder])
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
    }

    /// Drag-to-reorder / move a chat into or out of a project folder. Sends an
    /// empty folder to ungroup it. sortOrder positions it in the sidebar.
    func moveSession(_ id: String, folder: String, sortOrder: Double) async throws {
        var req = request("api/session/\(id)/move", method: "POST")
        req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        req.httpBody = formEncode(["folder": folder, "sort_order": String(sortOrder)])
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
    }

    /// Rename a project folder (re-tags every chat under the old name).
    func renameFolder(old: String, new: String) async throws {
        var req = request("api/folder/rename", method: "POST")
        req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        req.httpBody = formEncode(["old": old, "new": new])
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
    }

    /// Manual compaction — the server summarizes older messages into one
    /// compacted history entry, keeping a recent tail verbatim.
    func compactSession(_ id: String) async throws -> [String: Any] {
        try await sendJSON("api/session/\(id)/compact")
    }

    func deleteDocument(id: String) async {
        _ = try? await sendJSON("api/document/\(id)", method: "DELETE")
    }

    // MARK: AI check (ZeroGPT)

    func aiCheck(text: String) async throws -> AICheckResult {
        var req = request("api/ai-check", method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["text": text])
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
        return try JSONDecoder().decode(AICheckResult.self, from: data)
    }

    // MARK: Local integrations

    func emailStatus() async throws -> IntegrationStatus {
        try await getJSON("api/email/status")
    }
    func removeEmailSource(id: String) async throws {
        try await sendJSON("api/email/disconnect?id=\(id)")
    }

    func calendarStatus() async throws -> IntegrationStatus {
        try await getJSON("api/calendar/status")
    }

    func connectIMAP(email: String, imapHost: String, imapPort: Int, imapPassword: String,
                     smtpHost: String, smtpPort: Int, smtpPassword: String) async throws {
        try await sendJSON("api/email/connect-imap", body: [
            "email": email,
            "imap_host": imapHost,
            "imap_port": imapPort,
            "imap_user": email,
            "imap_password": imapPassword,
            "smtp_host": smtpHost,
            "smtp_port": smtpPort,
            "smtp_user": email,
            "smtp_password": smtpPassword,
        ])
    }

    func disconnectEmail() async throws {
        try await sendJSON("api/email/disconnect")
    }

    func connectCalDAV(url: String, principal: String, password: String) async throws {
        try await sendJSON("api/calendar/connect-caldav", body: [
            "url": url,
            "principal": principal,
            "password": password,
        ])
    }

    func connectMacOSCalendar(authorized: Bool) async throws {
        try await sendJSON("api/calendar/connect-macos", body: ["authorized": authorized])
    }

    func disconnectCalendar() async throws {
        try await sendJSON("api/calendar/disconnect")
    }

    // MARK: Pin / important

    func setSessionImportant(_ id: String, important: Bool) async throws {
        try await postForm("api/session/\(id)/important",
                           fields: ["important": important ? "true" : "false"])
    }

    // MARK: Model endpoints (settings)

    func modelEndpoints() async throws -> [EndpointInfo] {
        // Lenient decode — the endpoint list mixes probe results of varying
        // shapes (latency, status, errors), so strict Codable kept failing.
        let v: JSONValue = try await getJSON("api/model-endpoints")
        // The route returns a BARE array (not {"endpoints": [...]}).
        let rows = v.arrayValue ?? v["endpoints"]?.arrayValue ?? []
        return rows.compactMap { e in
            guard let id = e["id"]?.stringValue else { return nil }
            let host = (e["base_url"]?.stringValue).flatMap { URL(string: $0)?.host }
            return EndpointInfo(
                id: id,
                name: e["name"]?.stringValue,
                category: host ?? e["model_type"]?.stringValue,
                baseUrl: e["base_url"]?.stringValue,
                enabled: e["is_enabled"]?.boolValue)
        }
    }

    func addModelEndpoint(name: String, baseURL: String, apiKey: String) async throws {
        try await postForm("api/model-endpoints", fields: [
            "name": name,
            "base_url": baseURL,
            "api_key": apiKey,
            "skip_probe": "false",
        ])
    }

    func deleteModelEndpoint(id: String) async throws {
        try await sendJSON("api/model-endpoints/\(id)", method: "DELETE")
    }

    // MARK: Endpoint model visibility + defaults

    func endpointModels(epID: String) async throws -> [EndpointModelInfo] {
        try await getJSON("api/model-endpoints/\(epID)/models")
    }

    func setHiddenModels(epID: String, hidden: [String]) async throws {
        try await sendJSON("api/model-endpoints/\(epID)/models", method: "PATCH",
                           body: ["hidden": hidden])
    }

    func getPrefs() async throws -> JSONValue {
        try await getJSON("api/prefs")
    }

    func setPref(_ key: String, value: Any?) async {
        _ = try? await sendJSON("api/prefs/\(key)", method: "PUT", body: ["value": value ?? NSNull()])
    }

    // MARK: Memory edit

    func updateMemory(id: String, text: String, category: String) async throws {
        try await postFormMethod("api/memory/\(id)", method: "PUT",
                                 fields: ["text": text, "category": category])
    }

    private func postFormMethod(_ path: String, method: String, fields: [String: String]) async throws {
        var req = request(path, method: method)
        req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        req.httpBody = formEncode(fields)
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
    }

    @discardableResult
    private func postFormReturning(_ path: String, method: String, fields: [String: String]) async throws -> [String: Any] {
        var req = request(path, method: method)
        req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        req.httpBody = formEncode(fields)
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    // MARK: Custom API tools

    func apiTools() async throws -> [APITool] {
        let r: ToolsResponse = try await getJSON("api/tools")
        return r.tools
    }

    func createAPITool(name: String, baseURL: String, apiKey: String,
                       method: String, description: String) async throws {
        try await postForm("api/tools", fields: [
            "name": name,
            "base_url": baseURL,
            "api_key": apiKey,
            "method": method,
            "description": description,
        ])
    }

    func updateAPITool(id: String, fields: [String: String]) async throws {
        try await postFormMethod("api/tools/\(id)", method: "PUT", fields: fields)
    }

    func deleteAPITool(id: String) async throws {
        try await sendJSON("api/tools/\(id)", method: "DELETE")
    }

    // MARK: Plugins

    func plugins() async throws -> [Plugin] {
        let r: PluginsResponse = try await getJSON("api/plugins")
        return r.plugins
    }
    func createPlugin(name: String, kind: String, repoPath: String, description: String) async throws {
        try await postForm("api/plugins", fields: [
            "name": name, "kind": kind, "repo_path": repoPath, "description": description,
        ])
    }
    func updatePlugin(id: String, fields: [String: String]) async throws {
        try await postFormMethod("api/plugins/\(id)", method: "PUT", fields: fields)
    }
    func deletePlugin(id: String) async throws {
        try await sendJSON("api/plugins/\(id)", method: "DELETE")
    }

    // MARK: MCP servers (Model Context Protocol)

    func mcpServers() async throws -> [MCPServer] {
        let r: MCPServersResponse = try await getJSON("api/mcp")
        return r.servers
    }

    /// Create an MCP server. The backend attempts discovery (with a hard timeout)
    /// and returns the server with its discovered tools (or an error string).
    @discardableResult
    func createMCPServer(name: String, command: String, args: String, permission: String = "sandbox") async throws -> MCPServer? {
        let raw = try await postForm("api/mcp", fields: [
            "name": name, "command": command, "args": args, "permission_mode": permission,
        ])
        return decodeServer(raw)
    }

    @discardableResult
    func updateMCPServer(id: String, fields: [String: String]) async throws -> MCPServer? {
        let raw = try await postFormReturning("api/mcp/\(id)", method: "PUT", fields: fields)
        return decodeServer(raw)
    }

    func deleteMCPServer(id: String) async throws {
        try await sendJSON("api/mcp/\(id)", method: "DELETE")
    }

    private func decodeServer(_ raw: [String: Any]) -> MCPServer? {
        guard let server = raw["server"],
              let data = try? JSONSerialization.data(withJSONObject: server) else { return nil }
        return try? JSONDecoder().decode(MCPServer.self, from: data)
    }

    // MARK: Scheduled tasks

    func tasks() async throws -> [TaskItem] {
        let r: TasksResponse = try await getJSON("api/tasks")
        return r.tasks
    }

    func createTask(name: String, prompt: String, schedule: String, time: String, permissionMode: String = "sandbox", repeatDay: Int = 0) async throws {
        try await sendJSON("api/tasks", body: [
            "name": name,
            "prompt": prompt,
            "task_type": "llm",
            "schedule": schedule,
            "scheduled_time": time,
            "permission_mode": permissionMode,
            "repeat_day": repeatDay,
            "trigger_type": "schedule",
            "output_target": "session",
        ])
    }

    func updateTask(id: String, name: String, prompt: String, schedule: String, time: String, permissionMode: String = "sandbox", repeatDay: Int = 0) async throws {
        try await sendJSON("api/tasks/\(id)", method: "PUT", body: [
            "name": name, "prompt": prompt,
            "schedule": schedule, "scheduled_time": time,
            "permission_mode": permissionMode,
            "repeat_day": repeatDay,
        ])
    }

    func taskAction(_ id: String, _ action: String) async {
        // action: pause | resume
        _ = try? await sendJSON("api/tasks/\(id)/\(action)")
    }

    /// Answer a command-permission prompt: "allow" | "deny" | "always".
    func respondPermission(_ requestID: String, decision: String) async {
        _ = try? await sendJSON("api/permission/\(requestID)", body: ["decision": decision])
    }

    /// Run a saved task immediately and return the model's result text.
    func runTask(_ id: String) async throws -> String {
        let r = try await sendJSON("api/tasks/\(id)/run")
        return (r["result"] as? String) ?? "No result."
    }

    func deleteTask(_ id: String) async {
        _ = try? await sendJSON("api/tasks/\(id)", method: "DELETE")
    }

    // MARK: Filesystem browse (folder picker)

    func fsBrowse(path: String, device: String?) async throws -> (path: String, entries: [FSEntry]) {
        var q: [String: String] = [:]
        if !path.isEmpty { q["path"] = path }
        if let device, !device.isEmpty { q["device"] = device }
        let v: JSONValue = try await getJSON("api/fs/browse", query: q)
        let entries = (v["entries"]?.arrayValue ?? []).compactMap { e -> FSEntry? in
            guard let name = e["name"]?.stringValue else { return nil }
            let isDir = e["type"]?.stringValue == "dir" || e["is_dir"]?.boolValue == true
            return FSEntry(name: name, isDir: isDir)
        }
        return (v["path"]?.stringValue ?? path, entries)
    }

    func bindWorkdir(sid: String, path: String, device: String?) async throws {
        var body: [String: Any] = ["workdir": path]
        if let device, !device.isEmpty { body["device"] = device }
        try await sendJSON("api/session/\(sid)/workdir", method: "PUT", body: body)
    }

    /// Raw file bytes from the bound folder (for PDFs/images QuickLook).
    func downloadWorkdirFile(sid: String, sub: String, filename: String) async throws -> URL {
        try await downloadToTemp("api/session/\(sid)/workdir/raw", query: ["sub": sub], filename: filename)
    }

    /// Header / footer / footnote text of a .docx (NSAttributedString skips them).
    func docxExtras(sid: String, sub: String) async throws -> (header: String, footer: String, footnotes: String) {
        let v: JSONValue = try await getJSON("api/session/\(sid)/workdir/docx-extras", query: ["sub": sub])
        return (v["header"]?.stringValue ?? "",
                v["footer"]?.stringValue ?? "",
                v["footnotes"]?.stringValue ?? "")
    }

    func updateDocxExtra(sid: String, sub: String, region: String, content: String) async throws {
        var req = request("api/session/\(sid)/workdir/docx-extra", method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: [
            "sub": sub,
            "region": region,
            "content": content,
        ])
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
    }

    // MARK: Calendar quick-add (natural language)

    func quickParseEvent(text: String) async throws -> JSONValue {
        var req = request("api/calendar/quick-parse", method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: [
            "text": text, "tz": TimeZone.current.identifier,
        ])
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
        return try JSONDecoder().decode(JSONValue.self, from: data)
    }

    func createEventRaw(_ fields: [String: Any]) async throws {
        try await sendJSON("api/calendar/events", body: fields)
    }

    // MARK: Deep research

    func researchStart(query: String, endpointID: String? = nil, model: String? = nil) async throws -> String {
        var body: [String: Any] = ["query": query, "max_rounds": 0]
        if let endpointID { body["endpoint_id"] = endpointID }
        if let model { body["model"] = model }
        let r = try await sendJSON("api/research/start", body: body)
        guard let sid = r["session_id"] as? String else { throw APIError.badResponse }
        return sid
    }

    func researchActive() async throws -> [(id: String, query: String)] {
        let v: JSONValue = try await getJSON("api/research/active")
        return (v["active"]?.arrayValue ?? []).compactMap { e in
            guard let sid = e["session_id"]?.stringValue else { return nil }
            return (sid, e["query"]?.stringValue ?? "")
        }
    }

    func ping() async -> Double? {
        let t0 = Date()
        guard let (_, resp) = try? await session.data(for: request("api/ping")),
              (resp as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return Date().timeIntervalSince(t0)
    }

    func emailUnreadTotal() async -> Int? {
        guard let r = try? await emailList(folder: "INBOX", filter: "unread", limit: 1),
              r.error == nil else { return nil }
        return r.total ?? r.emails.count
    }

    /// Switch an existing session's model mid-chat (same PATCH the web uses).
    func updateSessionModel(_ id: String, model: String, endpointURL: String, endpointID: String) async throws {
        var req = request("api/session/\(id)", method: "PATCH")
        req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        req.httpBody = formEncode([
            "model": model, "endpoint_url": endpointURL, "endpoint_id": endpointID,
        ])
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
    }

    func deleteMessages(sessionID: String, ids: [String]) async throws {
        try await sendJSON("api/session/\(sessionID)/delete-messages", body: ["msg_ids": ids])
    }

    func appendMessage(sessionID: String, role: String, content: String, metadata: [String: Any]? = nil) async {
        var body: [String: Any] = ["role": role, "content": content]
        if let metadata { body["metadata"] = metadata }
        _ = try? await sendJSON("api/session/\(sessionID)/message", body: body)
    }

    func researchStatus(id: String) async throws -> JSONValue {
        try await getJSON("api/research/status/\(id)")
    }

    func researchCancel(id: String) async {
        _ = try? await sendJSON("api/research/cancel/\(id)")
    }

    func researchLibrary() async throws -> [ResearchItem] {
        let r: ResearchLibraryResponse = try await getJSON("api/research/library")
        return r.research
    }

    func researchDetail(id: String) async throws -> JSONValue {
        try await getJSON("api/research/detail/\(id)")
    }

    func researchArchive(id: String) async {
        _ = try? await sendJSON("api/research/\(id)/archive?archived=true")
    }

    func researchDelete(id: String) async {
        _ = try? await sendJSON("api/research/\(id)", method: "DELETE")
    }

    func archiveSession(_ id: String) async throws {
        let (data, resp) = try await session.data(for: request("api/session/\(id)/archive", method: "POST"))
        try check(data, resp)
    }

    func history(_ id: String) async throws -> HistoryResponse {
        let (data, resp) = try await session.data(for: request("api/history/\(id)"))
        try check(data, resp)
        return try JSONDecoder().decode(HistoryResponse.self, from: data)
    }

    /// Cheap freshness probe — the id of the latest message in a session. The
    /// open-chat poller compares this to decide whether to reload full history.
    func sessionTip(_ id: String) async throws -> (lastID: Int, mode: String, model: String) {
        let (data, resp) = try await session.data(for: request("api/session/\(id)/tip"))
        try check(data, resp)
        let obj = ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any]) ?? [:]
        let lastID = (obj["last_id"] as? Int) ?? (obj["last_id"] as? NSNumber)?.intValue ?? 0
        return (lastID, obj["mode"] as? String ?? "", obj["model"] as? String ?? "")
    }

    // MARK: Models

    func models() async throws -> [ModelChoice] {
        let (data, resp) = try await session.data(for: request("api/models"))
        try check(data, resp)
        let parsed = try JSONDecoder().decode(ModelsResponse.self, from: data)
        var out: [ModelChoice] = []
        for item in parsed.items {
            let epName = item.endpointName ?? "Endpoint"
            let epID = item.endpointId ?? item.url
            let allIDs = item.models + (item.modelsExtra ?? [])
            let allDisplay = (item.modelsDisplay ?? item.models) + (item.modelsExtraDisplay ?? item.modelsExtra ?? [])
            for (i, mid) in allIDs.enumerated() {
                out.append(ModelChoice(
                    modelID: mid,
                    display: i < allDisplay.count ? allDisplay[i] : mid,
                    endpointName: epName,
                    endpointURL: item.url,
                    endpointID: epID,
                    category: item.category ?? "cloud",
                    offline: item.offline ?? false
                ))
            }
        }
        return out
    }

    // MARK: Chat streaming

    func chatStream(
        message: String,
        sessionID: String,
        agentMode: Bool,
        useWeb: Bool,
        thinking: Bool?,
        attachmentIDs: [String],
        allowBash: Bool = false,
        permissionMode: String = "sandbox",
        askPermission: Bool = false,
        activeDocID: String? = nil,
        openDocIDs: [String] = [],
        model: String? = nil,
        endpointID: String? = nil,
        endpointURL: String? = nil,
        effort: String? = nil,
        maxToolCalls: Int = 0
    ) -> AsyncThrowingStream<StreamEvent, Error> {
        var req = request("api/chat_stream", method: "POST")
        req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        var fields: [String: String] = [
            "message": message,
            "session": sessionID,
            "mode": agentMode ? "agent" : "chat",
            "permission_mode": permissionMode,
        ]
        fields.merge(memoryRetrievalFields(for: message)) { _, new in new }
        if allowBash { fields["allow_bash"] = "true" }
        if askPermission { fields["ask_permission"] = "true" }
        if let activeDocID, !activeDocID.isEmpty { fields["active_doc_id"] = activeDocID }
        // Open editor tabs (ids + titles): edits to these are approved via the
        // in-editor diff banner, not the blocking permission prompt.
        if !openDocIDs.isEmpty { fields["open_docs"] = openDocIDs.joined(separator: "\n") }
        if let model, !model.isEmpty { fields["model"] = model }
        if let endpointID, !endpointID.isEmpty { fields["endpoint_id"] = endpointID }
        if let endpointURL, !endpointURL.isEmpty { fields["endpoint_url"] = endpointURL }
        fields["max_tokens"] = "8192"
        if let effort { fields["effort"] = effort }
        if maxToolCalls > 0 { fields["max_tool_calls"] = String(maxToolCalls) }
        // Backend semantics: in agent mode the toggle enables the web_search TOOL;
        // in chat mode it pre-fetches results into context.
        if useWeb {
            fields[agentMode ? "allow_web_search" : "use_web"] = "true"
        }
        if let thinking { fields["thinking"] = thinking ? "true" : "false" }
        if !attachmentIDs.isEmpty,
           let json = try? JSONSerialization.data(withJSONObject: attachmentIDs),
           let s = String(data: json, encoding: .utf8) {
            fields["attachments"] = s
        }
        req.httpBody = formEncode(fields)

        let urlSession = self.session
        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let (bytes, resp) = try await urlSession.bytes(for: req)
                    guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                        var detail = ""
                        for try await line in bytes.lines.prefix(6) { detail += line }
                        if let d = detail.data(using: .utf8),
                           let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
                            detail = (obj["detail"] as? String) ?? (obj["error"] as? String) ?? detail
                        }
                        throw APIError.http((resp as? HTTPURLResponse)?.statusCode ?? 0,
                                            detail.isEmpty ? "Stream failed" : String(detail.prefix(300)))
                    }
                    var sawError = false
                    for try await line in bytes.lines {
                        if line.hasPrefix("event: error") { sawError = true; continue }
                        guard line.hasPrefix("data: ") else { continue }
                        let payload = String(line.dropFirst(6))
                        if payload == "[DONE]" {
                            continuation.yield(.done)
                            break
                        }
                        guard let d = payload.data(using: .utf8),
                              let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any]
                        else { continue }
                        if sawError {
                            sawError = false
                            let msg = (obj["error"] as? String) ?? (obj["detail"] as? String) ?? payload
                            continuation.yield(.error(msg))
                            continue
                        }
                        if let delta = obj["delta"] as? String {
                            // Reasoning models stream thinking as flagged
                            // deltas — route them to the dropdown, not the reply
                            if obj["thinking"] as? Bool == true || obj["thinking"] as? Int == 1 {
                                continuation.yield(.thinkingDelta(delta))
                            } else {
                                continuation.yield(.delta(delta))
                            }
                        } else if let type = obj["type"] as? String {
                            switch type {
                            case "model_info":
                                if let m = obj["model"] as? String { continuation.yield(.modelInfo(m)) }
                            case "tool_start":
                                continuation.yield(.toolStart(
                                    tool: obj["tool"] as? String ?? "tool",
                                    command: obj["command"] as? String ?? ""))
                            case "tool_output":
                                continuation.yield(.toolOutput(
                                    tool: obj["tool"] as? String ?? "tool",
                                    output: obj["output"] as? String ?? "",
                                    exitCode: obj["exit_code"] as? Int))
                                if obj["ui_event"] as? String == "research_started" {
                                    continuation.yield(.researchStarted(
                                        id: obj["research_session_id"] as? String ?? ""))
                                }
                            case "memories_used":
                                if let arr = obj["data"] as? [Any] {
                                    let items = arr.compactMap { item -> String? in
                                        if let s = item as? String { return s }
                                        if let d = item as? [String: Any] {
                                            return (d["text"] as? String) ?? (d["content"] as? String)
                                        }
                                        return nil
                                    }
                                    if !items.isEmpty { continuation.yield(.memoriesUsed(items)) }
                                }
                            case "plugins_used":
                                if let arr = obj["data"] as? [Any] {
                                    let items = arr.compactMap { $0 as? String }
                                    if !items.isEmpty { continuation.yield(.pluginsUsed(items)) }
                                }
                            case "skills_used":
                                if let arr = obj["data"] as? [Any] {
                                    let items = arr.compactMap { $0 as? String }
                                    if !items.isEmpty { continuation.yield(.skillsUsed(items)) }
                                }
                            case "permission_request":
                                continuation.yield(.permissionRequest(
                                    id: obj["request_id"] as? String ?? "",
                                    tool: obj["tool"] as? String ?? "command",
                                    command: obj["command"] as? String ?? ""))
                            case "agent_step":
                                continuation.yield(.agentStep)
                            case "doc_stream_open":
                                continuation.yield(.docStreamOpen(
                                    title: obj["title"] as? String ?? "",
                                    language: obj["language"] as? String ?? ""))
                            case "doc_stream_delta":
                                continuation.yield(.docStreamDelta(content: obj["content"] as? String ?? ""))
                            case "doc_update":
                                continuation.yield(.docUpdate(
                                    id: obj["doc_id"] as? String ?? "",
                                    title: obj["title"] as? String ?? "",
                                    language: obj["language"] as? String,
                                    content: obj["content"] as? String ?? "",
                                    version: obj["version"] as? Int))
                            case "doc_suggestions":
                                if let arr = obj["suggestions"] as? [[String: Any]] {
                                    let suggestions = arr.compactMap { s -> DocSuggestion? in
                                        guard let find = s["find"] as? String,
                                              let replace = s["replace"] as? String else { return nil }
                                        return DocSuggestion(
                                            id: s["id"] as? String ?? UUID().uuidString,
                                            find: find, replace: replace,
                                            reason: s["reason"] as? String ?? "")
                                    }
                                    continuation.yield(.docSuggestions(
                                        docID: obj["doc_id"] as? String ?? "",
                                        suggestions: suggestions))
                                }
                            case "web_sources", "research_sources":
                                if let arr = obj["data"] as? [[String: Any]] {
                                    let sources = arr.compactMap { s -> WebSource? in
                                        guard let url = s["url"] as? String else { return nil }
                                        return WebSource(title: s["title"] as? String ?? url, url: url)
                                    }
                                    if !sources.isEmpty { continuation.yield(.webSources(sources)) }
                                }
                            case "metrics":
                                if let md = obj["data"] as? [String: Any] {
                                    var parts: [String] = []
                                    if let t = (md["total_time"] as? Double) ?? (md["response_time"] as? Double), t > 0 {
                                        parts.append(String(format: "%.1fs", t))
                                    }
                                    if let tok = md["tokens_per_second"] as? Double, tok > 0 {
                                        parts.append(String(format: "%.0f tok/s", tok))
                                    }
                                    let ctx = md["context_percent"] as? Double
                                    if !parts.isEmpty || ctx != nil {
                                        continuation.yield(.metrics(parts.joined(separator: " · "), contextPercent: ctx))
                                    }
                                }
                            default:
                                break
                            }
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    private func memoryRetrievalFields(for message: String) -> [String: String] {
        let lower = message.lowercased()
        let rawTokens = lower
            .components(separatedBy: CharacterSet.alphanumerics.inverted)
            .filter { !$0.isEmpty }
        let explicitMemoryIntent = rawTokens.contains("memory")
            || rawTokens.contains("memories")
            || rawTokens.contains("remember")
            || rawTokens.contains("remembered")
            || lower.contains("about me")

        let actionTokens: Set<String> = [
            "add", "create", "schedule", "reschedule", "move", "update",
            "delete", "remove", "send", "reply", "draft", "email",
            "calendar", "event", "meeting", "note", "notes", "task", "todo",
        ]
        let actionToolIntent = rawTokens.contains { actionTokens.contains($0) }

        let stopwords: Set<String> = [
            "a", "an", "and", "are", "as", "at", "be", "by", "can", "could",
            "do", "for", "from", "get", "give", "go", "help", "i", "in",
            "is", "it", "me", "my", "of", "on", "or", "please", "put", "set",
            "that", "the", "this", "to", "up", "want", "what", "when", "with",
            "you", "add", "create", "make", "new",
        ]
        let queryTerms = rawTokens.filter { $0.count > 2 && !stopwords.contains($0) }

        if (actionToolIntent && !explicitMemoryIntent) || queryTerms.count < 2 {
            return [
                "use_memory": "false",
                "memory": "false",
                "memory_mode": "off",
                "memory_max_results": "0",
            ]
        }

        var fields: [String: String] = [
            "memory_mode": "strict",
            "memory_strict": "true",
            "memory_min_score": "0.72",
            "memory_max_results": "3",
        ]
        if let data = try? JSONSerialization.data(withJSONObject: Array(queryTerms.prefix(8))),
           let terms = String(data: data, encoding: .utf8) {
            fields["memory_query_terms"] = terms
        }
        return fields
    }

    func stopStream(sessionID: String) async {
        _ = try? await session.data(for: request("api/chat/stop/\(sessionID)", method: "POST"))
        // Persist the partial assistant message with the stopped flag —
        // without this, stopping mid-stream left the turn unsaved.
        _ = try? await session.data(for: request("api/session/\(sessionID)/mark-stopped", method: "POST"))
    }

    // MARK: Uploads / attachments

    func upload(fileURL: URL) async throws -> UploadedFile {
        var req = request("api/upload", method: "POST")
        let boundary = "joebro-\(UUID().uuidString)"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let fileData = try Data(contentsOf: fileURL)
        let name = fileURL.lastPathComponent
        var body = Data()
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"files\"; filename=\"\(name)\"\r\n".data(using: .utf8)!)
        body.append("Content-Type: application/octet-stream\r\n\r\n".data(using: .utf8)!)
        body.append(fileData)
        body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)
        req.httpBody = body
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
        let parsed = try JSONDecoder().decode(UploadResponse.self, from: data)
        guard let first = parsed.files.first else { throw APIError.badResponse }
        return first
    }

    /// Download an uploaded file to a temp location (named so QuickLook
    /// picks the right renderer from the extension).
    func downloadAttachment(id: String, name: String) async throws -> URL {
        let (data, resp) = try await session.data(for: request("api/upload/\(id)"))
        try check(data, resp)
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("JoeBroPreviews", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let dest = dir.appendingPathComponent(id + "-" + name)
        try data.write(to: dest)
        return dest
    }

    // MARK: Generic JSON helpers

    func getJSON<T: Decodable>(_ path: String, query: [String: String] = [:]) async throws -> T {
        var comps = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty {
            comps.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
        }
        var req = URLRequest(url: comps.url!)
        req.setValue(String(-TimeZone.current.secondsFromGMT() / 60), forHTTPHeaderField: "X-TZ-Offset")
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
        return try JSONDecoder().decode(T.self, from: data)
    }

    @discardableResult
    func sendJSON(_ path: String, method: String = "POST", body: [String: Any]? = nil) async throws -> [String: Any] {
        var req: URLRequest
        if path.contains("?") {
            // appendingPathComponent escapes '?' — build query URLs properly
            req = URLRequest(url: URL(string: baseURL.absoluteString + "/" + path)!)
            req.httpMethod = method
        } else {
            req = request(path, method: method)
        }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    @discardableResult
    func postForm(_ path: String, fields: [String: String]) async throws -> [String: Any] {
        var req = request(path, method: "POST")
        req.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        req.httpBody = formEncode(fields)
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    /// Download any backend path to a named temp file (for QuickLook).
    func downloadToTemp(_ path: String, query: [String: String] = [:], filename: String) async throws -> URL {
        var comps = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty {
            comps.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
        }
        let (data, resp) = try await session.data(for: URLRequest(url: comps.url!))
        try check(data, resp)
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("JoeBroPreviews", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let dest = dir.appendingPathComponent(filename)
        try data.write(to: dest)
        return dest
    }

    // MARK: Email

    func emailList(folder: String, filter: String = "all", limit: Int = 0) async throws -> EmailListResponse {
        try await getJSON("api/email/list", query: ["folder": folder, "filter": filter, "limit": String(limit)])
    }

    func emailRead(uid: String, folder: String) async throws -> EmailDetail {
        try await getJSON("api/email/read/\(uid)", query: ["folder": folder])
    }

    func emailFolders() async throws -> [String] {
        let r: FoldersResponse = try await getJSON("api/email/folders")
        return r.folders
    }

    func emailMarkRead(uid: String, folder: String, read: Bool) async {
        _ = try? await sendJSON("api/email/\(read ? "mark-read" : "mark-unread")/\(uid)?folder=\(folder)")
    }

    func emailArchive(uid: String, folder: String) async throws {
        try await sendJSON("api/email/archive/\(uid)?folder=\(folder)")
    }

    func emailDelete(uid: String, folder: String) async throws {
        try await sendJSON("api/email/delete/\(uid)?folder=\(folder)", method: "DELETE")
    }

    func emailSend(to: String, cc: String, subject: String, body: String,
                   inReplyTo: String?, references: String?, attachmentTokens: [String]) async throws {
        var payload: [String: Any] = ["to": to, "subject": subject, "body": body]
        if !cc.isEmpty { payload["cc"] = cc }
        if let inReplyTo, !inReplyTo.isEmpty { payload["in_reply_to"] = inReplyTo }
        if let references, !references.isEmpty { payload["references"] = references }
        if !attachmentTokens.isEmpty { payload["attachments"] = attachmentTokens }
        let result = try await sendJSON("api/email/send", body: payload)
        if let ok = result["success"] as? Bool, !ok {
            throw APIError.http(500, result["error"] as? String ?? "Send failed")
        }
    }

    func emailSummarize(_ d: EmailDetail, folder: String) async throws -> String {
        let r = try await sendJSON("api/email/summarize", body: [
            "body": d.body ?? "", "subject": d.subject ?? "", "from": d.fromAddress ?? "",
            "uid": d.uid ?? "", "folder": folder, "message_id": d.messageId ?? "",
        ])
        guard r["success"] as? Bool == true, let s = r["summary"] as? String else {
            throw APIError.http(500, r["error"] as? String ?? "Summary failed")
        }
        return s
    }

    func emailAIReply(_ d: EmailDetail, folder: String) async throws -> String {
        let r = try await sendJSON("api/email/ai-reply", body: [
            "to": d.fromAddress ?? "", "subject": d.subject ?? "",
            "original_body": d.body ?? "", "uid": d.uid ?? "",
            "folder": folder, "message_id": d.messageId ?? "",
        ])
        guard r["success"] as? Bool == true, let s = r["reply"] as? String else {
            throw APIError.http(500, r["error"] as? String ?? "Draft failed")
        }
        return s
    }

    /// Upload a compose attachment; returns the server token to pass to /send.
    func composeUpload(fileURL: URL) async throws -> (token: String, filename: String) {
        var req = request("api/email/compose-upload", method: "POST")
        let boundary = "joebro-\(UUID().uuidString)"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        var body = Data()
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"file\"; filename=\"\(fileURL.lastPathComponent)\"\r\n".data(using: .utf8)!)
        body.append("Content-Type: application/octet-stream\r\n\r\n".data(using: .utf8)!)
        body.append(try Data(contentsOf: fileURL))
        body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)
        req.httpBody = body
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let token = obj["token"] as? String else { throw APIError.badResponse }
        return (token, obj["filename"] as? String ?? fileURL.lastPathComponent)
    }

    func downloadEmailAttachment(uid: String, index: Int, folder: String, filename: String) async throws -> URL {
        try await downloadToTemp("api/email/attachment/\(uid)/\(index)", query: ["folder": folder], filename: filename)
    }

    // MARK: Calendar

    func calendarEvents(start: Date, end: Date) async throws -> [CalEvent] {
        let df = ISO8601DateFormatter()
        let r: EventsResponse = try await getJSON("api/calendar/events", query: [
            "start": df.string(from: start), "end": df.string(from: end),
        ])
        return r.events
    }

    func createEvent(summary: String, start: Date, end: Date, allDay: Bool, location: String, description: String) async throws {
        let df = ISO8601DateFormatter()
        try await sendJSON("api/calendar/events", body: [
            "summary": summary,
            "dtstart": df.string(from: start),
            "dtend": df.string(from: end),
            "all_day": allDay,
            "location": location,
            "description": description,
        ])
    }

    func updateEvent(uid: String, summary: String, start: Date, end: Date, allDay: Bool, location: String, description: String) async throws {
        let df = ISO8601DateFormatter()
        try await sendJSON("api/calendar/events/\(uid)", method: "PUT", body: [
            "summary": summary,
            "dtstart": df.string(from: start),
            "dtend": df.string(from: end),
            "all_day": allDay,
            "location": location,
            "description": description,
        ])
    }

    func deleteEvent(uid: String) async throws {
        try await sendJSON("api/calendar/events/\(uid)", method: "DELETE")
    }

    // MARK: Memory (Brain)

    func memories() async throws -> [MemoryEntry] {
        let r: MemoryResponse = try await getJSON("api/memory")
        return r.memory
    }

    func addMemory(text: String, category: String) async throws {
        try await sendJSON("api/memory/add", body: ["text": text, "category": category, "source": "user"])
    }

    func pinMemory(id: String) async {
        _ = try? await sendJSON("api/memory/\(id)/pin")
    }

    func deleteMemory(id: String) async throws {
        try await sendJSON("api/memory/\(id)", method: "DELETE")
    }

    // MARK: Notes

    func notes() async throws -> [Note] {
        let r: NotesResponse = try await getJSON("api/notes")
        return r.notes
    }

    func createNote(title: String, content: String, items: [String]) async throws {
        var body: [String: Any] = ["title": title, "note_type": items.isEmpty ? "note" : "checklist"]
        if !content.isEmpty { body["content"] = content }
        if !items.isEmpty { body["items"] = items.map { ["text": $0, "done": false] } }
        try await sendJSON("api/notes", body: body)
    }

    func toggleNoteItem(noteID: String, index: Int) async {
        _ = try? await sendJSON("api/notes/\(noteID)/items/\(index)/toggle")
    }

    func pinNote(id: String) async {
        _ = try? await sendJSON("api/notes/\(id)/pin")
    }

    func archiveNote(id: String) async {
        _ = try? await sendJSON("api/notes/\(id)/archive")
    }

    // MARK: Documents

    func documentsLibrary(search: String = "") async throws -> [DocSummary] {
        var q = ["limit": "50", "sort": "recent"]
        if !search.isEmpty { q["search"] = search }
        let r: DocsLibraryResponse = try await getJSON("api/documents/library", query: q)
        return r.documents
    }

    func document(id: String) async throws -> DocDetail {
        try await getJSON("api/document/\(id)")
    }

    func updateDocument(id: String, content: String) async throws {
        var req = request("api/document/\(id)", method: "PUT")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["content": content])
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
    }

    func createDocument(title: String, content: String, language: String) async throws {
        try await sendJSON("api/document", body: ["title": title, "content": content, "language": language])
    }

    // MARK: Spreadsheets (xlsx <-> CSV; conversion runs in the backend)

    func xlsxToCSV(_ data: Data) async throws -> String {
        var req = request("api/spreadsheet/to_csv", method: "POST")
        req.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        req.httpBody = data
        let (out, resp) = try await session.data(for: req)
        try check(out, resp)
        return String(decoding: out, as: UTF8.self)
    }

    func csvToXLSX(_ csv: String) async throws -> Data {
        var req = request("api/spreadsheet/to_xlsx", method: "POST")
        req.setValue("text/csv", forHTTPHeaderField: "Content-Type")
        req.httpBody = Data(csv.utf8)
        let (out, resp) = try await session.data(for: req)
        try check(out, resp)
        return out
    }

    func apkgToText(_ data: Data) async throws -> String {
        var req = request("api/flashcards/to_text", method: "POST")
        req.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        req.httpBody = data
        let (out, resp) = try await session.data(for: req)
        try check(out, resp)
        return String(decoding: out, as: UTF8.self)
    }

    func textToAPKG(_ text: String, deckName: String) async throws -> Data {
        let q = deckName.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? "Flashcards"
        var req = request("api/flashcards/to_apkg?name=" + q, method: "POST")
        req.setValue("text/plain", forHTTPHeaderField: "Content-Type")
        req.httpBody = Data(text.utf8)
        let (out, resp) = try await session.data(for: req)
        try check(out, resp)
        return out
    }

    func learnSkills(path: String, device: String) async throws -> String {
        let r = try await sendJSON("api/skills/learn", body: ["path": path, "device": device])
        guard let job = r["job"] as? String, !job.isEmpty else {
            throw APIError.badResponse
        }
        return job
    }

    func learnSkillsStatus(job: String) async throws -> LearnSkillsStatus {
        try await getJSON("api/skills/learn-status", query: ["job": job])
    }

    func exportDocumentPDF(id: String, title: String) async throws -> URL {
        try await downloadToTemp("api/document/\(id)/export-pdf", filename: title + ".pdf")
    }

    // MARK: Workdir (chat-bound folder)

    func sessionWorkdir(sid: String) async throws -> String? {
        let r: WorkdirInfo = try await getJSON("api/session/\(sid)/workdir")
        return r.workdir
    }

    func setSessionWorkdir(sid: String, path: String?) async throws {
        try await sendJSON("api/session/\(sid)/workdir", method: "PUT",
                           body: ["workdir": path as Any])
    }

    func workdirList(sid: String, sub: String = "") async throws -> [WorkdirEntry] {
        let r: WorkdirListResponse = try await getJSON("api/session/\(sid)/workdir/list",
                                                       query: sub.isEmpty ? [:] : ["sub": sub])
        return r.entries
    }

    func deleteWorkdirFile(sid: String, sub: String) async throws {
        var req = request("api/session/\(sid)/workdir/delete-file", method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["sub": sub])
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
    }

    func renameWorkdirFile(sid: String, sub: String, newName: String) async throws {
        var req = request("api/session/\(sid)/workdir/rename-file", method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["sub": sub, "new_name": newName])
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
    }

    /// Opens a workdir file as an editable Document (file_path-mirrored:
    /// saves write back to disk). Returns the doc for the editor sheet.
    func openWorkdirFile(sid: String, sub: String) async throws -> DocDetail {
        var req = request("api/session/\(sid)/workdir/open-file", method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["sub": sub])
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
        return try JSONDecoder().decode(DocDetail.self, from: data)
    }

    // MARK: Local files / gallery

    func personalFiles() async throws -> [PersonalFile] {
        let r: PersonalFilesResponse = try await getJSON("api/personal")
        return r.files
    }

    func galleryLibrary() async throws -> [GalleryItem] {
        let r: GalleryLibraryResponse = try await getJSON("api/gallery/library", query: ["limit": "60"])
        return r.items
    }

    func galleryImageURL(filename: String) -> URL {
        baseURL.appendingPathComponent("api/generated-image/\(filename)")
    }

    // MARK: STT

    func transcribe(audioURL: URL) async throws -> String {
        var req = request("api/stt/transcribe", method: "POST")
        let boundary = "joebro-\(UUID().uuidString)"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        var body = Data()
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"file\"; filename=\"voice.m4a\"\r\n".data(using: .utf8)!)
        body.append("Content-Type: audio/mp4\r\n\r\n".data(using: .utf8)!)
        body.append(try Data(contentsOf: audioURL))
        body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)
        req.httpBody = body
        let (data, resp) = try await session.data(for: req)
        try check(data, resp)
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let text = obj["text"] as? String else { throw APIError.badResponse }
        return text
    }

    // MARK: Helpers

    private func formEncode(_ fields: [String: String]) -> Data {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~")
        return fields
            .map { key, value in
                let k = key.addingPercentEncoding(withAllowedCharacters: allowed) ?? key
                let v = value.addingPercentEncoding(withAllowedCharacters: allowed) ?? value
                return "\(k)=\(v)"
            }
            .joined(separator: "&")
            .data(using: .utf8) ?? Data()
    }
}
