import AppKit
import SwiftUI

private struct WhatsAppForm: Equatable {
    var enabled = false, groups = false, include = "", ignore = "", agents = ""

    init(_ reply: JSON) {
        let settings = reply.settings
        enabled = settings.enabled.flag
        groups = settings.groups.flag
        include = settings.include.list.map(\.str).joined(separator: ", ")
        ignore = settings.ignore.list.map(\.str).joined(separator: ", ")
        agents = settings.agents.list.map(\.str).joined(separator: ", ")
    }
}

/// The owner's own account as a linked device. Nothing here can send, reply or mark a chat
/// read. The link state is polled while it may change, because a QR rotates every 20 seconds.
@MainActor
struct SettingsWhatsAppPane: View {
    @Environment(Store.self) private var store
    private static let path = "/api/whatsapp"

    @State private var data: JSON = .null
    @State private var form = SettingsDraft(WhatsAppForm(.null))
    @State private var status = SettingsStatus.none
    @State private var problem: String?
    @State private var phone = ""
    @State private var code = ""
    @State private var linking = false

    private struct LinkState {
        var tone = "off", state = "Off", detail = "Turn it on, save, then link your phone."
        var canLink = false, canUnlink = false
    }

    private var bridge: JSON { data.bridge }
    private var pairing: Bool { bridge.status.str == "pairing" }
    /// Whether anything is about to change on its own.
    private var settled: Bool {
        !data.settings.enabled.flag || bridge.status.str == "connected" || (!bridge.isNull && bridge.status.str == "unlinked")
    }

    private var link: LinkState {
        var link = LinkState()
        if data.settings.enabled.flag && bridge.isNull {
            link.tone = "waiting"
            link.state = "Starting…"
            link.detail = data.problem.str
        } else if !bridge.isNull {
            switch bridge.status.str {
            case "connected":
                link = LinkState(tone: "connected", state: "Linked as +\(bridge.number.str)",
                                 detail: "Listening. Remove it any time here or from the phone's Linked devices.", canUnlink: true)
            case "pairing":
                link = LinkState(tone: "waiting", state: "Waiting for your phone", detail: "")
            case "connecting":
                link = LinkState(tone: "waiting", state: "Reconnecting…", detail: bridge.problem.str, canUnlink: bridge.linked.flag)
            default:
                link = LinkState(tone: bridge.status.str == "error" ? "waiting" : "off",
                                 state: bridge.linked.flag ? "Not connected" : "Not linked",
                                 detail: bridge.problem.optStr.flatMap { $0.isEmpty ? nil : $0 } ?? "Link your phone to start.",
                                 canLink: true, canUnlink: bridge.linked.flag)
            }
        }
        return link
    }

    private var counts: String? {
        let row = data.counts
        guard data.settings.enabled.flag || row.messages.int > 0 else { return nil }
        return "\(row.messages.int) messages from \(row.chats.int) chats, \(row.files.int) files kept for \(data.settings.keep_days.int) days"
    }

    var body: some View {
        Form {
            Section {
                SettingsProblemRow(problem: problem)
                SettingsSwitch(title: "WhatsApp", note: "Your own account, linked to this Mac the way WhatsApp Web is. The agent can read what arrives and the files people send you; nothing can send, reply or mark a chat read.", isOn: $form.value.enabled)
                    .disabled(!form.ready)
                linkRow
            }
            if pairing { pairingSection }
            Section {
                SettingsSwitch(title: "Group chats", note: "Off: only direct chats are recorded, plus the groups you name below.", isOn: $form.value.groups)
                TextField("Include groups", text: $form.value.include, prompt: Text("Group names, comma-separated"))
                TextField("Never record", text: $form.value.ignore, prompt: Text("Chat names or phone numbers"))
                TextField("Profiles that can read", text: $form.value.agents, prompt: Text("All profiles"))
                    .autocorrectionDisabled()
                    .help("Profile ids, comma-separated. Blank for all of them.")
            } footer: {
                if let counts { SettingsHint(counts) }
            }
            Section {
                SettingsActions(status: status) {
                    AsyncButton("Save") { _ = await save() }.buttonStyle(.borderedProminent).disabled(!form.ready)
                }
            }
        }
        .formStyle(.grouped)
        .settingsLoad { await load() }
        .task(id: settled) { await poll() }
        .onChange(of: pairing) { _, now in if !now { code = "" } }
    }

