import AppKit
import SwiftUI

/// Everything waiting on the owner. Whatever is approved here runs exactly as shown, so
/// nothing that will run is shortened or left out: a held command in full, with what changed
/// since a near-identical one was answered; a job's whole task and the check it runs first.
struct InboxView: View {
    @Environment(Store.self) private var store
    @State private var overdue = false

    var body: some View {
        Page("Inbox", subtitle: "Everything waiting on you, in one place: commands the guard held, jobs asking to start, proposed changes, sign-ins, questions from workers, and lessons that need a decision.") {
            if overdue { RuntimeNote() }
            InboxList()
        }
        // The first read normally lands in a moment, so the note only shows once it is late.
        .task(id: store.live) {
            overdue = false
            guard !store.live else { return }
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            if !Task.isCancelled { overdue = true }
        }
        .task { await quietRefresh(store) }
    }
}

/// So ages and counts stay honest without a live event, while the main window can be seen.
@MainActor private func quietRefresh(_ store: Store) async {
    while !Task.isCancelled {
        try? await Task.sleep(nanoseconds: 20_000_000_000)
        if Task.isCancelled { return }
        if NSApp.windows.contains(where: { $0.canBecomeMain && $0.isVisible && $0.occlusionState.contains(.visible) }) {
            await store.refresh()
        }
    }
}

private struct RuntimeNote: View {
    @Environment(Store.self) private var store
    var body: some View {
        Card(padding: 12) {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Waiting for the runtime").font(.system(size: 13, weight: .medium))
                    Text("What is shown here was saved earlier, so nothing can be decided until the current state has loaded.")
                        .font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                AsyncButton("Try again") { await store.refresh() }
            }
        }
    }
}

// MARK: The list

private struct KindInfo { let icon: String; let tone: Color; let label: String }
private func kindInfo(_ kind: String) -> KindInfo {
    switch kind {
    case "command": return KindInfo(icon: "exclamationmark.shield", tone: MC.warn, label: "Command held for you")
    case "tool": return KindInfo(icon: "exclamationmark.shield", tone: MC.warn, label: "Tool call held for you")
    case "job": return KindInfo(icon: "clock", tone: MC.info, label: "Recurring job wants to start")
    case "proposal": return KindInfo(icon: "arrow.triangle.branch", tone: MC.info, label: "Proposed change to Mission Control")
    case "signin": return KindInfo(icon: "globe", tone: MC.warn, label: "Website sign-in")
    case "lesson": return KindInfo(icon: "flask", tone: MC.warn, label: "Lesson needs a decision")
    case "question": return KindInfo(icon: "questionmark.bubble", tone: MC.warn, label: "A worker needs your answer")
    default: return KindInfo(icon: "tray", tone: .secondary, label: kind)
    }
}

private extension JSON {
    /// Jobs, proposals and lessons each number their own rows, so an id alone can repeat.
    var inboxRowKey: String { "\(self["kind"].str):\(self["id"].str):\(self["id"].isNull ? self["title"].str : "")" }
}

private struct InboxList: View {
    @Environment(Store.self) private var store

    var body: some View {
        let items = store.inbox
        Card {
            if items.isEmpty {
                if store.live { NothingNeedsYou() } else { EmptyNote(title: "Reading the inbox…") }
            } else {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(items, id: \.inboxRowKey) { item in
                        if item.inboxRowKey != items[0].inboxRowKey { Divider() }
                        InboxRow(item: item)
                    }
                }
            }
        }
    }
}

private struct NothingNeedsYou: View {
    @Environment(Store.self) private var store
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: -6) {
                ForEach(store.agents.prefix(4)) { agent in AgentBadge(agent: agent, size: 36) }
            }
            .padding(.top, 22)
            EmptyNote(title: "Nothing needs you", detail: "When the agent needs your say-so, it lands here and on your phone.")
        }
        .frame(maxWidth: .infinity)
    }
}

private struct InboxRow: View {
    @Environment(Store.self) private var store
    let item: JSON

