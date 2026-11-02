import AVFoundation
import SwiftUI

// MARK: Name and model

private struct ModelForm: Equatable {
    var provider = "", model = "", effort = "low", baseURL = "", key = "", fallbacks = ""

    init(_ full: JSON) {
        let llm = full.llm
        provider = llm.provider.str
        model = llm.model.str
        effort = llm.effort.optStr ?? "low"
        baseURL = llm.base_url.str
        // The base URL is the third word when a fallback has one, so saving keeps it.
        fallbacks = llm.fallbacks.list.map { row in
            [row.provider.str, row.model.str, row.base_url.str].filter { !$0.isEmpty }.joined(separator: " ")
        }.joined(separator: "\n")
    }
}

@MainActor
struct SettingsGeneralPane: View {
    @Environment(Store.self) private var store
    @State private var full = SettingsCache.last(SettingsCache.full)
    @State private var form = SettingsDraft(reply: SettingsCache.last(SettingsCache.full), ModelForm.init)
    @State private var models: [String] = []
    @State private var status = SettingsStatus.none
    @State private var problem: String?

    private var preset: JSON { full.llm.presets[form.value.provider] }
    private var isLocal: Bool { ["ollama", "lmstudio"].contains(form.value.provider) }
    private var needsKey: Bool { !preset.key_env.str.isEmpty }
    private var keySaved: Bool { full.llm.key_set.flag && form.value.provider == full.llm.provider.str }

    private var presetRows: [(id: String, label: String)] {
        full.llm.presets.dict
            .map { (id: $0.key, label: $0.value.label.str) }
            .sorted { $0.label.localizedCaseInsensitiveCompare($1.label) == .orderedAscending }
    }

    private var hint: String {
        if isLocal { return "Runs on your machine. Private, free, no key needed." }
        return needsKey ? "The key is stored in your local .env, never synced anywhere." : "Point this at any compatible endpoint."
    }

    var body: some View {
        Form {
            OwnerNameSection()
            Section {
                SettingsProblemRow(problem: problem)
                Picker("Provider", selection: Binding(get: { form.value.provider }, set: { changeProvider($0) })) {
                    ForEach(presetRows, id: \.id) { Text($0.label).tag($0.id) }
                    if preset.isNull { Text(form.value.provider.isEmpty ? "Loading…" : form.value.provider).tag(form.value.provider) }
                }
                SettingsModelField(title: "Model", text: $form.value.model, models: models)
                Picker("Effort", selection: $form.value.effort) {
                    Text("None, no thinking (fastest)").tag("none")
                    Text("Low (fast)").tag("low")
                    Text("Medium").tag("medium")
                    Text("High").tag("high")
                    Text("Max (slowest)").tag("max")
                }
                if form.ready && (isLocal || preset.base_url.str.isEmpty) {
                    TextField("Base URL", text: $form.value.baseURL, prompt: Text(preset.base_url.optStr ?? "https://host/v1"))
                        .autocorrectionDisabled()
                }
                if needsKey {
                    SecureField("API key", text: $form.value.key, prompt: Text(keySaved ? "saved" : "paste key"))
                }
            } header: {
                Text("Model")
            } footer: {
                SettingsHint(hint)
            }
            Section {
                SettingsTextBox(label: "Fallbacks", text: $form.value.fallbacks, prompt: "one per line: provider model\ndeepseek deepseek-v4-pro", height: 60)
            } header: {
                Text("Fallbacks")
            } footer: {
                SettingsHint("Tried in order while the main model is down.")
            }
            Section {
                SettingsActions(status: status) {
                    AsyncButton("Test") { await test() }.disabled(!form.ready)
                    AsyncButton("Save") { await save() }.buttonStyle(.borderedProminent).disabled(!form.ready)
                }
            }
        }
        .formStyle(.grouped)
        .settingsLoad { await load() }
        .task(id: "\(form.value.provider)|\(form.value.baseURL)") { await loadModels() }
    }

