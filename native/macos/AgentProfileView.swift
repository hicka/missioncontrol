import SwiftUI

/// The last read of each profile. It outlives the panel, so opening one again draws its
/// lessons and numbers at once.
@Observable @MainActor
private final class ProfileCache {
    static let shared = ProfileCache()

    private var base = ""
    private(set) var profiles: [String: JSON] = [:]
    private(set) var problems: [String: String] = [:]

    func load(_ agentID: String, api: API) async {
        if api.base != base { base = api.base; profiles = [:]; problems = [:] }
        do {
            let profile = try await api.get("/api/agents/\(API.escape(agentID))/profile")
            if Task.isCancelled { return }
            if profiles[agentID] != profile { profiles[agentID] = profile }
            problems[agentID] = nil
        } catch {
            if !Task.isCancelled { problems[agentID] = error.localizedDescription }
        }
    }
}

private func stamp(_ rows: [JSON], of agentID: String) -> Int {
    var hasher = Hasher()
    for row in rows where row.agent.str == agentID {
        hasher.combine(row.id)
        hasher.combine(row.status.str)
        hasher.combine(row.updated.num)
    }
    return hasher.finalize()
}

/// Reads again when this profile's work moves. Events come in bursts, so a read waits for a
/// pause, except the first one for a profile. The store is read in this body alone, which
/// keeps those events from redrawing the panel.
private struct ProfileLoader: View {
    @Environment(Store.self) private var store
    let agentID: String
    @State private var readAgent: String?

    private struct Key: Equatable {
        let agentID: String, base: String?, link: Bool, state: String?
        let executions: Int, missions: Int, jobs: Int, watches: Int
        let ticks: [Int]
    }

    var body: some View {
        let ticks = store.ticks
        let key = Key(agentID: agentID, base: store.api?.base, link: store.link, state: store.states[agentID],
                      executions: stamp(store.executions, of: agentID), missions: stamp(store.missions, of: agentID),
                      jobs: stamp(store.jobs, of: agentID), watches: stamp(store.watches, of: agentID),
                      ticks: [ticks.evolution, ticks.memory, ticks.skills, ticks.settings, ticks.improvements, ticks.browser])
        Color.clear.task(id: key) {
            if readAgent == agentID {
                try? await Task.sleep(nanoseconds: 600_000_000)
                if Task.isCancelled { return }
            }
            readAgent = agentID
            if let api = store.api { await ProfileCache.shared.load(agentID, api: api) }
        }
    }
}

/// One profile's work in one place: what it is doing now, what is scheduled for it, what it
/// finished and what it has learned. Everything the store holds draws at once; the profile's
/// own read adds its model, numbers and lessons behind that. A presenter gives it room:
/// an inspector column, or a sheet, where it offers Close itself.
struct AgentProfileView: View {
    let agentID: String
    @Environment(\.isPresented) private var isPresented
    @Environment(\.dismiss) private var dismiss

    init(agentID: String) { self.agentID = agentID }

    var body: some View {
        let cache = ProfileCache.shared
        let profile = cache.profiles[agentID]
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if isPresented {
                    HStack {
                        Spacer()
                        Button("Close") { dismiss() }.keyboardShortcut(.cancelAction).controlSize(.small)
                    }
                }
                ProfileHead(agentID: agentID, profile: profile, onRemoved: { if isPresented { dismiss() } })
                if profile == nil, let problem = cache.problems[agentID] {
                    Text(problem).font(.callout).foregroundStyle(MC.danger).fixedSize(horizontal: false, vertical: true)
                }
                ProfileStats(stats: profile?.stats ?? .null)
                ProfileNow(agentID: agentID, profile: profile)
                ProfileWork(agentID: agentID, profile: profile)
                ProfileLessons(lessons: profile?.lessons.list)
            }
            .padding(18)
        }
        .frame(minWidth: 300, idealWidth: 340, maxWidth: .infinity, minHeight: 420, idealHeight: 640, maxHeight: .infinity)
        .background { ProfileLoader(agentID: agentID) }
    }
}

private struct ProfileHead: View {
    @Environment(Store.self) private var store
    let agentID: String
    let profile: JSON?
    let onRemoved: () -> Void
    @State private var editing = false
    @State private var removed = false