    var body: some View {
        let kind = kindInfo(item.kind.str)
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: kind.icon)
                .font(.system(size: 15))
                .foregroundStyle(kind.tone)
                .frame(width: 22)
                .padding(.top, 1)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 8) {
                Text(verbatim: kind.label).font(.system(size: 12)).foregroundStyle(.secondary)
                content
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            TimelineView(.everyMinute) { _ in
                Text(ago(item.ts.num)).font(.system(size: 11)).foregroundStyle(.secondary).monospacedDigit().fixedSize()
            }
        }
        .padding(14)
    }

    @ViewBuilder private var content: some View {
        switch item.kind.str {
        case "command": CommandItem(item: item, who: who)
        case "tool": ToolItem(item: item, who: who)
        case "job": JobItem(item: item, who: who)
        case "proposal": ProposalItem(item: item, who: who)
        case "question": QuestionItem(item: item, who: who)
        case "signin": SigninItem(item: item, who: who)
        case "lesson": LessonItem(item: item, who: who)
        default: ItemTitle(text: item.title.str)
        }
    }

    private var who: String {
        let agent = item.agent.str
        if !agent.isEmpty, agent != "*" { return store.name(agent) }
        return item.kind.str == "lesson" ? "Everywhere" : ""
    }
}

// MARK: Pieces of a row

private struct ItemTitle: View {
    let text: String
    var body: some View {
        Text(verbatim: text)
            .font(.system(size: 13, weight: .medium))
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// Exactly what will run. Never clipped or scrolled away inside its row: it wraps, and takes
/// all the height it needs.
private struct HeldText: View {
    let text: String
    var body: some View {
        Text(verbatim: text)
            .font(.system(size: 12, design: .monospaced))
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(MC.line.opacity(0.6), lineWidth: 0.5))
    }
}

/// A line of explanation that starts with who is asking.
private struct Explanation: View {
    let who: String
    let text: String
    var body: some View {
        Text(attributed)
            .font(.system(size: 12))
            .foregroundStyle(.secondary)
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
    }
    private var attributed: AttributedString {
        var name = AttributedString(who)
        name.swiftUI.foregroundColor = .primary
        name.swiftUI.font = .system(size: 12, weight: .medium)
        return name + AttributedString(text)
    }
}

/// Three near-identical held commands are impossible to tell apart, so this says which one
/// was already answered and what moved since. It starts open: the difference is the point.
private struct PriorNote: View {
    let prior: JSON
    @State private var open = true

    var body: some View {
        if !prior.isNull {
            let verb = prior.status.str == "denied" ? "declined" : "approved"
            let when = Date(timeIntervalSince1970: prior.ts.num).formatted(date: .omitted, time: .shortened)
            if prior.identical.flag {
                Text(verbatim: "Identical to the command you \(verb) at \(when). Running it runs it again.")
                    .font(.system(size: 12))
                    .foregroundStyle(MC.warn)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                DisclosureGroup(isExpanded: $open) {
                    lines.padding(.top, 4)
                } label: {
                    Text(verbatim: "Differs from the command you \(verb) at \(when)").font(.system(size: 12))
                }
            }
        }
    }

