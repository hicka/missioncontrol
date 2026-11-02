import AppKit
import SwiftUI

/// The slash commands the server knows. They do not change while it runs, so one read per
/// server is enough, for this palette and for the chat composer's suggestions.
@Observable @MainActor
final class CommandCatalog {
    static let shared = CommandCatalog()

    private(set) var commands: [JSON] = []
    @ObservationIgnored private var base = ""

    func load(_ api: API?) async {
        guard let api, api.base != base || commands.isEmpty else { return }
        guard let reply = try? await api.get("/api/commands") else { return }
        base = api.base
        commands = reply.commands.list
    }
}

/// Follows the system, or holds light or dark, across every window.
@MainActor
enum AppTheme {
    private static let key = "mc-theme"

    static func apply() {
        switch UserDefaults.standard.string(forKey: key) {
        case "light": NSApp.appearance = NSAppearance(named: .aqua)
        case "dark": NSApp.appearance = NSAppearance(named: .darkAqua)
        default: NSApp.appearance = nil
        }
    }

    static func cycle(_ store: Store) {
        let next: (name: String, note: String)
        switch UserDefaults.standard.string(forKey: key) {
        case "light": next = ("dark", "Dark theme.")
        case "dark": next = ("auto", "Theme follows your system.")
        default: next = ("light", "Light theme.")
        }
        UserDefaults.standard.set(next.name, forKey: key)
        apply()
        store.notify(next.note)
    }
}

/// Reads the one field that opens the palette, so opening it redraws nothing behind it.
struct CommandPaletteHost: View {
    @Environment(Store.self) private var store

    var body: some View {
        if store.paletteOpen { CommandPalette() }
    }
}

private struct PaletteItem: Identifiable {
    /// Where a message found by search was written.
    struct Hit {
        let who: String, place: String, when: String
    }

    let id: String
    let group: String
    let title: String
    var sub = ""
    var icon = "chevron.right"
    var agent: Agent?
    var hit: Hit?
    let run: @MainActor () -> Void
}

private struct CommandPalette: View {
    @Environment(Store.self) private var store
    @State private var query = ""
    @State private var selected = 0
    /// Nil until a search for the words in the field has come back.
    @State private var hits: [JSON]?

