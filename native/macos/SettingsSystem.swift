import SwiftUI

// MARK: Machines

@MainActor
struct SettingsFleetPane: View {
    /// Moves when joining changes the machine list, so the manual section reads it again.
    @State private var revision = 0

    var body: some View {
        Form {
            TailscaleSection()
            JoinSection { revision += 1 }
            FleetManualSection(revision: revision)
        }
        .formStyle(.grouped)
    }
}

@MainActor
private struct TailscaleSection: View {
    @Environment(Store.self) private var store
    @State private var data: JSON = .null
    @State private var note = SettingsStatus.none
    @State private var signInLink: URL?
    @State private var working = false

    private var state: String {
        if data.isNull { return "checking…" }
        if !data.installed.flag { return "not installed" }
        return data.running.flag ? "up: \(data.host.str)" : "installed, not signed in"
    }

    private var action: String {
        if !data.installed.flag { return "Install" }
        return data.running.flag ? "Signed in" : "Sign in"
    }

    var body: some View {
        Section {
            LabeledContent("Tailscale") {
                HStack(spacing: 8) {
                    Text(state).foregroundStyle(.secondary).textSelection(.enabled)
                    AsyncButton(action) { await setUp() }
                        .disabled(working || data.isNull || (data.installed.flag && data.running.flag))
                }
            }
            .settingsLoad { await load() }
            SettingsStatusLine(status: note)
            if let signInLink {
                LabeledContent("Open this to add the machine") {
                    Link(signInLink.absoluteString, destination: signInLink)
                }
            }
        } header: {
            SettingsHint("Run Mission Control on several machines as one agent: shared memory, skills and playbooks, and tasks handed across machines. One paste connects them.")
        }
    }

    private func load() async {
        guard let api = store.api else { return }
        do {
            data = try await api.post("/api/fleet/tailscale", ["action": "status"])
        } catch {
            if !Task.isCancelled { note = .bad(error.localizedDescription) }
        }
    }

    private func setUp() async {
        working = true
        defer { working = false }
        note = .busy("Setting up Tailscale… this can take a minute.")
        signInLink = nil
        do {
            let api = try store.settingsAPI()
            _ = try await api.settingsLong("POST", "/api/fleet/tailscale", ["action": "install"])
            let reply = try await api.settingsLong("POST", "/api/fleet/tailscale", ["action": "up"], timeout: 180)
            if let address = reply.url.optStr, let url = URL(string: address) {
                signInLink = url
                note = .none
            } else {
                note = .good(reply.note.str)
            }
        } catch {
            note = .bad(error.localizedDescription)
        }
        await load()
    }
}

@MainActor
private struct JoinSection: View {
    @Environment(Store.self) private var store
    let joined: () -> Void
    @State private var code = ""
    @State private var entered = ""
    @State private var status = SettingsStatus.none

    var body: some View {
        Section {
            VStack(alignment: .leading, spacing: 8) {
                Text("On this machine, make a code the other one pastes")
                HStack {
                    AsyncButton("Create join code") { await create() }
                    if !code.isEmpty { SettingsCopyButton(text: code) }
                }
                if !code.isEmpty { SettingsCode(code) }
            }
            VStack(alignment: .leading, spacing: 8) {
                Text("Or paste a code from another machine to join it")
                SettingsTextBox(label: "Join code", text: $entered, prompt: "paste the other machine's join code", height: 54)
            }
            SettingsActions(status: status) {
                AsyncButton("Join fleet") { await join() }.buttonStyle(.borderedProminent)
            }
        } header: {
            Text("Add a machine")
        }
    }

    private func create() async {
        do {
            let reply = try await store.settingsAPI().get("/api/fleet/joincode")
            code = reply.code.str
            status = .good(reply.over_tailscale.flag
                ? "Code ready (works from anywhere via Tailscale). Paste it on the other machine."
                : "Code ready. Both machines must be on the same network, or set up Tailscale first.")
        } catch {
            status = .bad(error.localizedDescription)
        }
    }

    private func join() async {
        let text = entered.settingsTrimmed
        guard !text.isEmpty else { status = .bad("Paste a code first"); return }
        status = .busy("Joining…")
        do {
            let reply = try await store.settingsAPI().post("/api/fleet/join", ["code": text])
            let name = reply.joined.str
            status = .good("Joined \(name) (\(plural(reply.peer_agents.int, "profile")) there). They appear as name@\(name).")
            entered = ""
            joined()
        } catch {
            status = .bad(error.localizedDescription)
        }
    }
}

private struct FleetForm: Equatable {
    var node = "", key = "", peers = ""
    init(_ fleet: JSON) {
        node = fleet.node_name.str
        peers = fleet.peers.list.map { "\($0.name.str) \($0.url.str)" }.joined(separator: "\n")
    }
}