    var body: some View {
        let agent = store.agent(agentID)
        let name = agent?.name ?? profile?.agent.name.str ?? agentID
        let role = profile?.agent.role.optStr ?? store.agentInfo[agentID].role.str
        let state = store.states[agentID] ?? profile?.state.str ?? "idle"
        VStack(spacing: 6) {
            AgentBadge(agent: agent, size: 64)
            Text(name).font(.title3.weight(.semibold)).multilineTextAlignment(.center)
            Text(role.isEmpty ? "A role tasks can run as" : role)
                .font(.callout).foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            if let profile {
                let own = profile.agent.own_model.flag
                let model = profile.agent.model.str.isEmpty ? "default model" : profile.agent.model.str
                Text(model)
                    .font(.caption)
                    .lineLimit(1)
                    .padding(.horizontal, 8).padding(.vertical, 2)
                    .background(Color.primary.opacity(0.07), in: Capsule())
                    .help(own ? "Its own model" : "The default model")
                    .accessibilityLabel("\(model), \(own ? "its own model" : "the default model")")
            }
            HStack(spacing: 8) {
                Button { editing = true } label: { Label("Edit", systemImage: "pencil") }
                if state != "idle" {
                    AsyncButton(action: {
                        guard let api = store.api else { throw APIError(message: "Not connected to the runtime yet.") }
                        _ = try await api.post("/api/agents/\(API.escape(agentID))/stop")
                        store.notify("Stopped \(name).")
                    }) {
                        Label("Stop", systemImage: "stop.fill").foregroundStyle(MC.danger)
                    }
                }
            }
            .controlSize(.small)
            .padding(.top, 4)
        }
        .frame(maxWidth: .infinity)
        // The panel can only follow once the editor on top of it has gone.
        .sheet(isPresented: $editing, onDismiss: { if removed { onRemoved() } }) {
            ProfileEditor(agentID: agentID, onRemoved: { removed = true })
        }
    }
}

private struct ProfileStats: View {
    let stats: JSON

    var body: some View {
        HStack(spacing: 8) {
            figure(percent(stats.clean_rate), "clean runs")
            figure(count(stats.runs), "runs this week")
            figure(count(stats.corrections), "corrections")
        }
    }

    /// A dash until the profile has been read; after that a missing number is a real zero.
    private func count(_ value: JSON) -> String { stats.isNull ? "—" : "\(value.int)" }

    private func figure(_ value: String, _ word: String) -> some View {
        VStack(spacing: 1) {
            Text(value).font(.system(size: 16, weight: .semibold)).monospacedDigit()
            Text(word).font(.system(size: 11)).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 8)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .accessibilityElement(children: .combine)
    }
}

private struct ProfileRow: View {
    let icon: String
    let title: String
    var detail: String?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: icon).font(.system(size: 12)).foregroundStyle(.secondary).frame(width: 16).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.system(size: 13)).fixedSize(horizontal: false, vertical: true)
                if let detail, !detail.isEmpty {
                    Text(detail).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .accessibilityElement(children: .combine)
    }
}

private struct ProfileSection<Rows: View>: View {
    let title: String
    var count = 0
    var showsCount = true
    let empty: String
    @ViewBuilder var rows: Rows

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            SectionHead(title, count: showsCount && count > 0 ? count : nil)
            if count > 0 {
                rows
            } else {
                Text(empty).font(.callout).foregroundStyle(.secondary)
            }
        }
    }
}

private func toolDetail(_ execution: JSON) -> String {
    var arguments = execution.detail.arguments
    if case .string(let text) = arguments {
        let parsed = JSON(data: Data(text.utf8))
        if parsed.isNull { return String(text.prefix(120)) }
        arguments = parsed
    }
    for key in ["command", "url", "query", "action", "text", "name"] {
        let value = arguments[key].str
        if !value.isEmpty { return String(value.prefix(120)) }
    }
    return ""
}

/// Drawn from the event stream rather than the profile's read, because it moves the most.
private struct ProfileNow: View {
    @Environment(Store.self) private var store
    let agentID: String
    let profile: JSON?

    var body: some View {
        let running = Array(store.executions.filter { $0.agent.str == agentID && $0.status.str == "running" }.prefix(6))
        let queued = profile?.queued.int ?? store.agentInfo[agentID].queued.int
        ProfileSection(title: "Now", count: running.count + (queued > 0 ? 1 : 0), showsCount: false, empty: idleWords) {
            ForEach(running) { execution in
                ProfileRow(icon: "bolt", title: "Running \(execution.name.str.replacingOccurrences(of: "_", with: " "))", detail: toolDetail(execution))
            }
            if queued > 0 { ProfileRow(icon: "tray", title: "\(plural(queued, "message")) waiting in its queue") }
        }
    }

