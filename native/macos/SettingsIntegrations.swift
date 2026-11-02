import SwiftUI

// MARK: GitHub

private struct GitHubForm: Equatable {
    var enabled = false, repos = "", routeCI = "", routeCode = ""
    init(_ full: JSON) {
        let github = full.github
        enabled = github.enabled.flag
        repos = github.repos.list.map(\.str).joined(separator: ", ")
        routeCI = github.route_ci.str
        routeCode = github.route_code.str
    }
}

@MainActor
struct SettingsGitHubPane: View {
    @Environment(Store.self) private var store
    @State private var form = SettingsDraft(reply: SettingsCache.last(SettingsCache.full), GitHubForm.init)
    @State private var status = SettingsStatus.none
    @State private var problem: String?

    var body: some View {
        Form {
            Section {
                SettingsProblemRow(problem: problem)
                SettingsSwitch(title: "Watch GitHub repositories", isOn: $form.value.enabled).disabled(!form.ready)
                TextField("Repos", text: $form.value.repos, prompt: Text("owner/repo, owner/other")).autocorrectionDisabled()
                SettingsAgentPicker(title: "CI failures go to", selection: $form.value.routeCI)
                SettingsAgentPicker(title: "PRs and issues go to", selection: $form.value.routeCode)
            } footer: {
                SettingsHint("Needs the gh CLI signed in and cloudflared installed. Webhooks are created and kept updated automatically.")
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
            form.receive(GitHubForm(try await SettingsCache.read(store.settingsAPI(), SettingsCache.full)))
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func save() async {
        do {
            let api = try store.settingsAPI()
            _ = try await api.put("/api/settings/github", [
                "enabled": form.value.enabled, "repos": settingsList(form.value.repos),
                "route_ci": store.settingsAgent(form.value.routeCI), "route_code": store.settingsAgent(form.value.routeCode),
            ])
            status = .good("Saved. Takes effect on next restart.")
            form.reset(to: GitHubForm(try await SettingsCache.read(api, SettingsCache.full)))
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
    }
}

// MARK: Webhooks

@MainActor
struct SettingsHooksPane: View {
    @Environment(Store.self) private var store
    private static let path = "/api/hooks"

    @State private var data = SettingsCache.last(SettingsHooksPane.path)
    @State private var name = ""
    @State private var agent = ""
    @State private var mode = "wake"
    @State private var status = SettingsStatus.none
    @State private var problem: String?

    private var base: String {
        let address = data.public_url.str
        return address.isEmpty ? "(tunnel url appears after restart)" : address
    }

    var body: some View {
        Form {
            Section {
                SettingsProblemRow(problem: problem)
                let rows = data.hooks.list
                if !data.isNull && rows.isEmpty { Text("No routes yet.").foregroundStyle(.secondary) }
                ForEach(rows, id: \.self) { route in routeRow(route) }
            } header: {
                SettingsHint("Let any service reach the agent: each route gets a URL that wakes it with the payload, or just drops it in the chat at zero model cost.")
            }
            Section {
                TextField("Route name", text: $name).autocorrectionDisabled()
                SettingsAgentPicker(title: "Profile", selection: $agent)
                Picker("When it fires", selection: $mode) {
                    Text("Wake agent").tag("wake")
                    Text("Notify only").tag("notify")
                }
                SettingsActions(status: status) {
                    AsyncButton("Add") { await add() }.buttonStyle(.borderedProminent)
                }
            } header: {
                Text("Add a route")
            }
        }
        .formStyle(.grouped)
        .settingsLoad { await load() }
    }

    private func routeRow(_ route: JSON) -> some View {
        let address = "\(base)/hook/\(route.name.str)?token=\(route.secret.str)"
        let target = store.name(store.settingsAgent(route.agent.str))
        return VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text("\(route.name.str) → \(target) (\(route.mode.str))").fontWeight(.medium)
                Spacer()
                SettingsCopyButton(text: address, title: "Copy URL")
                SettingsConfirmButton(title: "Remove", question: "Remove the \(route.name.str) route?",
                                      detail: "Its URL stops working. Adding the route again makes a new URL.") {
                    await remove(route)
                }
            }
            SettingsCode(address)
        }
    }

    private func load() async {
        do {
            data = try await SettingsCache.read(store.settingsAPI(), Self.path)
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func add() async {
        let clean = name.settingsTrimmed
        guard !clean.isEmpty else { status = .bad("Name required"); return }
        do {
            _ = try await store.settingsAPI().post(Self.path, ["name": clean, "agent": store.settingsAgent(agent), "mode": mode])
            status = .good("Added. Restart if this is the first route.")
            name = ""
            await load()
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
    }

    private func remove(_ route: JSON) async {
        do {
            _ = try await store.settingsAPI().delete("\(Self.path)/\(API.escape(route.name.str))")
            await load()
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
    }
}

// MARK: MCP servers

@MainActor
struct SettingsMCPPane: View {
    @Environment(Store.self) private var store
    private static let path = "/api/mcp"

    @State private var servers = SettingsCache.last(SettingsMCPPane.path).servers.list
    @State private var loaded = !SettingsCache.last(SettingsMCPPane.path).isNull
    @State private var config = ""
    @State private var status = SettingsStatus.none
    @State private var problem: String?

    var body: some View {
        Form {
            Section {
                SettingsProblemRow(problem: problem)
                if loaded && servers.isEmpty { Text("No servers yet. Paste a block below to add one.").foregroundStyle(.secondary) }
                ForEach(servers, id: \.["name"]) { server in
                    MCPServerRow(server: server) { change in await save(server, change) } remove: { await remove(server) }
                }
            } header: {
                SettingsHint("Give the agent tools from other apps. It reaches them through the mcp tool; a call the server marks destructive waits in your Inbox until you approve it.")
            }
            Section {
                SettingsTextBox(label: "Add servers", text: $config, prompt: "Paste an \"mcpServers\" JSON block, from the other app's settings", height: 110)
                SettingsActions(status: status) {
                    AsyncButton("Add") { await importServers() }.buttonStyle(.borderedProminent)
                }
            } header: {
                Text("Add servers")
            }
        }
        .formStyle(.grouped)
        .settingsLoad { await load() }
    }

    private func load() async {
        do {
            servers = try await SettingsCache.read(store.settingsAPI(), Self.path).servers.list
            loaded = true
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    /// Only these fields are sent. A stored token, environment or header stays as it is
    /// unless it is sent, and the server never gives those back to send.
    private func save(_ server: JSON, _ change: [String: Any]) async {
        status = .busy("Saving…")
        var body: [String: Any] = ["transport": server.transport.raw, "url": server.url.raw, "command": server.command.raw,
                                   "args": server.args.raw, "cwd": server.cwd.raw, "enabled": server.enabled.raw,
                                   "agents": server.agents.raw, "approval": server.approval.raw]
        body.merge(change) { _, new in new }
        do {
            // Saving also connects to the server, which can be slow to start.
            let reply = try await store.settingsAPI().settingsLong("PUT", "\(Self.path)/\(API.escape(server.name.str))", body, timeout: 120)
            if let problem = reply.error.optStr { status = .bad("Saved, but \(problem)") }
            else { status = .good("Saved. \(plural(reply.tools.int, "tool")).") }
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
        await load()
    }

    private func remove(_ server: JSON) async {
        do {
            _ = try await store.settingsAPI().delete("\(Self.path)/\(API.escape(server.name.str))")
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
        await load()
    }

    private func importServers() async {
        let text = config.settingsTrimmed
        guard !text.isEmpty else { status = .bad("Paste a JSON block first"); return }
        status = .busy("Connecting…")
        do {
            let reply = try await store.settingsAPI().settingsLong("POST", "\(Self.path)/import", ["config": text], timeout: 180)
            config = ""
            let added = reply.added.list.map(\.str).joined(separator: ", ")
            let skipped = reply.problems.list.map(\.str)
            status = skipped.isEmpty ? .good("Added \(added).") : .bad("Added \(added). Skipped: \(skipped.joined(separator: "; "))")
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
        await load()
    }
}

@MainActor
private struct MCPServerRow: View {
    let server: JSON
    let save: ([String: Any]) async -> Void
    let remove: () async -> Void
    @State private var agents: String
    @State private var enabled: Bool?
    @State private var approval: String?
    @FocusState private var editingAgents: Bool

    init(server: JSON, save: @escaping ([String: Any]) async -> Void, remove: @escaping () async -> Void) {
        self.server = server
        self.save = save
        self.remove = remove
        _agents = State(initialValue: Self.agentsText(server))
    }

    private static func agentsText(_ server: JSON) -> String { server.agents.list.map(\.str).joined(separator: ", ") }

    private var state: (status: String, label: String) {
        if !server.enabled.flag { return ("off", "off") }
        if !server.error.isNull { return ("error", "problem") }
        return server.connected.flag ? ("connected", "connected") : ("waiting", "connecting")
    }

    private var summary: String {
        var parts: [String] = []
        if server.error.isNull && server.connected.flag {
            let title = server.title.optStr.flatMap { $0.isEmpty ? nil : $0 } ?? server.name.str
            parts.append("\(title) \(server.version.str) · \(plural(server.tools.list.count, "tool"))")
        }
        if !server.agents.list.isEmpty { parts.append("only \(Self.agentsText(server))") }
        if server.token_set.flag { parts.append("token saved") }
        return parts.joined(separator: " · ")
    }

    private var address: String {
        if server.transport.str == "http" { return server.url.str }
        return ([server.command.str] + server.args.list.map(\.str)).joined(separator: " ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text(server.name.str).fontWeight(.medium)
                Pill(status: state.status, label: state.label)
                Spacer()
                Toggle("Enable \(server.name.str)", isOn: Binding(get: { enabled ?? server.enabled.flag }, set: { on in
                    enabled = on
                    Task { await save(["enabled": on]); enabled = nil }
                }))
                    .labelsHidden()
                    .toggleStyle(.switch)
            }
            if !server.error.isNull {
                Text(server.error.str).font(.caption).foregroundStyle(MC.warn).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
            if !summary.isEmpty { Text(summary).font(.caption).foregroundStyle(.secondary) }
            SettingsCode(address).foregroundStyle(.secondary)
            HStack(spacing: 8) {
                TextField("Profiles", text: $agents, prompt: Text("All profiles"))
                    .labelsHidden()
                    .textFieldStyle(.roundedBorder)
                    .autocorrectionDisabled()
                    .focused($editingAgents)
                    .help("Profile ids that may use this server, comma-separated. Blank for every profile with the mcp tool.")
                    .onSubmit { commitAgents() }
                    .onChange(of: editingAgents) { _, now in if !now { commitAgents() } }
                Picker("Approval", selection: Binding(get: { approval ?? server.approval.str }, set: { next in
                    approval = next
                    Task { await save(["approval": next]); approval = nil }
                })) {
                    Text("Approve destructive calls").tag("destructive")
                    Text("Approve every change").tag("writes")
                    Text("Never ask me").tag("none")
                }
                .labelsHidden()
                .fixedSize()
                SettingsConfirmButton(title: "Remove", question: "Remove \(server.name.str)?",
                                      detail: "Agents lose its tools and its saved token is deleted.") {
                    await remove()
                }
            }
        }
        .padding(.vertical, 2)
        .onChange(of: server.agents) { if !editingAgents { agents = Self.agentsText(server) } }
    }

    private func commitAgents() {
        let ids = settingsList(agents)
        guard ids != server.agents.list.map(\.str) else { return }
        Task { await save(["agents": ids.isEmpty ? NSNull() : ids]) }
    }
}

// MARK: Import

@MainActor
struct SettingsImportPane: View {
    @Environment(Store.self) private var store
    @State private var address = ""
    @State private var kind = "skills"
    @State private var status = SettingsStatus.none

    var body: some View {
        Form {
            Section {
                TextField("Repository", text: $address, prompt: Text("https://github.com/user/repo")).autocorrectionDisabled()
                Picker("Import", selection: $kind) {
                    Text("Skills").tag("skills")
                    Text("Profiles").tag("agents")
                }
                SettingsActions(status: status) {
                    AsyncButton("Import") { await run() }.buttonStyle(.borderedProminent)
                }
            } header: {
                SettingsHint("Pull skills or starter profiles from a git repository. Only import sources you trust: skills are instructions the agent will follow.")
            }
        }
        .formStyle(.grouped)
    }

    private func run() async {
        let url = address.settingsTrimmed
        guard !url.isEmpty else { status = .bad("URL required"); return }
        status = .busy("Importing…")
        do {
            // The server clones the repository, which it allows two minutes for.
            let reply = try await store.settingsAPI().settingsLong("POST", "/api/import", ["url": url, "kind": kind], timeout: 180)
            status = .good("Imported \(reply.imported.str) \(reply.kind.str == "agents" ? "profiles" : reply.kind.str)")
            if reply.kind.str == "agents" { await store.refreshState() }
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
    }
}
