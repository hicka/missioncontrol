import AppKit
import ImageIO
import QuickLook
import SwiftUI

private let profilePanelKey = "chat.profilePanel"

/// One conversation. Messages are rows in a lazy list, kept once read, so opening a chat
/// draws what is known straight away and reads what is new behind it. The header, the list
/// and the composer each read only their own fields, so a keystroke or a streamed word
/// redraws the part it touches and never the messages.
struct ChatView: View {
    @Environment(Store.self) private var store
    let threadID: String
    @AppStorage(profilePanelKey) private var panelWanted = true
    @State private var panelShown: Bool

    @MainActor init(threadID: String) {
        self.threadID = threadID
        let wanted = UserDefaults.standard.object(forKey: profilePanelKey) as? Bool ?? true
        _panelShown = State(initialValue: threadID.hasPrefix("dm:") && wanted && Self.windowIsWide)
    }

    var body: some View {
        let model = store.thread(threadID)
        VStack(spacing: 0) {
            ChatHeader(threadID: threadID, panelShown: panelShown, togglePanel: togglePanel)
            Divider()
            MessageList(model: model, threadID: threadID)
                .overlay(alignment: .bottom) { LatestButton(model: model).padding(.bottom, 12) }
            ChatComposer(threadID: threadID)
                .zIndex(1)
        }
        .modifier(FileDropZone(threadID: threadID))
        .inspector(isPresented: $panelShown) {
            if threadID.hasPrefix("dm:") {
                AgentProfileView(agentID: String(threadID.dropFirst(3)))
                    .inspectorColumnWidth(min: 300, ideal: 340, max: 460)
            }
        }
        .task(id: model.jumps) {
            store.unread.remove(threadID)
            guard let api = store.api else { return }
            if let target = model.takeJump() {
                await model.load(api, at: target)
                if !model.loaded { await model.load(api) }
            } else {
                await model.load(api)
            }
        }
        // The past only stays up while someone is looking at it. Left there, this thread
        // would stop taking new messages, and the floating bar shows the same model.
        .onDisappear {
            if !model.atLatest, let api = store.api { Task { await model.load(api) } }
        }
    }

    /// On a wide window the panel follows the owner's last choice. On a narrow one it would
    /// crowd the chat, so it opens only when asked for, and that does not become the choice.
    private func togglePanel() {
        panelShown.toggle()
        if Self.windowIsWide { panelWanted = panelShown }
    }

    @MainActor private static var windowIsWide: Bool {
        ((NSApp.mainWindow ?? NSApp.keyWindow)?.frame.width ?? 1240) > 1180
    }
}

private struct ChatHeader: View {
    @Environment(Store.self) private var store
    let threadID: String
    let panelShown: Bool
    let togglePanel: () -> Void
    @State private var answersIMessage = false

    private var agentID: String? { threadID.hasPrefix("dm:") ? String(threadID.dropFirst(3)) : nil }

    var body: some View {
        HStack(spacing: 10) {
            if let agentID {
                AgentBadge(agent: store.agent(agentID), size: 28)
            } else {
                Image(systemName: "person.3").frame(width: 28).accessibilityHidden(true)
            }
            VStack(alignment: .leading, spacing: 1) {
                Text(verbatim: agentID.map { store.name($0) } ?? "Group chat").font(.system(size: 14, weight: .semibold))
                Text(verbatim: statusLine).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
            }
            if answersIMessage {
                Pill(status: "active", label: "iMessage").help("Responds to your iMessages")
            }
            Spacer()
            if let agentID {
                if store.isBusy(agentID) {
                    Button { Task { await store.stopAgent(agentID) } } label: { Label("Stop", systemImage: "stop.fill") }
                        .controlSize(.small)
                        .help("Stop this profile")
                }
                Button { store.go(.settings("profiles:\(agentID)")) } label: { Image(systemName: "pencil") }
                    .buttonStyle(.borderless)
                    .help("Edit profile")
                    .accessibilityLabel("Edit profile")
                Button(action: togglePanel) { Image(systemName: "sidebar.trailing") }
                    .buttonStyle(.borderless)
                    .foregroundStyle(panelShown ? Color.accentColor : Color.secondary)
                    .help(panelShown ? "Hide profile" : "Show profile")
                    .accessibilityLabel(panelShown ? "Hide profile" : "Show profile")
            }
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
        .task(id: store.ticks.settings) { await readIMessage() }
        .onChange(of: agentID.map { store.agent($0) != nil } ?? true) { _, stillThere in
            // This profile was removed, here or from another device.
            if !stillThere, !store.agents.isEmpty, store.route == .chat(threadID) { store.go(store.leadThread.map { .chat($0) } ?? .home) }
        }
    }

    private var statusLine: String {
        guard let agentID else {
            let busy = store.agents.filter { store.isBusy($0.id) }.count
            return busy > 0 ? "\(busy) working…" : plural(store.agents.count, "profile")
        }
        switch store.states[agentID] ?? "idle" {
        case "thinking": return "Thinking…"
        case "doing":
            if let tool = store.tools[agentID] { return "Working · \(tool.replacingOccurrences(of: "_", with: " "))" }
            return "Working…"
        default: return store.agentInfo[agentID].role.str
        }
    }

    private func readIMessage() async {
        guard let agentID, let settings = try? await store.api?.get("/api/settings") else { return }
        answersIMessage = settings.imessage_enabled.flag && settings.imessage_default_agent.str == agentID
    }
}

// MARK: The list

/// What the list draws: messages gathered the way they read, so a run of tool calls is one
/// row and a new day has its heading. Ids stay put as messages arrive, so only new rows draw.
private enum ChatRow: Identifiable, Equatable {
    case day(Message)
    case beats(first: Message, count: Int, tools: [Message])
    case tools([Message])
    case job(Message)
    case proposal(Message)
    case event(Message)
    case relay(Message)
    case message(Message, first: Bool)

    var id: String {
        switch self {
        case .day(let message): return "day-\(message.id)"
        case .beats(let first, _, _): return "beats-\(first.id)"
        case .tools(let group): return "tools-\(group.first?.id ?? 0)"
        case .job(let message), .proposal(let message), .event(let message), .relay(let message): return "row-\(message.id)"
        case .message(let message, _): return "message-\(message.id)"
        }
    }

    var isMessage: Bool {
        if case .message = self { return true }
        return false
    }

    func isMessage(id: Int?) -> Bool {
        if case .message(let message, _) = self { return message.id == id }
        return false
    }

    private static func isBeat(_ message: Message) -> Bool { message.kind == "sys" && message.sender == "heartbeat" }

