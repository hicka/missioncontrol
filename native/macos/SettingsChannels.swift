import SwiftUI

private struct ChannelMeta {
    let id: String, label: String, hint: String
    let fields: [(key: String, label: String)]
    let secret: String, senders: String

    static let all: [ChannelMeta] = [
        ChannelMeta(id: "imessage", label: "iMessage",
                    hint: "Needs a BlueBubbles server on a Mac. Its password is the secret below.",
                    fields: [("server_url", "BlueBubbles server")],
                    secret: "BlueBubbles password", senders: "Your phone or email as iMessage sees it"),
        ChannelMeta(id: "telegram", label: "Telegram",
                    hint: "Create a bot with @BotFather and paste its token. Works everywhere.",
                    fields: [], secret: "Bot token", senders: "Your numeric id or @username"),
        ChannelMeta(id: "whatsapp", label: "WhatsApp",
                    hint: "Meta Business Cloud API. Needs a stable public webhook URL.",
                    fields: [("phone_number_id", "Phone number id"), ("verify_token", "Verify token")],
                    secret: "Access token", senders: "Phone numbers, digits only"),
        ChannelMeta(id: "email", label: "Email",
                    hint: "A dedicated address the agent owns. Use an app password; unseen mail from allowed senders routes to the agent, replies go back on the same thread.",
                    fields: [("address", "Email address"), ("imap_host", "IMAP host"), ("smtp_host", "SMTP host")],
                    secret: "App password", senders: "Email addresses allowed to write in"),
    ]
}

@MainActor
struct SettingsChannelsPane: View {
    @Environment(Store.self) private var store
    @State private var full = SettingsCache.last(SettingsCache.full)
    @State private var problem: String?

    var body: some View {
        Form {
            Section {
                SettingsProblemRow(problem: problem)
            } header: {
                SettingsHint("Reach the agent from your phone. It answers on the channel you last spoke on.")
            }
            ForEach(ChannelMeta.all, id: \.id) { meta in
                ChannelSection(meta: meta, settings: full.channels.settings[meta.id], status: full.channels.status[meta.id], ready: !full.isNull)
            }
            if let address = full.public_url.optStr, !address.isEmpty {
                Section { SettingsHint("Public webhook base: \(address)").textSelection(.enabled) }
            }
            PhonesSection(devices: full.devices.list)
        }
        .formStyle(.grouped)
        .settingsLoad { await load() }
    }

    private func load() async {
        do {
            full = try await SettingsCache.read(store.settingsAPI(), SettingsCache.full)
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }
}

private struct ChannelForm: Equatable {
    var enabled = false, agent = "", senders = "", secret = ""
    var fields: [String: String] = [:]

    init(_ settings: JSON, meta: ChannelMeta) {
        enabled = settings.enabled.flag
        agent = settings.default_agent.str
        senders = settings.allowed_senders.list.map(\.str).joined(separator: ", ")
        for field in meta.fields { fields[field.key] = settings[field.key].str }
    }
}

@MainActor
private struct ChannelSection: View {
    let meta: ChannelMeta
    let settings: JSON
    let status: JSON
    let ready: Bool
    @Environment(Store.self) private var store
    @State private var form: SettingsDraft<ChannelForm>
    /// What the last save reported, which is newer than the reply the pane holds.
    @State private var liveStatus: JSON?
    @State private var open: Bool?
    @State private var outcome = SettingsStatus.none

    init(meta: ChannelMeta, settings: JSON, status: JSON, ready: Bool) {
        self.meta = meta
        self.settings = settings
        self.status = status
        self.ready = ready
        _form = State(initialValue: SettingsDraft(ChannelForm(settings, meta: meta), ready: ready))
    }

    private var current: JSON { liveStatus ?? status }
    private var isOpen: Bool { open ?? form.value.enabled }

    private var pill: (status: String, label: String) {
        if current.connected.flag { return ("connected", "connected") }
        guard form.saved.enabled else { return ("off", "off") }
        return ("waiting", clip(current.detail.optStr ?? "not connected", 44))
    }

