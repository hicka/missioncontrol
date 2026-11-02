import AppKit
import SwiftUI

/// The owner's view into the same persistent browser the agent's tools use. Each section
/// reads only the part it draws, so the poll below redraws only what actually moved.
@Observable @MainActor
private final class BrowserModel {
    var data: JSON = .null
    var busy = false
    var feedback = SettingsStatus.none
    @ObservationIgnored private var fetching = false
    @ObservationIgnored private var readFailed = false

    var loaded: Bool { !data.isNull }
    var ready: Bool { data.enabled.flag && data.status.str == "running" }

    func refresh(_ store: Store) async {
        guard !fetching, !busy, let api = store.api else { return }
        fetching = true
        defer { fetching = false }
        do {
            let next = try await api.get("/api/runtime/browser")
            if next != data { data = next }
            if readFailed { readFailed = false; feedback = .none }
        } catch {
            if !Task.isCancelled { readFailed = true; feedback = .bad(error.localizedDescription) }
        }
    }

    /// One action at a time, as the web does, with its progress and outcome in words.
    @discardableResult
    func act(_ store: Store, _ path: String, method: String = "POST", body: [String: Any]? = nil, working: String, success: String) async -> Bool {
        guard !busy else { return false }
        busy = true
        feedback = .busy(working)
        var ok = false
        do {
            let api = try store.settingsAPI()
            let target = "/api/runtime/\(path)"
            switch method {
            // Opening the browser can take the server up to 40 seconds.
            case "POST": _ = try await api.settingsLong("POST", target, body ?? [:], timeout: 60)
            case "PUT": _ = try await api.put(target, body ?? [:])
            default: _ = try await api.delete(target)
            }
            ok = true
            feedback = .good(success)
        } catch {
            feedback = .bad(error.localizedDescription)
        }
        busy = false
        await refresh(store)
        return ok
    }
}

@MainActor
struct SettingsBrowserPane: View {
    @Environment(Store.self) private var store
    @State private var model = BrowserModel()

    var body: some View {
        Form {
            SignInRequestsSection(model: model)
            AccessSection(model: model)
            SavedLoginsSection(model: model)
            OpenBrowserSection(model: model)
            LiveTabsSection(model: model)
            Section {
                SettingsHint("Browser actions appear in the agent's chat and on Home. Passwords and 2FA responses go directly to protected login fields. You handle passkeys, CAPTCHAs, password changes and payment fields in the browser.")
            }
        }
        .formStyle(.grouped)
        .settingsLoad(on: \.browser) { await model.refresh(store) }
        .task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                if !Task.isCancelled && NSApp.isActive { await model.refresh(store) }
            }
        }
    }
}

// MARK: Sign-in prompts

@MainActor
private struct SignInRequestsSection: View {
    let model: BrowserModel
    var body: some View {
        ForEach(model.data.requests.list) { request in
            SignInRequest(request: request, model: model)
        }
    }
}

/// A website waiting on the owner. What is typed here goes straight to the login field the
/// agent is holding open, so it is kept in this view only and cleared once it has gone.
@MainActor
private struct SignInRequest: View {
    let request: JSON
    let model: BrowserModel
    @Environment(Store.self) private var store
    @State private var account = ""
    @State private var username = ""
    @State private var password = ""
    @State private var value = ""
    @State private var remember = true
    @State private var feedback = SettingsStatus.none
    @State private var sending = false

    private var kind: String { request.kind.str }
    private var accounts: [JSON] { request.accounts.list }
    private var valueLabel: String { kind == "code" ? "Verification code" : "Private login response" }

    var body: some View {
        Section {
            HStack(spacing: 6) {
                Text("\(store.name(request.agent.str)) is waiting").font(.callout)
                Expiry(deadline: request.expires.num)
            }
            Text(request.message.str).fixedSize(horizontal: false, vertical: true)
            if let delivery { SettingsHint(delivery) }
            fields
            SettingsActions(status: feedback) {
                Button("Cancel request") { Task { await answer(["cancel": true]) } }.disabled(sending)
                Button(kind == "manual" ? "Continue" : "Use securely and continue") { Task { await submit() } }
                    .buttonStyle(.borderedProminent)
                    .disabled(sending || (usesValue && value.isEmpty))
            }
        } header: {
            Text(request.origin.str)
        }
    }

