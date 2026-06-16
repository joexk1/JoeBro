import EventKit
import SwiftUI
import Observation

@MainActor
@Observable
final class AppStore {
    // Sessions
    var sessions: [ChatSessionInfo] = []
    var selectedSessionID: String?
    var sessionSearch = ""

    // Conversation
    var messages: [Message] = []
    var isStreaming = false
    var workingSessions: Set<String> = []   // sessions generating in the background (sidebar spinner)
    var loadError: String?

    // Models
    var models: [ModelChoice] = []
    var selectedModel: ModelChoice?

    // Workspace
    var activeTab: WorkspaceTab = .chat {
        didSet {
            if activeTab == .email {
                // Opening the workspace acknowledges everything currently unread:
                // the dot stays while unread > 0, but it stops bouncing.
                emailSeenUnread = emailUnread
                emailBounce = false
            }
            if activeTab == .research { researchDot = false }
        }
    }

    // Email prefetch cache — Email tab renders instantly from this
    var emailCache: [EmailSummary]?
    var emailFoldersCache: [String]?

    // Deep-link targets from chat anchors (research / calendar event)
    var pendingResearchID: String?
    var pendingEventID: String?

    // Deep Research run lives HERE (not on the panel view) so leaving the tab
    // never stops or restarts it — the poll continues regardless of what's shown.
    var researchRun: ResearchRun?
    private var researchPollTask: Task<Void, Never>?

    // Status / notifications
    var backendStatus: BackendStatus = .checking
    var researchDot = false
    // Email badge: dot shows while unread > 0; bounces while there are emails
    // unread that arrived since the Email workspace was last opened.
    var emailUnread = 0
    var emailSeenUnread = 0
    var emailDot: Bool { emailUnread > 0 }
    var emailBounce = false

    /// Single source of truth for the sidebar badge. Dot shows while unread > 0;
    /// it bounces only when unread grew since the Email workspace was last open.
    func applyEmailUnread(_ unread: Int) {
        emailUnread = unread
        if activeTab == .email {
            emailSeenUnread = unread   // looking at the inbox — pin the baseline
            emailBounce = false
        } else {
            emailBounce = unread > emailSeenUnread
        }
    }

    /// Re-pull the unread count now (after the user reads/marks mail) so the
    /// sidebar badge reacts immediately instead of waiting for the poll.
    func refreshEmailUnread() async {
        if let unread = await api.emailUnreadTotal() { applyEmailUnread(unread) }
    }

    enum BackendStatus { case up, slow, down, checking }

    // Composer options
    var agentMode = false
    var useWeb = false
    var thinkingOn = true          // Think vs Fast
    var allowBash = false          // >_ terminal toggle
    var permissionMode = "sandbox" // lock menu: bound folder / readonly / full
    var askBeforeCommands: Bool = UserDefaults.standard.bool(forKey: "askBeforeCommands") {
        didSet { UserDefaults.standard.set(askBeforeCommands, forKey: "askBeforeCommands") }
    }
    var pendingPermission: PendingPermission?   // active command-approval prompt
    var pendingAttachments: [UploadedFile] = []

    // Editor pane (co-editing with the AI)
    var openDocs: [EditorDoc] = []
    var activeDocID: String?
    var editorVisible = false
    var editorDetached = false     // editor lives in its own window
    var quickLookURL: URL?            // binary files (PDF/images) from the bound folder
    var requireDocApproval: Bool = UserDefaults.standard.object(forKey: "requireDocApproval") == nil
        ? true : UserDefaults.standard.bool(forKey: "requireDocApproval")
    private var autosaveTask: Task<Void, Never>?

    // Bound folder (workdir) file tree — shown in the sidebar
    var workdirPath: String?
    var treeEntries: [String: [WorkdirEntry]] = [:]   // sub-path → entries
    var expandedDirs: Set<String> = []

    // Appearance
    var wallpaperURL: URL? {
        didSet { UserDefaults.standard.set(wallpaperURL?.path, forKey: "wallpaperPath") }
    }

    private let api = APIClient.shared
    private var streamTask: Task<Void, Never>?

    init() {
        if let p = UserDefaults.standard.string(forKey: "wallpaperPath"),
           FileManager.default.fileExists(atPath: p) {
            wallpaperURL = URL(fileURLWithPath: p)
        }
    }

    // MARK: Bootstrap

    func bootstrap() async {
        startStatusPoller()
        await waitForBackend()
        await loadAll()
    }

    private func waitForBackend() async {
        for _ in 0..<40 {
            if await api.ping() != nil { return }
            try? await Task.sleep(for: .milliseconds(150))
        }
    }

    private func startStatusPoller() {
        Task {
            var tick = 0
            while !Task.isCancelled {
                tick += 1
                backendStatus = (await api.ping()) != nil ? .up : .down
                if activeTab != .research,
                   let active = try? await api.researchActive(), !active.isEmpty {
                    researchDot = true
                }
                // IMAP is the expensive one — every 3rd tick is plenty.
                if tick % 3 == 1, let unread = await api.emailUnreadTotal() {
                    applyEmailUnread(unread)
                }
                try? await Task.sleep(for: .seconds(45))
            }
        }
    }

    private func loadAll() async {
        async let s: () = loadSessions()
        async let m: () = loadModels()
        _ = await (s, m)
        prefetchEmail()
    }

    /// Warms the inbox in the background so the Email tab opens instantly.
    func prefetchEmail() {
        Task.detached(priority: .utility) { [api] in
            let list = try? await api.emailList(folder: "INBOX", filter: "all")
            let folders = try? await api.emailFolders()
            await MainActor.run { [weak self] in
                if let emails = list?.emails, list?.error == nil { self?.emailCache = emails }
                if let folders { self?.emailFoldersCache = folders }
            }
        }
    }

    // MARK: Sessions

    func loadSessions() async {
        do {
            sessions = try await api.sessions().filter { $0.archived != true }
        } catch {
            if isCancellation(error) { return }
            loadError = "Sessions: " + error.localizedDescription
        }
    }

    private func isCancellation(_ error: Error) -> Bool {
        if error is CancellationError { return true }
        let ns = error as NSError
        return ns.domain == NSURLErrorDomain && ns.code == NSURLErrorCancelled
    }

    var filteredSessions: [ChatSessionInfo] {
        let base = sessionSearch.isEmpty
            ? sessions
            : sessions.filter { $0.name.localizedCaseInsensitiveContains(sessionSearch) }
        // Pinned chats float to the top, preserving order within each group.
        return base.filter { $0.isImportant == true } + base.filter { $0.isImportant != true }
    }

    func togglePin(_ id: String) {
        guard let i = sessions.firstIndex(where: { $0.id == id }) else { return }
        let newValue = !(sessions[i].isImportant == true)
        sessions[i].isImportant = newValue
        Task {
            do { try await api.setSessionImportant(id, important: newValue) }
            catch {
                loadError = "Pin: " + error.localizedDescription
                if let j = sessions.firstIndex(where: { $0.id == id }) {
                    sessions[j].isImportant = !newValue
                }
            }
        }
    }