    private var search: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        let items = paletteItems()
        ZStack(alignment: .top) {
            Color.black.opacity(0.18)
                .ignoresSafeArea()
                .contentShape(Rectangle())
                .onTapGesture { close() }
                .accessibilityHidden(true)
            VStack(spacing: 0) {
                HStack(spacing: 10) {
                    Image(systemName: "magnifyingglass")
                        .font(.system(size: 15))
                        .foregroundStyle(.secondary)
                        .accessibilityHidden(true)
                    PaletteField(
                        text: $query,
                        onMove: { move($0, among: items.count) },
                        onSubmit: { if items.indices.contains(selected) { choose(items[selected]) } },
                        onCancel: close
                    )
                    .frame(height: 22)
                    Button("esc", action: close)
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                        .keyboardShortcut(.cancelAction)
                        .accessibilityLabel("Close")
                }
                .padding(.horizontal, 14).padding(.vertical, 12)
                Divider()
                results(items)
                Divider()
                HStack(spacing: 14) {
                    Text("↑ ↓ move")
                    Text("↵ open")
                    Spacer()
                }
                .font(.system(size: 11))
                .foregroundStyle(.tertiary)
                .padding(.horizontal, 14).padding(.vertical, 7)
                .accessibilityHidden(true)
            }
            .frame(width: 600)
            .glassBackground(cornerRadius: 14)
            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).strokeBorder(MC.line.opacity(0.7), lineWidth: 0.5))
            .shadow(color: .black.opacity(0.28), radius: 28, y: 12)
            .padding(.top, 72)
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Search and commands")
            .accessibilityAddTraits(.isModal)
        }
        .onChange(of: query) { selected = 0 }
        .task { await CommandCatalog.shared.load(store.api) }
        .task(id: search) { await findMessages(search) }
    }

    private func results(_ items: [PaletteItem]) -> some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 1) {
                    ForEach(Array(items.enumerated()), id: \.element.id) { position, item in
                        if position == 0 || items[position - 1].group != item.group {
                            Text(item.group)
                                .font(.system(size: 11, weight: .medium))
                                .foregroundStyle(.secondary)
                                .padding(.horizontal, 8)
                                .padding(.top, position == 0 ? 4 : 10).padding(.bottom, 3)
                                .accessibilityAddTraits(.isHeader)
                        }
                        PaletteRow(item: item, selected: position == selected) { choose(item) }
                            .id(item.id)
                    }
                    if items.isEmpty {
                        Text(!search.isEmpty && hits == nil ? "Searching messages…" : "No matches.")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 28)
                    }
                }
                .padding(6)
            }
            .frame(height: 380)
            .onChange(of: selected) { _, position in
                if items.indices.contains(position) { proxy.scrollTo(items[position].id) }
            }
        }
    }

    private func move(_ delta: Int, among count: Int) {
        guard count > 0 else { return }
        selected = (selected + delta + count) % count
    }

    private func close() { store.paletteOpen = false }

    private func choose(_ item: PaletteItem) {
        close()
        item.run()
    }

    /// Waits out the typing first, and a later keystroke cancels this one, so only the last
    /// words typed reach the server. What was found stays up until the new answer arrives,
    /// so the list does not blink on every key.
    private func findMessages(_ words: String) async {
        guard !words.isEmpty else { hits = nil; return }
        try? await Task.sleep(nanoseconds: 180_000_000)
        guard !Task.isCancelled, let api = store.api else { return }
        let reply = try? await api.get("/api/search?q=\(API.escape(words))")
        if !Task.isCancelled { hits = reply?.results.list ?? [] }
    }

    // MARK: What it offers

    private func paletteItems() -> [PaletteItem] {
        let words = search.lowercased()
        let listed = goTo + profiles + actions + commands
        let matching = words.isEmpty ? listed : listed.filter { "\($0.title) \($0.sub)".lowercased().contains(words) }
        return matching + messages
    }

    private var goTo: [PaletteItem] {
        let screens: [(String, String, Route)] = [
            ("Home", "house", .home), ("Inbox", "tray", .inbox), ("Tasks", "checklist", .tasks),
            ("Activity", "bolt", .activity), ("Schedule", "clock", .schedule), ("Knowledge", "book", .knowledge),
            ("Evolution", "chart.line.uptrend.xyaxis", .evolution),
        ]
        return screens.map { title, icon, route in
            PaletteItem(id: "go:\(title)", group: "Go to", title: title, icon: icon) { store.go(route) }
        }
    }

    private var profiles: [PaletteItem] {
        var rows = [PaletteItem(id: "thread:group", group: "Profiles", title: "Group chat", sub: "The agent, or any profile you @mention", icon: "person.3") {
            store.go(.chat("group:team"))
        }]
        for agent in store.agents {
            rows.append(PaletteItem(id: "thread:\(agent.id)", group: "Profiles", title: agent.name, sub: store.agentInfo[agent.id].role.str, agent: agent) {
                store.go(.chat("dm:\(agent.id)"))
            })
        }
        return rows
    }

    private var actions: [PaletteItem] {
        [
            PaletteItem(id: "action:task", group: "Actions", title: "New task", icon: "plus") { store.focusHome() },
            PaletteItem(id: "action:profile", group: "Actions", title: "New profile", icon: "person.badge.plus") { store.go(.settings("profiles:new")) },
            PaletteItem(id: "action:settings", group: "Actions", title: "Settings", icon: "gearshape") { store.go(.settings("llm")) },
            PaletteItem(id: "action:theme", group: "Actions", title: "Switch theme", icon: "circle.lefthalf.filled") { AppTheme.cycle(store) },
            PaletteItem(id: "action:review", group: "Actions", title: "Run the learning review now", icon: "chart.line.uptrend.xyaxis") { runReview() },
        ]
    }

    private var commands: [PaletteItem] {
        let runAtOnce: Set<String> = ["help", "status", "agents", "watches"]
        return CommandCatalog.shared.commands.map { command in
            let name = command.name.str
            return PaletteItem(id: "command:\(name)", group: "Commands", title: command.usage.str, sub: command.description.str, icon: "terminal") {
                if runAtOnce.contains(name) { runHomeCommand("/\(name)", store) } else { store.focusHome(prefill: "/\(name) ") }
            }
        }
    }

    private var messages: [PaletteItem] {
        (hits ?? []).compactMap { message in
            // A message between two profiles lives inside the chat of the one who sent it.
            let thread = message.thread.str
            let opens = thread.hasPrefix("a2a:") ? "dm:\(message.sender.str)" : thread
            guard let row = store.threadRows.first(where: { $0.id == opens }) else { return nil }
            let sender = message.sender.str
            let messageID = message["id"].int
            // In a one-to-one chat the speaker is the conversation, so naming both repeats it.
            let who = sender == "user" ? "You" : opens == "dm:\(sender)" ? "" : store.name(sender)
            let hit = PaletteItem.Hit(who: who, place: row.name.str, when: ChatTime.shortDay(message.ts.num))
            return PaletteItem(id: "hit:\(messageID)", group: "Messages", title: clip(message.text.str, 120), hit: hit) {
                store.open(opens, at: messageID)
            }
        }
    }

    private func runReview() {
        Task {
            do {
                guard let api = store.api else { throw APIError(message: "Not connected to the runtime yet.") }
                _ = try await api.post("/api/evolution/review")
                store.notify("Review started. The Evolution page updates when it finishes.")
            } catch {
                store.notify(error.localizedDescription, error: true)
            }
        }
    }
}