    var body: some View {
        Section {
            HStack(spacing: 8) {
                Button { open = !isOpen } label: {
                    HStack(spacing: 8) {
                        Image(systemName: isOpen ? "chevron.down" : "chevron.right")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                            .frame(width: 12)
                        Text(meta.label).font(.headline)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(meta.label), \(isOpen ? "hide" : "show") details")
                Pill(status: pill.status, label: pill.label).help(current.detail.str)
                Spacer()
                Toggle("Enable \(meta.label)", isOn: enabled)
                    .labelsHidden()
                    .toggleStyle(.switch)
                    .disabled(!ready)
            }
            .onChange(of: settings) { _, next in form.receive(ChannelForm(next, meta: meta)) }
            .onChange(of: status) { liveStatus = nil }
            if isOpen { details }
        }
    }

    private var enabled: Binding<Bool> {
        Binding(get: { form.value.enabled }, set: { on in
            form.value.enabled = on
            if on { open = true }
        })
    }

    @ViewBuilder private var details: some View {
        SettingsHint(meta.hint)
        ForEach(meta.fields, id: \.key) { field in
            TextField(field.label, text: Binding(get: { form.value.fields[field.key] ?? "" }, set: { form.value.fields[field.key] = $0 }))
                .autocorrectionDisabled()
        }
        SecureField(meta.secret, text: $form.value.secret, prompt: Text(current.secret_set.flag ? "saved" : "paste"))
        TextField("Allowed senders", text: $form.value.senders, prompt: Text(meta.senders))
            .autocorrectionDisabled()
        SettingsAgentPicker(title: "Answered by", selection: $form.value.agent)
        SettingsActions(status: outcome) {
            AsyncButton("Save") { await save() }.buttonStyle(.borderedProminent).disabled(!ready)
        }
    }

    private func save() async {
        outcome = .busy("Saving…")
        var body: [String: Any] = ["enabled": form.value.enabled, "default_agent": store.settingsAgent(form.value.agent),
                                   "allowed_senders": settingsList(form.value.senders)]
        for field in meta.fields { body[field.key] = (form.value.fields[field.key] ?? "").settingsTrimmed }
        let secret = form.value.secret.settingsTrimmed
        // An empty field means keep the saved secret; it is only sent when replaced.
        if !secret.isEmpty { body["secret"] = secret }
        do {
            let reply = try await store.settingsAPI().put("/api/settings/channels/\(meta.id)", body)
            let next = reply.status[meta.id]
            liveStatus = next
            var saved = form.value
            saved.secret = ""
            form.reset(to: saved)
            if next.connected.flag { outcome = .good("Saved, connected") }
            else if saved.enabled { outcome = .bad("Saved. \(next.detail.optStr ?? "Not connected yet")") }
            else { outcome = .good("Saved") }
            Task { await store.refreshState() }
        } catch {
            outcome = .bad("Error: \(error.localizedDescription)")
        }
    }
}

@MainActor
private struct PhonesSection: View {
    let devices: [JSON]
    @Environment(Store.self) private var store
    @State private var status = SettingsStatus.none

    var body: some View {
        Section {
            LabeledContent("Phones", value: devices.isEmpty ? "No phone registered yet." : devices.map(\.name.str).joined(separator: ", "))
            SettingsActions(status: status) {
                AsyncButton("Send test push") { await test() }
            }
        } footer: {
            SettingsHint("The Mission Control app on a phone registers itself when it connects. When you are away from every app window, the agent's reply buzzes the phone directly; no account, no cloud in between.")
        }
    }

    private func test() async {
        status = .busy("Sending…")
        do {
            _ = try await store.settingsAPI().post("/api/devices/test")
            status = .good("Sent. Check the phone.")
        } catch {
            status = .bad("Failed: \(error.localizedDescription)")
        }
    }
}