    func select(_ id: String?) {
        activeTab = .chat
        guard id != selectedSessionID else { return }
        // Don't cancel an in-flight generation — let it finish in the background
        // (the sidebar shows a spinner). Only the Stop button cancels.
        selectedSessionID = id
        isStreaming = id.map { workingSessions.contains($0) } ?? false
        messages = []
        openDocs = []
        activeDocID = nil
        editorVisible = false
        Task { await loadWorkdir() }
        guard let id else { return }
        // The composer switch mirrors the session's saved mode — the
        // sidebar hammer and the switch always agree.
        agentMode = sessions.first(where: { $0.id == id })?.mode == "agent"
        Task { await loadHistory(for: id) }
    }

    private func loadHistory(for id: String) async {
        do {
            let h = try await api.history(id)
            guard selectedSessionID == id else { return }
            applyHistory(h)
        } catch {
            if isCancellation(error) { return }
            loadError = "History: " + error.localizedDescription
        }
    }

    private func applyHistory(_ h: HistoryResponse) {
        // Picker reflects the model this chat last used
        if let m = h.model, !m.isEmpty {
            if m == appleIntelligenceID, AppleIntelligence.isAvailable {
                selectedModel = .appleIntelligence
            } else if let match = models.first(where: { $0.modelID == m }) {
                selectedModel = match
            }
        }
        var loaded: [Message] = []
        for hm in h.history {
            let atts: [Attachment] = (hm.metadata?["attachments"]?.arrayValue ?? []).compactMap { a in
                guard let aid = a["id"]?.stringValue else { return nil }
                return Attachment(
                    id: aid,
                    name: a["name"]?.stringValue ?? "file",
                    mime: a["mime"]?.stringValue ?? ""
                )
            }
            if hm.role == "assistant" {
                // Metadata is the source of truth for agent runs: the
                // backend stores tool_events + thinking there and keeps
                // content clean. Content parsing is the fallback for
                // models that narrate calls inline (DSML etc.).
                var segments: [Message] = []
                for ev in hm.metadata?["tool_events"]?.arrayValue ?? [] {
                    let tool = ev["tool"]?.stringValue ?? "tool"
                    if isObsoleteBackendTool(tool) { continue }
                    var row = Message(role: "tool", kind: .tool, content: "")
                    row.toolName = tool
                    row.command = ev["command"]?.stringValue
                    row.toolOutput = ev["output"]?.stringValue
                    row.exitCode = ev["exit_code"]?.intValue
                    row.toolDone = true
                    segments.append(row)
                }
                segments += splitHistorySegments(hm.content)
                segments = segments.filter { $0.kind == .tool || !isDebrisContent($0.content) }
                let histModel = hm.metadata?["model"]?.stringValue ?? h.model
                for i in segments.indices where segments[i].kind == .text {
                    segments[i].modelName = histModel
                }
                // Agent-round narration (text before the final segment,
                // when tools ran) collapses like thinking.
                if segments.contains(where: { $0.kind == .tool }),
                   let lastText = segments.lastIndex(where: { $0.kind == .text }) {
                    for i in segments.indices
                    where segments[i].kind == .text && i != lastText {
                        segments[i].isIntermediate = true
                    }
                }
                if let ctx = hm.metadata?["context_percent"]?.doubleValue,
                   let last = segments.lastIndex(where: { $0.kind == .text }) {
                    segments[last].contextPercent = ctx
                    var parts: [String] = []
                    if let t = hm.metadata?["response_time"]?.doubleValue, t > 0 {
                        parts.append(String(format: "%.1fs", t))
                    }
                    if let tok = hm.metadata?["tokens_per_second"]?.doubleValue, tok > 0 {
                        parts.append(String(format: "%.0f tok/s", tok))
                    }
                    if !parts.isEmpty { segments[last].metrics = parts.joined(separator: " · ") }
                }
                if let think = hm.metadata?["thinking"]?.stringValue, !think.isEmpty,
                   let first = segments.firstIndex(where: { $0.kind == .text }) {
                    segments[first].thinking = think
                    segments[first].thinkingTime = hm.metadata?["thinking_time"]?.doubleValue
                }
                let mems = (hm.metadata?["memories_used"]?.arrayValue ?? []).compactMap {
                    $0.stringValue ?? $0["text"]?.stringValue ?? $0["content"]?.stringValue
                }
                if !mems.isEmpty, let last = segments.lastIndex(where: { $0.kind == .text }) {
                    segments[last].memoriesUsed = dedupeMemories(mems)
                }
                if !atts.isEmpty {
                    if let last = segments.indices.last, segments[last].kind == .text {
                        segments[last].attachments = atts
                    } else {
                        segments.append({
                            var m = Message(role: "assistant", content: "")
                            m.attachments = atts
                            return m
                        }())
                    }
                }
                loaded += segments
            } else {
                // Compaction marker: older turns were collapsed server-side.
                // Render a pill, keep the summary text out of the chat.
                if hm.role == "system",
                   hm.metadata?["compacted"]?.boolValue == true
                    || hm.content.hasPrefix("[Conversation summary") {
                    loaded.append(Message(role: "system", kind: .compacted, content: ""))
                    continue
                }
                var msg = Message(role: hm.role, content: hm.content)
                msg.dbID = hm.metadata?["_db_id"]?.stringValue
                    ?? hm.metadata?["_db_id"]?.intValue.map(String.init)
                msg.attachments = atts
                if !msg.content.isEmpty || !atts.isEmpty { loaded.append(msg) }
            }
        }
        messages = loaded
    }

    private func dedupeMemories(_ items: [String]) -> [String] {
        var seen = Set<String>()
        var out: [String] = []
        for item in items {
            let trimmed = item.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else { continue }
            let key = trimmed.lowercased()
            if seen.insert(key).inserted { out.append(trimmed) }
        }
        return out
    }

    private func appendMemoriesUsed(_ items: [String], to index: Int) {
        messages[index].memoriesUsed = dedupeMemories(messages[index].memoriesUsed + items)
    }

    func newChat() {
        stopStreaming()
        activeTab = .chat
        selectedSessionID = nil
        messages = []
        openDocs = []
        activeDocID = nil
        editorVisible = false
        workdirPath = nil
        treeEntries = [:]
        expandedDirs = []
    }

    func deleteSession(_ id: String) {
        Task {
            try? await api.deleteSession(id)
            sessions.removeAll { $0.id == id }
            if selectedSessionID == id { newChat() }
        }
    }

    func archiveSession(_ id: String) {
        Task {
            try? await api.archiveSession(id)
            sessions.removeAll { $0.id == id }
            if selectedSessionID == id { newChat() }
        }
    }

