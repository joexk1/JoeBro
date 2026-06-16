import AVFoundation
import SwiftUI

/// Realtime hands-free voice loop — native port of the web app's speech.js:
/// calibrate noise floor → detect voice (RMS VAD) → record utterance →
/// backend Whisper STT → send to chat → speak the reply → listen again.
/// Speaking while the reply plays interrupts it (barge-in), same as web.
@MainActor
@Observable
final class VoiceMode: NSObject, AVSpeechSynthesizerDelegate {
    enum State: String {
        case idle, listening, recording, thinking, speaking
    }

    var state: State = .idle
    var active = false

    private let engine = AVAudioEngine()
    private let synth = AVSpeechSynthesizer()
    private var frames: [Float] = []
    private var preroll: [[Float]] = []
    private var noiseFloor: Float = 0.006
    private var calibrationFrames = 0
    private var voicedFrames = 0
    private var silentFrames = 0
    private var sampleRate: Double = 16000
    private weak var store: AppStore?

    // Tuned to match speech.js: ~3 voiced frames to start, ~0.8s silence to stop
    private let startFrames = 3
    private let endSilenceFrames = 14
    private let minUtteranceFrames = 10

    override init() {
        super.init()
        synth.delegate = self
    }

    func toggle(store: AppStore) {
        if active { stop() } else { start(store: store) }
    }

    func start(store: AppStore) {
        self.store = store
        AVCaptureDevice.requestAccess(for: .audio) { granted in
            guard granted else { return }
            Task { @MainActor in self.beginListening() }
        }
    }

    func stop() {
        active = false
        state = .idle
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        synth.stopSpeaking(at: .immediate)
        frames = []
        preroll = []
    }

    private func beginListening() {
        active = true
        state = .listening
        calibrationFrames = 0
        noiseFloor = 0.006
        voicedFrames = 0
        silentFrames = 0
        frames = []
        preroll = []

        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        sampleRate = format.sampleRate
        input.installTap(onBus: 0, bufferSize: 2048, format: format) { [weak self] buffer, _ in
            guard let self else { return }
            let level = Self.rms(buffer)
            let samples = Self.mono(buffer)
            Task { @MainActor in self.processFrame(level: level, samples: samples) }
        }
        engine.prepare()
        try? engine.start()
    }

    private func processFrame(level: Float, samples: [Float]) {
        guard active else { return }

        // First ~20 frames calibrate the room's noise floor (speech.js RT.calib)
        if calibrationFrames < 20 {
            calibrationFrames += 1
            noiseFloor = max(noiseFloor, level * 1.4)
            return
        }
        let loud = level > max(noiseFloor * 2.2, 0.012)

        switch state {
        case .listening:
            preroll.append(samples)
            if preroll.count > 6 { preroll.removeFirst() }
            if loud {
                voicedFrames += 1
                if voicedFrames >= startFrames { startUtterance() }
            } else {
                voicedFrames = 0
            }
        case .speaking:
            // Barge-in: user talks over the reply → cut TTS, start recording
            if loud {
                voicedFrames += 1
                if voicedFrames >= startFrames + 1 {
                    synth.stopSpeaking(at: .immediate)
                    startUtterance()
                }
            } else {
                voicedFrames = 0
            }
        case .recording:
            frames.append(contentsOf: samples)
            if loud {
                silentFrames = 0
            } else {
                silentFrames += 1
                if silentFrames >= endSilenceFrames { endUtterance() }
            }
        default:
            break
        }
    }

    private func startUtterance() {
        state = .recording
        frames = preroll.flatMap { $0 }
        preroll = []
        silentFrames = 0
        voicedFrames = 0
    }

    private func endUtterance() {
        guard frames.count > minUtteranceFrames * 2048 / 2 else {
            state = .listening
            frames = []
            return
        }
        state = .thinking
        let utterance = frames
        frames = []
        Task {
            await transcribeAndSend(utterance)
        }
    }

    private func transcribeAndSend(_ samples: [Float]) async {
        guard let store else { return }
        do {
            let url = try Self.writeWAV(samples, sampleRate: sampleRate)
            let text = try await SpeechTranscriber.transcribe(url: url)
            try? FileManager.default.removeItem(at: url)
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty, active else {
                state = .listening
                return
            }
            store.send(trimmed)
            // Wait for the reply to finish streaming, then speak it
            while store.isStreaming && active {
                try? await Task.sleep(for: .milliseconds(250))
            }
            guard active else { return }
            let reply = store.messages.last(where: { $0.role == "assistant" && $0.kind == .text })?.content ?? ""
            speak(reply)
        } catch {
            state = .listening
        }
    }

    private func speak(_ markdown: String) {
        let plain = stripForSpeech(markdown)
        guard !plain.isEmpty else {
            state = .listening
            return
        }
        state = .speaking
        voicedFrames = 0
        let u = AVSpeechUtterance(string: String(plain.prefix(1200)))
        u.voice = AVSpeechSynthesisVoice(language: "en-GB") ?? AVSpeechSynthesisVoice(language: "en-US")
        u.rate = 0.52
        synth.speak(u)
    }

    private func stripForSpeech(_ s: String) -> String {
        var t = s
        for p in [#"```[\s\S]*?```"#, #"[*_#>`]"#, #"\[(.*?)\]\(.*?\)"#] {
            t = t.replacingOccurrences(of: p, with: "$1", options: .regularExpression)
        }
        return t.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor in
            if self.active { self.state = .listening }
        }
    }

    // MARK: Audio helpers

    private static func rms(_ buffer: AVAudioPCMBuffer) -> Float {
        guard let data = buffer.floatChannelData?[0] else { return 0 }
        let n = Int(buffer.frameLength)
        guard n > 0 else { return 0 }
        var sum: Float = 0
        for i in 0..<n { sum += data[i] * data[i] }
        return sqrt(sum / Float(n))
    }

    private static func mono(_ buffer: AVAudioPCMBuffer) -> [Float] {
        guard let data = buffer.floatChannelData?[0] else { return [] }
        return Array(UnsafeBufferPointer(start: data, count: Int(buffer.frameLength)))
    }

    /// 16-bit PCM WAV for the Whisper endpoint.
    private static func writeWAV(_ samples: [Float], sampleRate: Double) throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("joebro-rt-\(UUID().uuidString).wav")
        var data = Data()
        let sr = UInt32(sampleRate)
        let byteRate = sr * 2
        let dataSize = UInt32(samples.count * 2)
        data.append(contentsOf: Array("RIFF".utf8))
        data.append(le32(36 + dataSize))
        data.append(contentsOf: Array("WAVE".utf8))
        data.append(contentsOf: Array("fmt ".utf8))
        data.append(le32(16))
        data.append(le16(1))           // PCM
        data.append(le16(1))           // mono
        data.append(le32(sr))
        data.append(le32(byteRate))
        data.append(le16(2))           // block align
        data.append(le16(16))          // bits
        data.append(contentsOf: Array("data".utf8))
        data.append(le32(dataSize))
        for s in samples {
            let v = Int16(max(-1, min(1, s)) * 32767)
            data.append(UInt8(truncatingIfNeeded: v))
            data.append(UInt8(truncatingIfNeeded: v >> 8))
        }
        try data.write(to: url)
        return url
    }

    private static func le16(_ v: UInt16) -> Data {
        Data([UInt8(v & 0xff), UInt8(v >> 8)])
    }

    private static func le32(_ v: UInt32) -> Data {
        Data([UInt8(v & 0xff), UInt8((v >> 8) & 0xff), UInt8((v >> 16) & 0xff), UInt8(v >> 24)])
    }
}
