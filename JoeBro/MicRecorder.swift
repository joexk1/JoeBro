import AVFoundation
import SwiftUI

/// Records mic audio to an m4a temp file, then transcribes via the
/// backend Whisper (/api/stt/transcribe).
@MainActor
@Observable
final class MicRecorder {
    var isRecording = false
    var isTranscribing = false

    private var recorder: AVAudioRecorder?

    func toggle(onText: @escaping (String) -> Void) {
        if isRecording { stop(onText: onText) } else { start() }
    }

    private func start() {
        AVCaptureDevice.requestAccess(for: .audio) { granted in
            guard granted else { return }
            Task { @MainActor in
                let url = FileManager.default.temporaryDirectory
                    .appendingPathComponent("joebro-voice-\(UUID().uuidString).m4a")
                let settings: [String: Any] = [
                    AVFormatIDKey: kAudioFormatMPEG4AAC,
                    AVSampleRateKey: 16000,
                    AVNumberOfChannelsKey: 1,
                    AVEncoderAudioQualityKey: AVAudioQuality.medium.rawValue,
                ]
                self.recorder = try? AVAudioRecorder(url: url, settings: settings)
                self.recorder?.record()
                self.isRecording = self.recorder?.isRecording ?? false
            }
        }
    }

    private func stop(onText: @escaping (String) -> Void) {
        guard let recorder else { return }
        recorder.stop()
        isRecording = false
        let url = recorder.url
        self.recorder = nil
        isTranscribing = true
        Task {
            defer { isTranscribing = false }
            if let text = try? await SpeechTranscriber.transcribe(url: url), !text.isEmpty {
                onText(text)
            }
            try? FileManager.default.removeItem(at: url)
        }
    }
}