    func renameSession(_ id: String, to name: String) {
        let trimmed = name.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return }
        Task {
            do {
                try await api.renameSession(id, name: trimmed)
                if let i = sessions.firstIndex(where: { $0.id == id }) {
                    sessions[i].name = trimmed
                }
            } catch {
                loadError = "Rename: " + error.localizedDescription
            }
        }
    }

    // MARK: Chat actions (title dropdown)

    var compacting = false

    /// Server-side compaction: summarizes older messages into one entry,
    /// keeps a recent tail verbatim — same endpoint the web UI uses.
    func compactChat() {
        guard let sid = selectedSessionID, !compacting else { return }
        compacting = true
        Task {
            defer { compacting = false }
            do {
                _ = try await api.compactSession(sid)
                guard selectedSessionID == sid else { return }
                await loadHistory(for: sid)
                await loadSessions()
            } catch {
                loadError = "Compact: " + error.localizedDescription
            }
        }
    }

    /// Keep the server's idea of the session mode in sync with the
    /// composer switch, so the sidebar hammer is trustworthy.
    func persistChatMode() {
        guard let sid = selectedSessionID else { return }
        let mode = agentMode ? "agent" : "chat"
        if let i = sessions.firstIndex(where: { $0.id == sid }) {
            sessions[i].mode = mode
        }
        Task { try? await api.setSessionMode(sid, mode: mode) }
    }

    var chatTranscript: String {
        var out: [String] = []
        if let s = sessions.first(where: { $0.id == selectedSessionID }) {
            out.append("# \(s.name)\n")
        }
        for m in messages {
            switch m.kind {
            case .tool:
                out.append("> 🔧 \(m.toolName ?? "tool"): `\((m.command ?? "").prefix(200))`")
            case .text:
                out.append((m.isUser ? "**You:**" : "**JoeBro:**") + "\n\n" + m.content)
            case .compacted:
                out.append("> — earlier conversation compacted —")
            }
        }
        return out.joined(separator: "\n\n")
    }

    func copyChat() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(chatTranscript, forType: .string)
    }

    func saveChatToDocuments() {
        let name = sessions.first(where: { $0.id == selectedSessionID })?.name ?? "Chat"
        let text = chatTranscript
        Task {
            do {
                try await api.createDocument(title: name, content: text, language: "markdown")
            } catch {
                loadError = "Save: " + error.localizedDescription
            }
        }
    }

    /// Export the chat as PDF via a throwaway server document.
    func exportChatPDF() async -> URL? {
        let name = sessions.first(where: { $0.id == selectedSessionID })?.name ?? "Chat"
        let text = chatTranscript
        do {
            try await api.createDocument(title: "__pdf_export__" + name, content: text, language: "markdown")
            guard let doc = try await api.documentsLibrary(search: "__pdf_export__" + name).first else { return nil }
            let url = try await api.exportDocumentPDF(id: doc.id, title: name)
            await api.deleteDocument(id: doc.id)
            return url
        } catch {
            loadError = "PDF: " + error.localizedDescription
            return nil
        }
    }

    // MARK: Models

    func loadModels() async {
        do {
            var all = try await api.models().filter { !$0.offline }
            // Always listed (unless hidden in Settings) — availability is
            // checked at send time; it reports "not ready" transiently after boot.
            if !UserDefaults.standard.bool(forKey: "hideAppleIntelligence") {
                all.append(.appleIntelligence)
            }
            models = all
            if selectedModel == nil {
                let savedID = UserDefaults.standard.string(forKey: "selectedModelID")
                selectedModel = all.first { $0.id == savedID }
                    ?? all.first { $0.category == "local" }
                    ?? all.first
            }
        } catch {
            loadError = "Models: " + error.localizedDescription
        }
    }

    func pick(_ model: ModelChoice) {
        selectedModel = model
        UserDefaults.standard.set(model.id, forKey: "selectedModelID")
        // Mid-chat switches must update the SESSION, or the backend keeps
        // streaming from the old model.
        if let sid = selectedSessionID {
            if let i = sessions.firstIndex(where: { $0.id == sid }) {
                sessions[i].model = model.modelID   // sidebar shows last-used
            }
            // Apple Intelligence persists too — replies bypass the backend,
            // but the session remembers it (and the picker restores it).
            let epURL = model.isAppleIntelligence ? "apple://on-device" : model.endpointURL
            Task {
                do {
                    try await api.updateSessionModel(sid, model: model.modelID,
                                                     endpointURL: epURL,
                                                     endpointID: model.endpointID)
                } catch {
                    loadError = "Model switch: " + error.localizedDescription
                }
            }
        }
    }

    // MARK: Attachments

    /// Handles drops anywhere: file URLs and raw images (browser drags etc).
    @discardableResult
    func attachProviders(_ providers: [NSItemProvider]) -> Bool {
        var handled = false
        for p in providers {
            if p.canLoadObject(ofClass: URL.self) {
                handled = true
                _ = p.loadObject(ofClass: URL.self) { url, _ in
                    if let url { Task { @MainActor in self.attach(url) } }
                }
            } else if p.canLoadObject(ofClass: NSImage.self) {
                handled = true
                _ = p.loadObject(ofClass: NSImage.self) { img, _ in
                    guard let img = img as? NSImage,
                          let tiff = img.tiffRepresentation,
                          let rep = NSBitmapImageRep(data: tiff),
                          let png = rep.representation(using: .png, properties: [:]) else { return }
                    let url = FileManager.default.temporaryDirectory
                        .appendingPathComponent("dropped-\(UUID().uuidString.prefix(8)).png")
                    try? png.write(to: url)
                    Task { @MainActor in self.attach(url) }
                }
            }
        }
        return handled
    }

    func attach(_ url: URL) {
        Task {
            do {
                let f = try await api.upload(fileURL: url)
                pendingAttachments.append(f)
            } catch {
                loadError = "Upload failed: \(error.localizedDescription)"
            }
        }
    }

    /// Write an event the AI created (via the create_event tool) into the real
    /// macOS Calendar through EventKit. The AI parses the natural-language
    /// date/time itself and sends ISO datetimes — we just persist them.
    /// The backend's create_event tool only stores macOS events locally (it
    /// can't touch EventKit). Parse its JSON output and write the event into the
    /// real macOS Calendar here. No-op for non-macOS calendars (caldav events
    /// are already created server-side).
    private func writeAgentCalendarEvent(fromToolOutput output: String) async {
        guard let data = output.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let ev = obj["event"] as? [String: Any] else { return }
        guard (try? await api.calendarStatus())?.type == "macos" else { return }
        let summary = (ev["summary"] as? String) ?? "New event"
        guard let start = parseISO((ev["dtstart"] as? String) ?? "") else { return }
        let end = parseISO((ev["dtend"] as? String) ?? "") ?? start.addingTimeInterval(3600)
        let allDay = (ev["all_day"] as? Bool) ?? false
        _ = try? await writeMacCalendarEvent(summary: summary, start: start, end: end, allDay: allDay)
    }

    @discardableResult
    func writeMacCalendarEvent(summary: String, start: Date, end: Date, allDay: Bool) async throws -> String {
        guard await MacCalendarBridge.shared.requestFullAccess() else {
            throw NSError(domain: "JoeBroCalendar", code: 2, userInfo: [NSLocalizedDescriptionKey: "macOS Calendar permission was not granted."])
        }
        let store = MacCalendarBridge.shared.store
        guard let calendar = store.defaultCalendarForNewEvents
                ?? store.calendars(for: .event).first(where: { $0.allowsContentModifications }) else {
            throw NSError(domain: "JoeBroCalendar", code: 3, userInfo: [NSLocalizedDescriptionKey: "No writable macOS calendar is available."])
        }
        let event = EKEvent(eventStore: store)
        event.calendar = calendar
        event.title = summary
        event.startDate = start
        event.endDate = end
        event.isAllDay = allDay
        try store.save(event, span: .thisEvent, commit: true)
        return event.eventIdentifier ?? "macos-event"
    }

    // MARK: Send / stream

    /// Run a scheduled task as a chat: creates a new session titled
    /// `[TASK] <name>` (so the sidebar shows it as such) and sends the task's
    /// prompt as the first normal user message. The explicit name means
    /// `ensureSession` won't auto-derive a title from the prompt.
    func runTaskInChat(name: String, prompt: String) {
        newChat()
        let model = selectedModel
        Task {
            do {
                let created = try await api.createSession(
                    name: "[TASK] \(name)",
                    model: model?.modelID ?? "",
                    endpointURL: model?.endpointURL ?? "",
                    endpointID: model?.endpointID ?? ""
                )
                selectedSessionID = created.id
                sessions.insert(created, at: 0)
                // selectedSessionID is set, so send() reuses this session and
                // never overwrites its `[TASK] <name>` title.
                send(prompt)
            } catch {
                loadError = "Run task: " + error.localizedDescription
            }
        }
    }

    // MARK: Deep Research (owned by the store, not the panel)

    /// Kick off a new deep-research run and watch it to completion regardless of
    /// which tab is showing.
    func startResearch(query: String, endpointID: String?, model: String?) {
        Task {
            do {
                let id = try await api.researchStart(query: query, endpointID: endpointID, model: model)
                adoptResearch(id: id, query: query)
            } catch {
                loadError = "Research start failed: \(error.localizedDescription)"
            }
        }
    }

    /// Begin watching a run (new or one the agent started) — idempotent.
    func adoptResearch(id: String, query: String) {
        guard researchRun?.id != id else { return }
        let run = ResearchRun(id: id, query: query)
        researchRun = run
        startResearchPolling(run)
    }

    func cancelResearch() {
        if let id = researchRun?.id { Task { await api.researchCancel(id: id) } }
        researchPollTask?.cancel(); researchPollTask = nil
        researchRun = nil
    }

    /// The poll lives on the store, so tab switches never touch it.
    private func startResearchPolling(_ run: ResearchRun) {
        researchPollTask?.cancel()
        let started = Date()
        researchPollTask = Task { [weak self] in
            while !Task.isCancelled {
                run.elapsed = Date().timeIntervalSince(started)
                if let status = try? await APIClient.shared.researchStatus(id: run.id) {
                    if let avg = status["avg_duration"]?.doubleValue { run.avgDuration = avg }
                    if let p = status["progress"] {
                        run.phase = p["phase"]?.stringValue ?? run.phase
                        run.message = p["message"]?.stringValue ?? run.message
                    }
                    let s = status["status"]?.stringValue ?? ""
                    if ["completed", "done", "complete", "error", "failed", "cancelled", "canceled"].contains(s) {
                        await MainActor.run {
                            self?.researchDot = true
                            self?.researchRun = nil           // clears the progress card; report now in the library
                            self?.researchPollTask = nil
                        }
                        return
                    }
                }
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    /// Answer the active command-permission prompt and let the agent continue.
    func respondPermission(_ decision: String) {
        guard let p = pendingPermission else { return }
        pendingPermission = nil
        Task { await api.respondPermission(p.id, decision: decision) }
    }

    func send(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty || !pendingAttachments.isEmpty, !isStreaming else { return }

        let atts = pendingAttachments
        pendingAttachments = []

        // Terminal access only exists in agent mode — flip the switch so the
        // toggle visibly does something instead of silently no-oping.
        if (allowBash || permissionMode == "full") && !agentMode { agentMode = true }

        var userMsg = Message(role: "user", content: trimmed)
        userMsg.attachments = atts.map { Attachment(id: $0.id, name: $0.name, mime: $0.mime) }
        messages.append(userMsg)
        let turnStart = messages.count   // everything from here is this turn's output
        messages.append(Message(role: "assistant", content: "", isStreaming: true))
        isStreaming = true

        if selectedModel?.isAppleIntelligence == true {
            sendViaAppleIntelligence(trimmed)
            return
        }

        streamTask = Task {
            // Session creation is the one thing a mid-stream Stop must never
            // kill — losing it orphaned the whole chat. Shield it.
            let sid: String
            do {
                sid = try await Task { try await self.ensureSession(firstMessage: trimmed) }.value
            } catch {
                if let i = messages.indices.last, !messages[i].isUser {
                    messages[i].content = "> ⚠️ Couldn't create session: \(error.localizedDescription)"
                    messages[i].isStreaming = false
                }
                isStreaming = false
                return
            }
            var currentModel: String?
            // Track this session as working so the sidebar shows a spinner, and
            // let it keep generating if the user switches to another chat.
            workingSessions.insert(sid)
            var detached = false

            // The transcript is segmented like the web UI: text → tool rows →
            // text… Each tool_start finalizes the current text segment
            // (stripping narrated tool-call JSON) and appends a tool row.
            @MainActor func currentTextIndex() -> Int {
                if let i = messages.indices.last,
                   messages[i].role == "assistant", messages[i].kind == .text {
                    return i
                }
                var m = Message(role: "assistant", content: "", isStreaming: true)
                m.modelName = currentModel
                messages.append(m)
                return messages.count - 1
            }

            var buffer = ""
            var thinkBuffer = ""
            var lastFlush = Date()
            @MainActor func flush() {
                if !thinkBuffer.isEmpty {
                    let i = currentTextIndex()
                    messages[i].thinking = (messages[i].thinking ?? "") + thinkBuffer
                    thinkBuffer = ""
                }
                if !buffer.isEmpty {
                    messages[currentTextIndex()].content += buffer
                    buffer = ""
                }
                lastFlush = Date()
            }

            @MainActor func finalizeTextSegment() {
                flush()
                guard let i = messages.indices.last,
                      messages[i].role == "assistant", messages[i].kind == .text else { return }
                messages[i].content = stripToolArtifacts(messages[i].content)
                messages[i].isStreaming = false
                if isDebrisContent(messages[i].content)
                    && (messages[i].thinking ?? "").isEmpty
                    && messages[i].webSources.isEmpty
                    && messages[i].attachments.isEmpty {
                    messages.remove(at: i)
                }
            }

            do {
                let stream = api.chatStream(
                    message: trimmed,
                    sessionID: sid,
                    agentMode: agentMode,
                    useWeb: useWeb,
                    thinking: thinkingOn ? nil : false,
                    attachmentIDs: atts.map(\.id),
                    allowBash: allowBash,
                    permissionMode: permissionMode,
                    askPermission: askBeforeCommands,
                    // Never send the transient "_streaming_" placeholder — the
                    // backend can't resolve it, so the AI loses the open-doc
                    // context and creates a new file / asks "which document?".
                    activeDocID: (editorVisible && activeDocID != streamingDocID) ? activeDocID : nil,
                    model: selectedModel?.modelID,
                    endpointID: selectedModel?.endpointID,
                    endpointURL: selectedModel?.endpointURL,
                    effort: effortFor(selectedModel)
                )

                for try await event in stream {
                    // If the user has switched to another chat, go headless:
                    // keep draining (so the backend finishes + persists) but stop
                    // mutating the UI, which now belongs to a different session.
                    if selectedSessionID != sid { detached = true }
                    if detached { continue }
                    switch event {
                    case .delta(let d):
                        buffer += d
                        if Date().timeIntervalSince(lastFlush) > 0.15 { flush() }
                    case .thinkingDelta(let d):
                        thinkBuffer += d
                        if Date().timeIntervalSince(lastFlush) > 0.15 { flush() }
                    case .toolStart(let tool, let command):
                        if isObsoleteBackendTool(tool) { break }
                        // Round narration before a tool call is agent
                        // reasoning — collapse it like thinking.
                        if let i = messages.indices.last,
                           messages[i].role == "assistant", messages[i].kind == .text {
                            flush()
                            messages[i].isIntermediate = true
                        }
                        finalizeTextSegment()
                        var row = Message(role: "tool", kind: .tool, content: "")
                        row.toolName = tool
                        row.command = command
                        messages.append(row)
                    case .toolOutput(let tool, let output, let exitCode):
                        if isObsoleteBackendTool(tool) { break }
                        if let i = messages.lastIndex(where: { $0.kind == .tool && !$0.toolDone && $0.toolName == tool })
                            ?? messages.lastIndex(where: { $0.kind == .tool && !$0.toolDone }) {
                            messages[i].toolOutput = output
                            messages[i].exitCode = exitCode
                            messages[i].toolDone = true
                        }
                        // The AI's create_event tool resolves the date/time and
                        // returns the event JSON; write it into the real macOS
                        // Calendar via EventKit (backend only stores it locally).
                        if tool == "create_event", exitCode == 0 {
                            await writeAgentCalendarEvent(fromToolOutput: output)
                        }
                    case .permissionRequest(let id, let tool, let command):
                        pendingPermission = PendingPermission(id: id, tool: tool, command: command)
                    case .agentStep:
                        break
                    case .modelInfo(let m):
                        currentModel = m
                        messages[currentTextIndex()].modelName = m
                    case .metrics(let m, let ctx):
                        if let i = messages.lastIndex(where: { $0.role == "assistant" && $0.kind == .text }) {
                            messages[i].metrics = m
                            messages[i].contextPercent = ctx
                        }
                    case .webSources(let sources):
                        messages[currentTextIndex()].webSources += sources
                    case .researchStarted(let id):
                        researchDot = true
                        if !id.isEmpty { pendingResearchID = id }
                    case .memoriesUsed(let items):
                        appendMemoriesUsed(items, to: currentTextIndex())
                    case .docStreamOpen(let title, let language):
                        handleDocStreamOpen(title: title, language: language)
                    case .docStreamDelta(let content):
                        if let i = docIndex(streamingDocID) ?? docIndex(activeDocID) {
                            openDocs[i].content = content
                        }
                    case .docUpdate(let id, let title, let language, let content, let version):
                        handleDocUpdate(id: id, title: title, language: language, content: content, version: version)
                        refreshTree()   // AI may have created/renamed files on disk
                    case .docSuggestions(let docID, let suggestions):
                        if let i = docIndex(docID) ?? docIndex(activeDocID) {
                            openDocs[i].suggestions = suggestions
                            editorVisible = true
                        }
                    case .error(let e):
                        flush()
                        messages[currentTextIndex()].content += "\n\n> ⚠️ \(e)"
                    case .done:
                        break
                    }
                }
                if !detached { finalizeTextSegment() }
            } catch is CancellationError {
                if !detached { finalizeTextSegment() }
            } catch {
                if !detached {
                    flush()
                    messages[currentTextIndex()].content += "\n\n> ⚠️ \(error.localizedDescription)"
                }
            }
            workingSessions.remove(sid)
            if !detached {
                // Close out any rows still marked streaming/running
                for i in messages.indices {
                    messages[i].isStreaming = false
                    if messages[i].kind == .tool { messages[i].toolDone = true }
                }
                for i in openDocs.indices { openDocs[i].aiWriting = false }
                // Consolidate the turn into the exact shape a history reload
                // produces (tool rows first, one text block) — otherwise what
                // you see live differs from what you see when you come back.
                normalizeTurn(from: turnStart)
                isStreaming = false
            } else if selectedSessionID == sid {
                // Came back to this chat after it finished headless — load the result.
                isStreaming = false
                await loadHistory(for: sid)
            }
            // Unstructured task: refresh survives even if this task was
            // cancelled by Stop — a cancelled refresh was how new chats
            // vanished from the sidebar.
            Task { await self.loadSessions() }
        }
    }

    /// Rebuild this turn's messages into canonical (reload-identical) form.
    private func normalizeTurn(from start: Int) {
        guard start < messages.count else { return }
        let turn = Array(messages[start...])
        let tools = turn.filter { $0.kind == .tool }
        let texts = turn.filter { $0.kind == .text && !$0.content.isEmpty }
        guard !texts.isEmpty || !tools.isEmpty else { return }

        var final = Message(role: "assistant",
                            content: stripToolArtifacts(texts.map(\.content).joined(separator: "\n\n")))
        final.thinking = texts.compactMap(\.thinking).first
        final.thinkingTime = texts.compactMap(\.thinkingTime).first
        final.modelName = texts.compactMap(\.modelName).last ?? selectedModel?.modelID
        final.metrics = texts.compactMap(\.metrics).last
        final.contextPercent = texts.compactMap(\.contextPercent).last
        final.memoriesUsed = texts.flatMap(\.memoriesUsed)
        final.attachments = texts.flatMap(\.attachments)

        var rebuilt = tools
        if !isDebrisContent(final.content) || final.thinking != nil || !final.attachments.isEmpty {
            rebuilt.append(final)
        }
        messages.replaceSubrange(start..., with: rebuilt)
    }

    // Reasoning-effort support — frontier models only
    var effort: String = UserDefaults.standard.string(forKey: "effort") ?? "medium"

    func supportsEffort(_ model: ModelChoice?) -> Bool {
        guard let m = model?.modelID.lowercased() else { return false }
        return ["gpt-5", "o1", "o3", "o4", "gpt-oss"].contains { m.contains($0) }
    }

    private func effortFor(_ model: ModelChoice?) -> String? {
        supportsEffort(model) ? effort : nil
    }

    private func sendViaAppleIntelligence(_ text: String) {
        streamTask = Task {
            let chatKey = selectedSessionID ?? "new"
            guard AppleIntelligence.isAvailable else {
                if let i = messages.indices.last, !messages[i].isUser {
                    messages[i].content = "> ⚠️ Apple Intelligence isn't ready (still loading after boot, or disabled in System Settings → Apple Intelligence & Siri). Try again shortly."
                    messages[i].isStreaming = false
                }
                isStreaming = false
                return
            }
            do {
                let sid = try await ensureSession(firstMessage: text)
                var buffer = ""
                var lastFlush = Date()
                for try await cumulative in AppleIntelligence.stream(chatID: chatKey, prompt: text, history: messages) {
                    buffer = cumulative
                    if Date().timeIntervalSince(lastFlush) > 0.08 {
                        if let i = messages.indices.last, !messages[i].isUser {
                            messages[i].content = buffer
                            messages[i].modelName = "Apple Intelligence"
                        }
                        lastFlush = Date()
                    }
                }
                if let i = messages.indices.last, !messages[i].isUser {
                    messages[i].content = buffer
                    messages[i].modelName = "Apple Intelligence"
                    messages[i].isStreaming = false
                }
                // Persist both turns locally so the transcript stays unified
                await api.appendMessage(sessionID: sid, role: "user", content: text)
                await api.appendMessage(sessionID: sid, role: "assistant", content: buffer,
                                        metadata: ["model": "Apple Intelligence (on-device)"])
            } catch {
                if let i = messages.indices.last, !messages[i].isUser {
                    messages[i].content += "\n\n> ⚠️ Apple Intelligence: \(error.localizedDescription)"
                    messages[i].isStreaming = false
                }
            }
            isStreaming = false
        }
    }

    func resend(_ message: Message) {
        guard !isStreaming else { return }
        send(message.content)
    }

    /// Remove a sent message from the conversation (server + local).
    func unsend(_ message: Message) {
        guard let sid = selectedSessionID else {
            messages.removeAll { $0.id == message.id }
            return
        }
        Task {
            var dbID = message.dbID
            if dbID == nil {
                // Freshly-sent messages don't know their server id yet —
                // look it up by matching the latest history row.
                if let h = try? await api.history(sid) {
                    dbID = h.history.last(where: {
                        $0.role == message.role && $0.content == message.content
                    })?.metadata?["_db_id"]?.stringValue
                }
            }
            if let dbID {
                try? await api.deleteMessages(sessionID: sid, ids: [dbID])
            }
            messages.removeAll { $0.id == message.id }
        }
    }

    func stopStreaming() {
        guard isStreaming else { return }
        streamTask?.cancel()
        if let sid = selectedSessionID {
            Task {
                await api.stopStream(sessionID: sid)
                await self.loadSessions()
            }
        }
        mutateLast { $0.isStreaming = false }
        isStreaming = false
    }

    private func ensureSession(firstMessage: String) async throws -> String {
        if let sid = selectedSessionID { return sid }
        let model = selectedModel
        let name = String(firstMessage.prefix(48))
        let created = try await api.createSession(
            name: name.isEmpty ? "New Chat" : name,
            model: model?.modelID ?? "",
            endpointURL: model?.endpointURL ?? "",
            endpointID: model?.endpointID ?? ""
        )
        selectedSessionID = created.id
        sessions.insert(created, at: 0)
        return created.id
    }

    private func mutateLast(_ change: (inout Message) -> Void) {
        guard let i = messages.indices.last, !messages[i].isUser else { return }
        change(&messages[i])
    }

    // MARK: Editor pane (co-editing)

    private let streamingDocID = "_streaming_"

    func docIndex(_ id: String?) -> Int? {
        guard let id else { return nil }
        return openDocs.firstIndex { $0.id == id }
    }

    func openDoc(_ d: DocDetail) {
        if let i = docIndex(d.id) {
            // Re-clicking an open file focuses its tab; never clobber
            // unsaved local edits with the server copy.
            if !openDocs[i].dirty, let fresh = d.currentContent {
                openDocs[i].content = fresh
            }
        } else {
            openDocs.append(EditorDoc(
                id: d.id,
                title: d.title ?? "Untitled",
                language: d.language ?? Self.languageFromTitle(d.title) ?? "markdown",
                content: d.currentContent ?? "",
                version: d.versionCount ?? 1))
        }
        activeDocID = d.id
        editorVisible = true
    }

    /// Filename-extension → language, for docs the server didn't classify.
    /// Image types that open in the editor pane (like PDFs) rather than Quick Look.
    static let imageExts: Set<String> = ["png", "jpg", "jpeg", "gif", "webp", "heic", "bmp", "tiff", "tif"]

    static func languageFromTitle(_ title: String?) -> String? {
        guard let ext = (title as NSString?)?.pathExtension.lowercased(), !ext.isEmpty else { return nil }
        let map: [String: String] = [
            "svg": "svg", "py": "python", "js": "javascript", "ts": "typescript",
            "swift": "swift", "html": "html", "htm": "html", "css": "css",
            "json": "json", "sh": "shell", "zsh": "shell", "yaml": "yaml",
            "yml": "yaml", "xml": "xml", "md": "markdown", "markdown": "markdown",
            "txt": "text", "eml": "email", "csv": "csv", "toml": "toml",
            "rs": "rust", "go": "go", "c": "c", "cpp": "cpp", "h": "c",
        ]
        return map[ext]
    }

    func closeDoc(_ id: String) {
        if let i = docIndex(id), openDocs[i].dirty, openDocs[i].id != streamingDocID,
           openDocs[i].localFileURL == nil {
            // Auto-save dirty docs on close — never silently drop edits.
            // saveDoc routes .docx through the converter and .md to disk.
            saveDoc(id)
        }
        openDocs.removeAll { $0.id == id }
        if activeDocID == id { activeDocID = openDocs.last?.id }
        if openDocs.isEmpty { editorVisible = false }
    }

    func saveDoc(_ id: String) {
        guard let i = docIndex(id), openDocs[i].id != streamingDocID else { return }
        let doc = openDocs[i]
        // Rich .doc/.docx tabs persist themselves from the NSTextView (preserving
        // formatting); the String content path would clobber that.
        if doc.richDoc { return }
        if let path = doc.localPath {
            // Bound-folder file: write straight to disk, no backend hop.
            do {
                try doc.content.write(toFile: path, atomically: true, encoding: .utf8)
                openDocs[i].dirty = false
            } catch {
                loadError = "Save \(doc.title): " + error.localizedDescription
            }
            return
        }
        Task {
            do {
                try await api.updateDocument(id: doc.id, content: doc.content)
                if let j = docIndex(id) { openDocs[j].dirty = false }
            } catch {
                loadError = "Save: " + error.localizedDescription
            }
        }
    }

    func saveDocxExtra(docID: String, region: String, content: String) {
        guard let i = docIndex(docID),
              let sid = selectedSessionID,
              openDocs[i].richDoc,
              openDocs[i].title.lowercased().hasSuffix(".docx"),
              docID.hasPrefix("local-") else { return }
        let sub = String(docID.dropFirst("local-".count))
        Task {
            do {
                try await api.updateDocxExtra(sid: sid, sub: sub, region: region, content: content)
                if let j = docIndex(docID) {
                    if region == "header" {
                        openDocs[j].docHeader = content
                    } else if region == "footer" {
                        openDocs[j].docFooter = content
                    } else if region == "footnotes" {
                        openDocs[j].docFootnotes = content
                    }
                    openDocs[j].dirty = false
                }
            } catch {
                loadError = "Save \(region): " + error.localizedDescription
            }
        }
    }

    /// Debounced autosave — fires ~1.2s after the last keystroke.
    /// Never fires while the agent is streaming: the AI edits the doc
    /// server-side mid-run, and an autosave of our tab's copy would
    /// overwrite those edits with stale content (proven work-loss).
    func scheduleAutosave(_ id: String) {
        autosaveTask?.cancel()
        autosaveTask = Task {
            try? await Task.sleep(for: .milliseconds(1200))
            guard !Task.isCancelled else { return }
            if let i = docIndex(id), openDocs[i].dirty, !openDocs[i].aiWriting,
               !isStreaming {
                saveDoc(id)
            }
        }
    }

    /// Approve or reject a pending AI rewrite of an open document.
    func resolvePendingAIEdit(docID: String, accept: Bool) {
        guard let i = docIndex(docID), let pending = openDocs[i].pendingAIContent else { return }
        if accept {
            openDocs[i].content = pending
            openDocs[i].dirty = false       // server already has this version
        } else {
            openDocs[i].dirty = true
            saveDoc(docID)                  // push our version back over the AI's
        }
        openDocs[i].pendingAIContent = nil
    }

    /// Accept applies find→replace and saves; dismiss just drops the chip.
    func resolveSuggestion(docID: String, suggestion: DocSuggestion, accept: Bool) {
        guard let i = docIndex(docID) else { return }
        if accept {
            openDocs[i].content = openDocs[i].content.replacingOccurrences(
                of: suggestion.find, with: suggestion.replace)
            openDocs[i].dirty = true
            saveDoc(docID)
        }
        openDocs[i].suggestions.removeAll { $0.id == suggestion.id }
    }

    private func handleDocStreamOpen(title: String, language: String) {
        if let i = docIndex(streamingDocID) {
            openDocs[i].title = title.isEmpty ? openDocs[i].title : title
            openDocs[i].language = language.isEmpty ? openDocs[i].language : language
            openDocs[i].content = ""
            openDocs[i].aiWriting = true
        } else {
            var d = EditorDoc(id: streamingDocID,
                              title: title.isEmpty ? "Writing…" : title,
                              language: language.isEmpty ? "markdown" : language,
                              content: "")
            d.aiWriting = true
            openDocs.append(d)
        }
        activeDocID = streamingDocID
        editorVisible = true
    }

    /// Find the open tab an AI doc edit refers to. Match by exact id, then exact
    /// title, then FILENAME (so a path-prefix or id-scheme difference between the
    /// tool call and how the tab was opened still updates the right doc instead
    /// of silently appending a hidden duplicate).
    private func matchOpenDoc(id: String, title: String) -> Int? {
        if let i = docIndex(id) { return i }
        if !title.isEmpty, let i = openDocs.firstIndex(where: { $0.title == title }) { return i }
        let base = ((id.hasPrefix("local-") ? String(id.dropFirst(6)) : id) as NSString).lastPathComponent
        let titleBase = (title as NSString).lastPathComponent
        return openDocs.firstIndex(where: { d in
            let db = (d.title as NSString).lastPathComponent
            return !db.isEmpty && (db == base || (!titleBase.isEmpty && db == titleBase))
        })
    }

    private func handleDocUpdate(id: String, title: String, language: String?, content: String, version: Int?) {
        // Migrate the streaming temp tab to its real identity, or update an
        // already-open tab (the AI edited the doc you're looking at).
        if let i = docIndex(streamingDocID) {
            openDocs[i].id = id
            if !title.isEmpty { openDocs[i].title = title }
            if let language { openDocs[i].language = language }
            openDocs[i].content = content
            openDocs[i].version = version ?? openDocs[i].version
            openDocs[i].aiWriting = false
            openDocs[i].dirty = false
            if activeDocID == streamingDocID { activeDocID = id }
        } else if let i = matchOpenDoc(id: id, title: title) {
            openDocs[i].id = id
            if openDocs[i].richDoc {
                // The rich .doc/.docx editor reads from disk; the server already
                // wrote the file, so force it to re-read and show the AI's edit.
                openDocs[i].reloadNonce += 1
            } else if requireDocApproval && !openDocs[i].content.isEmpty && openDocs[i].content != content {
                // Gate AI edits to an open doc behind explicit approval —
                // the server already saved its version; Reject restores ours.
                openDocs[i].pendingAIContent = content
            } else {
                openDocs[i].content = content
                openDocs[i].dirty = false
            }
            openDocs[i].version = version ?? openDocs[i].version
            openDocs[i].aiWriting = false
        } else {
            openDocs.append(EditorDoc(
                id: id, title: title.isEmpty ? "Untitled" : title,
                language: language ?? "markdown", content: content,
                version: version ?? 1))
        }
        activeDocID = id
        editorVisible = true
    }

    // MARK: Bound folder (workdir) tree — native FileManager.
    //
    // Folder binding is still explicit even though the production app is not
    // app-sandboxed: the picker chooses the workdir and the agent permission
    // mode controls whether file tools stay inside it.

    private var workdirAccessURL: URL?   // held open while a folder is bound

    /// Persist a security-scoped bookmark so access survives relaunches.
    func saveWorkdirBookmark(for url: URL) {
        var bookmarks = UserDefaults.standard.dictionary(forKey: "workdirBookmarks") as? [String: Data] ?? [:]
        bookmarks[url.path] = try? url.bookmarkData(options: .withSecurityScope)
        UserDefaults.standard.set(bookmarks, forKey: "workdirBookmarks")
    }

    private func startWorkdirAccess(_ path: String) {
        stopWorkdirAccess()
        var bookmarks = UserDefaults.standard.dictionary(forKey: "workdirBookmarks") as? [String: Data] ?? [:]
        guard let data = bookmarks[path] else { return }
        var stale = false
        guard let url = try? URL(resolvingBookmarkData: data, options: .withSecurityScope,
                                 relativeTo: nil, bookmarkDataIsStale: &stale),
              url.startAccessingSecurityScopedResource() else { return }
        workdirAccessURL = url
        if stale, let fresh = try? url.bookmarkData(options: .withSecurityScope) {
            bookmarks[path] = fresh
            UserDefaults.standard.set(bookmarks, forKey: "workdirBookmarks")
        }
    }

    private func stopWorkdirAccess() {
        workdirAccessURL?.stopAccessingSecurityScopedResource()
        workdirAccessURL = nil
    }

    /// Jailed URL inside the bound folder — nil if sub escapes it.
    private func workdirURL(_ sub: String = "") -> URL? {
        guard let root = workdirPath else { return nil }
        let rootURL = URL(fileURLWithPath: root)
        guard !sub.isEmpty else { return rootURL }
        let target = rootURL.appendingPathComponent(sub).standardizedFileURL
        guard target.path == rootURL.path || target.path.hasPrefix(rootURL.path + "/") else { return nil }
        return target
    }

    private func listEntries(_ sub: String) -> [WorkdirEntry] {
        guard let dir = workdirURL(sub) else { return [] }
        let urls = (try? FileManager.default.contentsOfDirectory(
            at: dir, includingPropertiesForKeys: [.isDirectoryKey, .fileSizeKey],
            options: [.skipsHiddenFiles])) ?? []
        return urls.map { u in
            let vals = try? u.resourceValues(forKeys: [.isDirectoryKey, .fileSizeKey])
            return WorkdirEntry(name: u.lastPathComponent,
                                type: vals?.isDirectory == true ? "dir" : "file",
                                size: vals?.fileSize)
        }
    }

    func loadWorkdir() async {
        stopWorkdirAccess()
        workdirPath = nil
        treeEntries = [:]
        expandedDirs = []
        guard let sid = selectedSessionID else { return }
        workdirPath = try? await api.sessionWorkdir(sid: sid)
        if let path = workdirPath {
            startWorkdirAccess(path)
            treeEntries[""] = listEntries("")
        }
    }

    func bindWorkdir(_ path: String, device: String? = nil) async throws {
        // Fresh chat: create the session now so there's something to bind.
        let sid: String
        if let existing = selectedSessionID {
            sid = existing
        } else {
            sid = try await ensureSession(firstMessage: "New Chat")
            Task { await self.loadSessions() }
        }
        try await api.bindWorkdir(sid: sid, path: path, device: device)
        await loadWorkdir()
    }

    func toggleDir(_ sub: String) {
        if expandedDirs.contains(sub) {
            expandedDirs.remove(sub)
            return
        }
        expandedDirs.insert(sub)
        if treeEntries[sub] == nil {
            treeEntries[sub] = listEntries(sub)
        }
    }

    func refreshTree() {
        treeEntries[""] = listEntries("")
        for sub in expandedDirs {
            treeEntries[sub] = listEntries(sub)
        }
    }

    /// Open a file from the bound folder in the editor pane. Text files
    /// keep their on-disk path so saves write straight back to the file.
    func openWorkdirFile(sub: String) {
        guard let url = workdirURL(sub) else { return }
        let ext = url.pathExtension.lowercased()
        let name = url.lastPathComponent
        // Word files open in the native rich-text editor — rendered, edited,
        // and saved as real .doc/.docx (NSAttributedString does the I/O).
        if ext == "doc" || ext == "docx" {
            let id = "local-" + sub
            if let i = docIndex(id) {
                activeDocID = id
                editorVisible = true
                _ = i
                loadDocxExtrasIfNeeded(id: id, sub: sub, ext: ext)
                return
            }
            var d = EditorDoc(id: id, title: name, language: ext, content: "")
            d.localFileURL = url
            d.richDoc = true
            openDocs.append(d)
            activeDocID = id
            editorVisible = true
            loadDocxExtrasIfNeeded(id: id, sub: sub, ext: ext)
            return
        }
        let binary = ["mp4", "mov", "zip", "xlsx", "pptx", "key", "numbers", "pages"].contains(ext)
        if ext == "pdf" {
            // PDFs live in the editor pane next to the chat, like docs
            let id = "pdf-" + sub
            if docIndex(id) == nil {
                var d = EditorDoc(id: id, title: name, language: "pdf", content: "")
                d.localFileURL = url
                openDocs.append(d)
            }
            activeDocID = id
            editorVisible = true
            return
        }
        if Self.imageExts.contains(ext) {
            // Images open in the editor pane too, same as PDFs.
            let id = "img-" + sub
            if docIndex(id) == nil {
                var d = EditorDoc(id: id, title: name, language: "image", content: "")
                d.localFileURL = url
                openDocs.append(d)
            }
            activeDocID = id
            editorVisible = true
            return
        }
        if binary {
            quickLookURL = url
            return
        }
        guard let data = FileManager.default.contents(atPath: url.path) else {
            loadError = "Open \(name): file could not be read"
            return
        }
        if data.count > 2_000_000 || data.prefix(8192).contains(0) {
            quickLookURL = url   // too large / binary for the editor
            return
        }
        let id = "local-" + sub
        if let i = docIndex(id) {
            // Re-clicking focuses the tab; never clobber unsaved edits.
            if !openDocs[i].dirty {
                openDocs[i].content = String(decoding: data, as: UTF8.self)
            }
        } else {
            var d = EditorDoc(id: id, title: name,
                              language: Self.languageFromTitle(name) ?? "markdown",
                              content: String(decoding: data, as: UTF8.self))
            d.localPath = url.path
            openDocs.append(d)
        }
        activeDocID = id
        editorVisible = true
    }

    private func loadDocxExtrasIfNeeded(id: String, sub: String, ext: String) {
        guard ext == "docx", let sid = selectedSessionID else { return }
        Task {
            if let extras = try? await api.docxExtras(sid: sid, sub: sub),
               let j = docIndex(id) {
                openDocs[j].docHeader = extras.header.isEmpty ? nil : extras.header
                openDocs[j].docFooter = extras.footer.isEmpty ? nil : extras.footer
                openDocs[j].docFootnotes = extras.footnotes.isEmpty ? nil : extras.footnotes
            }
        }
    }

    /// Delete a file in the bound folder (right-click in Files).
    func deleteWorkdirFile(sub: String) {
        guard let url = workdirURL(sub) else { return }
        let name = url.lastPathComponent
        do {
            try FileManager.default.removeItem(at: url)
            // Close any editor tab mirroring the dead file.
            if let i = openDocs.firstIndex(where: { $0.title == name }) {
                let id = openDocs[i].id
                openDocs.remove(at: i)
                if activeDocID == id { activeDocID = openDocs.last?.id }
                if openDocs.isEmpty { editorVisible = false }
            }
            refreshTree()
        } catch {
            loadError = "Delete \(name): " + error.localizedDescription
        }
    }

    /// Rename a file or folder in the bound folder (right-click in Files).
    func renameWorkdirFile(sub: String, newName: String) {
        guard let url = workdirURL(sub) else { return }
        let oldName = url.lastPathComponent
        let trimmed = newName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed != oldName, !trimmed.contains("/") else { return }
        let dst = url.deletingLastPathComponent().appendingPathComponent(trimmed)
        do {
            try FileManager.default.moveItem(at: url, to: dst)
            // Close any editor tab mirroring the renamed file (reopen under new name).
            if let i = openDocs.firstIndex(where: { $0.title == oldName }) {
                let id = openDocs[i].id
                openDocs.remove(at: i)
                if activeDocID == id { activeDocID = openDocs.last?.id }
                if openDocs.isEmpty { editorVisible = false }
            }
            refreshTree()
        } catch {
            loadError = "Rename \(oldName): " + error.localizedDescription
        }
    }

    /// Open a library document from a [title](#document-<id>) chat anchor.
    func openDocumentByID(_ id: String) {
        Task {
            do {
                let d: DocDetail = try await api.getJSON("api/document/\(id)")
                openDoc(d)
            } catch {
                loadError = "Open document: " + error.localizedDescription
            }
        }
    }

    // MARK: Wallpaper

    func setWallpaper(from source: URL) {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("JoeBro", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        // Timestamped name: rewriting one fixed path meant the image cache
        // never busted and a new wallpaper only showed after relaunch.
        if let old = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil) {
            for f in old where f.lastPathComponent.hasPrefix("wallpaper") {
                try? FileManager.default.removeItem(at: f)
            }
        }
        let dest = dir.appendingPathComponent(
            "wallpaper-\(Int(Date().timeIntervalSince1970))."
            + (source.pathExtension.isEmpty ? "jpg" : source.pathExtension))
        do {
            try FileManager.default.copyItem(at: source, to: dest)
            wallpaperURL = dest
        } catch {
            loadError = "Couldn't set wallpaper: \(error.localizedDescription)"
        }
    }

    func clearWallpaper() {
        if let url = wallpaperURL { try? FileManager.default.removeItem(at: url) }
        wallpaperURL = nil
    }
}