    private func load() async {
        do {
            apply(try await SettingsCache.read(store.settingsAPI(), SettingsCache.full))
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func apply(_ reply: JSON) {
        full = reply
        form.receive(ModelForm(reply))
    }

    /// A different provider starts from its own first model, and a key typed for the old
    /// one must not be saved under the new one.
    private func changeProvider(_ next: String) {
        form.value.provider = next
        form.value.baseURL = ""
        form.value.key = ""
        form.value.model = full.llm.presets[next].models.list.first?.str ?? ""
        status = .none
    }

    private func loadModels() async {
        let provider = form.value.provider, base = form.value.baseURL.settingsTrimmed
        guard !provider.isEmpty, let api = store.api else { return }
        // This also runs for each letter of a base URL being typed.
        try? await Task.sleep(nanoseconds: 350_000_000)
        guard !Task.isCancelled else { return }
        let reply = try? await api.get("/api/settings/llm/models?provider=\(API.escape(provider))&base_url=\(API.escape(base))")
        if !Task.isCancelled { models = reply?.models.list.map(\.str) ?? [] }
    }

    private var requestBody: [String: Any] {
        let fallbacks: [[String: Any]] = form.value.fallbacks.split(separator: "\n").compactMap { line in
            let words = line.split(whereSeparator: \.isWhitespace).map(String.init)
            guard words.count >= 2 else { return nil }
            return ["provider": words[0], "model": words[1], "base_url": words.count > 2 ? words[2] : ""]
        }
        let limit = full.llm.max_tokens.int
        let key = form.value.key.settingsTrimmed
        return ["provider": form.value.provider, "model": form.value.model.settingsTrimmed, "effort": form.value.effort,
                "base_url": form.value.baseURL.settingsTrimmed, "max_tokens": limit > 0 ? limit : 3000,
                "api_key": key.isEmpty ? NSNull() : key, "fallbacks": fallbacks]
    }

    private func test() async {
        status = .busy("Testing…")
        do {
            _ = try await store.settingsAPI().settingsLong("POST", "/api/settings/llm/test", requestBody, timeout: 120)
            status = .good("Connection OK")
        } catch {
            status = .bad("Failed: \(error.localizedDescription)")
        }
    }

    private func save() async {
        do {
            let api = try store.settingsAPI()
            _ = try await api.put("/api/settings/llm", requestBody)
            status = .good("Saved")
            let reply = try await SettingsCache.read(api, SettingsCache.full)
            full = reply
            form.reset(to: ModelForm(reply))
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
    }
}

@MainActor
private struct OwnerNameSection: View {
    @Environment(Store.self) private var store
    @State private var name = SettingsDraft("")
    @State private var status = SettingsStatus.none

    var body: some View {
        Section {
            LabeledContent("Your name") {
                HStack(spacing: 8) {
                    TextField("Your name", text: $name.value, prompt: Text("You"))
                        .labelsHidden()
                        .multilineTextAlignment(.trailing)
                        .textFieldStyle(.plain)
                    AsyncButton("Save") { await save() }
                }
            }
            .onChange(of: store.user.name.str, initial: true) { _, next in name.receive(next) }
            if !status.text.isEmpty { SettingsStatusLine(status: status) }
        }
    }

    private func save() async {
        let clean = name.value.settingsTrimmed
        guard !clean.isEmpty else { status = .bad("Enter a name"); return }
        do {
            let reply = try await store.settingsAPI().put("/api/user", ["name": String(clean.prefix(40))])
            store.user = reply.user
            name.reset(to: reply.user.name.str)
            status = .good("Saved")
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
    }
}

// MARK: Voice

private struct VoiceForm: Equatable {
    var voice = "", speed = 0.95
    init(_ reply: JSON) {
        voice = reply.voice.str
        speed = reply.speed.optNum ?? 0.95
    }
}

@MainActor
struct SettingsVoicePane: View {
    @Environment(Store.self) private var store
    private static let path = "/api/settings/voice", voicesPath = "/api/settings/voice/voices"
    private static let speeds: [(value: Double, label: String)] = [
        (0.85, "Unhurried, 0.85×"), (0.95, "Natural, 0.95×"), (1, "Normal, 1×"), (1.1, "Brisk, 1.1×"),
    ]

    @State private var form = SettingsDraft(reply: SettingsCache.last(SettingsVoicePane.path), VoiceForm.init)
    @State private var providers = SettingsCache.last(SettingsVoicePane.path).providers
    @State private var voices = SettingsCache.last(SettingsVoicePane.voicesPath).voices.list
    @State private var status = SettingsStatus.none
    @State private var problem: String?
    @State private var player: AVAudioPlayer?

    private var listening: String {
        if providers.isNull { return "Checking…" }
        if providers.whisper.flag { return "Whisper on this Mac, nothing leaves the machine" }
        return providers.openai.flag ? "OpenAI Whisper" : "Not available: install faster-whisper in the server's venv"
    }

    var body: some View {
        Form {
            Section {
                SettingsProblemRow(problem: problem)
                Picker("Voice", selection: $form.value.voice) {
                    ForEach(voices, id: \.self) { Text($0.name.str).tag($0["id"].str) }
                    if !voices.contains(where: { $0["id"].str == form.value.voice }) {
                        Text(form.value.voice.isEmpty ? "System voice" : form.value.voice).tag(form.value.voice)
                    }
                }
                Picker("Speed", selection: $form.value.speed) {
                    ForEach(Self.speeds, id: \.value) { Text($0.label).tag($0.value) }
                    if !Self.speeds.contains(where: { $0.value == form.value.speed }) {
                        Text("\(form.value.speed, specifier: "%.2f")×").tag(form.value.speed)
                    }
                }
                LabeledContent("Listening", value: listening)
            } footer: {
                SettingsHint("One voice everywhere: voice mode here, spoken replies on the phone, voice notes on iMessage and Telegram. Licensed studio voices through your model provider's key.")
            }
            Section {
                SettingsActions(status: status) {
                    AsyncButton("Preview") { await preview() }.disabled(!form.ready)
                    AsyncButton("Save") { _ = await save() }.buttonStyle(.borderedProminent).disabled(!form.ready)
                }
            }
        }
        .formStyle(.grouped)
        .settingsLoad {
            // The voice list can come from the provider and take seconds, so it does not
            // hold up the rest of the form.
            async let settings: Void = loadSettings()
            async let list: Void = loadVoices()
            _ = await (settings, list)
        }
    }

    private func loadSettings() async {
        do {
            let reply = try await SettingsCache.read(store.settingsAPI(), Self.path)
            form.receive(VoiceForm(reply))
            providers = reply.providers
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func loadVoices() async {
        guard let api = store.api, let reply = try? await SettingsCache.read(api, Self.voicesPath, long: true) else { return }
        voices = reply.voices.list
    }

    private func save() async -> Bool {
        do {
            let reply = try await store.settingsAPI().put(Self.path, ["voice": form.value.voice, "speed": form.value.speed])
            form.reset(to: VoiceForm(reply))
            status = .good("Saved")
            return true
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
            return false
        }
    }

    private func preview() async {
        status = .busy("Synthesising…")
        guard await save() else { return }
        status = .busy("Synthesising…")
        do {
            let api = try store.settingsAPI()
            let reply = try await api.settingsLong("POST", "/api/tts", ["text": "Mission Control online. Standing by."], timeout: 120)
            let audio = try await api.getRaw(reply.url.str)
            let next = try AVAudioPlayer(data: audio.data)
            player = next
            next.play()
            status = .none
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
    }
}

// MARK: Usage and budget

@MainActor
struct SettingsUsagePane: View {
    @Environment(Store.self) private var store
    private static let usagePath = "/api/usage", budgetPath = "/api/settings/budget"

    @State private var usage = SettingsCache.last(SettingsUsagePane.usagePath)
    @State private var budget = SettingsCache.last(SettingsUsagePane.budgetPath)
    @State private var limit = SettingsDraft("")
    @State private var problem: String?

    private var budgetText: String {
        let used = budget.used_today.int.formatted()
        if budget.isNull { return "Checking…" }
        guard budget.daily_tokens.int > 0 else { return "\(used) tokens used today, no limit set" }
        return "\(used) of \(budget.daily_tokens.int.formatted()) tokens used today (\(Int((budget.pct.num * 100).rounded()))%)"
    }

    var body: some View {
        Form {
            Section {
                SettingsProblemRow(problem: problem)
                usageRows
            } header: {
                HStack {
                    Text("Usage, last 7 days")
                    Spacer()
                    AsyncButton("Refresh") { await loadUsage() }
                }
            }
            Section {
                LabeledContent("Daily budget") {
                    HStack(spacing: 8) {
                        TextField("Daily budget", text: $limit.value, prompt: Text("0 = no limit"))
                            .labelsHidden()
                            .multilineTextAlignment(.trailing)
                            .textFieldStyle(.plain)
                        AsyncButton("Save") { await saveBudget() }.disabled(!limit.ready)
                    }
                }
            } footer: {
                SettingsHint(budgetText)
            }
        }
        .formStyle(.grouped)
        .settingsLoad {
            async let rows: Void = loadUsage()
            async let today: Void = loadBudget()
            _ = await (rows, today)
        }
    }

    @ViewBuilder private var usageRows: some View {
        let rows = usage.per_agent.list
        if usage.isNull {
            Text("Checking…").foregroundStyle(.secondary)
        } else if rows.isEmpty {
            Text("No model calls recorded yet.").foregroundStyle(.secondary)
        } else {
            Grid(alignment: .trailing, horizontalSpacing: 18, verticalSpacing: 6) {
                GridRow {
                    Text("Profile").gridColumnAlignment(.leading)
                    Text("In")
                    Text("Out")
                    Text("Calls")
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                ForEach(rows, id: \.self) { row in
                    GridRow {
                        Text(store.name(row.agent.str))
                        Text(shortCount(row.in_tokens.int))
                        Text(shortCount(row.out_tokens.int))
                        Text("\(row.calls.int)")
                    }
                    .monospacedDigit()
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func shortCount(_ count: Int) -> String {
        count > 9999 ? "\(Int((Double(count) / 1000).rounded()))k" : "\(count)"
    }

    private func loadUsage() async {
        do {
            usage = try await SettingsCache.read(store.settingsAPI(), Self.usagePath)
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func loadBudget() async {
        guard let api = store.api, let reply = try? await SettingsCache.read(api, Self.budgetPath) else { return }
        apply(reply)
    }

    private func apply(_ reply: JSON) {
        budget = reply
        let tokens = reply.daily_tokens.int
        limit.receive(tokens > 0 ? String(tokens) : "")
    }

    private func saveBudget() async {
        do {
            let reply = try await store.settingsAPI().put(Self.budgetPath, ["daily_tokens": Int(limit.value.settingsTrimmed) ?? 0])
            limit.reset(to: reply.daily_tokens.int > 0 ? String(reply.daily_tokens.int) : "")
            budget = reply
        } catch {
            store.notify(error.localizedDescription, error: true)
        }
    }
}