    private var linkRow: some View {
        let current = link
        return HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Pill(status: current.tone, label: current.state)
                if !current.detail.isEmpty { Text(current.detail).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
            }
            Spacer()
            if current.canLink || !data.settings.enabled.flag {
                Button(bridge.linked.flag ? "Reconnect" : "Link") { Task { await startLink() } }
                    .disabled(linking || !form.ready)
            }
            if current.canUnlink {
                SettingsConfirmButton(title: "Unlink", question: "Unlink WhatsApp?",
                                      detail: "This Mac is removed from your phone's linked devices. Messages already stored stay until they age out.") {
                    await unlink()
                }
            }
        }
    }

    private var pairingSection: some View {
        Section {
            HStack(alignment: .top, spacing: 16) {
                PairingCode(svg: data.qr.str)
                VStack(alignment: .leading, spacing: 10) {
                    Text("On your phone, open WhatsApp › Settings › Linked devices › Link a device, and scan this code.")
                        .fixedSize(horizontal: false, vertical: true)
                    SettingsHint("On the phone itself? Link with a code instead: enter your number with its country code, then on the phone choose “Link with phone number instead”.")
                    HStack(spacing: 8) {
                        TextField("Phone number", text: $phone, prompt: Text("960 7xx xxxx"))
                            .labelsHidden()
                            .textFieldStyle(.roundedBorder)
                        AsyncButton("Get code") { await getCode() }.disabled(linking)
                    }
                    if !code.isEmpty {
                        Text(code)
                            .font(.system(size: 22, weight: .semibold, design: .monospaced))
                            .textSelection(.enabled)
                            .accessibilityLabel("Pairing code \(code)")
                    }
                }
            }
        }
    }

    private func load() async {
        do {
            apply(try await store.settingsAPI().get(Self.path))
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func apply(_ reply: JSON) {
        if reply != data { data = reply }
        form.receive(WhatsAppForm(reply))
    }

    private func poll() async {
        guard !settled else { return }
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 2_500_000_000)
            guard !Task.isCancelled, NSApp.isActive, let api = store.api else { continue }
            if let reply = try? await api.get(Self.path), !Task.isCancelled { apply(reply) }
        }
    }

    private func save(enabled: Bool? = nil) async -> Bool {
        status = .busy("Saving…")
        do {
            let reply = try await store.settingsAPI().put(Self.path, [
                "enabled": enabled ?? form.value.enabled, "groups": form.value.groups,
                "include": settingsList(form.value.include), "ignore": settingsList(form.value.ignore),
                "agents": settingsList(form.value.agents),
            ])
            if reply != data { data = reply }
            form.reset(to: WhatsAppForm(reply))
            status = .good("Saved")
            return true
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
            return false
        }
    }

    private func startLink(phone: String = "") async {
        linking = true
        defer { linking = false }
        if !data.settings.enabled.flag {
            form.value.enabled = true
            guard await save(enabled: true) else { return }
        }
        status = .busy(phone.isEmpty ? "Starting…" : "Asking WhatsApp for a code…")
        do {
            let api = try store.settingsAPI()
            // The bridge needs a moment after the service starts.
            var attempt = 0
            while true {
                do {
                    let reply = try await api.settingsLong("POST", "/api/whatsapp/link", ["phone": phone], timeout: 45)
                    if let given = reply.code.optStr, !given.isEmpty { code = given }
                    break
                } catch {
                    attempt += 1
                    let message = error.localizedDescription
                    guard attempt < 12, message.contains("not running") || message.contains("did not answer") else { throw error }
                    try await Task.sleep(nanoseconds: 1_000_000_000)
                }
            }
            status = .none
            await load()
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
    }

    private func getCode() async {
        let digits = phone.filter(\.isNumber)
        guard digits.count >= 8 else { status = .bad("Enter your number with its country code"); return }
        await startLink(phone: digits)
    }

    private func unlink() async {
        do {
            _ = try await store.settingsAPI().post("/api/whatsapp/unlink")
            await load()
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
    }
}

/// The bridge sends its pairing code as SVG, and there is no web view here to draw that.
/// It is always the same small shape, a row of horizontal strokes per line of the code, so
/// it is read directly and drawn as what it is. Black on white in either appearance,
/// because a phone's camera has to read it.
private struct PairingCode: View {
    let svg: String

    var body: some View {
        if let code = PairingCode.parse(svg) {
            let scale = max(1, (216 / code.size).rounded(.down))
            let side = code.size * scale
            Canvas { context, _ in
                context.scaleBy(x: scale, y: scale)
                context.fill(code.path, with: .color(.black))
            }
            .frame(width: side, height: side)
            .padding(8)
            .background(.white)
            .accessibilityLabel("WhatsApp pairing code. Scan it with your phone.")
        } else if let image = NSImage(data: Data(svg.utf8)) {
            Image(nsImage: image)
                .interpolation(.none)
                .resizable()
                .frame(width: 216, height: 216)
                .accessibilityLabel("WhatsApp pairing code. Scan it with your phone.")
        } else {
            Text("Waiting for the code…")
                .foregroundStyle(.secondary)
                .frame(width: 216, height: 216)
        }
    }

    static func parse(_ svg: String) -> (size: CGFloat, path: Path)? {
        guard let box = svg.range(of: "viewBox=\"0 0 "),
              let side = Double(svg[box.upperBound...].prefix { $0.isNumber }), side > 0,
              let stroke = svg.range(of: "stroke="),
              let opening = svg.range(of: " d=\"", range: stroke.upperBound..<svg.endIndex),
              let closing = svg.range(of: "\"", range: opening.upperBound..<svg.endIndex) else { return nil }
        let scanner = Scanner(string: String(svg[opening.upperBound..<closing.lowerBound]))
        scanner.charactersToBeSkipped = CharacterSet(charactersIn: " ,")
        var path = Path()
        var x = 0.0, y = 0.0
        while !scanner.isAtEnd {
            guard let command = scanner.scanCharacter(), let first = scanner.scanDouble() else { return nil }
            switch command {
            case "M":
                guard let second = scanner.scanDouble() else { return nil }
                x = first
                y = second
            case "m":
                guard let second = scanner.scanDouble() else { return nil }
                x += first
                y += second
            case "h":
                path.addRect(CGRect(x: x, y: y - 0.5, width: first, height: 1))
                x += first
            default:
                return nil
            }
        }
        return path.isEmpty ? nil : (CGFloat(side), path)
    }
}