    static func rows(_ messages: [Message], inDirectChat: Bool) -> [ChatRow] {
        var rows: [ChatRow] = []
        rows.reserveCapacity(messages.count + 8)
        var previous: Message?
        var index = 0
        while index < messages.count {
            let message = messages[index]
            if previous.map({ message.ts - $0.ts > 1800 }) ?? true { rows.append(.day(message)) }
            if isBeat(message) {
                // A run of pulses, with the tool calls they set off, is one line.
                var count = 1
                var tools: [Message] = []
                while index + 1 < messages.count, isBeat(messages[index + 1]) || messages[index + 1].kind == "tool" {
                    index += 1
                    if messages[index].kind == "tool" { tools.append(messages[index]) } else { count += 1 }
                }
                rows.append(.beats(first: message, count: count, tools: tools))
            } else if message.kind == "tool" {
                var group = [message]
                while index + 1 < messages.count, messages[index + 1].kind == "tool", messages[index + 1].sender == message.sender,
                      messages[index + 1].ts - messages[index].ts < 1800 {
                    index += 1
                    group.append(messages[index])
                }
                rows.append(.tools(group))
            } else if message.kind == "approval" {
                rows.append(.job(message))
            } else if message.kind == "improvement" {
                rows.append(.proposal(message))
            } else if message.kind == "sys" {
                if message.sender != "setup" { rows.append(.event(message)) }
            } else if message.kind == "a2a", inDirectChat {
                rows.append(.relay(message))
            } else {
                let first = previous.map { $0.sender != message.sender || $0.kind != message.kind || message.ts - $0.ts > 300 } ?? true
                rows.append(.message(message, first: first))
            }
            previous = messages[index]
            index += 1
        }
        return rows
    }
}

private struct MessageList: View {
    @Environment(Store.self) private var store
    let model: ThreadModel
    let threadID: String
    @State private var follow = Follow()

    /// Held outside SwiftUI's state: it flips while scrolling, and nothing it decides is drawn.
    final class Follow { var atFoot = true }
    private static let foot = "chat-foot"

    var body: some View {
        let rows = ChatRow.rows(model.messages, inDirectChat: threadID.hasPrefix("dm:"))
        let anchor = model.anchor
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    if model.hasMore {
                        OlderRow(model: model) { reached in
                            // With the foot in view too, the list has only just opened or fits
                            // the window, so it waits to be asked rather than jumping away.
                            if !reached || !follow.atFoot { loadOlder(proxy, keeping: rows) }
                        }
                    }
                    if rows.isEmpty, !model.hasMore, model.loaded { ThreadEmpty(threadID: threadID) }
                    ForEach(rows) { row in
                        ChatRowView(row: row, threadID: threadID, hit: row.isMessage(id: anchor)).equatable()
                    }
                    if model.atLatest {
                        LiveReplies(threadID: threadID) { if follow.atFoot { toFoot(proxy) } }
                    }
                    Color.clear.frame(height: 1).id(Self.foot)
                        .onAppear { follow.atFoot = true }
                        .onDisappear { follow.atFoot = false }
                }
                .padding(.horizontal, 20).padding(.vertical, 16)
                .frame(maxWidth: 820, alignment: .leading)
                .frame(maxWidth: .infinity)
            }
            .defaultScrollAnchor(.bottom)
            .id("\(threadID)#\(model.generation)")
            .onChange(of: model.messages.last?.id) {
                // Someone reading further up keeps their place, unless they just sent this.
                if follow.atFoot || model.messages.last?.sender == "user" { toFoot(proxy) }
            }
        }
    }

    private func toFoot(_ proxy: ScrollViewProxy) {
        DispatchQueue.main.async { proxy.scrollTo(Self.foot, anchor: .bottom) }
    }

    /// Older messages arrive above the reader, so the row they were looking at goes back to
    /// where it was once those are in.
    @MainActor private func loadOlder(_ proxy: ScrollViewProxy, keeping rows: [ChatRow]) {
        guard let api = store.api, !model.loading else { return }
        let keep = rows.first(where: \.isMessage)?.id ?? rows.first?.id
        Task {
            await model.loadOlder(api)
            guard let keep else { return }
            await Task.yield()
            proxy.scrollTo(keep, anchor: .top)
        }
    }
}

private struct OlderRow: View {
    let model: ThreadModel
    let load: (_ reached: Bool) -> Void

    var body: some View {
        Group {
            if model.loading {
                Text("Loading earlier messages…").font(.callout).foregroundStyle(.secondary)
            } else {
                Button("Earlier messages") { load(false) }.buttonStyle(.link).font(.callout)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 6)
        .onAppear { load(true) }
    }
}

private struct ThreadEmpty: View {
    @Environment(Store.self) private var store
    let threadID: String

    var body: some View {
        let agentID = threadID.hasPrefix("dm:") ? String(threadID.dropFirst(3)) : nil
        VStack(spacing: 8) {
            if let agentID {
                AgentBadge(agent: store.agent(agentID), size: 56)
            } else {
                Image(systemName: "person.3").font(.system(size: 28)).foregroundStyle(.secondary).accessibilityHidden(true)
            }
            Text(verbatim: agentID.map { store.name($0) } ?? "Group chat").font(.headline)
            Text(verbatim: detail(agentID)).font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 60)
    }

    private func detail(_ agentID: String?) -> String {
        guard let agentID else { return "Messages here reach the agent, or any profile you @mention." }
        let role = store.agentInfo[agentID].role.str
        return role.isEmpty ? "Ready when you are." : role
    }
}

/// A view of the past does not take new messages, so this is the way back to the present.
private struct LatestButton: View {
    @Environment(Store.self) private var store
    let model: ThreadModel

    var body: some View {
        if !model.atLatest {
            AsyncButton(action: { if let api = store.api { await model.load(api) } }) {
                HStack(spacing: 6) {
                    if model.missedNew { Circle().fill(Color.accentColor).frame(width: 6, height: 6).accessibilityHidden(true) }
                    Text("Latest")
                    Image(systemName: "arrow.down").font(.system(size: 11, weight: .semibold))
                }
                .font(.system(size: 12.5))
                .foregroundStyle(model.missedNew ? Color.primary : Color.secondary)
                .padding(.horizontal, 12).frame(height: 30)
                .glassBackground(cornerRadius: 15)
                .contentShape(Capsule())
            }
            .buttonStyle(.plain)
            .shadow(color: .black.opacity(0.14), radius: 8, y: 3)
            .help("Jump to the latest messages")
            .accessibilityLabel(model.missedNew ? "Latest, new messages" : "Latest")
        }
    }
}

/// Equatable, so a new message at the foot of the list does not redraw the ones above it.
private struct ChatRowView: View, Equatable {
    let row: ChatRow
    let threadID: String
    let hit: Bool
    nonisolated static func == (lhs: ChatRowView, rhs: ChatRowView) -> Bool { lhs.row == rhs.row && lhs.hit == rhs.hit }

    var body: some View {
        switch row {
        case .day(let message): DayRow(ts: message.ts)
        case .beats(_, let count, let tools): BeatRow(count: count, tools: tools)
        case .tools(let group): ToolGroupRow(group: group)
        case .job(let message): JobCard(message: message)
        case .proposal(let message): ProposalCard(message: message)
        case .event(let message): EventRow(message: message)
        case .relay(let message): RelayRow(message: message, threadID: threadID)
        case .message(let message, let first): MessageRow(message: message, first: first, hit: hit)
        }
    }
}

/// How times read in a chat and in search results.
enum ChatTime {
    private static let clock: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateStyle = .none
        formatter.timeStyle = .short
        return formatter
    }()
    private static let monthDay: DateFormatter = {
        let formatter = DateFormatter()
        formatter.setLocalizedDateFormatFromTemplate("MMMd")
        return formatter
    }()

