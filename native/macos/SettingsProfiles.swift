import SwiftUI

private enum ProfileSheet: Identifiable {
    case new
    case edit(JSON)
    var id: String {
        switch self {
        case .new: return "new"
        case .edit(let profile): return profile.id
        }
    }
}

@MainActor
struct SettingsProfilesPane: View {
    /// `new` or a profile's id: a chat's "edit" button lands here with that sheet open.
    let open: String
    @Environment(Store.self) private var store
    private static let path = "/api/agents-full"

    @State private var profiles = SettingsCache.last(SettingsProfilesPane.path).agents.list
    @State private var loaded = !SettingsCache.last(SettingsProfilesPane.path).isNull
    @State private var settings = SettingsCache.last("/api/settings")
    @State private var full = SettingsCache.last(SettingsCache.full)
    @State private var sheet: ProfileSheet?
    @State private var problem: String?
    @State private var opened = ""

    private var ordered: [JSON] { profiles.sorted { $0.order.int < $1.order.int } }

    var body: some View {
        Form {
            Section {
                SettingsProblemRow(problem: problem)
                if loaded && profiles.isEmpty { Text("No profiles yet.").foregroundStyle(.secondary) }
                ForEach(ordered) { profile in row(profile) }
            } header: {
                HStack {
                    SettingsHint("One agent, with a profile for each role its tasks can run as.")
                    Spacer()
                    Button("New profile") { sheet = .new }
                }
            }
        }
        .formStyle(.grouped)
        .settingsLoad { await load() }
        .sheet(item: $sheet) { item in
            SettingsProfileEditor(sheet: item, full: full, imessageAgent: settings.imessage_default_agent.str, colorIndex: profiles.count) { created in
                sheet = nil
                if let created { store.go(.chat("dm:\(created)")) } else { Task { await load() } }
            }
        }
        .onChange(of: open, initial: true) { openRequested() }
    }

