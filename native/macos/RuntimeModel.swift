import AppKit
import Combine
import Foundation
import ServiceManagement
import AVFoundation
import ApplicationServices

struct MCService: Decodable, Identifiable {
    let id: String, name: String, description: String, status: String, error: String
    let enabled: Bool
    let settings_tab: String
    let tasks: Int
}
struct MCProcess: Decodable, Identifiable {
    var id: Int { pid }
    let pid: Int, parent_pid: Int
    let created: Double, memory_mb: Double
    let name: String, status: String, service: String, source: String
    let owned: Bool, can_stop: Bool
    let agent: String?
}
struct MCLaunchService: Decodable, Identifiable {
    let id: String, label: String, path: String, domain: String, data_home: String
    let loaded: Bool, can_manage: Bool, run_at_login: Bool, keep_alive: Bool
}
struct MCTask: Decodable, Identifiable {
    let id: String, service: String, label: String, status: String
    let agent: String?
}
struct MCAgent: Decodable, Identifiable {
    let id: String, name: String, state: String
    let queued: Int, max_tool_rounds: Int, max_tool_calls: Int
    let tools: [String]?, skills: [String]?, skill_run: String?
}
struct MCHarness: Decodable { let validation: String; let agents: [MCAgent] }
struct MCSnapshot: Decodable {
    let instance: String, data_home: String
    let pid: Int, owner_pid: Int
    let started: Double
    let services: [MCService], processes: [MCProcess], launch_services: [MCLaunchService]
    let tasks: [MCTask], recent_tasks: [MCTask]
    let harness: MCHarness
}
struct MCInventory: Decodable { let processes: [MCProcess]; let launch_services: [MCLaunchService] }

@MainActor
final class RuntimeModel: ObservableObject {
    @Published var status = "Stopped"
    @Published var snapshot: MCSnapshot?
    @Published var processes: [MCProcess] = []
    @Published var loginServices: [MCLaunchService] = []
    @Published var error = ""
    @Published var logs = ""
    @Published var busy = false
    @Published var lastUpdated: Date?
    @Published var runtimePort: String
    @Published var dataHome: String
    @Published var keepRunning: Bool { didSet { UserDefaults.standard.set(keepRunning, forKey: "keepRunning") } }
    @Published var restartOnFailure: Bool { didSet { UserDefaults.standard.set(restartOnFailure, forKey: "restartOnFailure") } }
    @Published var loginStatus = "Not registered"
    @Published var microphone = "Not requested"
    @Published var accessibility = false
    @Published var screenCapture = false
    @Published var crashCount = 0
    @Published var inventoryPID: Int32?
    var onReady: ((URL) -> Void)?
    var onOpenSettings: ((String) -> Void)?
    var onShowWorkspace: (() -> Void)?
    var onNativeState: (() -> Void)?
    private(set) var engine: Process?
    private let instance = UUID().uuidString
    private var timer: Timer?
    private var refreshing = false
    private var desired = false
    private var restarting = false
    private var quitting = false
    private var cleaning = false
    private var loadedPID: Int32 = 0
    private var started: Date?
    private var logHandle: FileHandle?
    private var helperQueue = DispatchQueue(label: "com.missioncontrol.inventory", qos: .utility)
    private var lastInventory = Date.distantPast
    var isRunning: Bool { engine?.isRunning == true }
    var url: URL { URL(string: "http://127.0.0.1:\(Int(runtimePort) ?? 8899)")! }
    var worker: URL { Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/MCBackend.app/Contents/MacOS/MCBackend") }

    init() {
        let defaults = UserDefaults.standard
        defaults.register(defaults: ["keepRunning": true, "restartOnFailure": true])
        let args = CommandLine.arguments
        func option(_ name: String) -> String? { guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }; return args[i + 1] }
        dataHome = option("--data-home") ?? defaults.string(forKey: "dataHome") ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/MissionControl").path
        runtimePort = option("--port") ?? defaults.string(forKey: "runtimePort") ?? "8899"
        keepRunning = defaults.bool(forKey: "keepRunning")
        restartOnFailure = defaults.bool(forKey: "restartOnFailure")
        updatePermissions()
    }

