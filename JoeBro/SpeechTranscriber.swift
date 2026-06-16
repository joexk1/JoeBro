import Foundation
import Speech

/// On-device speech-to-text. Apple's recognizer runs first, with the local
/// backend transcription route available as a fallback.
enum SpeechTranscriber {
    static func requestAuth() async -> Bool {
        await withCheckedContinuation { cont in
            SFSpeechRecognizer.requestAuthorization { status in
                cont.resume(returning: status == .authorized)
            }
        }
    }

    static func transcribe(url: URL) async throws -> String {
        if await requestAuth(),
           let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-GB")) ?? SFSpeechRecognizer(),
           recognizer.isAvailable {
            do {
                return try await recognize(url: url, with: recognizer)
            } catch {
                // fall through to Whisper
            }
        }
        return try await APIClient.shared.transcribe(audioURL: url)
    }

    private static func recognize(url: URL, with recognizer: SFSpeechRecognizer) async throws -> String {
        try await withCheckedThrowingContinuation { cont in
            let req = SFSpeechURLRecognitionRequest(url: url)
            req.shouldReportPartialResults = false
            if recognizer.supportsOnDeviceRecognition {
                req.requiresOnDeviceRecognition = true
            }
            var finished = false
            recognizer.recognitionTask(with: req) { result, error in
                guard !finished else { return }
                if let result, result.isFinal {
                    finished = true
                    cont.resume(returning: result.bestTranscription.formattedString)
                } else if let error {
                    finished = true
                    cont.resume(throwing: error)
                }
            }
        }
    }
}
