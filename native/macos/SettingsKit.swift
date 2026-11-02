import AppKit
import SwiftUI

/// A form's fields, and what the server last said they were. A fresh reply replaces the
/// fields only while the owner has not changed them, so nothing typed is ever overwritten.
struct SettingsDraft<Value: Equatable> {
    var value: Value
    private(set) var saved: Value
    /// The server has answered. Until then the fields are blank, and saving them would
    /// wipe the real settings.
    private(set) var ready: Bool

    init(_ value: Value, ready: Bool = false) { self.value = value; saved = value; self.ready = ready }
    init(reply: JSON, _ make: (JSON) -> Value) { self.init(make(reply), ready: !reply.isNull) }

    mutating func receive(_ next: Value) {
        if value == saved { value = next }
        saved = next
        ready = true
    }

    mutating func reset(to next: Value) { value = next; saved = next; ready = true }
}

/// Each pane's last reply, in memory only, so a pane opened again draws its values at once
/// while the fresh ones are on their way.
@MainActor
enum SettingsCache {
    static let full = "/api/settings/full"
    private static var server = ""
    private static var replies: [String: JSON] = [:]

    static func last(_ path: String) -> JSON { replies[path] ?? .null }

    static func read(_ api: API, _ path: String, long: Bool = false) async throws -> JSON {
        let reply = long ? try await api.settingsLong("GET", path) : try await api.get(path)
        if server != api.base { server = api.base; replies = [:] }
        replies[path] = reply
        return reply
    }
}

extension Store {
    func settingsAPI() throws -> API {
        guard let api else { throw APIError(message: "Not connected to the runtime yet.") }
        return api
    }

    /// A picker with nothing saved shows the first profile, so that is what gets saved.
    func settingsAgent(_ id: String) -> String { id.isEmpty ? (agents.first?.id ?? "") : id }
}

extension API {
    fileprivate static let settingsSession: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 900
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: config)
    }()

    /// For work the server takes minutes over, such as a git clone or installing Tailscale,
    /// which the usual request limit would cut off. These replies sometimes explain a
    /// failure in `note` rather than `error`, so that is read too.
    func settingsLong(_ method: String = "POST", _ path: String, _ body: [String: Any]? = nil, timeout: TimeInterval = 600) async throws -> JSON {
        guard let url = URL(string: base + path) else { throw APIError(message: "That address is not valid: \(path)") }
        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = method
        if !token.isEmpty { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await API.settingsSession.data(for: request)
        let json = JSON(data: data)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if status >= 400 || json["ok"] == .bool(false) {
            throw APIError(message: json["error"].optStr ?? json["detail"].optStr ?? json["note"].optStr ?? "That did not work. Please try again.")
        }
        return json
    }
}

extension String {
    var settingsTrimmed: String { trimmingCharacters(in: .whitespacesAndNewlines) }
}

/// Comma-separated text, as the web's list fields take it.
func settingsList(_ text: String) -> [String] {
    text.split(separator: ",").map { String($0).settingsTrimmed }.filter { !$0.isEmpty }
}

func settingsClock(_ ts: Double) -> String {
    Date(timeIntervalSince1970: ts).formatted(date: .omitted, time: .shortened)
}

private struct SettingsReload: ViewModifier {
    @Environment(Store.self) private var store
    let tick: KeyPath<Store.Ticks, Int>
    let load: @MainActor () async -> Void

    /// The ticks are read here rather than in the pane, because most of them move many
    /// times a second while the agent works and the pane's fields should not redraw for it.
    func body(content: Content) -> some View {
        content.task(id: "\(store.api?.base ?? "")|\(store.link)|\(store.ticks.settings)|\(store.ticks[keyPath: tick])") { await load() }
    }
}