    static func time(_ ts: Double) -> String { clock.string(from: Date(timeIntervalSince1970: ts)) }

    static func day(_ ts: Double) -> String {
        let date = Date(timeIntervalSince1970: ts)
        if Calendar.current.isDateInToday(date) { return "Today" }
        if Calendar.current.isDateInYesterday(date) { return "Yesterday" }
        return monthDay.string(from: date)
    }

    /// The time today, otherwise the day.
    static func shortDay(_ ts: Double) -> String {
        Calendar.current.isDateInToday(Date(timeIntervalSince1970: ts)) ? time(ts) : day(ts)
    }
}

private struct DayRow: View {
    let ts: Double
    var body: some View {
        HStack(spacing: 4) {
            Text(verbatim: ChatTime.day(ts)).fontWeight(.semibold)
            Text(verbatim: ChatTime.time(ts))
        }
        .font(.system(size: 11))
        .foregroundStyle(.secondary)
        .frame(maxWidth: .infinity)
        .padding(.top, 18).padding(.bottom, 2)
    }
}

private struct SenderHead: View {
    @Environment(Store.self) private var store
    let sender: String
    var ts: Double = 0

    var body: some View {
        HStack(spacing: 10) {
            if let agent = store.agent(sender) {
                AgentBadge(agent: agent, size: 24)
            } else {
                Image(systemName: sender == "control" ? "terminal" : "gearshape")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .frame(width: 24, height: 24)
                    .accessibilityHidden(true)
            }
            HStack(spacing: 6) {
                Text(verbatim: store.name(sender)).font(.system(size: 12, weight: .semibold))
                if ts > 0 { Text(verbatim: ChatTime.time(ts)).font(.system(size: 11)).foregroundStyle(.tertiary) }
            }
        }
    }
}

private struct MessageRow: View {
    let message: Message
    let first: Bool
    let hit: Bool

    /// What the server stores for a message that was only a file says nothing beside the file.
    private var words: String {
        message.text == "(sent a file)" && !message.meta.attachments.list.isEmpty ? "" : message.text
    }

