import SwiftUI

/// The first run. A new install has an agent but nothing for it to think with, so before
/// anything else the owner plugs in a model. A local model already running is one click.
@MainActor
struct WelcomeView: View {
    @Environment(Store.self) private var store
    let full: JSON
    let close: () -> Void

    private static let order = ["anthropic", "openai", "deepseek", "minimax", "ollama", "lmstudio", "custom-openai"]
    private static let labels = ["ollama": "Ollama", "lmstudio": "LM Studio", "custom-openai": "Other endpoint"]
    private static let keyPages = [
        "anthropic": ("console.anthropic.com", "https://console.anthropic.com/settings/keys"),
        "openai": ("platform.openai.com", "https://platform.openai.com/api-keys"),
        "deepseek": ("platform.deepseek.com", "https://platform.deepseek.com/api_keys"),
        "minimax": ("platform.minimax.io", "https://platform.minimax.io/user-center/basic-information/interface-key"),
    ]

    @State private var name = ""
    @State private var provider = ""
    @State private var key = ""
    @State private var model = ""
    @State private var baseURL = ""
    @State private var models: [String] = []
    @State private var local: (provider: String, kind: String, model: String)?
    @State private var status = SettingsStatus.none
    @State private var working = false

    private var presets: JSON { full.llm.presets }
    private var preset: JSON { presets[provider] }
    private var isLocal: Bool { ["ollama", "lmstudio"].contains(provider) }
    private var needsKey: Bool { !preset.key_env.str.isEmpty }
    private var choices: [String] { Self.order.filter { !presets[$0].isNull } }
    private var baseExample: String {
        let known = preset.base_url.str
        return known.isEmpty ? "https://host/v1" : known
    }