extension View {
    /// Reads a pane's endpoint when it appears, when the runtime comes back, and when the
    /// server says that part of the settings changed.
    func settingsLoad(on tick: KeyPath<Store.Ticks, Int> = \.settings, _ load: @escaping @MainActor () async -> Void) -> some View {
        modifier(SettingsReload(tick: tick, load: load))
    }
}

/// What a form last said, in words; colour only supports them.
struct SettingsStatus: Equatable {
    enum Tone { case plain, busy, good, bad }
    var text = ""
    var tone = Tone.plain

    static let none = SettingsStatus()
    static func plain(_ text: String) -> SettingsStatus { SettingsStatus(text: text, tone: .plain) }
    static func busy(_ text: String) -> SettingsStatus { SettingsStatus(text: text, tone: .busy) }
    static func good(_ text: String) -> SettingsStatus { SettingsStatus(text: text, tone: .good) }
    static func bad(_ text: String) -> SettingsStatus { SettingsStatus(text: text, tone: .bad) }
}

struct SettingsStatusLine: View {
    let status: SettingsStatus

    var body: some View {
        if !status.text.isEmpty {
            HStack(spacing: 6) {
                if status.tone == .busy { ProgressView().controlSize(.small) }
                Text(status.text)
                    .font(.callout)
                    .foregroundStyle(tone)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            .accessibilityElement(children: .combine)
        }
    }

    private var tone: Color {
        switch status.tone {
        case .good: return MC.good
        case .bad: return MC.danger
        case .plain, .busy: return .secondary
        }
    }
}

/// The bottom row of a form: what happened on the left, its buttons on the right.
struct SettingsActions<Buttons: View>: View {
    var status = SettingsStatus.none
    @ViewBuilder var buttons: Buttons

    var body: some View {
        HStack(spacing: 8) {
            SettingsStatusLine(status: status)
            Spacer(minLength: 12)
            buttons
        }
    }
}

/// Shown when a pane could not read its endpoint. Saving stays off until it can.
struct SettingsProblemRow: View {
    let problem: String?
    var body: some View {
        if let problem { SettingsStatusLine(status: .bad("Could not read these settings: \(problem)")) }
    }
}

struct SettingsHint: View {
    let text: String
    var warn = false
    init(_ text: String, warn: Bool = false) { self.text = text; self.warn = warn }
    var body: some View {
        Text(text)
            .font(.callout)
            .fontWeight(.regular)
            .foregroundStyle(warn ? MC.warn : Color.secondary)
            .textCase(nil)
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// Text an agent or the owner wrote and might want to copy: commands, addresses, code.
struct SettingsCode: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text)
            .font(.system(size: 12, design: .monospaced))
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
    }
}

struct SettingsSwitch: View {
    let title: String
    var note: String?
    @Binding var isOn: Bool

    var body: some View {
        Toggle(isOn: $isOn) {
            Text(title)
            if let note { Text(note) }
        }
        .toggleStyle(.switch)
    }
}

/// A profile to choose. Nothing saved shows the first profile, as the web does, and a
/// saved one that has since gone stays visible rather than silently becoming another.
struct SettingsAgentPicker: View {
    @Environment(Store.self) private var store
    let title: String
    @Binding var selection: String

    var body: some View {
        Picker(title, selection: Binding(get: { store.settingsAgent(selection) }, set: { selection = $0 })) {
            ForEach(store.agents) { Text($0.name).tag($0.id) }
            let shown = store.settingsAgent(selection)
            if !store.agents.contains(where: { $0.id == shown }) {
                Text(shown.isEmpty ? "No profiles yet" : store.name(shown)).tag(shown)
            }
        }
    }
}

/// A model name: type any, or pick from what the provider lists.
struct SettingsModelField: View {
    let title: String
    @Binding var text: String
    var prompt = "type or pick a model"
    let models: [String]