private struct PaletteRow: View {
    let item: PaletteItem
    let selected: Bool
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 10) {
                if let agent = item.agent {
                    AgentBadge(agent: agent, size: 20)
                } else {
                    Image(systemName: item.hit == nil ? item.icon : "text.bubble")
                        .font(.system(size: 13))
                        .foregroundStyle(.secondary)
                        .frame(width: 20, height: 20)
                        .accessibilityHidden(true)
                }
                if let hit = item.hit {
                    VStack(alignment: .leading, spacing: 1) {
                        HStack(spacing: 6) {
                            if !hit.who.isEmpty { Text(verbatim: hit.who).font(.system(size: 11, weight: .medium)) }
                            Text(verbatim: hit.place).font(.system(size: 11)).foregroundStyle(.secondary)
                            Spacer(minLength: 8)
                            Text(verbatim: hit.when).font(.system(size: 11)).foregroundStyle(.tertiary)
                        }
                        .lineLimit(1)
                        Text(Markdown.inline(item.title)).font(.system(size: 12.5)).lineLimit(2)
                    }
                } else {
                    Text(verbatim: item.title).font(.system(size: 13, weight: .medium)).lineLimit(1).layoutPriority(1)
                    if !item.sub.isEmpty {
                        Text(verbatim: item.sub).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                    }
                    Spacer(minLength: 0)
                }
            }
            .padding(.horizontal, 8).padding(.vertical, 6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(selected ? Color.accentColor.opacity(0.2) : hovering ? Color.primary.opacity(0.06) : Color.clear, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            .contentShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .accessibilityLabel(accessibilityText)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private var accessibilityText: String {
        if let hit = item.hit { return [hit.who, hit.place, hit.when, item.title].filter { !$0.isEmpty }.joined(separator: ", ") }
        return item.sub.isEmpty ? item.title : "\(item.title), \(item.sub)"
    }
}

/// The arrow keys, Return and Escape have to reach the palette before the field editor acts
/// on them, which only the AppKit delegate is told about reliably. It takes the keyboard as
/// it appears and hands it back to whatever had it when it goes.
private struct PaletteField: NSViewRepresentable {
    @Binding var text: String
    let onMove: (Int) -> Void
    let onSubmit: () -> Void
    let onCancel: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(parent: self) }

    func makeNSView(context: Context) -> FieldView {
        let field = FieldView()
        field.isBordered = false
        field.drawsBackground = false
        field.focusRingType = .none
        field.font = .systemFont(ofSize: 15)
        field.placeholderString = "Search views, profiles, commands and messages"
        field.lineBreakMode = .byTruncatingTail
        field.usesSingleLineMode = true
        field.setAccessibilityLabel("Search")
        field.delegate = context.coordinator
        let coordinator = context.coordinator
        field.onWindow = { [weak field] window in
            coordinator.window = window
            var holder = window.firstResponder
            // Typing in a text field really happens in the window's shared editor, which
            // this field is about to borrow, so the one to go back to is the field itself.
            if let editor = holder as? NSTextView, editor.isFieldEditor { holder = editor.delegate as? NSResponder }
            coordinator.previous = holder
            DispatchQueue.main.async { if let field { window.makeFirstResponder(field) } }
        }
        return field
    }

    func updateNSView(_ field: FieldView, context: Context) {
        context.coordinator.parent = self
        if field.stringValue != text { field.stringValue = text }
    }

    static func dismantleNSView(_ field: FieldView, coordinator: Coordinator) {
        guard let window = coordinator.window, let previous = coordinator.previous else { return }
        if let view = previous as? NSView, view.window !== window { return }
        window.makeFirstResponder(previous)
    }

    final class FieldView: NSTextField {
        var onWindow: ((NSWindow) -> Void)?
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            if let window, let onWindow {
                self.onWindow = nil
                onWindow(window)
            }
        }
    }

    @MainActor
    final class Coordinator: NSObject, NSTextFieldDelegate {
        var parent: PaletteField
        weak var window: NSWindow?
        weak var previous: NSResponder?
        init(parent: PaletteField) { self.parent = parent }

        func controlTextDidChange(_ notification: Notification) {
            if let field = notification.object as? NSTextField { parent.text = field.stringValue }
        }

        func control(_ control: NSControl, textView: NSTextView, doCommandBy selector: Selector) -> Bool {
            switch selector {
            case #selector(NSResponder.moveDown(_:)): parent.onMove(1)
            case #selector(NSResponder.moveUp(_:)): parent.onMove(-1)
            case #selector(NSResponder.insertNewline(_:)): parent.onSubmit()
            case #selector(NSResponder.cancelOperation(_:)): parent.onCancel()
            // The keyboard stays inside the palette while it is open.
            case #selector(NSResponder.insertTab(_:)), #selector(NSResponder.insertBacktab(_:)): break
            default: return false
            }
            return true
        }
    }
}
