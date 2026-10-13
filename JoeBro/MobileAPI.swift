import Foundation

/// Backend calls for phone access, push and the inbox (1.3.0). Thin wrappers over
/// APIClient's generic JSON helpers; only the APNs key upload needs its own
/// multipart request.
extension APIClient {
    func mobileStatus() async throws -> JSONValue { try await getJSON("api/mobile") }

    func setMobile(enabled: Bool, bind: String) async throws {
        try await sendJSON("api/mobile/enable", body: ["enabled": enabled, "bind": bind])
    }

    func rotateMobileToken() async throws {
        try await sendJSON("api/mobile/rotate")
    }

    func mobileDevices() async throws -> [JSONValue] {
        let r: JSONValue = try await getJSON("api/mobile/devices")
        return r["devices"]?.arrayValue ?? []
    }

    func removeMobileDevice(_ id: String) async throws {
        try await sendJSON("api/mobile/devices/\(id)", method: "DELETE")
    }

    func pushStatus() async throws -> JSONValue { try await getJSON("api/push/status") }

    func removeAPNsKey() async throws {
        try await sendJSON("api/mobile/apns", method: "DELETE")
    }

    func sendTestNotification() async throws {
        try await sendJSON("api/notify", body: ["message": "Test from JoeBro", "channel": "mobile"])
    }

    /// Multipart upload of the .p8 key plus team/key/bundle ids.
    func saveAPNs(keyFile: URL, teamID: String, keyID: String, bundleID: String) async throws {
        let boundary = "joebro-\(UUID().uuidString)"
        var body = Data()
        func field(_ name: String, _ value: String) {
            body.append("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"\r\n\r\n\(value)\r\n".data(using: .utf8)!)
        }
        field("team_id", teamID)
        field("key_id", keyID)
        field("bundle_id", bundleID)
        body.append("--\(boundary)\r\nContent-Disposition: form-data; name=\"key\"; filename=\"\(keyFile.lastPathComponent)\"\r\nContent-Type: application/octet-stream\r\n\r\n".data(using: .utf8)!)
        body.append(try Data(contentsOf: keyFile))
        body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)
        var req = URLRequest(url: baseURL.appendingPathComponent("api/mobile/apns"))
        req.httpMethod = "POST"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard let http = resp as? HTTPURLResponse else { throw APIError.badResponse }
        guard (200..<300).contains(http.statusCode) else {
            let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            throw APIError.http(http.statusCode, (obj?["detail"] as? String) ?? "")
        }
    }

    // MARK: Inbox

    func inbox() async throws -> [InboxItem] {
        let r: JSONValue = try await getJSON("api/inbox")
        return (r["inbox"]?.arrayValue ?? []).compactMap { v in
            guard let id = v["id"]?.stringValue ?? v["id"]?.intValue.map(String.init) else { return nil }
            return InboxItem(id: id, kind: v["kind"]?.stringValue ?? "",
                             title: v["title"]?.stringValue ?? "",
                             body: v["body"]?.stringValue ?? "",
                             read: (v["read"]?.boolValue ?? false) || (v["read"]?.intValue ?? 0) != 0,
                             createdAt: v["created_at"]?.stringValue)
        }
    }

    func markInboxRead(_ id: String?) async {
        _ = try? await sendJSON("api/inbox/read", body: id.map { ["id": $0] } ?? [:])
    }

    func deleteInbox(ids: [String]) async {
        _ = try? await sendJSON("api/inbox/delete", body: ["ids": ids])
    }

    func clearInbox() async {
        _ = try? await sendJSON("api/inbox/delete", body: ["all": true])
    }

    // MARK: Phone memory (Muse files)

    func museFiles() async throws -> [String] {
        let r: JSONValue = try await getJSON("api/muse")
        return (r["files"]?.arrayValue ?? []).compactMap { $0["name"]?.stringValue ?? $0.stringValue }
    }

    func museRead(_ name: String) async throws -> String {
        let r: JSONValue = try await getJSON("api/muse/\(name)")
        return r["content"]?.stringValue ?? ""
    }

    func museWrite(_ name: String, content: String) async throws {
        try await sendJSON("api/muse/\(name)", method: "PUT", body: ["content": content])
    }

    func museCreate(_ name: String) async throws {
        try await sendJSON("api/muse", body: ["name": name, "content": ""])
    }

    func museDelete(_ name: String) async throws {
        try await sendJSON("api/muse/\(name)", method: "DELETE")
    }

    // MARK: Phone-agent tasks

    func personalTasks() async throws -> [TaskItem] {
        let r: TasksResponse = try await getJSON("api/tasks", query: ["scope": "personal"])
        return r.tasks
    }

    func createStarterTask(_ id: String) async throws {
        try await sendJSON("api/tasks/starters", body: ["id": id])
    }
}

struct InboxItem: Identifiable, Hashable {
    var id: String
    var kind: String
    var title: String
    var body: String
    var read: Bool
    var createdAt: String?

    var icon: String {
        switch kind {
        case "heartbeat", "pulse": return "waveform.path.ecg"
        case "task": return "clock.badge.checkmark"
        case "agent": return "sparkles"
        case "test": return "bell"
        default: return "tray"
        }
    }
}