    var body: some View {
        Group {
            if message.sender == "user" {
                HStack {
                    Spacer(minLength: 80)
                    VStack(alignment: .trailing, spacing: 6) {
                        AttachmentList(meta: message.meta)
                        if !words.isEmpty {
                            MarkdownView(text: words)
                                .padding(.horizontal, 12).padding(.vertical, 8)
                                .background(Color.accentColor.opacity(0.16), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                        }
                    }
                    .modifier(HitMark(on: hit))
                }
                .help(ChatTime.time(message.ts))
            } else {
                VStack(alignment: .leading, spacing: 5) {
                    if first { SenderHead(sender: message.sender, ts: message.ts) }
                    VStack(alignment: .leading, spacing: 6) {
                        AttachmentList(meta: message.meta)
                        if let approval = message.meta.approval.optStr {
                            ApprovalCard(id: approval, fallback: words).frame(maxWidth: 620, alignment: .leading)
                        } else {
                            MarkdownView(text: words)
                        }
                    }
                    .modifier(HitMark(on: hit))
                    .padding(.leading, 34)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.top, first ? 14 : 5)
    }
}

/// The message a search hit opened at.
private struct HitMark: ViewModifier {
    let on: Bool

    @ViewBuilder func body(content: Content) -> some View {
        if on {
            content
                .padding(5)
                .background(MC.warn.opacity(0.12), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(MC.warn.opacity(0.55), lineWidth: 1.5))
                .accessibilityHint("The message your search found")
        } else {
            content
        }
    }
}

/// Between two profiles, as seen from one profile's chat.
private struct RelayRow: View {
    @Environment(Store.self) private var store
    let message: Message
    let threadID: String

    var body: some View {
        let own = String(threadID.dropFirst(3))
        let sent = message.sender == own
        let other = sent ? message.meta.to.str : message.sender
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                Text(sent ? "Messaged" : "From").foregroundStyle(.secondary)
                AgentBadge(agent: store.agent(other), size: 18)
                Text(verbatim: store.name(other)).fontWeight(.medium)
                Text(verbatim: ChatTime.time(message.ts)).foregroundStyle(.tertiary)
            }
            .font(.system(size: 11))
            MarkdownView(text: message.text, size: 12.5)
                .padding(.horizontal, 10).padding(.vertical, 7)
                .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        }
        .padding(.top, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct BeatRow: View {
    let count: Int
    let tools: [Message]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 6) {
                Image(systemName: "heart.fill").font(.system(size: 9)).foregroundStyle(MC.danger).accessibilityHidden(true)
                Text(verbatim: tools.isEmpty ? plural(count, "heartbeat") : "\(plural(count, "heartbeat")) · \(plural(tools.count, "step"))")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
            .padding(.leading, 34).padding(.top, 8)
            if !tools.isEmpty { ToolGroupRow(group: tools) }
        }
    }
}

/// A run of steps as one line. Claude Code runs are worth watching, so those start open.
private struct ToolGroupRow: View {
    let group: [Message]
    @State private var toggled: Bool?

    private func line(_ step: Message) -> String {
        let text = String(step.text.prefix(400))
        return step.meta.tool.str == "terminal" ? "$ \(text)" : text
    }

    var body: some View {
        let open = toggled ?? group.contains { $0.meta.tool.str == "claude" }
        VStack(alignment: .leading, spacing: 5) {
            Button { toggled = !open } label: {
                HStack(spacing: 6) {
                    Image(systemName: "bolt").font(.system(size: 10)).accessibilityHidden(true)
                    Text(verbatim: plural(group.count, "step")).font(.system(size: 11, weight: .medium))
                    Image(systemName: open ? "chevron.down" : "chevron.right").font(.system(size: 8, weight: .bold)).accessibilityHidden(true)
                }
                .foregroundStyle(.secondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(open ? "Open" : "Closed")
            if open {
                LazyVStack(alignment: .leading, spacing: 3) {
                    ForEach(group) { step in
                        Text(verbatim: line(step))
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .textSelection(.enabled)
                .padding(9)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            }
        }
        .padding(.leading, 34).padding(.top, 8)
    }
}

/// System events are written for the agent they wake: an approval's output, a nudge, a
/// watch result. The owner gets one readable line, with the full text behind a disclosure.
private struct EventSummary {
    let gist: String
    let detail: String

    init(_ message: Message) {
        let text = message.text.trimmingCharacters(in: .whitespacesAndNewlines)
        let firstBreak = text.range(of: #"\n\s*\n"#, options: .regularExpression)
        switch message.sender {
        case "approval":
            let command = text.range(of: "`[^`]+`", options: .regularExpression).map { String(text[$0].dropFirst().dropLast()) }
            let ran = text.contains("and it has been run")
            let rest = firstBreak.map { String(text[$0.upperBound...]) } ?? ""
            gist = "you approved " + (command.map { "`\(clip($0, 60))`" } ?? "a command") + (ran ? ", and it ran" : "")
            detail = ran ? rest.replacingOccurrences(of: #"\s*Carry on from here, or reply exactly NO_REPLY\.?$"#, with: "", options: .regularExpression) : ""
        case "watchdog":
            gist = text
            detail = ""
        case "liveness":
            let marker = "The last thing the owner asked was:"
            if let found = text.range(of: marker) {
                let asked = text[found.upperBound...].trimmingCharacters(in: .whitespacesAndNewlines)
                let firstPart = asked.range(of: "\n\n").map { String(asked[..<$0.lowerBound]) } ?? asked
                gist = "nudged to finish: " + clip(firstPart, 140)
            } else {
                gist = "nudged to finish the last request"
            }
            detail = text
        default:
            gist = clip(firstBreak.map { String(text[..<$0.lowerBound]) } ?? text, 180)
            detail = gist == text ? "" : text
        }
    }
}

private struct EventRow: View {
    @Environment(Store.self) private var store
    let message: Message
    @State private var open = false

    var body: some View {
        let summary = EventSummary(message)
        VStack(alignment: .leading, spacing: 5) {
            if summary.detail.isEmpty {
                line(summary.gist)
            } else {
                Button { open.toggle() } label: {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        line(summary.gist)
                        Image(systemName: open ? "chevron.down" : "chevron.right").font(.system(size: 8, weight: .bold)).accessibilityHidden(true)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityValue(open ? "Open" : "Closed")
                if open {
                    Text(verbatim: summary.detail)
                        .font(.system(size: 11, design: .monospaced))
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(9)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                }
            }
        }
        .foregroundStyle(.secondary)
        .padding(.leading, 34).padding(.top, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func line(_ gist: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(verbatim: store.name(message.sender)).font(.system(size: 11, weight: .medium))
            Text(Markdown.inline(gist)).font(.system(size: 12)).fixedSize(horizontal: false, vertical: true)
        }
    }
}

// MARK: Decisions in the chat

/// A recurring job asking to start.
private struct JobCard: View {
    @Environment(Store.self) private var store
    let message: Message

    var body: some View {
        let meta = message.meta
        let jobID = meta.job_id.str
        let job = store.jobs.first { $0.id == jobID }
        let status = job?.status.str ?? "removed"
        let monitor = job?.monitor_cmd.str ?? ""
        Card(padding: 12) {
            VStack(alignment: .leading, spacing: 6) {
                Label("Scheduled job wants your approval", systemImage: "clock")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(.secondary)
                Text(verbatim: meta.name.str).font(.system(size: 13, weight: .semibold))
                Text(verbatim: "\(store.name(meta.agent.str)) · \(meta.schedule.str)").font(.system(size: 12)).foregroundStyle(.secondary)
                Text(verbatim: meta.task.str).font(.system(size: 12.5)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                if !monitor.isEmpty {
                    Text("Before every run it executes this check in your shell:").font(.system(size: 12)).foregroundStyle(.secondary)
                    WholeCode(text: monitor)
                }
                if status == "pending" {
                    HStack(spacing: 8) {
                        Spacer()
                        AsyncButton("Reject") { try await decide(jobID, "delete") }
                        AsyncButton("Approve") { try await decide(jobID, "approve") }.buttonStyle(.borderedProminent)
                    }
                    .disabled(!store.live)
                    .padding(.top, 4)
                } else {
                    Pill(status: status == "removed" ? "denied" : status, label: status == "removed" ? "rejected" : status)
                }
            }
        }
        .padding(.top, 12)
    }

    private func decide(_ jobID: String, _ action: String) async throws {
        guard let api = store.api, store.live else { throw APIError(message: "Still reading the current state. Try again in a moment.") }
        _ = try await api.post("/api/cron/\(API.escape(jobID))/\(action)")
        store.refreshSoon(after: 0.05)
    }
}

/// Code that is approved as shown, so all of it is on screen, wrapped, never folded.
private struct WholeCode: View {
    let text: String
    var body: some View {
        Text(verbatim: text)
            .font(.system(size: 11.5, design: .monospaced))
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
            .padding(9)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

/// Where each proposed change stands, read once however many of them a chat shows.
@Observable @MainActor
private final class ProposalBook {
    static let shared = ProposalBook()

    private(set) var statuses: [String: String] = [:]
    private(set) var loaded = false
    @ObservationIgnored private var reading = false
    @ObservationIgnored private var readTick = -1
    @ObservationIgnored private var base = ""

    func load(_ api: API, tick: Int) async {
        guard !reading, readTick != tick || base != api.base else { return }
        reading = true; defer { reading = false }
        guard let reply = try? await api.get("/api/improvements") else { return }
        readTick = tick
        base = api.base
        var next: [String: String] = [:]
        for row in reply.improvements.list { next[row.id] = row.status.str }
        if next != statuses { statuses = next }
        loaded = true
    }
}

/// A proposed change to Mission Control.
private struct ProposalCard: View {
    @Environment(Store.self) private var store
    let message: Message
    @State private var showingDiff = false
    @State private var confirming = false

    private static let wording = [
        "fixing": "fixing", "pending": "waiting on you", "applied": "landed", "pushed": "landed and pushed",
        "discarded": "discarded", "rolled_back": "rolled back", "failed": "failed", "rejected": "rejected", "removed": "removed",
    ]

    var body: some View {
        let meta = message.meta
        let id = meta.improvement_id.str
        let book = ProposalBook.shared
        let status = book.statuses[id] ?? (book.loaded ? "removed" : "")
        let files = meta.files.list.prefix(8).map(\.str)
        Card(padding: 12) {
            VStack(alignment: .leading, spacing: 6) {
                Label("A change to Mission Control wants your approval", systemImage: "arrow.triangle.branch")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(.secondary)
                Text(verbatim: meta.title.str).font(.system(size: 13, weight: .semibold))
                Text(verbatim: "\(store.name(meta.agent.str)) · \(plural(files.count, "file")) · +\(meta.added.int)/-\(meta.removed.int) · tests green")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                MarkdownView(text: meta.rationale.str, size: 12.5)
                Text(verbatim: files.joined(separator: "  "))
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if status == "pending" {
                    HStack(spacing: 8) {
                        Spacer()
                        Button("View diff") { showingDiff = true }
                        AsyncButton("Reject") {
                            guard let api = store.api, store.live else { throw APIError(message: "Still reading the current state. Try again in a moment.") }
                            _ = try await api.post("/api/improvements/\(API.escape(id))/reject")
                            store.refreshSoon(after: 0.05)
                        }
                        Button("Approve and restart") { confirming = true }.buttonStyle(.borderedProminent)
                    }
                    .disabled(!store.live)
                    .padding(.top, 4)
                } else if !status.isEmpty {
                    HStack(spacing: 8) {
                        Pill(status: status == "removed" ? "denied" : status, label: Self.wording[status] ?? status)
                        if status != "fixing", status != "discarded", status != "removed" {
                            Button("View diff") { showingDiff = true }.buttonStyle(.link).font(.system(size: 12))
                        }
                    }
                }
            }
        }
        .padding(.top, 12)
        .task(id: store.ticks.improvements) {
            if let api = store.api { await book.load(api, tick: store.ticks.improvements) }
        }
        .sheet(isPresented: $showingDiff) { ProposalDiffSheet(id: id) }
        .confirmationDialog("Approve and restart?", isPresented: $confirming, titleVisibility: .visible) {
            Button("Approve and restart") { Task { await approve(id) } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Mission Control restarts on the new code. If it does not come back, it rolls back by itself.")
        }
    }

    /// The server goes down as part of answering, so losing the connection here is the
    /// change going in, not it failing.
    private func approve(_ id: String) async {
        guard let api = store.api, store.live else {
            store.notify("Still reading the current state. Try again in a moment.", error: true)
            return
        }
        do {
            _ = try await api.post("/api/improvements/\(API.escape(id))/approve")
        } catch let error as URLError where error.code == .timedOut || error.code == .networkConnectionLost {
        } catch {
            store.notify(error.localizedDescription, error: true)
            return
        }
        store.notify("Applying the change and restarting. If it does not come back, it rolls back by itself.")
        store.refreshSoon(after: 0.05)
    }
}

// MARK: Attachments

private func attachmentData(_ path: String, _ api: API) async throws -> Data {
    if path.hasPrefix("http"), let url = URL(string: path) { return try await URLSession.shared.data(from: url).0 }
    return try await api.getRaw(path.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? path).data
}

private struct AttachmentList: View {
    let meta: JSON

    var body: some View {
        let files = meta.attachments.list
        if !files.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(files.enumerated()), id: \.offset) { _, file in AttachmentView(file: file) }
            }
        }
    }
}

/// A click shows the file in Quick Look. An agent can attach anything, and opening a script
/// in its default app could run it, so opening it for real stays one deliberate step away.
private struct AttachmentView: View {
    @Environment(Store.self) private var store
    let file: JSON
    @State private var previewing: URL?

    /// Stored names begin with eight hex digits that keep them apart on disk.
    private var name: String {
        let shown = file.name.str.replacingOccurrences(of: "^[0-9a-f]{8}-", with: "", options: .regularExpression)
        return shown.isEmpty ? "File" : (shown as NSString).lastPathComponent
    }

    var body: some View {
        Group {
            if file.kind.str == "image" {
                Button(action: view) { AttachmentThumbnail(path: file.url.str) }.buttonStyle(.plain)
            } else {
                Button(action: view) { Label(name, systemImage: "paperclip").font(.system(size: 12)) }.buttonStyle(.link)
            }
        }
        .help(name)
        .accessibilityLabel(file.kind.str == "image" ? "Image \(name)" : "File \(name)")
        .contextMenu {
            Button("View", action: view)
            Button("Download…", action: download)
        }
        .quickLookPreview($previewing)
    }

    private func read() async throws -> Data {
        guard let api = store.api else { throw APIError(message: "Not connected to the runtime yet.") }
        return try await attachmentData(file.url.str, api)
    }

    private func view() {
        let folder = (file.name.str as NSString).lastPathComponent
        let shown = name
        Task {
            do {
                let data = try await read()
                previewing = try await Task.detached(priority: .userInitiated) {
                    let directory = FileManager.default.temporaryDirectory
                        .appendingPathComponent("MissionControl Attachments", isDirectory: true)
                        .appendingPathComponent(folder.isEmpty ? UUID().uuidString : folder, isDirectory: true)
                    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
                    let copy = directory.appendingPathComponent(shown)
                    try data.write(to: copy, options: .atomic)
                    return copy
                }.value
            } catch {
                store.notify(error.localizedDescription, error: true)
            }
        }
    }

    private func download() {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = name
        panel.begin { response in
            guard response == .OK, let target = panel.url else { return }
            Task { @MainActor in
                do {
                    let data = try await read()
                    try await Task.detached(priority: .userInitiated) { try data.write(to: target, options: .atomic) }.value
                    store.notify("Saved \(target.lastPathComponent).")
                } catch {
                    store.notify(error.localizedDescription, error: true)
                }
            }
        }
    }
}

/// Decoded small and off the main thread, and kept, so an image scrolling back into view
/// draws at once instead of being read again.
@MainActor
private enum Thumbnails {
    private static let cache: NSCache<NSString, NSImage> = {
        let cache = NSCache<NSString, NSImage>()
        cache.countLimit = 200
        return cache
    }()

    /// Never changed after it is made, so it is safe to hand across.
    private struct Decoded: @unchecked Sendable { let image: CGImage }

    static func cached(_ path: String) -> NSImage? { cache.object(forKey: path as NSString) }

    static func load(_ path: String, api: API) async -> NSImage? {
        if let hit = cached(path) { return hit }
        guard let data = try? await attachmentData(path, api) else { return nil }
        let decoded = await Task.detached(priority: .userInitiated) { () -> Decoded? in
            guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
            let options: [CFString: Any] = [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceShouldCacheImmediately: true,
                kCGImageSourceThumbnailMaxPixelSize: 960,
            ]
            return CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary).map(Decoded.init)
        }.value
        guard let decoded else { return nil }
        let image = NSImage(cgImage: decoded.image, size: NSSize(width: decoded.image.width, height: decoded.image.height))
        cache.setObject(image, forKey: path as NSString)
        return image
    }
}

private struct AttachmentThumbnail: View {
    @Environment(Store.self) private var store
    let path: String
    @State private var image: NSImage?

    var body: some View {
        Group {
            if let shown = image ?? Thumbnails.cached(path) {
                Image(nsImage: shown).resizable().scaledToFit().frame(maxWidth: 320, maxHeight: 240)
            } else {
                Color.primary.opacity(0.05).frame(width: 200, height: 140)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .task(id: path) {
            if image == nil, let api = store.api { image = await Thumbnails.load(path, api: api) }
        }
    }
}

// MARK: Replies being written

/// The reply as it is written, or a quiet note that it is being worked on.
private struct LiveReplies: View {
    @Environment(Store.self) private var store
    let threadID: String
    let onGrow: () -> Void

    /// Work a profile does for another profile shows in its own chat too.
    private func shows(_ thread: String, agent: String) -> Bool {
        thread == threadID || (threadID == "dm:\(agent)" && thread.hasPrefix("a2a:"))
    }

    var body: some View {
        let live = store.streams.values.filter { shows($0.thread, agent: $0.agent) }.sorted { $0.agent < $1.agent }
        let writing = Set(live.map(\.agent))
        let waiting = store.typing.compactMap { key -> String? in
            let parts = key.split(separator: "|", maxSplits: 1).map(String.init)
            guard parts.count == 2, shows(parts[1], agent: parts[0]), !writing.contains(parts[0]) else { return nil }
            return parts[0]
        }
        ForEach(live, id: \.agent) { entry in
            VStack(alignment: .leading, spacing: 5) {
                SenderHead(sender: entry.agent)
                Group {
                    if entry.text.isEmpty {
                        // The model's reasoning before it starts the answer: the tail of it, dimmed.
                        Text(verbatim: entry.thinking.count > 600 ? "…" + entry.thinking.suffix(600) : entry.thinking)
                            .font(.system(size: 12))
                            .foregroundStyle(.tertiary)
                            .fixedSize(horizontal: false, vertical: true)
                    } else {
                        MarkdownView(text: entry.text, ink: true)
                    }
                }
                .padding(.leading, 34)
            }
            .padding(.top, 14)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        ForEach(Array(Set(waiting)).sorted(), id: \.self) { id in
            HStack(spacing: 10) {
                AgentBadge(agent: store.agent(id), size: 24)
                ProgressView().controlSize(.small)
                Text(verbatim: store.tools[id].map { "Running \($0.replacingOccurrences(of: "_", with: " "))" } ?? "Thinking…")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            }
            .padding(.top, 14)
        }
        Color.clear.frame(height: 0)
            .onChange(of: live.reduce(0) { $0 + $1.text.utf8.count + $1.thinking.utf8.count }) { onGrow() }
    }
}

// MARK: Dropping files

/// Files can land anywhere in the conversation. The highlight is kept to this modifier, so
/// dragging across the window does not redraw the messages.
private struct FileDropZone: ViewModifier {
    @Environment(Store.self) private var store
    let threadID: String
    @State private var targeted = false

    func body(content: Content) -> some View {
        content
            .dropDestination(for: URL.self) { urls, _ in
                let files = urls.filter(\.isFileURL)
                guard !files.isEmpty else { return false }
                ComposerDraft.draft(for: threadID).attach(files, store)
                return true
            } isTargeted: { targeted = $0 }
            .overlay {
                if targeted {
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .strokeBorder(Color.accentColor, style: StrokeStyle(lineWidth: 2, dash: [6, 4]))
                        .background(Color.accentColor.opacity(0.06), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                        .overlay { Text("Drop to attach").font(.headline).foregroundStyle(Color.accentColor) }
                        .padding(8)
                        .allowsHitTesting(false)
                }
            }
    }
}

// MARK: The composer

/// What is being written in one conversation. It outlives the screen, so a half-written
/// message and its files survive a look elsewhere. The text view holds the text while it is
/// on screen and reports each change here; the suggestions are worked out from it too,
/// because they depend on where the caret is.
@Observable @MainActor
private final class ComposerDraft {
    private static var drafts: [String: ComposerDraft] = [:]

    static func draft(for threadID: String) -> ComposerDraft {
        if let draft = drafts[threadID] { return draft }
        let draft = ComposerDraft(threadID: threadID)
        drafts[threadID] = draft
        return draft
    }

    struct PendingFile: Identifiable {
        let id = UUID()
        let name: String
        var attachment: JSON?
    }

    struct Option: Identifiable, Equatable {
        let id: String, title: String, detail: String, insert: String
        var agent: Agent?
        /// A command that takes nothing after its name, so Return can simply send it.
        var complete = false
    }

    static let minHeight: CGFloat = 20
    static let maxHeight: CGFloat = 130
    /// The server keeps this many on a message and silently drops the rest.
    private static let maxFiles = 6

    let threadID: String
    private(set) var text = ""
    var height = ComposerDraft.minHeight
    private(set) var files: [PendingFile] = []
    private(set) var options: [Option] = []
    private(set) var selected = 0
    @ObservationIgnored weak var editor: NSTextView?
    @ObservationIgnored private var tokenRange = NSRange(location: 0, length: 0)
    @ObservationIgnored private var dismissed = false

    private init(threadID: String) { self.threadID = threadID }

    var uploading: Bool { files.contains { $0.attachment == nil } }
    var canSend: Bool {
        !uploading && (!files.isEmpty || !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
    }

    // MARK: Text

    func textChanged(_ new: String) {
        text = new
        dismissed = false
    }

    /// Through the text view's own editing, so Undo still works after a send.
    private func replaceAll(with new: String) {
        guard let editor else { text = new; return }
        let whole = NSRange(location: 0, length: (editor.string as NSString).length)
        if editor.shouldChangeText(in: whole, replacementString: new) {
            editor.replaceCharacters(in: whole, with: new)
            editor.didChangeText()
        }
    }

    func send(_ store: Store) {
        guard canSend else { return }
        let words = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let sending = files
        files = []
        replaceAll(with: "")
        Task {
            let result = await store.send(words, to: threadID, attachments: sending.compactMap(\.attachment))
            // The server did not take it, so it comes back rather than being lost.
            if result == nil {
                if text.isEmpty { replaceAll(with: words) }
                files = sending + files
            }
        }
    }

    // MARK: Files

    /// Each file goes up on its own, off the main thread, and the message can be written
    /// meanwhile. It cannot be sent until they are all up.
    func attach(_ urls: [URL], _ store: Store) {
        guard let api = store.api else {
            store.notify("Not connected to the runtime yet.", error: true)
            return
        }
        for url in urls {
            guard files.count < Self.maxFiles else {
                store.notify("A message can carry \(Self.maxFiles) files at most.", error: true)
                return
            }
            let file = PendingFile(name: url.lastPathComponent)
            files.append(file)
            Task {
                do {
                    let attachment = try await api.upload(file: url)
                    if let index = files.firstIndex(where: { $0.id == file.id }) { files[index].attachment = attachment }
                } catch {
                    files.removeAll { $0.id == file.id }
                    store.notify("Could not attach \(file.name): \(error.localizedDescription)", error: true)
                }
            }
        }
    }

    func remove(_ file: PendingFile) { files.removeAll { $0.id == file.id } }

    // MARK: Suggestions

    private struct Token {
        let isCommand: Bool
        let range: NSRange
        let query: String
    }

    /// The word the caret is in, when it asks for a suggestion. A command only counts at the
    /// very start of the message, since that is the only place the server reads one.
    private static func token(in text: NSString, caret: Int) -> Token? {
        guard caret <= text.length else { return nil }
        func isSpace(_ unit: unichar) -> Bool {
            guard let scalar = UnicodeScalar(unit) else { return false }
            return CharacterSet.whitespacesAndNewlines.contains(scalar)
        }
        var start = caret
        while start > 0, !isSpace(text.character(at: start - 1)) { start -= 1 }
        var end = caret
        while end < text.length, !isSpace(text.character(at: end)) { end += 1 }
        let typed = text.substring(with: NSRange(location: start, length: caret - start))
        let range = NSRange(location: start, length: end - start)
        if typed.hasPrefix("/"), start == 0 { return Token(isCommand: true, range: range, query: String(typed.dropFirst())) }
        if typed.hasPrefix("@") { return Token(isCommand: false, range: range, query: String(typed.dropFirst())) }
        return nil
    }

    func updateOptions(_ store: Store) {
        guard !dismissed, let editor, editor.selectedRange().length == 0,
              let token = Self.token(in: editor.string as NSString, caret: editor.selectedRange().location) else {
            closeOptions()
            return
        }
        tokenRange = token.range
        let next = token.isCommand ? commandOptions(token.query.lowercased()) : mentionOptions(token.query.lowercased(), store)
        if next != options {
            options = next
            selected = 0
        }
    }

    private func commandOptions(_ query: String) -> [Option] {
        let commands = CommandCatalog.shared.commands
        let starting = commands.filter { $0.name.str.hasPrefix(query) }
        let containing = commands.filter { !$0.name.str.hasPrefix(query) && $0.name.str.contains(query) }
        return (starting + containing).map { command in
            let name = "/\(command.name.str)"
            return Option(id: name, title: name, detail: command.description.str, insert: "\(name) ", complete: command.usage.str == name)
        }
    }

    /// The id goes into the message, because that is the form a command reads, and the
    /// server recognises it in ordinary messages too.
    private func mentionOptions(_ query: String, _ store: Store) -> [Option] {
        store.agents.filter { agent in
            guard !query.isEmpty else { return true }
            let name = agent.name.lowercased()
            return agent.id.lowercased().hasPrefix(query) || name.hasPrefix(query) || name.contains(" \(query)")
        }
        .map { Option(id: $0.id, title: $0.name, detail: "@\($0.id)", insert: "@\($0.id) ", agent: $0) }
    }

    func closeOptions() {
        if !options.isEmpty {
            options = []
            selected = 0
        }
    }

    func dismissOptions() {
        dismissed = true
        closeOptions()
    }

    func moveSelection(_ delta: Int) -> Bool {
        guard !options.isEmpty else { return false }
        selected = (selected + delta + options.count) % options.count
        return true
    }

    func accept(_ option: Option) {
        guard let editor else { return }
        editor.insertText(option.insert, replacementRange: tokenRange)
        editor.window?.makeFirstResponder(editor)
    }

    func acceptSelected() -> Bool {
        guard options.indices.contains(selected) else { return false }
        accept(options[selected])
        return true
    }

    /// Return takes the highlighted suggestion, unless it is a whole command already written
    /// out that needs nothing more, which should simply send.
    func acceptOnReturn() -> Bool {
        guard options.indices.contains(selected), let editor else { return false }
        let option = options[selected]
        if option.complete, (editor.string as NSString).substring(with: tokenRange) == option.title { return false }
        return acceptSelected()
    }
}

struct ChatComposer: View {
    @Environment(Store.self) private var store
    let threadID: String

    private var placeholder: String {
        threadID.hasPrefix("dm:") ? "Message \(store.name(String(threadID.dropFirst(3))))" : "Message the agent, or @mention a profile"
    }

    var body: some View {
        let draft = ComposerDraft.draft(for: threadID)
        VStack(alignment: .leading, spacing: 8) {
            PendingFiles(draft: draft)
            ComposerField(draft: draft, placeholder: placeholder)
            ComposerFoot(draft: draft)
        }
        .padding(10)
        .background(MC.surface, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(MC.line.opacity(0.7), lineWidth: 0.5))
        .overlay(alignment: .top) {
            ComposerOptions(draft: draft).alignmentGuide(.top) { $0[.bottom] + 6 }
        }
        .padding(.horizontal, 20).padding(.vertical, 12)
        .frame(maxWidth: 860)
        .frame(maxWidth: .infinity)
        .task {
            await CommandCatalog.shared.load(store.api)
            draft.updateOptions(store)
        }
    }
}

private struct ComposerField: View {
    @Environment(Store.self) private var store
    let draft: ComposerDraft
    let placeholder: String

    var body: some View {
        ZStack(alignment: .topLeading) {
            if draft.text.isEmpty {
                Text(verbatim: placeholder)
                    .font(.system(size: 13))
                    .foregroundStyle(.tertiary)
                    .padding(.top, 2)
                    .allowsHitTesting(false)
                    .accessibilityHidden(true)
            }
            ComposerTextView(draft: draft, store: store, label: placeholder)
                .frame(height: draft.height)
        }
    }
}

private struct PendingFiles: View {
    let draft: ComposerDraft

    var body: some View {
        if !draft.files.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(draft.files) { file in
                        HStack(spacing: 5) {
                            Image(systemName: "paperclip").font(.system(size: 10)).foregroundStyle(.secondary).accessibilityHidden(true)
                            Text(verbatim: file.name).font(.system(size: 12)).lineLimit(1)
                            if file.attachment == nil {
                                ProgressView().controlSize(.mini).accessibilityLabel("Uploading")
                            } else {
                                Button { draft.remove(file) } label: {
                                    Image(systemName: "xmark").font(.system(size: 9, weight: .semibold))
                                }
                                .buttonStyle(.plain)
                                .foregroundStyle(.secondary)
                                .help("Remove")
                                .accessibilityLabel("Remove \(file.name)")
                            }
                        }
                        .padding(.horizontal, 9).frame(height: 24)
                        .background(Color.primary.opacity(0.07), in: Capsule())
                    }
                }
            }
        }
    }
}

private struct ComposerFoot: View {
    @Environment(Store.self) private var store
    let draft: ComposerDraft

    var body: some View {
        HStack(spacing: 8) {
            Button(action: pickFiles) {
                Image(systemName: "paperclip").font(.system(size: 14)).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .help("Attach a file")
            .accessibilityLabel("Attach a file")
            Text("Enter to send · Shift Enter for a new line")
                .font(.system(size: 11))
                .foregroundStyle(.tertiary)
                .lineLimit(1)
            Spacer(minLength: 8)
            Button { draft.send(store) } label: {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.system(size: 20))
                    .foregroundStyle(draft.canSend ? Color.accentColor : Color.secondary.opacity(0.4))
            }
            .buttonStyle(.plain)
            .disabled(!draft.canSend)
            .help("Send")
            .accessibilityLabel("Send")
        }
    }

    private func pickFiles() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true
        panel.prompt = "Attach"
        panel.begin { response in
            guard response == .OK else { return }
            let urls = panel.urls
            Task { @MainActor in draft.attach(urls, store) }
        }
    }
}

/// Commands or profiles to choose from, above the composer so nothing else moves. It reads
/// the suggestions itself, so moving through them redraws only this.
private struct ComposerOptions: View {
    let draft: ComposerDraft
    private static let rowHeight: CGFloat = 32

    var body: some View {
        if !draft.options.isEmpty {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 0) {
                        ForEach(Array(draft.options.enumerated()), id: \.element.id) { index, option in
                            row(option, selected: index == draft.selected).id(option.id)
                        }
                    }
                }
                .onChange(of: draft.selected) { _, index in
                    if draft.options.indices.contains(index) { proxy.scrollTo(draft.options[index].id) }
                }
            }
            .frame(height: min(CGFloat(draft.options.count), 6.5) * Self.rowHeight)
            .frame(maxWidth: .infinity)
            .background(MC.surface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(MC.line.opacity(0.8), lineWidth: 0.5))
            .shadow(color: .black.opacity(0.16), radius: 12, y: 3)
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Suggestions")
        }
    }

    private func row(_ option: ComposerDraft.Option, selected: Bool) -> some View {
        Button { draft.accept(option) } label: {
            HStack(spacing: 8) {
                if let agent = option.agent { AgentBadge(agent: agent, size: 20) }
                Text(verbatim: option.title).font(.system(size: 13, weight: .medium)).lineLimit(1).layoutPriority(1)
                Text(verbatim: option.detail).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 10)
            .frame(height: Self.rowHeight)
            .background(selected ? Color.accentColor.opacity(0.2) : Color.clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(option.title), \(option.detail)")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

/// A plain AppKit text view. Knowing the caret decides which word a suggestion replaces, and
/// its delegate is told about Return and the arrow keys before it acts on them, neither of
/// which a SwiftUI text field offers on this system. SwiftUI never writes the text back, so
/// typing in it costs the composer one redraw at most.
private struct ComposerTextView: NSViewRepresentable {
    let draft: ComposerDraft
    let store: Store
    let label: String

    func makeCoordinator() -> Coordinator { Coordinator(draft: draft, store: store) }

    func makeNSView(context: Context) -> NSScrollView {
        let view = Editor(usingTextLayoutManager: false)
        view.isRichText = false
        view.importsGraphics = false
        view.allowsUndo = true
        view.drawsBackground = false
        view.font = .systemFont(ofSize: 13)
        view.textColor = .labelColor
        view.textContainerInset = NSSize(width: 0, height: 2)
        view.textContainer?.lineFragmentPadding = 0
        view.isAutomaticQuoteSubstitutionEnabled = false
        view.isAutomaticDashSubstitutionEnabled = false
        view.isAutomaticTextReplacementEnabled = false
        view.isAutomaticLinkDetectionEnabled = false
        view.isAutomaticDataDetectionEnabled = false
        view.isVerticallyResizable = true
        view.isHorizontallyResizable = false
        view.autoresizingMask = [.width]
        // With nothing typed it still has to be tall enough to click on.
        view.minSize = NSSize(width: 0, height: ComposerDraft.minHeight)
        view.setFrameSize(NSSize(width: 0, height: ComposerDraft.minHeight))
        view.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        view.textContainer?.widthTracksTextView = true
        view.textContainer?.containerSize = NSSize(width: 0, height: CGFloat.greatestFiniteMagnitude)
        view.setAccessibilityLabel(label)
        view.string = draft.text

        let coordinator = context.coordinator
        view.delegate = coordinator
        view.onFiles = { [draft, store] urls in draft.attach(urls, store) }
        view.onWidthChange = { [weak view] in
            // This comes in the middle of layout, which is no time to change what SwiftUI draws.
            DispatchQueue.main.async { if let view { coordinator.measure(view) } }
        }
        draft.editor = view

        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.documentView = view
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        scroll.documentView?.setAccessibilityLabel(label)
    }

    final class Editor: NSTextView {
        var onFiles: (([URL]) -> Void)?
        var onWidthChange: (() -> Void)?
        private var wantsFocus = true
        private var lastWidth: CGFloat = 0

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            guard let window, wantsFocus else { return }
            wantsFocus = false
            DispatchQueue.main.async { [weak self] in
                if let self, self.window === window { window.makeFirstResponder(self) }
            }
        }

        override func setFrameSize(_ newSize: NSSize) {
            super.setFrameSize(newSize)
            if newSize.width != lastWidth {
                lastWidth = newSize.width
                onWidthChange?()
            }
        }

        // A file dropped on the field is an attachment, as it is anywhere else in the chat,
        // rather than its path typed into the message.
        private func files(_ sender: NSDraggingInfo) -> [URL] {
            let found = sender.draggingPasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true])
            return found as? [URL] ?? []
        }

        override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
            files(sender).isEmpty ? super.draggingEntered(sender) : .copy
        }

        override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
            files(sender).isEmpty ? super.draggingUpdated(sender) : .copy
        }

        override func prepareForDragOperation(_ sender: NSDraggingInfo) -> Bool {
            files(sender).isEmpty ? super.prepareForDragOperation(sender) : true
        }

        override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
            let dropped = files(sender)
            guard !dropped.isEmpty else { return super.performDragOperation(sender) }
            onFiles?(dropped)
            return true
        }
    }

    @MainActor
    final class Coordinator: NSObject, NSTextViewDelegate {
        let draft: ComposerDraft
        let store: Store

        init(draft: ComposerDraft, store: Store) {
            self.draft = draft
            self.store = store
        }

        func textDidChange(_ notification: Notification) {
            guard let view = notification.object as? NSTextView else { return }
            draft.textChanged(view.string)
            measure(view)
            draft.updateOptions(store)
        }

        func textViewDidChangeSelection(_ notification: Notification) {
            draft.updateOptions(store)
        }

        func textDidEndEditing(_ notification: Notification) {
            draft.closeOptions()
        }

        func textView(_ textView: NSTextView, doCommandBy selector: Selector) -> Bool {
            switch selector {
            case #selector(NSResponder.insertNewline(_:)):
                let modifiers = NSApp.currentEvent?.modifierFlags ?? []
                if modifiers.contains(.shift) || modifiers.contains(.option) {
                    textView.insertNewlineIgnoringFieldEditor(nil)
                } else if !draft.acceptOnReturn() {
                    draft.send(store)
                }
                return true
            case #selector(NSResponder.insertLineBreak(_:)):
                // Its own default is a line separator, which reads as a new line here but
                // would reach the server as a stray character.
                textView.insertNewlineIgnoringFieldEditor(nil)
                return true
            case #selector(NSResponder.moveUp(_:)):
                return draft.moveSelection(-1)
            case #selector(NSResponder.moveDown(_:)):
                return draft.moveSelection(1)
            case #selector(NSResponder.insertTab(_:)):
                if !draft.acceptSelected() { textView.window?.selectNextKeyView(nil) }
                return true
            case #selector(NSResponder.insertBacktab(_:)):
                textView.window?.selectPreviousKeyView(nil)
                return true
            case #selector(NSResponder.cancelOperation(_:)):
                // Left alone, Escape opens the system's own word completion.
                draft.dismissOptions()
                return true
            default:
                return false
            }
        }

        func measure(_ view: NSTextView) {
            guard view.frame.width > 1, let layout = view.layoutManager, let container = view.textContainer else { return }
            layout.ensureLayout(for: container)
            let used = layout.usedRect(for: container).height + view.textContainerInset.height * 2
            let next = min(max(used, ComposerDraft.minHeight), ComposerDraft.maxHeight)
            guard abs(next - draft.height) > 0.5 else { return }
            draft.height = next
            // Typing a new line scrolls the text up before the field has grown to hold it,
            // and once everything fits it should not stay scrolled.
            if used <= ComposerDraft.maxHeight { DispatchQueue.main.async { view.scroll(.zero) } }
        }
    }
}
