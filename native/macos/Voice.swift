import AVFoundation
import Foundation
import Observation
import Speech

/// Speaking to the agent. Recognition runs on this Mac wherever the system supports it;
/// where it does not, macOS sends the audio to Apple for recognition, as dictation does.
@Observable @MainActor
final class VoiceInput {
    var listening = false
    var level: CGFloat = 0
    var transcript = ""
    @ObservationIgnored var onFinish: ((String) -> Void)?
    @ObservationIgnored var onError: ((String) -> Void)?
    @ObservationIgnored private let engine = AVAudioEngine()
    @ObservationIgnored private var request: SFSpeechAudioBufferRecognitionRequest?
    @ObservationIgnored private var task: SFSpeechRecognitionTask?
    @ObservationIgnored private var silence: Task<Void, Never>?
    /// How long a pause ends what was being said.
    private static let pause: UInt64 = 1_500_000_000

    func toggle() { if listening { finish() } else { start() } }

    func start() {
        SFSpeechRecognizer.requestAuthorization { status in
            AVCaptureDevice.requestAccess(for: .audio) { allowed in
                Task { @MainActor in
                    guard status == .authorized, allowed else { self.onError?("Allow Microphone and Speech Recognition for Mission Control in System Settings."); return }
                    self.begin()
                }
            }
        }
    }

    private func begin() {
        guard !listening else { return }
        guard let recognizer = SFSpeechRecognizer(), recognizer.isAvailable else { onError?("Speech recognition is not available right now."); return }
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        if recognizer.supportsOnDeviceRecognition { request.requiresOnDeviceRecognition = true }
        self.request = request
        transcript = ""
        let input = engine.inputNode
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 2048, format: input.outputFormat(forBus: 0), block: Self.tap(request: request, target: self))
        engine.prepare()
        do { try engine.start() } catch { input.removeTap(onBus: 0); self.request = nil; onError?(error.localizedDescription); return }
        listening = true
        task = recognizer.recognitionTask(with: request, resultHandler: Self.results(target: self))
        waitForPause()
    }

    /// These run on the audio and recognition threads, so they touch nothing directly.
    private nonisolated static func tap(request: SFSpeechAudioBufferRecognitionRequest, target: VoiceInput) -> AVAudioNodeTapBlock {
        { [weak target] buffer, _ in
            request.append(buffer)
            guard let samples = buffer.floatChannelData?[0], buffer.frameLength > 0 else { return }
            var sum: Float = 0
            for index in 0..<Int(buffer.frameLength) { sum += samples[index] * samples[index] }
            let loudness = CGFloat(min(1, sqrt(sum / Float(buffer.frameLength)) * 9))
            Task { @MainActor in target?.level = loudness }
        }
    }

    private nonisolated static func results(target: VoiceInput) -> (SFSpeechRecognitionResult?, Error?) -> Void {
        { [weak target] result, error in
            let text = result?.bestTranscription.formattedString
            let done = result?.isFinal == true || error != nil
            Task { @MainActor in
                guard let target, target.listening else { return }
                if let text, text != target.transcript { target.transcript = text; target.waitForPause() }
                if done { target.finish() }
            }
        }
    }

    private func waitForPause() {
        silence?.cancel()
        silence = Task {
            try? await Task.sleep(nanoseconds: Self.pause)
            // Nothing said yet is not a pause; give it longer before giving up.
            if !transcript.isEmpty, !Task.isCancelled { finish(); return }
            try? await Task.sleep(nanoseconds: 6_000_000_000)
            if !Task.isCancelled { cancel() }
        }
    }

    func finish() {
        guard listening else { return }
        let said = transcript
        teardown()
        if !said.isEmpty { onFinish?(said) }
    }

    func cancel() { guard listening else { return }; teardown() }

    private func teardown() {
        silence?.cancel()
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        request?.endAudio()
        task?.cancel()
        request = nil; task = nil
        listening = false; level = 0; transcript = ""
    }
}

/// Speaking the reply: the voice configured on the server, or the system's when there is none.
@Observable @MainActor
final class Speaker: NSObject, AVAudioPlayerDelegate, AVSpeechSynthesizerDelegate {
    var speaking = false
    /// How loud the reply is right now, for the bar to move with it.
    var level: CGFloat {
        guard speaking, let player, player.isPlaying else { return speaking ? 0.25 : 0 }
        player.updateMeters()
        return CGFloat(max(0, min(1, (player.averagePower(forChannel: 0) + 42) / 42)))
    }
    @ObservationIgnored private var player: AVAudioPlayer?
    @ObservationIgnored private let synthesizer = AVSpeechSynthesizer()
    @ObservationIgnored private var job: Task<Void, Never>?
    @ObservationIgnored private var queue: [(String, API)] = []

    override init() {
        super.init()
        synthesizer.delegate = self
    }

    /// A reply arrives as several messages, so what is said queues up behind what is being said.
    func speak(_ text: String, api: API) {
        queue.append((text.replacingOccurrences(of: "`", with: ""), api))
        if !speaking { next() }
    }

    private func next() {
        guard !queue.isEmpty else { speaking = false; return }
        let (text, api) = queue.removeFirst()
        speaking = true
        job = Task {
            if let reply = try? await api.post("/api/tts", ["text": text]), let path = reply.url.optStr,
               let url = URL(string: path.hasPrefix("http") ? path : api.base + path),
               let (data, _) = try? await URLSession.shared.data(from: url),
               !Task.isCancelled, let player = try? AVAudioPlayer(data: data) {
                self.player = player
                player.delegate = self
                player.isMeteringEnabled = true
                if player.play() { return }
            }
            if !Task.isCancelled { synthesizer.speak(AVSpeechUtterance(string: text)) }
        }
    }

    func stop() {
        queue = []
        job?.cancel(); job = nil
        player?.stop(); player = nil
        if synthesizer.isSpeaking { synthesizer.stopSpeaking(at: .immediate) }
        speaking = false
    }

    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in self.next() }
    }
    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor in self.next() }
    }
}