@MainActor
private struct FleetManualSection: View {
    @Environment(Store.self) private var store
    let revision: Int
    @State private var fleet = SettingsCache.last(SettingsCache.full).fleet
    @State private var form = SettingsDraft(reply: SettingsCache.last(SettingsCache.full)) { FleetForm($0.fleet) }
    @State private var addresses: JSON = .null
    @State private var detecting = SettingsStatus.none
    @State private var status = SettingsStatus.none
    @State private var problem: String?

    var body: some View {
        Group {
            Section {
                SettingsProblemRow(problem: problem)
                TextField("This machine's name", text: $form.value.node, prompt: Text("studio"))
                    .autocorrectionDisabled()
                    .settingsLoad { await load() }
                    .onChange(of: revision) { Task { await load(replacing: true) } }
                SecureField("Fleet key", text: $form.value.key, prompt: Text(fleet.key_set.flag ? "saved" : "same key on every machine"))
                VStack(alignment: .leading, spacing: 5) {
                    Text("Machines")
                    SettingsTextBox(label: "Machines", text: $form.value.peers, prompt: "one per line: name http://host:8902\nlaptop http://192.168.1.20:8902", height: 72)
                }
                peerStatus
            } header: {
                Text("Fleet, manual")
            } footer: {
                SettingsHint("Set names, key and machine addresses by hand. The section above does this for you. Every machine needs the same fleet key and each other's address. Knowledge syncs every minute; restart the app after first enabling. No static IP? Install Tailscale and use Detect to get a stable address that works from anywhere.")
            }
            Section {
                LabeledContent("This machine's address") {
                    AsyncButton("Detect") { await detect() }
                }
                SettingsStatusLine(status: detecting)
                if !addresses.isNull { addressRows }
                SettingsActions(status: status) {
                    AsyncButton("Save") { await save() }.buttonStyle(.borderedProminent).disabled(!form.ready)
                }
            }
        }
    }