    var body: some View {
        LabeledContent(title) {
            HStack(spacing: 6) {
                TextField(title, text: $text, prompt: Text(prompt))
                    .labelsHidden()
                    .multilineTextAlignment(.trailing)
                    .textFieldStyle(.plain)
                    .autocorrectionDisabled()
                if !models.isEmpty {
                    Menu {
                        ForEach(models, id: \.self) { model in Button(model) { text = model } }
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
    }
}

/// A button whose action cannot be taken back, behind the system's own dialog.
struct SettingsConfirmButton: View {
    let title: String
    let question: String
    var detail: String?
    var confirm: String?
    var destructive = true
    let action: () async -> Void
    @State private var asking = false
    @State private var running = false

    var body: some View {
        Button(title, role: destructive ? .destructive : nil) { asking = true }
            .disabled(running)
            .confirmationDialog(question, isPresented: $asking, titleVisibility: .visible) {
                Button(confirm ?? title, role: destructive ? .destructive : nil) {
                    running = true
                    Task { await action(); running = false }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                if let detail { Text(detail) }
            }
    }
}

struct SettingsCopyButton: View {
    let text: String
    var title = "Copy"
    @State private var copied = false

    var body: some View {
        Button(copied ? "Copied" : title) {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
            copied = true
            Task {
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                copied = false
            }
        }
    }
}

/// Text that has to stay exactly as typed. JSON, schedules and skills break if the system
/// turns a quote into a curly one or corrects a word, and a SwiftUI editor cannot be told
/// not to, so this is the AppKit text view with those features off. It also holds a very
/// long diff without slowing down, which a SwiftUI Text does not.
struct SettingsTextView: NSViewRepresentable {
    let label: String
    @Binding var text: String
    var mono = true
    var editable = true

    func makeCoordinator() -> Coordinator { Coordinator(text: $text) }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSTextView.scrollableTextView()
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        guard let view = scroll.documentView as? NSTextView else { return scroll }
        view.delegate = context.coordinator
        view.isRichText = false
        view.allowsUndo = true
        view.drawsBackground = false
        view.isAutomaticQuoteSubstitutionEnabled = false
        view.isAutomaticDashSubstitutionEnabled = false
        view.isAutomaticTextReplacementEnabled = false
        view.isAutomaticSpellingCorrectionEnabled = false
        view.isAutomaticLinkDetectionEnabled = false
        view.isAutomaticDataDetectionEnabled = false
        view.isContinuousSpellCheckingEnabled = false
        view.isGrammarCheckingEnabled = false
        view.font = mono ? .monospacedSystemFont(ofSize: 12, weight: .regular) : .systemFont(ofSize: 13)
        view.textColor = .labelColor
        view.textContainerInset = NSSize(width: 4, height: 6)
        view.isEditable = editable
        view.setAccessibilityLabel(label)
        view.string = text
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.text = $text
        guard let view = scroll.documentView as? NSTextView else { return }
        if view.string != text { view.string = text }
        view.isEditable = editable
    }

    @MainActor
    final class Coordinator: NSObject, NSTextViewDelegate {
        var text: Binding<String>
        init(text: Binding<String>) { self.text = text }
        func textDidChange(_ notification: Notification) {
            if let view = notification.object as? NSTextView { text.wrappedValue = view.string }
        }
    }
}

/// A bordered multi-line field for a form row.
struct SettingsTextBox: View {
    let label: String
    @Binding var text: String
    var prompt = ""
    var height: CGFloat = 92
    var mono = true

    var body: some View {
        SettingsTextView(label: label, text: $text, mono: mono)
            .overlay(alignment: .topLeading) {
                if text.isEmpty && !prompt.isEmpty {
                    Text(prompt)
                        .font(mono ? .system(size: 12, design: .monospaced) : .system(size: 13))
                        .foregroundStyle(.tertiary)
                        .padding(.horizontal, 9)
                        .padding(.vertical, 6)
                        .allowsHitTesting(false)
                        .accessibilityHidden(true)
                }
            }
            .frame(height: height)
            .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 6, style: .continuous).strokeBorder(MC.line))
    }
}