    private func row(_ profile: JSON) -> some View {
        Button { sheet = .edit(profile) } label: {
            HStack(spacing: 10) {
                AgentBadge(agent: Agent(profile))
                VStack(alignment: .leading, spacing: 1) {
                    Text(profile.name.str).foregroundStyle(.primary)
                    Text(subtitle(profile)).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer()
                if settings.imessage_default_agent.str == profile.id && settings.imessage_enabled.flag {
                    Pill(status: "active", label: "iMessage")
                }
                Image(systemName: "chevron.right").font(.caption).foregroundStyle(.tertiary).accessibilityHidden(true)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Edit \(profile.name.str)")
    }

    private func subtitle(_ profile: JSON) -> String {
        let model = profile.llm.model.str
        let role = profile.blurb.optStr.flatMap { $0.isEmpty ? nil : $0 } ?? clip(profile.prompt.str, 70)
        return model.isEmpty ? role : "\(model) · \(role)"
    }

    private func load() async {
        do {
            let api = try store.settingsAPI()
            async let rows = SettingsCache.read(api, Self.path)
            async let answering = SettingsCache.read(api, "/api/settings")
            async let everything = SettingsCache.read(api, SettingsCache.full)
            profiles = try await rows.agents.list
            loaded = true
            settings = try await answering
            full = try await everything
            problem = nil
            openRequested()
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func openRequested() {
        guard loaded, !open.isEmpty, opened != open else { return }
        opened = open
        if open == "new" { sheet = .new } else if let profile = profiles.first(where: { $0.id == open }) { sheet = .edit(profile) }
    }
}

@MainActor
private struct SettingsProfileEditor: View {
    let sheet: ProfileSheet
    let full: JSON
    let imessageAgent: String
    let colorIndex: Int
    /// Called with the new profile's id, or nil when nothing new was made.
    let finish: (String?) -> Void

    @Environment(Store.self) private var store
    private static let swatches = ["#7a68c9", "#e0559b", "#d6493f", "#f5a623", "#3aa655", "#30c8bd", "#3f7bd9", "#8e8e93", "#1d1d1f"]

    @State private var name: String
    @State private var color: String
    @State private var role: String
    @State private var folder: String
    @State private var provider: String
    @State private var model: String
    @State private var rounds: String
    @State private var calls: String
    @State private var tools: String
    @State private var skills: String
    @State private var imessage: Bool
    @State private var models: [String] = []
    @State private var error = ""
    @State private var saving = false

    init(sheet: ProfileSheet, full: JSON, imessageAgent: String, colorIndex: Int, finish: @escaping (String?) -> Void) {
        self.sheet = sheet
        self.full = full
        self.imessageAgent = imessageAgent
        self.colorIndex = colorIndex
        self.finish = finish
        var profile = JSON.null
        if case .edit(let row) = sheet { profile = row }
        func namesText(_ value: JSON) -> String {
            guard case .array(let names) = value else { return "" }
            return names.isEmpty ? "none" : names.map(\.str).joined(separator: ", ")
        }
        let folderValue = profile.cwd.str
        _name = State(initialValue: profile.name.str)
        _color = State(initialValue: profile.color.optStr ?? Self.swatches[colorIndex % Self.swatches.count])
        _role = State(initialValue: profile.prompt.str)
        _folder = State(initialValue: folderValue == "~" ? "" : folderValue)
        _provider = State(initialValue: profile.llm.provider.str)
        _model = State(initialValue: profile.llm.model.str)
        _rounds = State(initialValue: String(profile.max_tool_rounds.optNum.map(Int.init) ?? 40))
        _calls = State(initialValue: String(profile.max_tool_calls.optNum.map(Int.init) ?? 160))
        _tools = State(initialValue: namesText(profile.tools))
        _skills = State(initialValue: namesText(profile.skills))
        _imessage = State(initialValue: !profile.isNull && profile.id == imessageAgent)
    }

    private var existing: JSON? {
        if case .edit(let row) = sheet { return row }
        return nil
    }
    private var isDefaultResponder: Bool { existing?.id == imessageAgent && !imessageAgent.isEmpty }
    private var presets: JSON { full.llm.presets }

    private var presetRows: [(id: String, label: String)] {
        presets.dict
            .map { (id: $0.key, label: $0.value.label.str) }
            .sorted { $0.label.localizedCaseInsensitiveCompare($1.label) == .orderedAscending }
    }

    /// A provider whose key is missing would fail on this profile's first turn.
    private var modelHint: (text: String, warn: Bool)? {
        guard !provider.isEmpty else { return nil }
        let preset = presets[provider]
        let keyEnv = preset.key_env.str
        if !keyEnv.isEmpty, !(full.llm.key_env.str == keyEnv && full.llm.key_set.flag) {
            return ("\(preset.label.str) needs \(keyEnv) in Settings, General, before this profile can use it.", true)
        }
        return ("This profile thinks with this model; the others are unaffected.", false)
    }

    var body: some View {
        VStack(spacing: 0) {
            Form {
                identity
                modelSection
                if existing != nil { limits }
                Section {
                    Toggle("Responds to your iMessages", isOn: $imessage)
                        .toggleStyle(.switch)
                        .disabled(isDefaultResponder)
                        .help(isDefaultResponder ? "This profile is the iMessage responder. Turn this on for another profile to move it." : "Route texts from your phone to this profile")
                }
            }
            .formStyle(.grouped)
            Divider()
            footer.padding(14)
        }
        .frame(width: 540, height: 640)
        .task(id: provider) { await loadModels() }
    }

    private var identity: some View {
        Section {
            HStack(spacing: 10) {
                AgentBadge(agent: Agent(.object(["id": .string(existing?.id ?? "new"), "name": .string(name), "color": .string(color)])), size: 34)
                TextField("Name", text: $name, prompt: Text("Name, e.g. QA Engineer"))
            }
            swatchRow
            VStack(alignment: .leading, spacing: 5) {
                Text("Role")
                SettingsTextBox(label: "Role", text: $role, prompt: "What it owns, how it works, what it is responsible for. Written to it as 'You are…'", height: 110, mono: false)
            }
            TextField("Workspace folder", text: $folder, prompt: Text("Optional, e.g. ~/Sites/myrepo"))
                .autocorrectionDisabled()
        } header: {
            Text(existing == nil ? "New profile" : "Edit profile")
        }
    }

    private var swatchRow: some View {
        LabeledContent("Colour") {
            HStack(spacing: 6) {
                ForEach(Array(Self.swatches.enumerated()), id: \.element) { index, swatch in
                    Button { color = swatch } label: {
                        Circle()
                            .fill(SwiftUI.Color(hex: swatch))
                            .frame(width: 20, height: 20)
                            .overlay {
                                if color.lowercased() == swatch {
                                    Image(systemName: "checkmark").font(.system(size: 10, weight: .bold)).foregroundStyle(.white)
                                }
                            }
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Colour \(index + 1)")
                    .accessibilityAddTraits(color.lowercased() == swatch ? .isSelected : [])
                }
            }
        }
    }

    private var modelSection: some View {
        Section {
            Picker("Provider", selection: $provider) {
                Text("Default (\(full.llm.model.optStr ?? "the default model"))").tag("")
                ForEach(presetRows, id: \.id) { Text($0.label).tag($0.id) }
                if !provider.isEmpty && presets[provider].isNull { Text(provider).tag(provider) }
            }
            if !provider.isEmpty {
                SettingsModelField(title: "Model", text: $model, prompt: "model", models: models)
            }
        } header: {
            Text("Model")
        } footer: {
            if let modelHint {
                SettingsHint(modelHint.text, warn: modelHint.warn)
            }
        }
    }

    private var limits: some View {
        Section {
            TextField("Tool rounds", text: $rounds)
            TextField("Total tool calls", text: $calls)
            TextField("Allowed tools", text: $tools, prompt: Text("All tools (or comma-separated names)"))
                .autocorrectionDisabled()
            TextField("Allowed skills", text: $skills, prompt: Text("All skills (or comma-separated slugs)"))
                .autocorrectionDisabled()
        } header: {
            Text("Limits and permissions")
        } footer: {
            SettingsHint("Blank uses all available capabilities. Enter “none” to disable. Skill contracts can narrow tool access further.")
        }
    }

    private var footer: some View {
        HStack(spacing: 8) {
            if let existing {
                SettingsConfirmButton(title: "Remove profile", question: "Remove \(existing.name.str)?", confirm: "Remove") { await remove(existing) }
            }
            if !error.isEmpty { SettingsStatusLine(status: .bad(error)) }
            Spacer(minLength: 8)
            Button("Cancel", role: .cancel) { finish(nil) }.keyboardShortcut(.cancelAction)
            Button(existing == nil ? "Create" : "Save") { Task { await submit() } }
                .keyboardShortcut(.defaultAction)
                .buttonStyle(.borderedProminent)
                .disabled(saving)
        }
    }

    private func loadModels() async {
        models = []
        guard !provider.isEmpty, let api = store.api else { return }
        let reply = try? await api.get("/api/settings/llm/models?provider=\(API.escape(provider))&base_url=")
        if !Task.isCancelled { models = reply?.models.list.map(\.str) ?? [] }
    }

    private func allowlist(_ text: String) -> Any {
        let raw = text.settingsTrimmed
        if raw.isEmpty { return NSNull() }
        return raw.lowercased() == "none" ? [String]() : settingsList(raw)
    }

    /// What the owner set by other means, such as `/effort`, stays on the profile while
    /// the provider stays the same. The web's form would drop it on every save.
    private var modelBody: [String: Any] {
        guard !provider.isEmpty else { return [:] }
        var body: [String: Any] = [:]
        if let existing, existing.llm.provider.str == provider { body = existing.llm.dict.mapValues(\.raw) }
        body["provider"] = provider
        body["model"] = model.settingsTrimmed
        return body
    }

    private func submit() async {
        let cleanName = name.settingsTrimmed, cleanRole = role.settingsTrimmed
        guard !cleanName.isEmpty, !cleanRole.isEmpty else { error = "Name and role description are both required."; return }
        guard provider.isEmpty || !model.settingsTrimmed.isEmpty else { error = "Pick a model for that provider, or go back to the default."; return }
        saving = true
        defer { saving = false }
        error = ""
        let cleanFolder = folder.settingsTrimmed
        var payload: [String: Any] = ["name": cleanName, "prompt": cleanRole, "color": color,
                                      "cwd": cleanFolder.isEmpty ? "~" : cleanFolder, "llm": modelBody]
        do {
            let api = try store.settingsAPI()
            let id: String
            if let existing {
                id = existing.id
                payload["tools"] = allowlist(tools)
                payload["skills"] = allowlist(skills)
                payload["max_tool_rounds"] = min(max(Int(rounds.settingsTrimmed) ?? 40, 1), 200)
                payload["max_tool_calls"] = min(max(Int(calls.settingsTrimmed) ?? 160, 1), 800)
                _ = try await api.put("/api/agents/\(API.escape(id))", payload)
            } else {
                id = try await api.post("/api/agents", payload).agent.id
            }
            if imessage && !isDefaultResponder { _ = try await api.post("/api/settings", ["imessage_default_agent": id]) }
            await store.refreshState()
            finish(existing == nil ? id : nil)
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func remove(_ profile: JSON) async {
        do {
            _ = try await store.settingsAPI().delete("/api/agents/\(API.escape(profile.id))")
            await store.refreshState()
            finish(nil)
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private extension SwiftUI.Color {
    init(hex: String) { self = color(hex: hex, fallback: .gray) }
}