    @ViewBuilder private var peerStatus: some View {
        ForEach(fleet.peers.list, id: \.self) { peer in
            let ok = peer.ok == .bool(true)
            let word = ok ? "connected" : (peer.ok == .bool(false) ? "unreachable" : "not seen yet")
            HStack(spacing: 8) {
                Pill(status: ok ? "connected" : (peer.ok == .bool(false) ? "waiting" : "off"), label: word)
                Text("\(peer.name.str): \(detail(peer, ok: ok))").font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func detail(_ peer: JSON, ok: Bool) -> String {
        let fallback = peer.detail.optStr.flatMap { $0.isEmpty ? nil : $0 }
        guard ok else { return fallback ?? "unreachable" }
        guard peer.last_sync.num > 0 else { return fallback ?? "not seen yet" }
        let minutes = max(1, Int(((Date().timeIntervalSince1970 - peer.last_sync.num) / 60).rounded()))
        return "synced \(minutes)m ago"
    }

    @ViewBuilder private var addressRows: some View {
        LabeledContent("Same network") { SettingsCode(addresses.lan.str) }
        if let tailscale = addresses.tailscale.optStr, !tailscale.isEmpty {
            LabeledContent("Anywhere (Tailscale)") { SettingsCode(tailscale) }
        } else {
            LabeledContent("Tailscale", value: "not available (install from tailscale.com for a stable address that works from anywhere)")
        }
        SettingsHint("Paste the address into the Machines list on your other machines.")
    }

    private func load(replacing: Bool = false) async {
        do {
            let reply = try await SettingsCache.read(store.settingsAPI(), SettingsCache.full)
            fleet = reply.fleet
            if replacing { form.reset(to: FleetForm(reply.fleet)) } else { form.receive(FleetForm(reply.fleet)) }
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func detect() async {
        detecting = .busy("Detecting…")
        do {
            addresses = try await store.settingsAPI().get("/api/fleet/addresses")
            detecting = .none
        } catch {
            detecting = .bad("Detection failed")
        }
    }

    private func save() async {
        let peers: [[String: Any]] = form.value.peers.split(separator: "\n").compactMap { line in
            let words = line.split(whereSeparator: \.isWhitespace).map(String.init)
            guard words.count >= 2 else { return nil }
            return ["name": words[0], "url": words[1]]
        }
        var body: [String: Any] = ["node_name": form.value.node.settingsTrimmed, "peers": peers]
        let key = form.value.key.settingsTrimmed
        // An empty field means keep the saved key; it is only sent when replaced.
        if !key.isEmpty { body["sync_key"] = key }
        do {
            let reply = try await store.settingsAPI().put("/api/settings/fleet", body)
            fleet = reply.fleet
            form.reset(to: FleetForm(reply.fleet))
            status = .good("Saved. Restart the app to apply.")
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
    }
}

// MARK: Mobile access

@MainActor
struct SettingsRemotePane: View {
    @Environment(Store.self) private var store
    @State private var data: JSON = .null
    @State private var revealed = false
    @State private var status = SettingsStatus.none
    @State private var problem: String?
    @State private var changing: Bool?

    private var token: String { data.token.str }

    var body: some View {
        Form {
            Section {
                SettingsProblemRow(problem: problem)
                SettingsSwitch(title: "Mobile access",
                               note: "Let the Mission Control mobile app connect from your phone. Turn on, restart, and use your Tailscale or LAN address with the token below.",
                               isOn: Binding(get: { changing ?? data.expose.flag }, set: { setExpose($0) }))
                    .disabled(data.isNull)
                tokenRow
                SettingsStatusLine(status: status)
            } footer: {
                SettingsHint("Local connections never need the token; anything remote does. Keep remote access on a private network like Tailscale.")
            }
        }
        .formStyle(.grouped)
        .settingsLoad { await load() }
    }

    @ViewBuilder private var tokenRow: some View {
        LabeledContent("Access token") {
            HStack(spacing: 8) {
                if token.isEmpty {
                    Text("generated when you enable").foregroundStyle(.secondary)
                } else {
                    Text(revealed ? token : String(repeating: "•", count: 16))
                        .font(.system(size: 11, design: .monospaced))
                        .textSelection(.enabled)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Button(revealed ? "Hide" : "Show") { revealed.toggle() }
                    SettingsCopyButton(text: token)
                }
                SettingsConfirmButton(title: "New", question: "Replace the access token?",
                                      detail: "Phones using the current token stop connecting until they have the new one. Restart to apply.",
                                      confirm: "Replace token") {
                    await save(expose: data.expose.flag, regenerate: true)
                }
                .disabled(data.isNull)
            }
        }
    }

    private func load() async {
        do {
            data = try await store.settingsAPI().get("/api/remote")
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func setExpose(_ on: Bool) {
        changing = on
        Task {
            await save(expose: on, regenerate: false)
            changing = nil
        }
    }

    private func save(expose: Bool, regenerate: Bool) async {
        do {
            let reply = try await store.settingsAPI().put("/api/remote", ["expose": expose, "regenerate": regenerate])
            data = reply
            status = .good("Saved. Restart to apply.")
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
    }
}

// MARK: Ports

private struct PortsForm: Equatable {
    var app = "8899", webhook = "8901", fleet = "8902"
    init(_ full: JSON) {
        app = full.ports.app.optStr ?? "8899"
        webhook = full.ports.public.optStr ?? "8901"
        fleet = full.ports.fleet.optStr ?? "8902"
    }
}

@MainActor
struct SettingsPortsPane: View {
    @Environment(Store.self) private var store
    @State private var form = SettingsDraft(reply: SettingsCache.last(SettingsCache.full), PortsForm.init)
    @State private var status = SettingsStatus.none
    @State private var problem: String?

    var body: some View {
        Form {
            Section {
                SettingsProblemRow(problem: problem)
                TextField("App", text: $form.value.app)
                TextField("Webhook listener", text: $form.value.webhook)
                TextField("Fleet", text: $form.value.fleet)
            } header: {
                SettingsHint("Where Mission Control listens on this machine. Restart to apply.")
            } footer: {
                SettingsHint("Running the server under launchd or your own supervisor? Update its command to match the app port.")
            }
            Section {
                SettingsActions(status: status) {
                    AsyncButton("Save") { await save() }.buttonStyle(.borderedProminent).disabled(!form.ready)
                }
            }
        }
        .formStyle(.grouped)
        .settingsLoad { await load() }
    }

    private func load() async {
        do {
            form.receive(PortsForm(try await SettingsCache.read(store.settingsAPI(), SettingsCache.full)))
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func save() async {
        let values = [("App", form.value.app), ("Webhook listener", form.value.webhook), ("Fleet", form.value.fleet)]
        for (name, text) in values where Int(text.settingsTrimmed) == nil {
            status = .bad("Error: \(name) needs a port number")
            return
        }
        do {
            let reply = try await store.settingsAPI().put("/api/settings/ports", [
                "app": Int(form.value.app.settingsTrimmed) ?? 0, "public": Int(form.value.webhook.settingsTrimmed) ?? 0,
                "fleet": Int(form.value.fleet.settingsTrimmed) ?? 0,
            ])
            form.reset(to: PortsForm(.object(["ports": reply.ports])))
            status = .good("Saved. Restart Mission Control to apply.")
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
    }
}
