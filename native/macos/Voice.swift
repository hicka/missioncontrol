import AVFoundation
import Foundation
import Observation

/// Capture only while requested. Recognition uses the server's selected speech engine.
@Observable @MainActor
final class VoiceInput {
    private(set) var listening = false
    private(set) var preparing = false
    private(set) var transcribing = false
    private(set) var level: CGFloat = 0
    private(set) var transcript = ""
    private(set) var error = ""
    var busy: Bool { listening || preparing || transcribing }
    @ObservationIgnored var api: (() -> API?)?
    @ObservationIgnored var onStart: (() -> Void)?
    @ObservationIgnored var onFinish: ((String) -> Void)?
    @ObservationIgnored var onError: ((String) -> Void)?
    @ObservationIgnored private var recorder: AVAudioRecorder?
    @ObservationIgnored private var recording: URL?
    @ObservationIgnored private var monitor: Task<Void, Never>?
    @ObservationIgnored private var job: Task<Void, Never>?
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private var speechTicks = 0
    @ObservationIgnored private var activeAPI: API?
    @ObservationIgnored private let authorize: () async -> Bool

    init(authorize: @escaping () async -> Bool = { await AVCaptureDevice.requestAccess(for: .audio) }) {
        self.authorize = authorize
    }

    func toggle() {
        if listening { finish() }
        else if busy { cancel() }
        else { start() }
    }

    func start(pushToTalk: Bool = false) {
        cancel()
        error = ""; transcript = ""
        guard let api = api?() else { fail("Connect to Mission Control before speaking."); return }
        activeAPI = api
        onStart?()
        preparing = true
        let current = generation
        job = Task {
            let allowed = await authorize()
            guard current == generation, !Task.isCancelled else { return }
            preparing = false
            guard allowed else { fail("Allow Microphone for Mission Control in System Settings → Privacy & Security."); return }
            do {
                try begin(pushToTalk: pushToTalk)
                _ = try? await api.post("/api/voice/warmup")
            } catch { fail(error.localizedDescription) }
        }
    }

    private func begin(pushToTalk: Bool) throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("mc-voice-\(UUID().uuidString).m4a")
        recording = url
        let recorder = try AVAudioRecorder(url: url, settings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 16000,
            AVNumberOfChannelsKey: 1, AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
        ])
        recorder.isMeteringEnabled = true
        guard recorder.record() else { throw APIError(message: "The microphone could not start recording.") }
        self.recorder = recorder
        listening = true; speechTicks = 0
        let current = generation
        monitor = Task {
            var quiet = 0, ticks = 0
            while !Task.isCancelled, current == generation, listening {
                do { try await Task.sleep(nanoseconds: 50_000_000) } catch { return }
                guard current == generation, listening else { return }
                recorder.updateMeters()
                let power = recorder.averagePower(forChannel: 0)
                level = CGFloat(min(1, pow(10, power / 20) * 9))
                ticks += 1
                if power > -38 { speechTicks += 1; quiet = 0 }
                else if speechTicks > 0 { quiet += 1 }
                if ticks >= 600 || (!pushToTalk && speechTicks >= 3 && quiet >= 12) { finish(); return }
                if !pushToTalk && speechTicks < 3 && ticks >= 160 { cancel(); return }
            }
        }
    }

    func finish() {
        // A key release during permission approval must invalidate that pending start.
        if preparing { cancel(); return }
        guard listening else { return }
        guard let file = recording, let api = activeAPI else { cancel(); return }
        let hasSpeech = speechTicks >= 3
        monitor?.cancel(); monitor = nil
        recorder?.stop(); recorder = nil
        recording = nil; activeAPI = nil; listening = false; level = 0
        job?.cancel()
        guard hasSpeech else { try? FileManager.default.removeItem(at: file); return }
        transcribing = true
        let current = generation
        job = Task {
            defer { try? FileManager.default.removeItem(at: file) }
            do {
                let text = try await api.transcribe(file: file).trimmingCharacters(in: .whitespacesAndNewlines)
                guard current == generation, !Task.isCancelled else { return }
                transcribing = false
                transcript = text
                if !text.isEmpty { onFinish?(text) }
            } catch {
                guard current == generation, !Task.isCancelled else { return }
                fail(error.localizedDescription)
            }
        }
    }

    func cancel() {
        generation += 1
        job?.cancel(); job = nil
        monitor?.cancel(); monitor = nil
        recorder?.stop(); recorder = nil
        if let recording { try? FileManager.default.removeItem(at: recording) }
        recording = nil; activeAPI = nil; error = ""
        listening = false; preparing = false; transcribing = false; level = 0; transcript = ""
    }

    private func fail(_ message: String) {
        cancel()
        error = message
        onError?(message)
    }
}

/// Play the configured voice progressively; failures stay visible instead of changing voices.
@Observable @MainActor
final class Speaker: NSObject, AVAudioPlayerDelegate {
    private(set) var speaking = false
    private(set) var error = ""
    var level: CGFloat {
        guard speaking, let player, player.isPlaying else { return speaking ? 0.15 : 0 }
        player.updateMeters()
        return CGFloat(max(0, min(1, (player.averagePower(forChannel: 0) + 42) / 42)))
    }
    @ObservationIgnored var onError: ((String) -> Void)?
    @ObservationIgnored private var player: AVAudioPlayer?
    @ObservationIgnored private var job: Task<Void, Never>?
    @ObservationIgnored private var queue: [(String, API)] = []
    @ObservationIgnored private var playback: CheckedContinuation<Void, Error>?
    @ObservationIgnored private var generation = 0

    func speak(_ text: String, api: API) {
        let text = text.replacingOccurrences(of: "`", with: "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        error = ""
        queue.append((text, api))
        if !speaking { next() }
    }

    private func next() {
        guard !queue.isEmpty else { speaking = false; job = nil; return }
        let (text, api) = queue.removeFirst()
        speaking = true
        let current = generation
        job = Task {
            do {
                for try await data in api.speechChunks(text) {
                    try Task.checkCancellation()
                    guard current == generation else { return }
                    try await play(data)
                }
                guard current == generation, !Task.isCancelled else { return }
                next()
            } catch {
                guard current == generation, !Task.isCancelled else { return }
                stop()
                self.error = error.localizedDescription
                onError?(error.localizedDescription)
            }
        }
    }

    private func play(_ data: Data) async throws {
        let player = try AVAudioPlayer(data: data)
        self.player = player
        player.delegate = self
        player.isMeteringEnabled = true
        try await withCheckedThrowingContinuation { continuation in
            playback = continuation
            if !player.play() { complete(APIError(message: "Audio output could not start.")) }
        }
    }

    private func complete(_ error: Error? = nil) {
        let waiting = playback; playback = nil
        player?.delegate = nil; player = nil
        if let error { waiting?.resume(throwing: error) } else { waiting?.resume() }
    }

    func stop() {
        generation += 1
        queue = []
        job?.cancel(); job = nil
        player?.stop()
        complete(CancellationError())
        speaking = false; error = ""
    }

    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in
            guard self.player === player else { return }
            self.complete(flag ? nil : APIError(message: "Audio playback did not complete."))
        }
    }

    nonisolated func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        Task { @MainActor in
            guard self.player === player else { return }
            self.complete(error ?? APIError(message: "The speech audio could not be decoded."))
        }
    }
}