    private var idleWords: String {
        switch store.states[agentID] ?? "idle" {
        case "idle": return "Idle"
        case "thinking": return "Thinking…"
        case "doing":
            if let tool = store.tools[agentID] { return "Working · \(tool.replacingOccurrences(of: "_", with: " "))" }
            return "Working…"
        case let state: return state.capitalized
        }
    }
}

private struct ProfileWork: View {
    @Environment(Store.self) private var store
    let agentID: String
    let profile: JSON?

    var body: some View {
        let mine = profile == nil ? store.missions.filter { $0.agent.str == agentID } : []
        let week = Date().timeIntervalSince1970 - 7 * 86400
        let active = profile?.active.list ?? Array(mine.filter { ["queued", "running", "waiting"].contains($0.status.str) }.prefix(12))
        let done = profile?.done.list ?? Array(mine.filter { ["completed", "failed", "cancelled"].contains($0.status.str) && $0.updated.num >= week }.prefix(12))
        let jobs = profile?.scheduled.jobs.list ?? store.jobs.filter { $0.agent.str == agentID && ["active", "pending", "paused"].contains($0.status.str) }
        let watches = profile?.scheduled.watches.list ?? store.watches.filter { $0.agent.str == agentID && $0.status.str == "active" }

        ProfileSection(title: "Active", count: active.count, empty: "No open tasks") {
            ForEach(active) { ProfileRow(icon: "checklist", title: clip($0.objective.str, 140), detail: missionLine($0)) }
        }
        ProfileSection(title: "Scheduled", count: jobs.count + watches.count, empty: "Nothing scheduled") {
            ForEach(jobs) { ProfileRow(icon: "clock", title: $0.name.str, detail: "\($0.schedule.str) · \($0.status.str)") }
            ForEach(watches) { ProfileRow(icon: "arrow.clockwise", title: $0.name.str, detail: "watch · \($0.checks.int)/\($0.max_checks.int) checks") }
        }
        ProfileSection(title: "Done this week", count: done.count, empty: "Nothing finished yet this week") {
            ForEach(done) { mission in
                ProfileRow(icon: mission.status.str == "completed" ? "checkmark" : "exclamationmark.triangle", title: clip(mission.objective.str, 140), detail: missionLine(mission))
            }
        }
    }

    private func missionLine(_ mission: JSON) -> String { "\(mission.status.str.replacingOccurrences(of: "_", with: " ")) · \(ago(mission.updated.num))" }
}

private struct ProfileLessons: View {
    let lessons: [JSON]?

    var body: some View {
        let rows = lessons ?? []
        ProfileSection(title: "Lessons", count: rows.count, empty: lessons == nil ? "Loading…" : "No lessons yet") {
            ForEach(rows.prefix(8)) { lesson in
                let adopted = lesson.status.str == "active"
                ProfileRow(icon: adopted ? "checkmark" : "testtube.2", title: lesson.text.str,
                           detail: "\(lesson.agent.str == "*" ? "everywhere" : "own") · \(adopted ? "adopted" : "on trial")")
            }
        }
    }
}

// MARK: Editing a profile

private struct ProfileEditor: View {
    @Environment(Store.self) private var store
    @Environment(\.dismiss) private var dismiss
    let agentID: String
    let onRemoved: () -> Void

    @State private var loaded = false
    @State private var name = ""
    @State private var prompt = ""
    @State private var colour = ""
    @State private var folder = ""
    @State private var provider = ""
    @State private var model = ""
    @State private var original: [String: JSON] = [:]
    @State private var rounds = 40
    @State private var calls = 160
    @State private var tools = ""
    @State private var skills = ""
    @State private var respondsToTexts = false
    @State private var respondsLocked = false
    @State private var llm: JSON = .null
    @State private var suggestions: [String] = []
    @State private var problem: String?
    @State private var saving = false
    @State private var confirmingRemove = false

    private static let swatches = [("#7a68c9", "Violet"), ("#e0559b", "Pink"), ("#d6493f", "Red"), ("#f5a623", "Orange"), ("#3aa655", "Green"),
                                   ("#30c8bd", "Teal"), ("#3f7bd9", "Blue"), ("#8e8e93", "Grey"), ("#1d1d1f", "Black")]