    func begin() {
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in Task { @MainActor in await self?.refresh() } }
        Task { await refresh() }
        if !CommandLine.arguments.contains("--no-start") { start() }
    }

    private func environment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["MISSIONCONTROL_HOME"] = dataHome
        env["MISSIONCONTROL_INSTANCE"] = instance
        env["MISSIONCONTROL_OWNER_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        env["MISSIONCONTROL_PORT"] = runtimePort
        let browser = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/AgentBrowser.app")
        if let executable = Bundle(url: browser)?.executableURL { env["MISSIONCONTROL_BROWSER_EXECUTABLE"] = executable.path }
        env["PATH"] = "\(FileManager.default.homeDirectoryForCurrentUser.path)/.local/bin:/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        // The app port is explicit; the other listeners retain workspace settings.
        return env
    }

    func start() {
        guard !isRunning, !busy, !cleaning else { return }
        guard let port = Int(runtimePort), (1024...65535).contains(port) else { error = "Choose an app port between 1024 and 65535."; return }
        guard FileManager.default.isExecutableFile(atPath: worker.path) else { error = "Bundled runtime is missing. Rebuild or reinstall Mission Control."; return }
        desired = true; quitting = false; status = "Starting"; error = ""; started = Date()
        let process = Process()
        process.executableURL = worker
        process.environment = environment()
        do {
            try FileManager.default.createDirectory(atPath: dataHome, withIntermediateDirectories: true)
            let logfile = URL(fileURLWithPath: dataHome).appendingPathComponent("runtime.log")
            if !FileManager.default.fileExists(atPath: logfile.path) { FileManager.default.createFile(atPath: logfile.path, contents: nil, attributes: [.posixPermissions: 0o600]) }
            let handle = try FileHandle(forWritingTo: logfile); try handle.seekToEnd()
            logHandle = handle; process.standardOutput = handle; process.standardError = handle
            process.currentDirectoryURL = URL(fileURLWithPath: dataHome)
            process.terminationHandler = { [weak self] process in
                Task { @MainActor in self?.exited(process.terminationStatus) }
            }
            engine = process
            try process.run()
            onNativeState?()
        } catch { engine = nil; try? logHandle?.close(); logHandle = nil; status = "Failed"; self.error = error.localizedDescription; desired = false }
    }

    private func exited(_ code: Int32) {
        try? logHandle?.close(); logHandle = nil; loadedPID = 0; snapshot = nil
        cleaning = true; status = "Cleaning up"
        Task {
            do { _ = try await helper(["--cleanup-owned"]) }
            catch { self.error = "Cleanup: " + error.localizedDescription }
            cleaning = false
            finishExit(code)
        }
    }

    private func finishExit(_ code: Int32) {
        if restarting && !quitting { restarting = false; status = "Stopped"; busy = false; start(); return }
        if desired && !quitting && restartOnFailure && crashCount < 3 && code != 78 {
            crashCount += 1; status = "Restarting (\(crashCount)/3)"
            error = "Runtime exited with code \(code). Diagnostics are in runtime.log."
            let delay = Double(crashCount * 2)
            Task { try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000)); if desired && !quitting { start() } }
        } else {
            status = code == 0 || !desired ? "Stopped" : "Failed"
            if desired { error = "Runtime stopped (exit \(code)). Inspect logs and detected instances before restarting." }
            desired = false
        }
        onNativeState?()
    }

    func stop(restart: Bool = false) {
        if restart { crashCount = 0 }
        desired = restart; restarting = restart; status = "Stopping"
        guard let process = engine, process.isRunning else { status = "Stopped"; if restart { start() }; return }
        let pid = process.processIdentifier
        // SIGTERM runs the Python lifespan: services, tool trees and child tasks stop.
        process.terminate()
        Task {
            for _ in 0..<40 { if !process.isRunning { return }; try? await Task.sleep(nanoseconds: 200_000_000) }
            if process.isRunning && process.processIdentifier == pid { kill(-pid, SIGKILL); kill(pid, SIGKILL) }
        }
    }

    func quit(_ completion: @escaping () -> Void) {
        quitting = true; desired = false; restarting = false; timer?.invalidate(); stop()
        Task {
            for _ in 0..<50 { if !isRunning && !cleaning { completion(); return }; try? await Task.sleep(nanoseconds: 200_000_000) }
            if let process = engine, process.isRunning { kill(-process.processIdentifier, SIGKILL); kill(process.processIdentifier, SIGKILL) }
            completion()
        }
    }

    func refresh() async {
        guard !refreshing, !quitting else { return }; refreshing = true
        defer { refreshing = false; onNativeState?() }
        updatePermissions()
        if isRunning {
            do {
                var request = URLRequest(url: url.appendingPathComponent("api/runtime")); request.timeoutInterval = 3
                let (data, response) = try await URLSession.shared.data(for: request)
                guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
                let next = try JSONDecoder().decode(MCSnapshot.self, from: data)
                guard next.instance == instance, next.pid == Int(engine?.processIdentifier ?? 0) else {
                    error = "Port \(runtimePort) belongs to another instance. Choose a different port or stop that instance below."; return
                }
                snapshot = next; processes = next.processes; loginServices = next.launch_services; lastUpdated = Date(); status = "Running"
                if loadedPID != engine!.processIdentifier { loadedPID = engine!.processIdentifier; onReady?(url) }
            } catch {
                if let started, Date().timeIntervalSince(started) > 45 { self.error = "Runtime is not responding. Open logs to inspect startup or port conflicts." }
            }
        }
        if snapshot == nil && Date().timeIntervalSince(lastInventory) > 5 {
            lastInventory = Date()
            do { let data = try await helper(["--inventory"]); let inventory = try JSONDecoder().decode(MCInventory.self, from: data); processes = inventory.processes; loginServices = inventory.launch_services; lastUpdated = Date() }
            catch { self.error = "Inventory: " + error.localizedDescription }
        }
    }

    func helper(_ args: [String]) async throws -> Data {
        let executable = worker, env = environment()
        return try await withCheckedThrowingContinuation { continuation in
            helperQueue.async {
                let process = Process(), output = Pipe()
                process.executableURL = executable; process.arguments = args; process.environment = env
                process.standardOutput = output; process.standardError = FileHandle.nullDevice
                do {
                    try process.run()
                    Task { @MainActor in self.inventoryPID = process.processIdentifier }
                    let timer = DispatchSource.makeTimerSource(queue: DispatchQueue.global())
                    timer.schedule(deadline: .now() + 20); timer.setEventHandler {
                        if process.isRunning {
                            process.terminate()
                            DispatchQueue.global().asyncAfter(deadline: .now() + 2) { if process.isRunning { kill(process.processIdentifier, SIGKILL) } }
                        }
                    }; timer.resume()
                    let data = output.fileHandleForReading.readDataToEndOfFile()
                    process.waitUntilExit(); timer.cancel()
                    Task { @MainActor in self.inventoryPID = nil }
                    if process.terminationStatus != 0 {
                        let message = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String ?? "Inventory helper failed."
                        throw NSError(domain: "MissionControl", code: Int(process.terminationStatus), userInfo: [NSLocalizedDescriptionKey: message])
                    }
                    continuation.resume(returning: data)
                } catch { continuation.resume(throwing: error) }
            }
        }
    }

    func serviceAction(_ service: MCService, _ action: String) {
        perform { try await self.post("api/runtime/services/\(service.id)/\(action)"); await self.refresh() }
    }
    private func post(_ path: String, body: [String: Any] = [:]) async throws {
        guard snapshot?.instance == instance else { throw NSError(domain: "MissionControl", code: 1, userInfo: [NSLocalizedDescriptionKey: "Managed runtime is not connected."]) }
        var request = URLRequest(url: url.appendingPathComponent(path)); request.httpMethod = "POST"; request.timeoutInterval = 50
        request.setValue("application/json", forHTTPHeaderField: "Content-Type"); request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await URLSession.shared.data(for: request)
        let result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        if (response as? HTTPURLResponse)?.statusCode != 200 || result?["ok"] as? Bool == false {
            throw NSError(domain: "MissionControl", code: 1, userInfo: [NSLocalizedDescriptionKey: result?["error"] as? String ?? "Runtime action failed."])
        }
    }
    func stopProcess(_ process: MCProcess) {
        perform { _ = try await self.helper(["--stop-process", String(process.pid), "--created", String(process.created)]); await self.refresh() }
    }
    func loginAction(_ service: MCLaunchService, _ action: String) {
        perform { _ = try await self.helper(["--login-service", service.id, "--action", action]); await self.refresh() }
    }
    func perform(_ work: @escaping () async throws -> Void) {
        guard !busy else { return }; busy = true; error = ""
        Task { defer { busy = false }; lastInventory = .distantPast; do { try await work() } catch { self.error = error.localizedDescription } }
    }
    func showLogs() {
        let path = URL(fileURLWithPath: dataHome).appendingPathComponent("runtime.log")
        NSWorkspace.shared.open(path)
    }
    func revealData() { NSWorkspace.shared.open(URL(fileURLWithPath: dataHome)) }
    func chooseData() {
        let panel = NSOpenPanel(); panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.canCreateDirectories = true
        panel.message = "Choose the folder containing your Mission Control settings and database. Stop an older runtime before using its folder."
        if panel.runModal() == .OK, let folder = panel.url { dataHome = folder.path; UserDefaults.standard.set(dataHome, forKey: "dataHome") }
    }
    func savePort() { if let port = Int(runtimePort), (1024...65535).contains(port) { UserDefaults.standard.set(runtimePort, forKey: "runtimePort") } }
    func configure(_ tab: String) { onOpenSettings?(tab) }
    func setLogin(_ enabled: Bool) {
        do { if enabled { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }; updatePermissions() }
        catch { self.error = error.localizedDescription; updatePermissions() }
    }
    func updatePermissions() {
        switch SMAppService.mainApp.status {
        case .enabled: loginStatus = "Enabled"
        case .requiresApproval: loginStatus = "Awaiting macOS approval"
        case .notFound: loginStatus = "App must be installed"
        default: loginStatus = "Not registered"
        }
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: microphone = "Allowed"
        case .denied: microphone = "Denied"
        case .restricted: microphone = "Restricted"
        default: microphone = "Not requested"
        }
        accessibility = AXIsProcessTrusted(); screenCapture = CGPreflightScreenCaptureAccess()
    }
    func requestMicrophone() { AVCaptureDevice.requestAccess(for: .audio) { _ in Task { @MainActor in self.updatePermissions() } } }
    func requestAccessibility() { let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary; _ = AXIsProcessTrustedWithOptions(options); updatePermissions() }
    func requestScreen() { _ = CGRequestScreenCaptureAccess(); updatePermissions() }
    func openPrivacy(_ pane: String) { if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(pane)") { NSWorkspace.shared.open(url) } }
    func openLoginSettings() { SMAppService.openSystemSettingsLoginItems() }
}