    private var lines: some View {
        let rows = prior.diff.list.map(\.str)
        return VStack(alignment: .leading, spacing: 1) {
            if rows.isEmpty {
                Text("only whitespace changed").font(.system(size: 12)).foregroundStyle(.secondary)
            }
            ForEach(Array(rows.enumerated()), id: \.offset) { _, line in
                Text(verbatim: line)
                    .font(.system(size: 12, design: .monospaced))
                    .foregroundStyle(line.hasPrefix("+") ? MC.good : MC.danger)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: Deciding

/// Refuses while the list may still be the disk snapshot, which can show something already
/// decided. The buttons are disabled then too; this covers a press already on its way.
@MainActor private func liveAPI(_ store: Store) throws -> API {
    guard let api = store.api, store.live else { throw APIError(message: "Still reading the current state. Try again in a moment.") }
    return api
}

private struct Choice: Identifiable {
    let title: String
    var primary = false
    /// Asked first, for what cannot be taken back with a click.
    var confirm: String?
    var confirmDetail: String?
    let run: () async throws -> Void
    var id: String { title }
}

/// One row's decisions. One press holds them all until the row has gone, since a decision
/// made twice only earns an "already decided". If the row is somehow still there after a
/// few seconds, they come back rather than leave it stuck.
private struct Decisions: View {
    @Environment(Store.self) private var store
    let choices: [Choice]
    @State private var running: String?
    @State private var confirming: Choice?

    var body: some View {
        HStack(spacing: 8) {
            ForEach(choices) { choice in
                let button = Button { press(choice) } label: {
                    HStack(spacing: 5) {
                        if running == choice.title { ProgressView().controlSize(.mini) }
                        Text(choice.title)
                    }
                }
                .disabled(!store.live || running != nil)
                if choice.primary { button.buttonStyle(.borderedProminent) } else { button.buttonStyle(.bordered) }
            }
        }
        .confirmationDialog(confirming?.confirm ?? "", isPresented: Binding(get: { confirming != nil }, set: { if !$0 { confirming = nil } }), titleVisibility: .visible, presenting: confirming) { choice in
            Button(choice.title) { start(choice) }
            Button("Cancel", role: .cancel) {}
        } message: { choice in
            if let detail = choice.confirmDetail { Text(detail) }
        }
    }

    private func press(_ choice: Choice) {
        if choice.confirm != nil { confirming = choice } else { start(choice) }
    }

    private func start(_ choice: Choice) {
        guard running == nil else { return }
        running = choice.title
        Task {
            do {
                try await choice.run()
                try? await Task.sleep(nanoseconds: 3_000_000_000)
            } catch {
                store.notify(error.localizedDescription, error: true)
            }
            running = nil
        }
    }
}

/// The server holds its reply until an approved command has finished, and a long one can
/// outlast the connection's patience. It is still running, not failed, so it must not read
/// as a failure that invites running it again.
@MainActor private func approvalChoices(_ item: JSON, _ store: Store, approve: String) -> [Choice] {
    [
        Choice(title: approve, primary: true) {
            do {
                try await store.decide(approval: item.id, approve: true)
                store.notify("Ran it. The task has the output.")
            } catch let error as URLError where error.code == .timedOut {
                store.notify("It is still running. The task will have the output.")
                store.refreshSoon(after: 0.05)
            }
        },
        Choice(title: "Decline") {
            try await store.decide(approval: item.id, approve: false)
            store.notify("Declined. The agent has been told.")
        },
    ]
}

// MARK: Each kind

private struct CommandItem: View {
    @Environment(Store.self) private var store
    let item: JSON
    let who: String

    var body: some View {
        HeldText(text: item.title.str)
        Explanation(who: who, text: " wants to run this in \(item.detail.str). Running it runs exactly what is shown.")
        ReasonLine(text: item.reason.str)
        PriorNote(prior: item.prior)
        Decisions(choices: approvalChoices(item, store, approve: "Run it"))
    }
}

private struct ToolItem: View {
    @Environment(Store.self) private var store
    let item: JSON
    let who: String

    var body: some View {
        HeldText(text: item.title.str)
        Explanation(who: who, text: " wants to make this change through the \(item.detail.str) MCP server, which marks it as one that cannot be taken back. Approving sends exactly what is shown.")
        ReasonLine(text: item.reason.str)
        PriorNote(prior: item.prior)
        Decisions(choices: approvalChoices(item, store, approve: "Approve"))
    }
}

private struct ReasonLine: View {
    let text: String
    var body: some View {
        if !text.isEmpty {
            Text(verbatim: text)
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct JobItem: View {
    @Environment(Store.self) private var store
    let item: JSON
    let who: String

    var body: some View {
        let runs = item.max_runs.int
        let monitor = item.monitor_cmd.str
        ItemTitle(text: item.title.str)
        Explanation(who: who, text: " · \(item.schedule.str) · \(runs > 0 ? "stops after \(runs) runs" : "runs until you stop it")")
        Text(verbatim: item.detail.str)
            .font(.system(size: 12.5))
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
        if !monitor.isEmpty {
            Text("Before every run it executes this check in your shell:")
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
            HeldText(text: monitor)
        }
        Decisions(choices: [
            Choice(title: "Approve", primary: true) { try await act("approve") },
            Choice(title: "Reject") { try await act("delete") },
        ])
    }

    private func act(_ action: String) async throws {
        _ = try await liveAPI(store).post("/api/cron/\(API.escape(item.id))/\(action)")
        store.refreshSoon(after: 0.05)
    }
}

private struct ProposalItem: View {
    @Environment(Store.self) private var store
    let item: JSON
    let who: String
    @State private var showingDiff = false

    var body: some View {
        let stat = item.stat
        ItemTitle(text: item.title.str)
        Explanation(who: who, text: " · \(plural(stat.files.list.count, "file")) · +\(stat.added.int)/-\(stat.removed.int) · tests green")
        Text(verbatim: item.detail.str)
            .font(.system(size: 12))
            .foregroundStyle(.secondary)
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
        HStack(spacing: 8) {
            Button("View diff") { showingDiff = true }
            Decisions(choices: [
                Choice(title: "Approve and restart", primary: true, confirm: "Approve and restart?",
                       confirmDetail: "Mission Control restarts on the new code. If it does not come back, it rolls back by itself.") { try await approve() },
                Choice(title: "Reject") {
                    _ = try await liveAPI(store).post("/api/improvements/\(API.escape(item.id))/reject")
                    store.refreshSoon(after: 0.05)
                },
            ])
        }
        .sheet(isPresented: $showingDiff) { DiffSheet(id: item.id) }
    }

    /// The server goes down as part of answering, so losing the connection here is the
    /// change going in, not it failing.
    private func approve() async throws {
        let api = try liveAPI(store)
        do {
            _ = try await api.post("/api/improvements/\(API.escape(item.id))/approve")
        } catch let error as URLError where error.code == .timedOut || error.code == .networkConnectionLost {}
        store.notify("Applying the change and restarting. If it does not come back, it rolls back by itself.")
        store.refreshSoon(after: 0.05)
    }
}

/// An answer in progress outlives the row, so scrolling away or a look at another screen
/// does not lose it.
@Observable @MainActor
private final class AnswerDraft {
    private static var drafts: [String: AnswerDraft] = [:]
    static func draft(for id: String) -> AnswerDraft {
        if let draft = drafts[id] { return draft }
        let draft = AnswerDraft()
        drafts[id] = draft
        return draft
    }
    static func forget(_ id: String) { drafts[id] = nil }

    var text = ""
}

private struct QuestionItem: View {
    let item: JSON
    let who: String

    var body: some View {
        ItemTitle(text: item.title.str)
        Explanation(who: who, text: " stopped on this while working on: \(item.detail.str)")
        AnswerBox(id: item.id)
    }
}

/// Its own view, so each keystroke redraws this and not the list around it.
private struct AnswerBox: View {
    @Environment(Store.self) private var store
    let id: String
    @State private var draft: AnswerDraft
    @State private var sending = false

    init(id: String) {
        self.id = id
        _draft = State(initialValue: AnswerDraft.draft(for: id))
    }

    var body: some View {
        @Bindable var draft = draft
        let answer = draft.text.trimmingCharacters(in: .whitespacesAndNewlines)
        TextEditor(text: $draft.text)
            .font(.system(size: 13))
            .scrollContentBackground(.hidden)
            .padding(5)
            .frame(height: 64)
            .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 7, style: .continuous).strokeBorder(MC.line, lineWidth: 0.5))
            .overlay(alignment: .topLeading) {
                if draft.text.isEmpty {
                    Text("Your answer goes straight to this worker.")
                        .font(.system(size: 13))
                        .foregroundStyle(.tertiary)
                        .padding(.horizontal, 10).padding(.vertical, 5)
                        .allowsHitTesting(false)
                        .accessibilityHidden(true)
                }
            }
            .accessibilityLabel("Your answer")
        Button("Send") { send(answer) }
            .buttonStyle(.borderedProminent)
            .disabled(!store.live || answer.isEmpty || sending)
    }

    private func send(_ answer: String) {
        sending = true
        Task {
            defer { sending = false }
            do {
                _ = try await liveAPI(store).post("/api/kernel/sessions/\(API.escape(id))/message", ["text": answer])
                AnswerDraft.forget(id)
                draft.text = ""
                store.notify("Sent.")
                store.refreshSoon(after: 0.05)
            } catch {
                store.notify(error.localizedDescription, error: true)
            }
        }
    }
}

private struct SigninItem: View {
    @Environment(Store.self) private var store
    let item: JSON
    let who: String

    var body: some View {
        ItemTitle(text: item.title.str)
        Explanation(who: who.isEmpty ? "The agent" : who, text: " is waiting. \(item.detail.str)")
        Button("Open sign-in") { store.go(.settings("browser")) }
            .buttonStyle(.borderedProminent)
    }
}

private struct LessonItem: View {
    @Environment(Store.self) private var store
    let item: JSON
    let who: String

    var body: some View {
        ItemTitle(text: item.title.str)
        Explanation(who: who, text: " · \(item.detail.str)")
        HStack(spacing: 8) {
            Decisions(choices: choices)
            Button("Review") { store.go(.evolution) }.buttonStyle(.borderless)
        }
    }

    /// What can be done depends on why it is here: a lesson waiting for an OK, one the
    /// regression guard held back, or one the review flagged.
    private var choices: [Choice] {
        let ownerRule = ["owner", "owner-rule"].contains(item.source.str)
        switch (item.reason.str, item.status.str) {
        case ("proposed", _):
            return [set("active", "Use it", primary: true), set("retired", "Discard")]
        case ("suspended", _):
            return [set(ownerRule ? "active" : "trial", "Restore"), set("retired", "Retire")]
        case (_, "active"):
            return [set("active", "Keep it", primary: true), set("retired", "Retire")]
        default:
            return [set("active", "Keep it as a rule", primary: true), set("retired", "Dismiss")]
        }
    }

    private func set(_ status: String, _ title: String, primary: Bool = false) -> Choice {
        Choice(title: title, primary: primary) {
            _ = try await liveAPI(store).put("/api/lessons/\(API.escape(item.id))", ["status": status])
            store.refreshSoon(after: 0.05)
        }
    }
}

// MARK: The whole patch

/// So approving is a decision about code that has been read. A patch can run to 200 KB, which
/// is too much for one SwiftUI text; the system's text view lays it out as it scrolls, and
/// brings select all and find with it.
private struct DiffSheet: View {
    @Environment(Store.self) private var store
    @Environment(\.dismiss) private var dismiss
    let id: String
    @State private var title = "Proposed change"
    @State private var text: NSAttributedString?
    @State private var problem: String?

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(verbatim: title).font(.headline).lineLimit(1)
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            .padding(.horizontal, 16).padding(.vertical, 12)
            Divider()
            if let text {
                PatchText(text: text)
            } else if let problem {
                EmptyNote(title: "Could not read the change", detail: problem)
                    .frame(maxHeight: .infinity)
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(minWidth: 640, idealWidth: 760, minHeight: 440, idealHeight: 620)
        .task { await load() }
    }

    private func load() async {
        guard let api = store.api else {
            problem = "Not connected to the runtime yet."
            return
        }
        do {
            let reply = try await api.get("/api/improvements/\(API.escape(id))/diff")
            if !reply.title.str.isEmpty { title = reply.title.str }
            let diff = reply.diff.str
            text = await Task.detached(priority: .userInitiated) { StyledPatch(diff) }.value.text
        } catch {
            problem = error.localizedDescription
        }
    }
}

/// The same sheet, for a proposal shown in a chat.
struct ProposalDiffSheet: View {
    let id: String
    var body: some View { DiffSheet(id: id) }
}

/// Built off the main thread and never changed afterwards, so it is safe to hand across.
private struct StyledPatch: @unchecked Sendable {
    let text: NSAttributedString

    init(_ diff: String) {
        let font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        let body = diff.isEmpty ? "(empty)" : diff
        let styled = NSMutableAttributedString()
        for line in body.split(separator: "\n", omittingEmptySubsequences: false) {
            let tone: NSColor
            if line.hasPrefix("+++") || line.hasPrefix("---") || line.hasPrefix("@@") || line.hasPrefix("diff ") || line.hasPrefix("index ") {
                tone = .secondaryLabelColor
            } else if line.hasPrefix("+") {
                tone = .systemGreen
            } else if line.hasPrefix("-") {
                tone = .systemRed
            } else {
                tone = .labelColor
            }
            styled.append(NSAttributedString(string: line + "\n", attributes: [.font: font, .foregroundColor: tone]))
        }
        text = styled
    }
}

private struct PatchText: NSViewRepresentable {
    let text: NSAttributedString

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSTextView.scrollableTextView()
        scroll.drawsBackground = false
        if let view = scroll.documentView as? NSTextView {
            view.isEditable = false
            view.isSelectable = true
            view.drawsBackground = false
            view.usesFindBar = true
            view.textContainerInset = NSSize(width: 12, height: 10)
            view.textStorage?.setAttributedString(text)
        }
        return scroll
    }

    // Only ever made once the patch has loaded, and a patch does not change while it is read.
    func updateNSView(_ scroll: NSScrollView, context: Context) {}
}