    var body: some View {
        VStack(spacing: 0) {
            Text("Edit profile").font(.headline).padding(.top, 16).padding(.bottom, 4)
            if loaded { form } else {
                Text(problem ?? "Loading…").font(.callout).foregroundStyle(problem == nil ? Color.secondary : MC.danger)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            footer
        }
        .frame(width: 540)
        .frame(minHeight: 500, idealHeight: 660)
        .task { await load() }
        .task(id: provider) { await readSuggestions() }
        .confirmationDialog("Remove the profile “\(name)”?", isPresented: $confirmingRemove, titleVisibility: .visible) {
            Button("Remove", role: .destructive) { Task { await remove() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("It stops taking tasks and its chat leaves the sidebar.")
        }
    }

    private var form: some View {
        Form {
            Section {
                TextField("Name", text: $name, prompt: Text("Name, e.g. QA Engineer"))
                LabeledContent("Colour") { swatchRow }
            }
            Section {
                TextEditor(text: $prompt)
                    .font(.system(size: 13))
                    .frame(minHeight: 110)
                    .accessibilityLabel("Role")
            } header: {
                Text("Role")
            } footer: {
                Text("What it owns, how it works, what it is responsible for. Written to it as “You are…”")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section {
                TextField("Workspace folder", text: $folder, prompt: Text("Optional, e.g. ~/Sites/myrepo"))
                    .autocorrectionDisabled()
            }
            Section("Model") {
                Picker("Provider", selection: $provider) {
                    Text("Default (\(llm.model.str.isEmpty ? "the default model" : llm.model.str))").tag("")
                    ForEach(presets, id: \.key) { Text($0.label).tag($0.key) }
                }
                HStack(spacing: 6) {
                    TextField("Model", text: $model, prompt: Text(provider.isEmpty ? "uses the default model" : "model"))
                        .autocorrectionDisabled()
                        .disabled(provider.isEmpty)
                    if !provider.isEmpty, !suggestions.isEmpty {
                        Menu {
                            ForEach(suggestions, id: \.self) { option in Button(option) { model = option } }
                        } label: {
                            Image(systemName: "chevron.up.chevron.down")
                        }
                        .menuStyle(.borderlessButton)
                        .fixedSize()
                        .accessibilityLabel("Choose a model")
                    }
                }
                if let hint = modelHint {
                    Text(hint.text).font(.caption).foregroundStyle(hint.warn ? MC.warn : .secondary)
                }
            }
            Section {
                DisclosureGroup("Limits and permissions") {
                    TextField("Tool rounds", value: $rounds, format: .number)
                    TextField("Total tool calls", value: $calls, format: .number)
                    TextField("Allowed tools", text: $tools, prompt: Text("All tools (or comma-separated names)")).autocorrectionDisabled()
                    TextField("Allowed skills", text: $skills, prompt: Text("All skills (or comma-separated slugs)")).autocorrectionDisabled()
                    Text("Blank uses all available capabilities. Enter “none” to disable. Skill contracts can narrow tool access further.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Section {
                Toggle("Responds to your iMessages", isOn: $respondsToTexts)
                    .disabled(respondsLocked)
                    .help(respondsLocked ? "This profile is the iMessage responder. Turn this on for another profile to move it." : "Route texts from your phone to this profile")
            }
        }
        .formStyle(.grouped)
    }

    private var swatchRow: some View {
        HStack(spacing: 6) {
            ForEach(Self.swatches, id: \.0) { hex, word in
                let selected = colour.lowercased() == hex
                Button { colour = hex } label: {
                    Circle()
                        .fill(color(hex: hex))
                        .frame(width: 18, height: 18)
                        .overlay {
                            if selected { Image(systemName: "checkmark").font(.system(size: 9, weight: .bold)).foregroundStyle(.white) }
                        }
                }
                .buttonStyle(.plain)
                .help(word)
                .accessibilityLabel(word)
                .accessibilityAddTraits(selected ? .isSelected : [])
            }
        }
    }

    private var footer: some View {
        VStack(spacing: 8) {
            if loaded, let problem {
                Text(problem).font(.callout).foregroundStyle(MC.danger).frame(maxWidth: .infinity, alignment: .leading).fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 8) {
                Button("Remove profile", role: .destructive) { confirmingRemove = true }.disabled(!loaded)
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Save") { Task { await save() } }
                    .keyboardShortcut(.return, modifiers: .command)
                    .buttonStyle(.borderedProminent)
                    .disabled(!loaded || saving)
            }
        }
        .padding(14)
    }

    private var presets: [(key: String, label: String, keyEnv: String)] {
        llm.presets.dict.map { (key: $0.key, label: $0.value.label.str, keyEnv: $0.value.key_env.str) }.sorted { $0.label < $1.label }
    }

    /// A provider whose key is missing would fail on this profile's first turn.
    private var modelHint: (text: String, warn: Bool)? {
        guard !provider.isEmpty, let preset = presets.first(where: { $0.key == provider }) else { return nil }
        let keyReady = llm.key_env.str == preset.keyEnv && llm.key_set.flag
        if !preset.keyEnv.isEmpty, !keyReady {
            return ("\(preset.label) needs \(preset.keyEnv) in Settings > Model before this profile can use it.", true)
        }
        return ("This profile thinks with this model; the others are unaffected.", false)
    }

    private func load() async {
        guard let api = store.api else { problem = "Not connected to the runtime yet."; return }
        do {
            async let rows = api.get("/api/agents-full")
            async let texts = api.get("/api/settings")
            async let full = api.get("/api/settings/full")
            let (agents, imessage, settings) = try await (rows, texts, full)
            guard let row = agents.agents.list.first(where: { $0.id == agentID }) else { problem = "This profile no longer exists."; return }
            name = row.name.str
            prompt = row.prompt.str
            colour = row.color.str
            folder = row.cwd.str == "~" ? "" : row.cwd.str
            original = row.llm.dict
            provider = row.llm.provider.str
            model = row.llm.model.str
            rounds = row.max_tool_rounds.optNum.map { Int($0) } ?? 40
            calls = row.max_tool_calls.optNum.map { Int($0) } ?? 160
            tools = allowlistText(row.tools)
            skills = allowlistText(row.skills)
            respondsLocked = imessage.imessage_default_agent.str == agentID
            respondsToTexts = respondsLocked
            llm = settings.llm
            loaded = true
        } catch {
            problem = error.localizedDescription
        }
    }

    private func readSuggestions() async {
        suggestions = []
        if provider.isEmpty { model = ""; return }
        guard let api = store.api else { return }
        guard let reply = try? await api.get("/api/settings/llm/models?provider=\(API.escape(provider))&base_url="), !Task.isCancelled else { return }
        suggestions = reply.models.list.map(\.str).filter { !$0.isEmpty }
        // A provider that advertises its models can fill the box in.
        if model.isEmpty, let first = suggestions.first { model = first }
    }

    private func allowlistText(_ value: JSON) -> String {
        if value.isNull { return "" }
        return value.list.isEmpty ? "none" : value.list.map(\.str).joined(separator: ", ")
    }

    private func allowlist(_ text: String) -> Any {
        let raw = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if raw.isEmpty { return NSNull() }
        if raw.lowercased() == "none" { return [String]() }
        return raw.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }

    private func save() async {
        guard let api = store.api else { problem = "Not connected to the runtime yet."; return }
        let cleanName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanPrompt = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanModel = model.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanName.isEmpty, !cleanPrompt.isEmpty else { problem = "Name and role description are both required."; return }
        guard provider.isEmpty || !cleanModel.isEmpty else { problem = "Pick a model for that provider, or go back to the default."; return }
        guard (1...200).contains(rounds) else { problem = "Tool rounds must be from 1 to 200."; return }
        guard (1...800).contains(calls) else { problem = "Total tool calls must be from 1 to 800."; return }
        saving = true; defer { saving = false }
        problem = nil
        // Settings this form has no field for stay with their provider.
        var override: [String: Any] = [:]
        if !provider.isEmpty {
            if original["provider"]?.str == provider { override = original.mapValues(\.raw) }
            override["provider"] = provider
            override["model"] = cleanModel
        }
        let cleanFolder = folder.trimmingCharacters(in: .whitespacesAndNewlines)
        let body: [String: Any] = [
            "name": cleanName, "prompt": cleanPrompt, "color": colour, "cwd": cleanFolder.isEmpty ? "~" : cleanFolder, "llm": override,
            "tools": allowlist(tools), "skills": allowlist(skills), "max_tool_rounds": rounds, "max_tool_calls": calls,
        ]
        do {
            _ = try await api.put("/api/agents/\(API.escape(agentID))", body)
            if respondsToTexts, !respondsLocked { _ = try await api.post("/api/settings", ["imessage_default_agent": agentID]) }
            await store.refreshState()
            dismiss()
        } catch {
            problem = error.localizedDescription
        }
    }

    private func remove() async {
        guard let api = store.api else { problem = "Not connected to the runtime yet."; return }
        do {
            _ = try await api.delete("/api/agents/\(API.escape(agentID))")
            await store.refreshState()
            if store.route == .chat("dm:\(agentID)") {
                let latest = store.threadRows.max { $0.last.ts.num < $1.last.ts.num }
                store.go(latest.map { .chat($0.id) } ?? .home)
            }
            dismiss()
            onRemoved()
        } catch {
            problem = error.localizedDescription
        }
    }
}
