import Foundation
import UniformTypeIdentifiers

struct APIError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

/// A client for one Mission Control server: the runtime this app owns, or one it attached
/// to. Everything here runs off the main thread, parsing included, so a large reply never
/// costs the interface a frame.
struct API: Sendable {
    let base: String
    let token: String

    init(base: String, token: String = "") {
        self.base = base.hasSuffix("/") ? String(base.dropLast()) : base
        self.token = token
    }

    fileprivate static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 30
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.httpMaximumConnectionsPerHost = 8
        return URLSession(configuration: config)
    }()

    func get(_ path: String) async throws -> JSON { try await fetch("GET", path, nil).json }
    func post(_ path: String, _ body: [String: Any] = [:]) async throws -> JSON {
        try await fetch("POST", path, try JSONSerialization.data(withJSONObject: body)).json
    }
    func put(_ path: String, _ body: [String: Any] = [:]) async throws -> JSON {
        try await fetch("PUT", path, try JSONSerialization.data(withJSONObject: body)).json
    }
    func delete(_ path: String) async throws -> JSON { try await fetch("DELETE", path, nil).json }
    /// The reply as sent, for the on-disk snapshot, along with its parsed form.
    func getRaw(_ path: String) async throws -> (data: Data, json: JSON) { try await fetch("GET", path, nil) }

    private func fetch(_ method: String, _ path: String, _ body: Data?) async throws -> (data: Data, json: JSON) {
        guard let url = URL(string: base + path) else { throw APIError(message: "That address is not valid: \(path)") }
        var request = URLRequest(url: url)
        request.httpMethod = method
        if !token.isEmpty { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        let (data, response) = try await Self.session.data(for: request)
        let json = JSON(data: data)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status >= 400 || json["ok"] == .bool(false) {
            throw APIError(message: json["error"].optStr ?? json["detail"].optStr ?? "That did not work. Please try again.")
        }
        return (data, json)
    }

    /// The server describes an image with a model before it answers, which the usual request
    /// limit would cut off.
    private static let uploadSession: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 300
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: config)
    }()

    /// A file for a message, sent as the multipart form `/api/upload` reads. The reply is the
    /// attachment to send along with the message. The file is read here, off the main thread.
    func upload(file: URL) async throws -> JSON {
        try await multipart(file: file, path: "/api/upload").attachment
    }

    func transcribe(file: URL) async throws -> String {
        try await multipart(file: file, path: "/api/transcribe").text.str
    }

    private func multipart(file: URL, path: String) async throws -> JSON {
        guard let url = URL(string: base + path) else { throw APIError(message: "That address is not valid: \(path)") }
        let info = try file.resourceValues(forKeys: [.isDirectoryKey, .fileSizeKey])
        if info.isDirectory == true { throw APIError(message: "A folder cannot be attached.") }
        if (info.fileSize ?? 0) > 50 * 1024 * 1024 { throw APIError(message: "That file is over the 50 MB limit.") }
        let contents = try Data(contentsOf: file, options: .mappedIfSafe)
        let boundary = "MissionControl-\(UUID().uuidString)"
        let name = file.lastPathComponent.replacingOccurrences(of: "[\"\\r\\n]", with: "_", options: .regularExpression)
        let mime = UTType(filenameExtension: file.pathExtension)?.preferredMIMEType ?? "application/octet-stream"
        var body = Data(capacity: contents.count + 512)
        body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(name)\"\r\nContent-Type: \(mime)\r\n\r\n".utf8))
        body.append(contents)
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        var request = URLRequest(url: url, timeoutInterval: 300)
        request.httpMethod = "POST"
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        if !token.isEmpty { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        let (data, response) = try await Self.uploadSession.upload(for: request, from: body)
        let json = JSON(data: data)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status >= 400 || json["ok"] == .bool(false) {
            throw APIError(message: json["error"].optStr ?? json["detail"].optStr ?? "That file could not be uploaded.")
        }
        return json
    }

    /// Download ahead while the consumer plays each completed sentence.
    func speechChunks(_ text: String) -> AsyncThrowingStream<Data, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    guard let url = URL(string: base + "/api/tts/stream") else { throw APIError(message: "The speech address is invalid.") }
                    var request = URLRequest(url: url, timeoutInterval: 120)
                    request.httpMethod = "POST"
                    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                    if !token.isEmpty { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
                    request.httpBody = try JSONSerialization.data(withJSONObject: ["text": text])
                    let (bytes, response) = try await Self.session.bytes(for: request)
                    guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw APIError(message: "Speech unavailable. Update the Mission Control server and check Voice settings.") }
                    for try await line in bytes.lines {
                        try Task.checkCancellation()
                        if line.isEmpty { continue }
                        let reply = JSON(data: Data(line.utf8))
                        guard reply.ok.flag, reply.url.str.hasPrefix("/uploads/tts/") else {
                            throw APIError(message: reply.error.optStr ?? "The speech server returned invalid audio.")
                        }
                        let audio = try await getRaw(reply.url.str)
                        try Task.checkCancellation()
                        continuation.yield(audio.data)
                    }
                    continuation.finish()
                } catch { continuation.finish(throwing: error) }
            }
            continuation.onTermination = { @Sendable _ in task.cancel() }
        }
    }

    /// For a path segment: a thread id such as `dm:cto`, or a search term.
    static func escape(_ text: String) -> String {
        var allowed = CharacterSet.urlQueryAllowed
        allowed.remove(charactersIn: "&=+?#/")
        return text.addingPercentEncoding(withAllowedCharacters: allowed) ?? text
    }

    var socketURL: URL? {
        var address = base.replacingOccurrences(of: "https://", with: "wss://").replacingOccurrences(of: "http://", with: "ws://") + "/ws"
        if !token.isEmpty { address += "?token=" + API.escape(token) }
        return URL(string: address)
    }
}

