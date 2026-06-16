import Foundation
import FoundationModels

/// On-device Apple Intelligence via the FoundationModels framework.
/// Free, private, no backend round-trip. (Private Cloud Compute isn't exposed
/// to third-party apps — the system decides; this API is the on-device
/// model.) Replies persist to the local chat session afterwards so
/// the transcript stays unified.
enum AppleIntelligence {
    static var isAvailable: Bool {
        if case .available = SystemLanguageModel.default.availability { return true }
        return false
    }

    private static var sessions: [String: LanguageModelSession] = [:]

    static func reset(chatID: String) {
        sessions.removeValue(forKey: chatID)
    }

    /// Streams cumulative response text for the prompt within the chat's
    /// running on-device session (context carries across turns).
    static func stream(chatID: String, prompt: String, history: [Message]) -> AsyncThrowingStream<String, Error> {
        let session: LanguageModelSession
        if let existing = sessions[chatID] {
            session = existing
        } else {
            // Seed a new on-device session with recent context from the chat.
            let recent = history.suffix(8)
                .filter { $0.kind == .text && !$0.content.isEmpty }
                .map { "\($0.isUser ? "User" : "Assistant"): \($0.content.prefix(600))" }
                .joined(separator: "\n")
            let now = Date().formatted(date: .complete, time: .shortened)
            let instructions = """
            You are JoeBro, a private local assistant running on-device. \
            Today is \(now) — trust this date over your training cutoff. \
            You have NO web access; say so if asked to search. \
            Answer directly and concisely in markdown. Never narrate your reasoning.
            \(recent.isEmpty ? "" : "Recent conversation:\n\(recent)")
            """
            session = LanguageModelSession(instructions: instructions)
            sessions[chatID] = session
        }

        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let stream = session.streamResponse(to: prompt)
                    for try await partial in stream {
                        continuation.yield(partial.content)
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}
