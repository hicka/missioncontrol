import Foundation

@main
struct VoiceCheck {
    @MainActor static func main() async throws {
        // Releasing the hold shortcut before the permission callback must never open the mic.
        var permission: CheckedContinuation<Bool, Never>?
        let input = VoiceInput(authorize: { await withCheckedContinuation { permission = $0 } })
        input.api = { API(base: "http://127.0.0.1:8899") }
        input.start(pushToTalk: true)
        while permission == nil { await Task.yield() }
        precondition(input.preparing)
        input.finish()
        permission?.resume(returning: true)
        try await Task.sleep(nanoseconds: 50_000_000)
        precondition(!input.busy, "Late permission opened the microphone")

        let denied = VoiceInput(authorize: { false })
        denied.api = input.api
        denied.start()
        try await Task.sleep(nanoseconds: 50_000_000)
        precondition(!denied.busy && !denied.error.isEmpty, "Permission denial was hidden")

        let missing = VoiceInput(authorize: { preconditionFailure("Permission requested without a server") })
        missing.start()
        precondition(!missing.busy && !missing.error.isEmpty)
        print("Native cancellation and permission checks passed.")

        if CommandLine.arguments.contains("--live") {
            let api = API(base: "http://127.0.0.1:8899")
            let sample = URL(fileURLWithPath: FileManager.default.currentDirectoryPath + "/uploads/voice-benchmark/heart-1.wav")
            let text = try await api.transcribe(file: sample)
            precondition(text.lowercased().contains("here"), "Local transcription failed")
            var count = 0
            for try await data in api.speechChunks("Hey, I'm here. The native speech connection is ready.") {
                precondition(String(data: data.prefix(4), encoding: .utf8) == "RIFF")
                count += 1
            }
            precondition(count >= 2, "Speech was not streamed")
            print("Native API transcription and streamed WAV checks passed.")
            if CommandLine.arguments.contains("--playback") {
                let speaker = Speaker()
                speaker.speak("Hey, I'm here. This sentence will be interrupted.", api: api)
                try await Task.sleep(nanoseconds: 800_000_000)
                speaker.stop()
                precondition(!speaker.speaking)
                speaker.speak("The floating bar's local voice is ready.", api: api)
                for _ in 0..<200 {
                    if !speaker.speaking { break }
                    try await Task.sleep(nanoseconds: 100_000_000)
                }
                precondition(!speaker.speaking && speaker.error.isEmpty, "Playback failed or stalled after interruption")
                print("Native playback, interruption and replay passed.")
            }
        }
    }
}