/// The server's live events. Reconnects on its own, and says when the link comes and goes
/// so the store can read again whatever it missed.
final class EventStream: @unchecked Sendable {
    private var task: Task<Void, Never>?

    func start(_ api: API, onLink: @escaping @MainActor (Bool) -> Void, onEvent: @escaping @MainActor (JSON) -> Void) {
        stop()
        guard let url = api.socketURL else { return }
        task = Task.detached(priority: .userInitiated) {
            var backoff: UInt64 = 400_000_000
            while !Task.isCancelled {
                let socket = API.session.webSocketTask(with: url)
                socket.maximumMessageSize = 8_000_000
                socket.resume()
                // The server says nothing until something happens, so a ping is what
                // proves the socket is open, and later that it has not silently died.
                let keepalive = Task {
                    while !Task.isCancelled {
                        try? await Task.sleep(nanoseconds: 20_000_000_000)
                        if Task.isCancelled { return }
                        do { try await Self.ping(socket) } catch { socket.cancel(with: .goingAway, reason: nil); return }
                    }
                }
                do {
                    try await Self.ping(socket)
                    await onLink(true)
                    backoff = 400_000_000
                    while !Task.isCancelled {
                        let message = try await socket.receive()
                        let event: JSON
                        switch message {
                        case .string(let text): event = JSON(data: Data(text.utf8))
                        case .data(let data): event = JSON(data: data)
                        @unknown default: continue
                        }
                        await onEvent(event)
                    }
                } catch {}
                keepalive.cancel()
                socket.cancel(with: .goingAway, reason: nil)
                if Task.isCancelled { return }
                await onLink(false)
                try? await Task.sleep(nanoseconds: backoff)
                backoff = min(backoff * 2, 5_000_000_000)
            }
        }
    }

    func stop() { task?.cancel(); task = nil }

    private static func ping(_ socket: URLSessionWebSocketTask) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            socket.sendPing { error in
                if let error { continuation.resume(throwing: error) } else { continuation.resume() }
            }
        }
    }
}