    private var usesValue: Bool { kind != "login" && kind != "manual" }

    @ViewBuilder private var fields: some View {
        if kind == "login" {
            if !accounts.isEmpty {
                Picker("Saved account", selection: $account) {
                    Text("Enter another login").tag("")
                    ForEach(accounts) { Text($0.label.str).tag($0.id) }
                }
            }
            if account.isEmpty {
                TextField("Username or email", text: $username).autocorrectionDisabled()
                SecureField("Password", text: $password)
                Toggle("Save login in the OS vault for this website", isOn: $remember).toggleStyle(.switch)
            }
        } else if kind == "manual" {
            SettingsHint("Complete the step in the managed browser, then choose Continue.")
        } else if kind == "code" {
            SecureField(valueLabel, text: $value).textContentType(.oneTimeCode)
        } else {
            SecureField(valueLabel, text: $value)
        }
    }

    private var delivery: String? {
        let channel = request.delivery.str
        guard !channel.isEmpty, channel != "app" else { return nil }
        let state: String
        switch request.delivery_status.str {
        case "sent": state = "nudge sent"
        case "failed": state = "nudge failed; enter the code here"
        default: state = "sending nudge / awaiting delivery"
        }
        return "\(channel): \(state). You can reply with the code in your private chat."
    }

    private func submit() async {
        var values: [String: Any] = ["confirmed": true, "remember": false]
        if kind == "login" {
            values["username"] = String(username.prefix(320))
            values["password"] = String(password.prefix(4096))
            values["remember"] = remember
            if !account.isEmpty { values["credential_id"] = account }
        } else if kind != "manual" {
            values["value"] = String(value.prefix(4096))
        }
        await answer(values)
    }

    private func answer(_ values: [String: Any]) async {
        sending = true
        defer { sending = false }
        feedback = .busy("Sending directly to the waiting browser…")
        do {
            _ = try await store.settingsAPI().post("/api/runtime/browser/requests/\(API.escape(request.id))", values)
            username = ""
            password = ""
            value = ""
            feedback = .good("Response received. The agent is continuing.")
            model.feedback = values["cancel"] == nil ? .good("Response received. The agent is continuing.") : .good("Sign-in request cancelled.")
            await model.refresh(store)
        } catch {
            feedback = .bad(error.localizedDescription)
        }
    }
}

/// Redraws itself each second without touching the fields above it.
private struct Expiry: View {
    let deadline: Double
    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            let seconds = max(0, Int((deadline - context.date.timeIntervalSince1970).rounded(.up)))
            Text("expires in \(seconds / 60):\(String(format: "%02d", seconds % 60))")
                .font(.callout)
                .monospacedDigit()
                .foregroundStyle(.secondary)
        }
    }
}

// MARK: Access

@MainActor
private struct AccessSection: View {
    let model: BrowserModel
    @Environment(Store.self) private var store
    // A switch moves at once and settles on the server's answer when it arrives.
    @State private var enabled: Bool?
    @State private var visible: Bool?
    @State private var channel: String?

    private var state: String {
        let data = model.data
        if !model.loaded { return "Checking…" }
        if !data.enabled.flag { return "Off" }
        if !model.ready { return "Start the agent engine in Runtime Manager" }
        return data.running.flag ? "Browser connected" : "Ready · opens on demand"
    }

