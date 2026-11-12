import Foundation

/// Marketing screenshots and first looks without a backend. Launch with
/// `-JoeBroDemo` (or `JOEBRO_DEMO=1` in the environment) and the app talks to a
/// fake host answered from the bundled `demo.json`: chats with several models,
/// linked coding agents with subagents, documents and a calendar. Nothing is
/// read from or written to the real backend, and the bundled backend is not started.
enum DemoMode {
    static let active: Bool = {
        ProcessInfo.processInfo.arguments.contains("-JoeBroDemo")
            || ProcessInfo.processInfo.environment["JOEBRO_DEMO"] == "1"
    }()
    static let host = "demo.joebro.invalid"
    static let url = URL(string: "https://\(host)")!

    static func install(_ config: URLSessionConfiguration) {
        guard active else { return }
        config.protocolClasses = [DemoURLProtocol.self] + (config.protocolClasses ?? [])
    }
}

/// Serves `"METHOD /path"` entries of demo.json. Unlisted writes succeed with
/// `{"ok":true}` and change nothing; unlisted reads are a 404. Tokens such as
/// `{{d-1:09:30}}` become a local date-time relative to today, so the calendar
/// always looks current.
final class DemoURLProtocol: URLProtocol {
    private static let table: [String: Any] = {
        guard let file = Bundle.main.url(forResource: "demo", withExtension: "json"),
              let data = try? Data(contentsOf: file),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return object
    }()

    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == DemoMode.host }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}

    private static func resolveDates(_ text: String) -> String {
        guard let re = try? NSRegularExpression(pattern: #"\{\{d([+-]?\d+):(\d\d):(\d\d)\}\}"#) else { return text }
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "en_US_POSIX")
        fmt.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        let ns = text as NSString
        var out = ""
        var last = 0
        for m in re.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
            out += ns.substring(with: NSRange(location: last, length: m.range.location - last))
            let day = Int(ns.substring(with: m.range(at: 1))) ?? 0
            let h = Int(ns.substring(with: m.range(at: 2))) ?? 0
            let mi = Int(ns.substring(with: m.range(at: 3))) ?? 0
            let cal = Calendar.current
            let base = cal.date(byAdding: .day, value: day, to: cal.startOfDay(for: Date())) ?? Date()
            out += fmt.string(from: cal.date(bySettingHour: h, minute: mi, second: 0, of: base) ?? base)
            last = m.range.location + m.range.length
        }
        return out + ns.substring(from: last)
    }

    override func startLoading() {
        guard let url = request.url else { return }
        let method = request.httpMethod ?? "GET"
        let path = url.path
        let keys = [url.query.map { "\(method) \(path)?\($0)" }, "\(method) \(path)"].compactMap { $0 }
        var status = 200
        var type = "application/json"
        var body = Data()
        if path == "/api/chat_stream" {
            type = "text/event-stream"
            for event in (Self.table["chat_events"] as? [[String: Any]]) ?? [] {
                let json = (try? JSONSerialization.data(withJSONObject: event)) ?? Data()
                body.append(Data("data: ".utf8) + json + Data("\n\n".utf8))
            }
            let reply = (Self.table["chat_reply"] as? String) ?? "This is demo mode."
            let words = reply.split(separator: " ", omittingEmptySubsequences: false)
            for (i, word) in words.enumerated() {
                let piece = String(word) + (i < words.count - 1 ? " " : "")
                let json = (try? JSONSerialization.data(withJSONObject: ["delta": piece])) ?? Data()
                body.append(Data("data: ".utf8) + json + Data("\n\n".utf8))
            }
            body.append(Data("data: [DONE]\n\n".utf8))
        } else if let key = keys.first(where: { Self.table[$0] != nil }),
                  let data = try? JSONSerialization.data(withJSONObject: Self.table[key]!, options: [.fragmentsAllowed]) {
            body = Data(Self.resolveDates(String(decoding: data, as: UTF8.self)).utf8)
        } else if method == "GET" {
            status = 404
            body = Data(#"{"detail":"Not part of the demo"}"#.utf8)
        } else {
            body = Data(#"{"ok":true}"#.utf8)
        }
        let response = HTTPURLResponse(url: url, statusCode: status, httpVersion: "HTTP/1.1",
                                       headerFields: ["Content-Type": type])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }
}