    var body: some View {
        VStack(spacing: 0) {
            WelcomeMark().padding(.bottom, 14)
            Text("Plug in a model").font(.title2.weight(.semibold))
            Text("Your agent is here. It only needs something to think with.")
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.top, 4)
            VStack(spacing: 10) {
                TextField("Your name", text: $name, prompt: Text("Your name"))
                    .textFieldStyle(.roundedBorder)
                divider("and a model")
                if let local { localOption(local); divider("or use an API key") }
                providers
                if needsKey {
                    SecureField("\(preset.label.str) API key", text: $key, prompt: Text("\(preset.label.str) API key"))
                        .textFieldStyle(.roundedBorder)
                }
                hint
                modelField
                if isLocal || preset.base_url.str.isEmpty {
                    TextField("Base URL", text: $baseURL, prompt: Text(baseExample))
                        .textFieldStyle(.roundedBorder)
                        .autocorrectionDisabled()
                }
                Button { Task { await connect() } } label: { Text("Connect").frame(maxWidth: .infinity) }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    .keyboardShortcut(.defaultAction)
                    .disabled(working)
                SettingsStatusLine(status: status).frame(minHeight: 18)
            }
            .padding(.top, 20)
            Button("Skip for now", action: close)
                .buttonStyle(.link)
                .foregroundStyle(.secondary)
                .keyboardShortcut(.cancelAction)
                .padding(.top, 2)
        }
        .padding(.horizontal, 32).padding(.vertical, 28)
        .frame(width: 460)
        .task { await begin() }
        .task(id: "\(provider)|\(baseURL)") { await loadModels() }
    }

    private func divider(_ text: String) -> some View {
        HStack(spacing: 10) {
            VStack { Divider() }
            Text(text).font(.caption).foregroundStyle(.secondary).fixedSize()
            VStack { Divider() }
        }
        .padding(.vertical, 2)
    }

    private func localOption(_ found: (provider: String, kind: String, model: String)) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 7) {
                Circle().fill(MC.good).frame(width: 7, height: 7)
                Text("Local model found").fontWeight(.semibold)
                Pill(status: "ok", label: found.kind)
                Spacer()
            }
            Text("\(found.model) is running on this Mac. Private, free, no key.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Button { Task { await useLocal(found) } } label: { Text("Use it").frame(maxWidth: .infinity) }
                .buttonStyle(.borderedProminent)
                .disabled(working)
        }
        .padding(12)
        .background(MC.good.opacity(0.08), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(MC.good.opacity(0.3)))
    }

    private var providers: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 112), spacing: 6)], spacing: 6) {
            ForEach(choices, id: \.self) { id in
                let on = id == provider
                Button { pick(id) } label: {
                    Text(Self.labels[id] ?? presets[id].label.str)
                        .font(.callout)
                        .lineLimit(1)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 5)
                        .background(on ? Color.accentColor.opacity(0.16) : MC.surface, in: Capsule())
                        .overlay(Capsule().strokeBorder(on ? Color.accentColor : MC.line))
                        .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(on ? .isSelected : [])
            }
        }
    }

    @ViewBuilder private var hint: some View {
        Group {
            if let page = Self.keyPages[provider], let url = URL(string: page.1) {
                (Text("Get a key at ") + Text(.init("[\(page.0)](\(url.absoluteString))")) + Text(". It stays in a local .env on this Mac."))
            } else if isLocal {
                Text("Runs on this Mac. Private, free, no key.")
            } else {
                Text("Any OpenAI-compatible endpoint.")
            }
        }
        .font(.caption)
        .foregroundStyle(.secondary)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var modelField: some View {
        HStack(spacing: 6) {
            TextField("Model", text: $model, prompt: Text("model"))
                .textFieldStyle(.roundedBorder)
                .autocorrectionDisabled()
            if !models.isEmpty {
                Menu {
                    ForEach(models, id: \.self) { option in Button(option) { model = option } }
                } label: {
                    Image(systemName: "chevron.up.chevron.down")
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .help("Choose a model")
                .accessibilityLabel("Choose a model")
            }
        }
    }

    // MARK: Doing

    private func begin() async {
        let current = store.user.name.str
        name = ["You", "Alex"].contains(current) ? "" : current
        let saved = full.llm.provider.str
        pick(!presets[saved].key_env.str.isEmpty ? saved : "anthropic")
        // A local model already running is the zero-step path.
        for (id, kind) in [("ollama", "Ollama"), ("lmstudio", "LM Studio")] {
            guard let api = store.api, let reply = try? await api.get("/api/settings/llm/models?provider=\(id)&base_url="),
                  let first = reply.models.list.first?.str else { continue }
            local = (id, kind, first)
            return
        }
    }

    /// A different provider starts from its own first model, and a key typed for the old
    /// one is not sent under the new one.
    private func pick(_ id: String) {
        provider = id
        key = ""
        baseURL = ""
        model = presets[id].models.list.first?.str ?? ""
        status = .none
    }

    private func loadModels() async {
        let chosen = provider, base = baseURL.settingsTrimmed
        guard !chosen.isEmpty, let api = store.api else { return }
        // This also runs for each letter of a base URL being typed.
        try? await Task.sleep(nanoseconds: 350_000_000)
        guard !Task.isCancelled else { return }
        let reply = try? await api.get("/api/settings/llm/models?provider=\(API.escape(chosen))&base_url=\(API.escape(base))")
        if !Task.isCancelled { models = reply?.models.list.map(\.str) ?? [] }
    }

    private func useLocal(_ found: (provider: String, kind: String, model: String)) async {
        await save(["provider": found.provider, "model": found.model, "base_url": "", "max_tokens": 3000], test: false)
    }

    private func connect() async {
        let chosen = model.settingsTrimmed
        guard !chosen.isEmpty else { status = .bad("Pick a model"); return }
        let typed = key.settingsTrimmed
        if needsKey && typed.isEmpty && !(full.llm.key_set.flag && full.llm.key_env.str == preset.key_env.str) {
            status = .bad("Paste your \(preset.label.str) API key"); return
        }
        await save(["provider": provider, "model": chosen, "base_url": baseURL.settingsTrimmed, "max_tokens": 3000,
                    "api_key": typed.isEmpty ? NSNull() : typed], test: true)
    }

    private func save(_ body: [String: Any], test: Bool) async {
        working = true
        defer { working = false }
        do {
            let api = try store.settingsAPI()
            if test {
                status = .busy("Testing connection…")
                _ = try await api.settingsLong("POST", "/api/settings/llm/test", body, timeout: 120)
            }
            status = .busy("Setting up…")
            _ = try await api.put("/api/settings/llm", body)
            await saveName(api)
            _ = try? await SettingsCache.read(api, SettingsCache.full)
            status = .good("Connected. Your agent is saying hello.")
            try? await Task.sleep(nanoseconds: 900_000_000)
            close()
            if let lead = store.leadThread { store.go(.chat(lead)) }
        } catch {
            status = .bad(test ? "Failed: \(error.localizedDescription)" : "Could not save: \(error.localizedDescription)")
        }
    }

    private func saveName(_ api: API) async {
        let clean = name.settingsTrimmed
        guard !clean.isEmpty, let reply = try? await api.put("/api/user", ["name": String(clean.prefix(40))]) else { return }
        store.user = reply.user
    }
}

/// Three dots, the mark the web app uses, in the bar's colours.
private struct WelcomeMark: View {
    var body: some View {
        ZStack(alignment: .topLeading) {
            dot(.init(red: 0.42, green: 0.5, blue: 1), opacity: 1).offset(x: 0, y: 0)
            dot(.init(red: 0.62, green: 0.42, blue: 0.95), opacity: 0.6).offset(x: 24, y: 10)
            dot(.init(red: 0.3, green: 0.82, blue: 0.86), opacity: 0.85).offset(x: 10, y: 24)
        }
        .frame(width: 44, height: 44, alignment: .topLeading)
        .accessibilityHidden(true)
    }

    private func dot(_ color: Color, opacity: Double) -> some View {
        Circle().fill(color.opacity(opacity)).frame(width: 20, height: 20).shadow(color: color.opacity(0.5), radius: 6)
    }
}

/// Shown once per launch while no model is ready. Skipping keeps it away until the next launch.
@MainActor
enum WelcomeGate {
    static var skipped = false
}