    var body: some View {
        Section {
            SettingsSwitch(title: "Website access",
                           note: "\(state). Turning this off closes the managed browser and stops browser tool access.",
                           isOn: Binding(get: { enabled ?? model.data.enabled.flag }, set: { setEnabled($0) }))
                .disabled(model.busy || !model.loaded)
            SettingsSwitch(title: "Show browser windows",
                           note: "Changing visibility closes open tabs. Saved sign-ins remain in this workspace.",
                           isOn: Binding(get: { visible ?? !model.data.headless.flag }, set: { setVisible($0) }))
                .disabled(model.busy || !model.loaded)
            Picker(selection: Binding(get: { channel ?? (model.data.auth_channel.optStr ?? "auto") }, set: { setChannel($0) })) {
                Text("Connected messaging channel").tag("auto")
                Text("In this app only").tag("app")
                Text("iMessage").tag("imessage")
                Text("Telegram").tag("telegram")
                Text("WhatsApp").tag("whatsapp")
            } label: {
                Text("2FA delivery")
                Text("We nudge your configured private chat when a code is needed. Reply with the code to continue. Replies bypass agent chat and memory; your messaging app manages its own history.")
            }
            .disabled(model.busy || !model.loaded)
            SettingsStatusLine(status: model.feedback)
        } header: {
            SettingsHint("The agent can browse, search and work in websites you assign. Open its browser and sign in once; every task shares those sessions. Use chat to give it the website and task.")
        }
    }

    private func setEnabled(_ on: Bool) {
        enabled = on
        Task {
            await model.act(store, "services/browser/\(on ? "start" : "stop")",
                            working: on ? "Enabling website access…" : "Closing the browser…",
                            success: on ? "Website access enabled." : "Website access disabled. Saved sign-ins are preserved.")
            enabled = nil
        }
    }

    private func setVisible(_ on: Bool) {
        visible = on
        Task {
            await model.act(store, "browser/settings", method: "PUT", body: ["headless": !on],
                            working: "Updating browser visibility…", success: "Visibility updated. Reopen the browser when ready.")
            visible = nil
        }
    }

    private func setChannel(_ next: String) {
        channel = next
        Task {
            await model.act(store, "browser/settings", method: "PUT", body: ["auth_channel": next],
                            working: "Updating 2FA delivery…", success: "2FA delivery updated.")
            channel = nil
        }
    }
}

// MARK: Saved logins

@MainActor
private struct SavedLoginsSection: View {
    let model: BrowserModel
    @Environment(Store.self) private var store
    @State private var editingID = ""
    @State private var address = ""
    @State private var label = ""
    @State private var username = ""
    @State private var password = ""
    @FocusState private var usernameFocused: Bool

    private var vault: JSON { model.data.vault }
    private var vaultStatus: String {
        if !model.loaded { return "Checking secure storage…" }
        return vault.available.flag ? vault.name.str : (vault.error.optStr ?? "Secure storage unavailable")
    }
    private var complete: Bool {
        !address.settingsTrimmed.isEmpty && !username.isEmpty && !password.isEmpty
    }

    var body: some View {
        Section {
            LabeledContent("Secure storage", value: vaultStatus)
            let rows = model.data.credentials.list
            if model.loaded && rows.isEmpty {
                SettingsHint("No saved logins yet. Add a website account below, or save it when the agent asks you to sign in.")
            }
            ForEach(rows) { entry in
                HStack {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(entry.label.str)
                        Text(entry.origin.str).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                    }
                    Spacer()
                    Button("Update login") { beginUpdate(entry) }
                    SettingsConfirmButton(title: "Forget login", question: "Forget this login?",
                                          detail: "\(entry.label.str) on \(entry.origin.str). Existing website sessions remain signed in.",
                                          confirm: "Forget login") {
                        await model.act(store, "browser/credentials/\(API.escape(entry.id))", method: "DELETE",
                                        working: "Removing saved login…", success: "Saved login removed. Existing website sessions remain signed in.")
                    }
                }
            }
            TextField("Website address", text: $address, prompt: Text("https://accounts.example.com")).autocorrectionDisabled()
            TextField("Account label", text: $label, prompt: Text("Optional"))
            TextField("Username or email", text: $username).autocorrectionDisabled().focused($usernameFocused)
            SecureField("Password", text: $password)
            HStack {
                Spacer()
                if !editingID.isEmpty { Button("Cancel update") { clear() } }
                AsyncButton("Save login securely") { await save() }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.busy || !vault.available.flag || !complete)
            }
        } header: {
            Text("Saved logins")
        } footer: {
            SettingsHint("Saved in your operating system's credential vault for this exact HTTPS address. The agent uses the login without receiving its password. 2FA codes are used once and never saved in the vault.")
        }
    }

    /// The vault never gives a password back, so updating means entering it again.
    private func beginUpdate(_ entry: JSON) {
        editingID = entry.id
        address = entry.origin.str
        label = entry.label.str
        username = ""
        password = ""
        usernameFocused = true
    }

    private func clear() {
        editingID = ""
        address = ""
        label = ""
        username = ""
        password = ""
    }

    private func save() async {
        let body: [String: Any] = ["url": address.settingsTrimmed, "label": String(label.settingsTrimmed.prefix(100)),
                                   "username": String(username.prefix(320)), "password": String(password.prefix(4096)),
                                   "credential_id": editingID.isEmpty ? NSNull() : editingID]
        let ok = await model.act(store, "browser/credentials", body: body,
                                 working: "Saving in the OS credential vault…", success: "Login saved securely for this website.")
        if ok { clear() }
    }
}

// MARK: Opening it, and what is open

@MainActor
private struct OpenBrowserSection: View {
    let model: BrowserModel
    @Environment(Store.self) private var store
    @State private var address = ""

    var body: some View {
        Section {
            LabeledContent("Website") {
                HStack(spacing: 8) {
                    TextField("Website URL", text: $address, prompt: Text("Website URL (optional)"))
                        .labelsHidden()
                        .multilineTextAlignment(.trailing)
                        .textFieldStyle(.plain)
                        .autocorrectionDisabled()
                        .onSubmit { open() }
                    Button("Open browser / sign in") { open() }
                        .buttonStyle(.borderedProminent)
                        .disabled(model.busy || !model.ready || model.data.headless.flag)
                }
            }
        } footer: {
            SettingsHint("This browser has its own profile. Sign in here for sites that need an account; your personal browser's logins stay separate.")
        }
    }

    private func open() {
        guard !model.busy, model.ready, !model.data.headless.flag else { return }
        Task {
            await model.act(store, "browser/open", body: ["url": address.settingsTrimmed], working: "Opening your browser…",
                            success: "Browser opened. Sign in there, then give the agent your task in chat.")
        }
    }
}

@MainActor
private struct LiveTabsSection: View {
    let model: BrowserModel
    @Environment(Store.self) private var store

    var body: some View {
        Section {
            let tabs = model.data.sessions.list
            if model.loaded && tabs.isEmpty {
                SettingsHint("No open tabs. Open the browser to sign in, or give the agent a website task.")
            }
            ForEach(tabs) { tab in
                HStack {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(tab.title.str).lineLimit(1)
                        Text("\(owner(of: tab)) · \(tab.url.str)").font(.caption).foregroundStyle(.secondary).lineLimit(1).textSelection(.enabled)
                    }
                    Spacer()
                    Button("Show tab") { act(on: tab, "focus", "Tab brought to front.") }
                        .disabled(model.busy || model.data.headless.flag)
                    Button("Close tab") { act(on: tab, "close", "Tab closed.") }
                        .disabled(model.busy)
                }
            }
            LabeledContent("Agent access") {
                Text(model.data.agents.list.map { "\($0.name.str): \($0.allowed.flag ? "browser allowed" : "browser restricted")" }.joined(separator: " · "))
                    .foregroundStyle(.secondary)
            }
        } header: {
            HStack {
                Text("Live browser tabs")
                Spacer()
                AsyncButton("Refresh") { await model.refresh(store) }
            }
        }
    }

    private func owner(of tab: JSON) -> String {
        let agent = tab.agent.str
        if agent == "owner" { return "Your login tab" }
        return model.data.agents.list.first { $0.id == agent }?.name.str ?? agent
    }

    private func act(on tab: JSON, _ action: String, _ success: String) {
        Task {
            await model.act(store, "browser/sessions/\(API.escape(tab.id))/\(action)", working: "Updating tab…", success: success)
        }
    }
}
