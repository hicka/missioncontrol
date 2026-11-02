import AppKit
import SwiftUI

/// The first screen: how things stand, somewhere to hand over a task, and each part of
/// the workspace in brief. Every section is its own view reading only the fields it draws,
/// so a tool starting redraws "In progress" and not the composer being typed in.
struct HomeView: View {
    @Environment(Store.self) private var store
    @State private var overdue = false

    var body: some View {
        Page(greeting) {
            if overdue { RuntimeNote() }
            Hero()
            NeedsSummary()
            ProfilesSection()
            HomeColumns {
                InProgressSection()
                GrowthSection()
            }
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

    private var greeting: String {
        let hour = Calendar.current.component(.hour, from: Date())
        let part = hour < 5 ? "Working late" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening"
        let first = store.user.name.str.split(separator: " ").first.map(String.init) ?? ""
        return first.isEmpty || first == "You" ? part : "\(part), \(first)"
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
                    Text("What is shown here was saved earlier.").font(.system(size: 12)).foregroundStyle(.secondary)
                }
                Spacer()
                AsyncButton("Try again") { await store.refresh() }
            }
        }
    }
}

// MARK: Small shared pieces

private struct Ago: View {
    let ts: Double
    var body: some View {
        TimelineView(.everyMinute) { _ in
            Text(ago(ts)).font(.system(size: 11)).foregroundStyle(.secondary).monospacedDigit().fixedSize()
        }
    }
}

private struct MoreLink: View {
    let title: String
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            HStack(spacing: 3) {
                Text(title)
                Image(systemName: "chevron.right").font(.system(size: 9, weight: .semibold))
            }
            .font(.system(size: 12))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .foregroundStyle(.secondary)
    }
}

/// A whole row that lights up under the pointer.
private struct RowStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View { Surface(configuration: configuration) }
    private struct Surface: View {
        let configuration: Configuration
        @State private var hovering = false
        var body: some View {
            configuration.label
                .contentShape(Rectangle())
                .background(Color.primary.opacity(configuration.isPressed ? 0.08 : hovering ? 0.04 : 0))
                .onHover { hovering = $0 }
        }
    }
}

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
    var homeRowKey: String { "\(self["kind"].str):\(self["id"].str):\(self["id"].isNull ? self["title"].str : "")" }
}

/// Two columns, the first the wider, one above the other when there is no room for both.
private struct HomeColumns: Layout {
    private let gap: CGFloat = 18

    private func columns(_ width: CGFloat) -> (left: CGFloat, right: CGFloat)? {
        guard width >= 680 else { return nil }
        let room = width - gap
        return (room * 1.55 / 2.55, room / 2.55)
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? 720
        if subviews.count == 2, let columns = columns(width) {
            let left = subviews[0].sizeThatFits(ProposedViewSize(width: columns.left, height: nil)).height
            let right = subviews[1].sizeThatFits(ProposedViewSize(width: columns.right, height: nil)).height
            return CGSize(width: width, height: max(left, right))
        }
        let heights = subviews.map { $0.sizeThatFits(ProposedViewSize(width: width, height: nil)).height }
        return CGSize(width: width, height: heights.reduce(0, +) + gap * CGFloat(max(0, subviews.count - 1)))
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        if subviews.count == 2, let columns = columns(bounds.width) {
            subviews[0].place(at: bounds.origin, proposal: ProposedViewSize(width: columns.left, height: nil))
            subviews[1].place(at: CGPoint(x: bounds.minX + columns.left + gap, y: bounds.minY), proposal: ProposedViewSize(width: columns.right, height: nil))
            return
        }
        var y = bounds.minY
        for subview in subviews {
            let proposed = ProposedViewSize(width: bounds.width, height: nil)
            subview.place(at: CGPoint(x: bounds.minX, y: y), proposal: proposed)
            y += subview.sizeThatFits(proposed).height + gap
        }
    }
}

// MARK: The hero

/// What is being written, kept apart from the screen so a half-written task survives a look
/// at another screen, and so a task still being sent can report back wherever the owner is.
@Observable @MainActor
private final class HomeDraft {
    static let shared = HomeDraft()

    struct Feedback: Equatable {
        let text: String
        var error = false
        var thread: String?
    }

    var text = ""
    var sending = false
    var feedback: Feedback?
    var focusRequests = 0

    var trimmed: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }

    func prefill(_ text: String) {
        self.text = text
        focusRequests += 1
    }

    func send(_ store: Store) {
        let task = trimmed
        guard !task.isEmpty, !sending else { return }
        guard let api = store.api else {
            say("Not connected to the runtime yet.", error: true, store)
            return
        }
        sending = true
        Task {
            defer { sending = false }
            do {
                let result = try await api.post("/api/command", ["text": task])
                // Whatever has been written since belongs to the owner, not to this send.
                if trimmed == task { text = "" }
                if !result.mission.isNull {
                    let thread = result.mission.thread.str
                    say("Task started with \(store.name(result.mission.agent.str)). Follow it in your chat or under Tasks.",
                        thread: thread.isEmpty ? "group:team" : thread, store)
                } else if !result.reply.str.isEmpty {
                    say(result.reply.str, store)
                }
                await store.refresh()
            } catch {
                say(error.localizedDescription, error: true, store)
            }
        }
    }

    func run(command: String, _ store: Store) {
        guard let api = store.api else {
            store.notify("Not connected to the runtime yet.", error: true)
            return
        }
        Task {
            do {
                let result = try await api.post("/api/command", ["text": command])
                say(result.reply.str.isEmpty ? "Done." : result.reply.str, store)
            } catch {
                store.notify(error.localizedDescription, error: true)
            }
        }
    }

    private func say(_ text: String, error: Bool = false, thread: String? = nil, _ store: Store) {
        if store.route == .home {
            feedback = Feedback(text: text, error: error, thread: thread)
        } else {
            store.notify(text, error: error)
        }
    }
}

private struct Hero: View {
    @Environment(Store.self) private var store

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SummaryLine()
            Composer()
            HStack(spacing: 8) {
                Button { HomeDraft.shared.run(command: "/status", store) } label: { Label("What is running?", systemImage: "person.2") }
                Button { HomeDraft.shared.prefill("Summarize what got done today, and what is still open.") } label: { Label("Today's summary", systemImage: "checklist") }
                Button {
                    UserDefaults.standard.set("skills", forKey: "knowledge.tab")
                    store.go(.knowledge)
                } label: { Label("Run a skill", systemImage: "lightbulb") }
            }
            .buttonStyle(.bordered)
            .buttonBorderShape(.capsule)
            FeedbackBanner()
        }
        .background(alignment: .top) { HomeGlow() }
        .onDisappear { HomeDraft.shared.feedback = nil }
    }
}

/// A soft wash in the colours of whichever profiles are busy. It holds still, because a
/// drifting blur would cost frames on a screen that can stay open all day.
private struct HomeGlow: View {
    @Environment(Store.self) private var store

    var body: some View {
        let busy = store.agents.filter { store.isBusy($0.id) }
        let first = color(hex: busy.first?.color ?? store.lead?.color ?? "", fallback: .clear)
        let second = busy.count > 1 ? color(hex: busy[1].color, fallback: first) : first
        ZStack {
            RadialGradient(colors: [first, .clear], center: .center, startRadius: 0, endRadius: 320)
            RadialGradient(colors: [second, .clear], center: UnitPoint(x: 0.7, y: 0.6), startRadius: 0, endRadius: 260)
        }
        .frame(maxWidth: 760)
        .frame(height: 360)
        .opacity(busy.isEmpty ? 0 : 0.22)
        .animation(.easeInOut(duration: 1.2), value: busy.isEmpty)
        .offset(y: -60)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

/// What the kernel says is open, so this can never read "all quiet" over a waiting task.
private struct SummaryLine: View {
    @Environment(Store.self) private var store
    private static let open: Set<String> = ["queued", "working", "waiting", "blocked", "failed", "stalled"]

    var body: some View {
        Text(line)
            .font(.callout)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    private var line: AttributedString {
        let working = store.workers.filter { $0.status.str == "working" }.count
        let waiting = store.workers.filter { Self.open.contains($0.status.str) }.count - working
        var bits: [AttributedString] = []
        if working > 0 { bits.append(strong(plural(working, "task")) + AttributedString(" working")) }
        if waiting > 0 {
            bits.append(working > 0 ? strong("\(waiting)") + AttributedString(" more open") : strong(plural(waiting, "task")) + AttributedString(" open"))
        }
        if working == 0, waiting == 0 { bits.append(AttributedString("All quiet")) }
        let needs = store.inbox.count
        if needs > 0 { bits.append(strong("\(needs)") + AttributedString(needs == 1 ? " thing needs you" : " things need you")) }
        let clean = store.evolution.week.clean_rate
        if !clean.isNull { bits.append(strong(percent(clean)) + AttributedString(" clean runs this week")) }
        var joined = AttributedString()
        for (index, bit) in bits.enumerated() {
            if index > 0 { joined += AttributedString("  ·  ") }
            joined += bit
        }
        return joined
    }

    private func strong(_ text: String) -> AttributedString {
        var run = AttributedString(text)
        run.swiftUI.foregroundColor = .primary
        run.swiftUI.font = .callout.weight(.medium)
        return run
    }
}

private struct Composer: View {
    @Environment(Store.self) private var store
    @Bindable private var draft = HomeDraft.shared
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField("Hand over a task, or ask anything…", text: $draft.text, axis: .vertical)
                .textFieldStyle(.plain)
                .font(.system(size: 15))
                .lineLimit(2...10)
                .focused($focused)
                .accessibilityLabel("New task")
                .onSubmit { draft.send(store) }
                .onKeyPress(.return, phases: .down) { press in
                    guard press.modifiers.contains(.shift) else { return .ignored }
                    draft.text += "\n"
                    return .handled
                }
            HStack(spacing: 8) {
                RouteChip(text: draft.trimmed)
                Spacer(minLength: 8)
                Text("Enter to send · @ to choose who · / for commands")
                    .font(.system(size: 11)).foregroundStyle(.tertiary).lineLimit(1)
                Button { draft.send(store) } label: {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.system(size: 24))
                        .foregroundStyle(canSend ? Color.accentColor : Color.secondary.opacity(0.35))
                }
                .buttonStyle(.plain)
                .disabled(!canSend)
                .help("Send")
                .accessibilityLabel("Send")
            }
        }
        .padding(.horizontal, 14).padding(.top, 12).padding(.bottom, 8)
        .background(MC.surface, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).strokeBorder(focused ? Color.primary.opacity(0.28) : MC.line, lineWidth: focused ? 1 : 0.5))
        .shadow(color: .black.opacity(0.07), radius: 10, y: 3)
        .onChange(of: draft.focusRequests) { focused = true }
        .task(id: store.homeComposer) { await answer(store.homeComposer) }
    }

    private var canSend: Bool { !draft.trimmed.isEmpty && !draft.sending }

    /// The field has to be in the window before it can take focus, and taking focus selects
    /// its text, which the next key would replace, so each step waits for the one before.
    private func answer(_ request: Store.HomeComposerRequest?) async {
        guard let request else { return }
        if let text = request.text { draft.text = text }
        try? await Task.sleep(nanoseconds: 30_000_000)
        if Task.isCancelled { return }
        focused = true
        try? await Task.sleep(nanoseconds: 30_000_000)
        if Task.isCancelled { return }
        (NSApp.keyWindow?.firstResponder as? NSTextView)?.moveToEndOfDocument(nil)
        store.homeComposer = nil
    }
}

/// A command run from outside Home, whose reply lands where Home's own buttons put theirs.
@MainActor func runHomeCommand(_ command: String, _ store: Store) {
    HomeDraft.shared.run(command: command, store)
}

/// Who a task goes to: the lead, whoever is @mentioned, or nobody for a slash command.
private struct RouteChip: View {
    @Environment(Store.self) private var store
    let text: String

    var body: some View {
        Group {
            if text.hasPrefix("/") {
                Label("Command · runs locally", systemImage: "terminal")
            } else if let mentioned {
                HStack(spacing: 6) {
                    AgentBadge(agent: mentioned, size: 18)
                    Text(mentioned.name)
                }
            } else if let lead = store.lead {
                HStack(spacing: 6) {
                    AgentBadge(agent: lead, size: 18)
                    Text(verbatim: "Lead · \(lead.name)")
                }
            }
        }
        .font(.system(size: 12))
        .foregroundStyle(.secondary)
        .lineLimit(1)
        .padding(.leading, 5).padding(.trailing, 10)
        .frame(height: 26)
        .overlay(Capsule().strokeBorder(MC.line, lineWidth: 0.5))
    }

    private var mentioned: Agent? {
        guard text.hasPrefix("@") else { return nil }
        let name = text.dropFirst().prefix { $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "_" || $0 == "-") }
        return store.agent(String(name))
    }
}

private struct FeedbackBanner: View {
    @Environment(Store.self) private var store
    private let draft = HomeDraft.shared

    var body: some View {
        if let feedback = draft.feedback {
            let tone: Color = feedback.error ? MC.danger : .primary
            HStack(alignment: .top, spacing: 10) {
                if feedback.error {
                    Image(systemName: "exclamationmark.triangle").foregroundStyle(tone).accessibilityLabel("Error")
                }
                Text(verbatim: feedback.text)
                    .font(.system(size: 13))
                    .foregroundStyle(tone)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if let thread = feedback.thread {
                    Button("Open") { store.go(.chat(thread)) }.controlSize(.small)
                }
                Button { draft.feedback = nil } label: {
                    Image(systemName: "xmark").font(.system(size: 10, weight: .semibold))
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .help("Dismiss")
                .accessibilityLabel("Dismiss")
            }
            .padding(.horizontal, 12).padding(.vertical, 9)
            .background(feedback.error ? MC.danger.opacity(0.1) : MC.surface, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(feedback.error ? Color.clear : MC.line.opacity(0.6), lineWidth: 0.5))
        }
    }
}

// MARK: Needs you

/// A summary only: deciding happens in the Inbox, where a held command is shown in full
/// before anyone can run it.
private struct NeedsSummary: View {
    @Environment(Store.self) private var store

    var body: some View {
        let inbox = store.inbox
        if !inbox.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                SectionHead(title: "Needs you", count: inbox.count) {
                    MoreLink(title: "Open inbox") { store.go(.inbox) }
                }
                Card {
                    ForEach(Array(inbox.prefix(4)), id: \.homeRowKey) { item in
                        if item.homeRowKey != inbox[0].homeRowKey { Divider() }
                        NeedsRow(item: item)
                    }
                    if inbox.count > 4 {
                        Divider()
                        Button { store.go(.inbox) } label: {
                            Text("and \(inbox.count - 4) more")
                                .font(.system(size: 12)).foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.horizontal, 14).padding(.vertical, 8)
                        }
                        .buttonStyle(RowStyle())
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            }
        }
    }
}

private struct NeedsRow: View {
    @Environment(Store.self) private var store
    let item: JSON

    var body: some View {
        let kind = kindInfo(item.kind.str)
        Button { store.go(.inbox) } label: {
            HStack(spacing: 11) {
                Image(systemName: kind.icon).foregroundStyle(kind.tone).frame(width: 22)
                VStack(alignment: .leading, spacing: 2) {
                    Text(verbatim: firstLine)
                        .font(item.kind.str == "command" ? .system(size: 12.5, design: .monospaced) : .system(size: 13, weight: .medium))
                        .lineLimit(1)
                        .truncationMode(.tail)
                    Text(verbatim: subtitle(kind)).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 8)
                Ago(ts: item.ts.num)
                Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold)).foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 14).padding(.vertical, 9)
        }
        .buttonStyle(RowStyle())
    }

    private var firstLine: String {
        let line = item.title.str.prefix { $0 != "\n" && $0 != "\r\n" }
        return String(line.prefix(300))
    }

    private func subtitle(_ kind: KindInfo) -> String {
        let agent = item.agent.str
        return agent.isEmpty || agent == "*" ? kind.label : "\(kind.label) · \(store.name(agent))"
    }
}

// MARK: Profiles

private struct ProfilesSection: View {
    @Environment(Store.self) private var store
    @State private var adding = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionHead("Profiles", count: store.agents.count)
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 220), spacing: 12, alignment: .top)], spacing: 12) {
                ForEach(store.agents) { agent in ProfileCard(agent: agent) }
                AddProfileCard { adding = true }
            }
        }
        .sheet(isPresented: $adding) { NewProfileSheet() }
    }
}

private struct ProfileCard: View {
    @Environment(Store.self) private var store
    let agent: Agent
    @State private var hovering = false

    var body: some View {
        Button { store.go(.chat("dm:\(agent.id)")) } label: {
            Card(padding: 16) {
                VStack(alignment: .leading, spacing: 12) {
                    HStack(spacing: 12) {
                        AgentBadge(agent: agent, size: 40)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(verbatim: agent.name).font(.system(size: 14, weight: .semibold)).lineLimit(1)
                            ProfileRole(agentID: agent.id)
                        }
                    }
                    ProfileDoing(agentID: agent.id)
                    ProfileStats(agentID: agent.id)
                }
            }
            .overlay { ProfileBusyTint(agent: agent) }
            .overlay {
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(Color.primary.opacity(hovering ? 0.22 : 0), lineWidth: 1)
            }
            .contentShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .accessibilityHint("Opens the chat with this profile")
    }
}

private struct ProfileRole: View {
    @Environment(Store.self) private var store
    let agentID: String
    var body: some View {
        let role = store.agentInfo[agentID].role.str
        Text(verbatim: role.isEmpty ? "A role tasks can run as" : role)
            .font(.system(size: 12))
            .foregroundStyle(.secondary)
            .lineLimit(2, reservesSpace: true)
            .fixedSize(horizontal: false, vertical: true)
    }
}

private struct ProfileBusyTint: View {
    @Environment(Store.self) private var store
    let agent: Agent
    var body: some View {
        let busy = store.isBusy(agent.id)
        RadialGradient(colors: [color(hex: agent.color, fallback: .gray).opacity(0.14), .clear], center: .topLeading, startRadius: 0, endRadius: 240)
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            .opacity(busy ? 1 : 0)
            .animation(.easeInOut(duration: 0.6), value: busy)
            .allowsHitTesting(false)
            .accessibilityHidden(true)
    }
}

private struct ProfileDoing: View {
    @Environment(Store.self) private var store
    let agentID: String

    var body: some View {
        let state = store.states[agentID] ?? "idle"
        let doing = activity(state)
        HStack(spacing: 7) {
            StatusDot(state: state)
            if doing.isEmpty {
                TimelineView(.everyMinute) { _ in Text(idleText).lineLimit(1) }
            } else {
                Text(verbatim: doing).lineLimit(1)
            }
        }
        .font(.system(size: 12))
        .foregroundStyle(.secondary)
    }

    private func activity(_ state: String) -> String {
        if state == "thinking" { return "Thinking…" }
        guard state == "doing" else { return "" }
        if let tool = store.tools[agentID] { return "Working · \(tool.replacingOccurrences(of: "_", with: " "))" }
        return "Working…"
    }

    private var idleText: String {
        let last = store.threadRows.first { $0.id == "dm:\(agentID)" }?.last.ts.num ?? 0
        return last > 0 ? "Idle · last active \(ago(last))" : "Idle"
    }
}

private struct StatusDot: View {
    let state: String
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        let active = state == "thinking" || state == "doing"
        let tint: Color = state == "thinking" ? MC.warn : state == "doing" ? MC.good : Color.secondary.opacity(0.45)
        Group {
            if active && !reduceMotion {
                PhaseAnimator([1.0, 0.4]) { level in
                    Circle().fill(tint).opacity(level)
                } animation: { _ in .easeInOut(duration: state == "doing" ? 0.55 : 0.7) }
            } else {
                Circle().fill(tint)
            }
        }
        .frame(width: 8, height: 8)
        .accessibilityHidden(true)
    }
}

private struct ProfileStats: View {
    @Environment(Store.self) private var store
    let agentID: String

    var body: some View {
        HStack(spacing: 6) {
            stat(active, "active")
            stat(scheduled, "scheduled")
            stat(done, "done")
        }
    }

    private var active: Int {
        store.missions.filter { $0.agent.str == agentID && ["running", "queued", "waiting"].contains($0.status.str) }.count
    }

    private var scheduled: Int {
        store.jobs.filter { $0.agent.str == agentID && ["active", "pending", "paused"].contains($0.status.str) }.count
            + store.watches.filter { $0.agent.str == agentID && $0.status.str == "active" }.count
    }

    private var done: Int {
        let week = Date().timeIntervalSince1970 - 7 * 86400
        return store.missions.filter { $0.agent.str == agentID && $0.status.str == "completed" && $0.updated.num >= week }.count
    }

    private func stat(_ value: Int, _ label: String) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text("\(value)").font(.system(size: 14, weight: .semibold)).monospacedDigit()
            Text(label).font(.system(size: 11)).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

private struct AddProfileCard: View {
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            VStack(spacing: 8) {
                Image(systemName: "plus").font(.system(size: 20, weight: .regular))
                Text("Add a profile").font(.system(size: 13))
            }
            .foregroundStyle(hovering ? Color.primary : Color.secondary)
            .frame(maxWidth: .infinity, minHeight: 150)
            .overlay {
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [5, 4]))
                    .foregroundStyle(MC.line)
            }
            .contentShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
    }
}

// MARK: A new profile

private struct NewProfileSheet: View {
    private static let swatches: [(hex: String, name: String)] = [
        ("#7a68c9", "Purple"), ("#e0559b", "Pink"), ("#d6493f", "Red"), ("#f5a623", "Orange"), ("#3aa655", "Green"),
        ("#30c8bd", "Teal"), ("#3f7bd9", "Blue"), ("#8e8e93", "Grey"), ("#1d1d1f", "Black"),
    ]

    @Environment(Store.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var chosenColor: String?
    @State private var prompt = ""
    @State private var folder = ""
    @State private var provider = ""
    @State private var model = ""
    @State private var models: [String] = []
    @State private var imessage = false
    @State private var llm: JSON = .null
    @State private var problem = ""
    @State private var creating = false

    /// A different colour for each new profile, until one is picked.
    private var colorHex: String { chosenColor ?? Self.swatches[store.agents.count % Self.swatches.count].hex }

    private var presets: [(key: String, label: String)] {
        llm.presets.dict.map { (key: $0.key, label: $0.value.label.str) }.sorted { $0.label < $1.label }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("New profile").font(.title2.weight(.semibold))
            HStack(spacing: 12) {
                AgentBadge(agent: preview, size: 44)
                TextField("Name, e.g. QA Engineer", text: $name)
                    .textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Name")
            }
            swatchRow
            label("Role")
            roleEditor
            label("Workspace folder")
            TextField("Optional, e.g. ~/Sites/myrepo", text: $folder)
                .textFieldStyle(.roundedBorder)
                .autocorrectionDisabled()
                .accessibilityLabel("Workspace folder")
            label("Model")
            modelRow
            if let hint {
                Text(verbatim: hint.text)
                    .font(.system(size: 12))
                    .foregroundStyle(hint.warn ? MC.warn : .secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Toggle("Responds to your iMessages", isOn: $imessage)
                .toggleStyle(.checkbox)
                .help("Route texts from your phone to this profile")
            if !problem.isEmpty {
                Text(verbatim: problem).font(.system(size: 12)).foregroundStyle(MC.danger).fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Create", action: create)
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
                    .disabled(creating)
            }
            .padding(.top, 4)
        }
        .padding(20)
        .frame(width: 500)
        .task {
            if let full = try? await store.api?.get("/api/settings/full") { llm = full.llm }
        }
        .task(id: provider) {
            models = []
            guard !provider.isEmpty, let api = store.api else { return }
            if let reply = try? await api.get("/api/settings/llm/models?provider=\(API.escape(provider))&base_url=") {
                models = reply.models.list.map(\.str)
            }
        }
    }

    private var preview: Agent {
        Agent(.object(["id": .string("new"), "name": .string(name.isEmpty ? "?" : name), "color": .string(colorHex)]))
    }

    private func label(_ title: String) -> some View {
        Text(title).font(.system(size: 12, weight: .medium)).foregroundStyle(.secondary).padding(.bottom, -6)
    }

    private var swatchRow: some View {
        HStack(spacing: 8) {
            ForEach(Self.swatches, id: \.hex) { swatch in
                let selected = swatch.hex == colorHex
                Button { chosenColor = swatch.hex } label: {
                    Circle()
                        .fill(color(hex: swatch.hex))
                        .frame(width: 22, height: 22)
                        .padding(3)
                        .overlay(Circle().strokeBorder(selected ? Color.primary.opacity(0.6) : .clear, lineWidth: 1.5))
                }
                .buttonStyle(.plain)
                .help(swatch.name)
                .accessibilityLabel(swatch.name)
                .accessibilityAddTraits(selected ? .isSelected : [])
            }
        }
    }

    private var roleEditor: some View {
        TextEditor(text: $prompt)
            .font(.system(size: 13))
            .scrollContentBackground(.hidden)
            .padding(5)
            .frame(height: 104)
            .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 7, style: .continuous).strokeBorder(MC.line, lineWidth: 0.5))
            .overlay(alignment: .topLeading) {
                if prompt.isEmpty {
                    Text("What it owns, how it works, what it is responsible for. Written to it as 'You are…'")
                        .font(.system(size: 13))
                        .foregroundStyle(.tertiary)
                        .padding(.horizontal, 10).padding(.vertical, 5)
                        .allowsHitTesting(false)
                        .accessibilityHidden(true)
                }
            }
            .accessibilityLabel("Role")
    }

    private var modelRow: some View {
        HStack(spacing: 8) {
            Picker("Provider", selection: $provider) {
                Text(verbatim: "Default (\(llm.model.str.isEmpty ? "the default model" : llm.model.str))").tag("")
                ForEach(presets, id: \.key) { preset in Text(verbatim: preset.label).tag(preset.key) }
            }
            .labelsHidden()
            .help("Which model this profile thinks with")
            .frame(maxWidth: 220)
            TextField(provider.isEmpty ? "uses the default model" : "model", text: $model)
                .textFieldStyle(.roundedBorder)
                .autocorrectionDisabled()
                .disabled(provider.isEmpty)
                .accessibilityLabel("Model")
            if !models.isEmpty {
                Menu {
                    ForEach(models, id: \.self) { name in Button(name) { model = name } }
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

    /// A provider whose key is missing would fail on this profile's first turn.
    private var hint: (text: String, warn: Bool)? {
        guard !provider.isEmpty else { return nil }
        let preset = llm.presets[provider]
        let keyEnv = preset.key_env.str
        if !preset.isNull, !keyEnv.isEmpty, !(llm.key_env.str == keyEnv && llm.key_set.flag) {
            return ("\(preset.label.str) needs \(keyEnv) in Settings > Model before this profile can use it.", true)
        }
        return ("This profile thinks with this model; the others are unaffected.", false)
    }

    private func create() {
        let name = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let prompt = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        let folder = folder.trimmingCharacters(in: .whitespacesAndNewlines)
        let model = model.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, !prompt.isEmpty else {
            problem = "Name and role description are both required."
            return
        }
        guard provider.isEmpty || !model.isEmpty else {
            problem = "Pick a model for that provider, or go back to the default."
            return
        }
        guard let api = store.api else {
            problem = "Not connected to the runtime yet."
            return
        }
        problem = ""
        creating = true
        let body: [String: Any] = [
            "name": name, "prompt": prompt, "color": colorHex, "cwd": folder.isEmpty ? "~" : folder,
            "llm": provider.isEmpty ? [String: String]() : ["provider": provider, "model": model],
        ]
        let routeTexts = imessage
        Task {
            defer { creating = false }
            do {
                let result = try await api.post("/api/agents", body)
                let id = result.agent.id
                if routeTexts { _ = try? await api.post("/api/settings", ["imessage_default_agent": id]) }
                await store.refreshState()
                dismiss()
                store.go(.chat("dm:\(id)"))
            } catch {
                problem = error.localizedDescription
            }
        }
    }
}

// MARK: In progress

private enum HomeInspect: Identifiable, Equatable {
    case mission(String), execution(String)
    var id: String {
        switch self {
        case .mission(let id): return "mission:\(id)"
        case .execution(let id): return "execution:\(id)"
        }
    }
}

@MainActor private func resumeMission(_ id: String, _ store: Store) async throws {
    guard let api = store.api else { throw APIError(message: "Not connected to the runtime yet.") }
    _ = try await api.post("/api/command", ["text": "/resume \(id)"])
    store.notify("Resumed.")
    store.refreshSoon(after: 0.05)
}

private func canResume(_ mission: JSON) -> Bool { ["waiting", "failed", "cancelled"].contains(mission.status.str) }

/// The tool's most telling argument, so the row says what it is working on.
private func toolDetail(_ execution: JSON) -> String {
    let arguments = execution.detail.arguments
    var parsed = arguments
    if case .string(let raw) = arguments {
        if raw.isEmpty { return "" }
        parsed = JSON(data: Data(raw.utf8))
        if parsed.isNull { return String(raw.prefix(120)) }
    }
    for key in ["command", "url", "query", "action", "text", "name"] {
        let value = parsed[key].str
        if !value.isEmpty { return String(value.prefix(120)) }
    }
    return ""
}

private struct InProgressSection: View {
    @Environment(Store.self) private var store
    @State private var inspecting: HomeInspect?

    var body: some View {
        let open = store.openMissions
        let openIDs = Set(open.map(\.id))
        let running = store.executions.filter {
            $0.status.str == "running" && $0.kind.str == "tool" && !openIDs.contains($0.mission.str)
        }
        VStack(alignment: .leading, spacing: 8) {
            SectionHead(title: "In progress") {
                MoreLink(title: "All tasks") { store.go(.tasks) }
            }
            Card {
                if running.isEmpty && open.isEmpty {
                    Text("Nothing in flight. Hand over a task above and it shows up here.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: .infinity)
                        .padding(22)
                } else {
                    ForEach(running.prefix(4)) { execution in
                        if execution.id != running[0].id { Divider() }
                        ExecutionRow(execution: execution) { inspecting = .execution(execution.id) }
                    }
                    if !running.isEmpty, !open.isEmpty { Divider() }
                    ForEach(open.prefix(6)) { mission in
                        if mission.id != open[0].id { Divider() }
                        MissionRow(mission: mission) { inspecting = .mission(mission.id) }
                    }
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
        .sheet(item: $inspecting) { InspectorSheet(current: $0) }
    }
}

private struct ExecutionRow: View {
    @Environment(Store.self) private var store
    let execution: JSON
    let open: () -> Void

    var body: some View {
        let detail = toolDetail(execution)
        Button(action: open) {
            HStack(spacing: 10) {
                AgentBadge(agent: store.agent(execution.agent.str))
                VStack(alignment: .leading, spacing: 2) {
                    Text(verbatim: "\(store.name(execution.agent.str)) is running \(execution.name.str.replacingOccurrences(of: "_", with: " "))")
                        .font(.system(size: 13, weight: .medium)).lineLimit(1)
                    if !detail.isEmpty {
                        Text(verbatim: detail).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                Spacer(minLength: 8)
                Ago(ts: execution.ts.num)
            }
            .padding(.horizontal, 14).padding(.vertical, 9)
        }
        .buttonStyle(RowStyle())
    }
}

private struct MissionRow: View {
    @Environment(Store.self) private var store
    let mission: JSON
    let open: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Button(action: open) {
                HStack(spacing: 10) {
                    AgentBadge(agent: store.agent(mission.agent.str))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(verbatim: mission.objective.str).font(.system(size: 13, weight: .medium)).lineLimit(2)
                        TimelineView(.everyMinute) { _ in
                            Text(verbatim: "\(store.name(mission.agent.str)) · updated \(ago(mission.updated.num))")
                                .font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                    Spacer(minLength: 0)
                }
                .padding(.leading, 14).padding(.vertical, 9)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            Pill(status: mission.status.str)
            if canResume(mission) {
                AsyncButton("Resume") { try await resumeMission(mission.id, store) }.controlSize(.small)
            }
        }
        .padding(.trailing, 14)
    }
}

/// Steps of a task open in place, so going from a task to one of its steps does not bounce
/// the sheet closed and open again.
private struct InspectorSheet: View {
    @Environment(Store.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State var current: HomeInspect

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(verbatim: title).font(.headline).lineLimit(1)
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            .padding(.horizontal, 16).padding(.vertical, 12)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    switch current {
                    case .mission(let id): MissionDetail(id: id) { current = $0 }
                    case .execution(let id): ExecutionDetail(id: id)
                    }
                }
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .frame(minWidth: 560, idealWidth: 640, minHeight: 380, idealHeight: 540)
    }

    private var title: String {
        switch current {
        case .mission: return "Task"
        case .execution(let id):
            let name = store.executions.first { $0.id == id }?.name.str ?? "Step"
            return name.replacingOccurrences(of: "_", with: " ")
        }
    }
}

private struct OutputBlock: View {
    let text: String
    var body: some View {
        Text(verbatim: text)
            .font(.system(size: 12, design: .monospaced))
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

private struct DetailHead: View {
    let title: String
    var body: some View { Text(title).font(.system(size: 13, weight: .semibold)).padding(.top, 4) }
}

private struct MissionDetail: View {
    @Environment(Store.self) private var store
    @Environment(\.dismiss) private var dismiss
    let id: String
    let show: (HomeInspect) -> Void

    var body: some View {
        if let mission = store.missions.first(where: { $0.id == id }) {
            let records = store.executions.filter { $0.mission.str == id }
            let worker = store.workers.first { $0.mission.str == id }
            Pill(status: mission.status.str)
            DetailHead(title: "Objective")
            OutputBlock(text: mission.objective.str)
            DetailHead(title: "Latest result or checkpoint")
            OutputBlock(text: mission.result.str.isEmpty ? "No result has been reported yet." : mission.result.str)
            if !records.isEmpty {
                DetailHead(title: "Steps recorded")
                Card {
                    ForEach(records.prefix(12)) { record in
                        if record.id != records[0].id { Divider() }
                        Button { show(.execution(record.id)) } label: {
                            HStack(spacing: 10) {
                                Image(systemName: "bolt").foregroundStyle(.secondary).frame(width: 18)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(verbatim: record.name.str.replacingOccurrences(of: "_", with: " ")).font(.system(size: 13, weight: .medium)).lineLimit(1)
                                    Text(verbatim: "\(store.name(record.agent.str)) · \(ago(record.ts.num))").font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                                }
                                Spacer(minLength: 8)
                                Pill(status: record.status.str)
                            }
                            .padding(.horizontal, 12).padding(.vertical, 7)
                        }
                        .buttonStyle(RowStyle())
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            }
            Text(verbatim: "\(store.name(mission.agent.str)) · task \(mission.id) · started \(Date(timeIntervalSince1970: mission.ts.num).formatted(date: .abbreviated, time: .shortened))")
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
            HStack(spacing: 8) {
                if let worker {
                    Button("Open worker") {
                        store.focusWorker = worker.id
                        store.go(.activity)
                        dismiss()
                    }
                    .buttonStyle(.borderedProminent)
                }
                if worker == nil {
                    Button("Open conversation") { openConversation(mission) }.buttonStyle(.borderedProminent)
                } else {
                    Button("Open conversation") { openConversation(mission) }
                }
                if canResume(mission) {
                    AsyncButton("Resume") {
                        try await resumeMission(mission.id, store)
                        dismiss()
                    }
                }
            }
            .padding(.top, 4)
        } else {
            EmptyNote(title: "This task is no longer listed")
        }
    }

    private func openConversation(_ mission: JSON) {
        store.go(.chat(mission.thread.str))
        dismiss()
    }
}

private struct ExecutionDetail: View {
    @Environment(Store.self) private var store
    let id: String

    var body: some View {
        if let record = store.executions.first(where: { $0.id == id }) {
            Pill(status: record.status.str)
            Text(verbatim: meta(record))
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
            DetailHead(title: "Inputs")
            OutputBlock(text: pretty(record.detail))
            DetailHead(title: "Result")
            OutputBlock(text: record.result.str.isEmpty ? (record.status.str == "running" ? "Running…" : "No output recorded.") : record.result.str)
        } else {
            EmptyNote(title: "This step is no longer listed")
        }
    }

    private func meta(_ record: JSON) -> String {
        let time = Date(timeIntervalSince1970: record.ts.num).formatted(date: .omitted, time: .standard)
        let seconds = String(format: "%.1f", max(0, record.updated.num - record.ts.num))
        var parts = [store.name(record.agent.str), record.kind.str, time, "\(seconds)s"]
        if !record.mission.str.isEmpty { parts.append("task \(record.mission.str)") }
        return parts.joined(separator: " · ")
    }

    private func pretty(_ value: JSON) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: value.raw, options: [.prettyPrinted, .sortedKeys, .fragmentsAllowed, .withoutEscapingSlashes]),
              let text = String(data: data, encoding: .utf8) else { return value.str }
        return text
    }
}

// MARK: Getting better

private struct GrowthSection: View {
    @Environment(Store.self) private var store
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionHead(title: "Getting better") {
                MoreLink(title: "Details") { store.go(.evolution) }
            }
            GrowthCard(evolution: store.evolution)
        }
    }
}

private struct GrowthCard: View {
    let evolution: JSON

    var body: some View {
        Card(padding: 16) {
            if evolution.enabled.isNull {
                Text("Measuring…").font(.callout).foregroundStyle(.secondary)
            } else {
                VStack(alignment: .leading, spacing: 14) {
                    figures
                    counts
                    Divider()
                    latest
                    if !evolution.enabled.flag {
                        Text("Learning is paused. Turn it on from the Evolution page.")
                            .font(.system(size: 12)).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    private var figures: some View {
        let week = evolution.week, previous = evolution.previous_week
        let rate = week.clean_rate.optNum, priorRate = previous.clean_rate.optNum
        let errors = week.error_rate.optNum, priorErrors = previous.error_rate.optNum
        let measured = rate != nil

        var cleanNote = measured ? plural(week.runs.int, "run") : "counted from today"
        if let rate, let priorRate {
            let delta = Int(((rate - priorRate) * 100).rounded())
            cleanNote = "\(delta >= 0 ? "+" : "")\(delta) pts vs last week"
        }
        var errorNote = ""
        if let errors, let priorErrors {
            let tenths = ((errors - priorErrors) * 1000).rounded()
            let delta = tenths == 0 ? 0 : tenths / 10
            errorNote = "\(delta > 0 ? "+" : "")\(delta.formatted(.number.precision(.fractionLength(0...1)))) pts vs last week"
        }

        let clean = VStack(alignment: .leading, spacing: 4) {
                Text("Clean runs, 7 days").font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                if measured {
                    Text(percent(week.clean_rate)).font(.system(size: 28, weight: .semibold)).monospacedDigit()
                } else {
                    Text("Measuring…").font(.system(size: 15, weight: .medium)).foregroundStyle(.secondary)
                }
                Text(cleanNote).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
            }
            .fixedSize()
        let failing = VStack(alignment: .leading, spacing: 4) {
                Text("Tool calls failing").font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
                Text(errors.map { String(format: "%.1f%%", $0 * 100) } ?? "—").font(.system(size: 28, weight: .semibold)).monospacedDigit()
                Text(errorNote.isEmpty ? " " : errorNote).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(1)
            }
            .fixedSize()
        let trend = Sparkline(values: evolution.days.list.map { (measured ? $0.clean_rate : $0.error_rate).optNum })
        // Side by side when the card is wide; the trend drops below when it is not.
        return ViewThatFits(in: .horizontal) {
            HStack(alignment: .bottom, spacing: 14) { clean; failing; Spacer(minLength: 0); trend }
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .top, spacing: 18) { clean; failing }
                trend
            }
        }
    }

    private var counts: some View {
        HStack(spacing: 16) {
            count(evolution.active.int, "lessons adopted")
            count(evolution.trial.int, "on trial")
            count(evolution.week.corrections.int, "corrections this week")
        }
        .font(.system(size: 12))
    }

    private func count(_ value: Int, _ label: String) -> some View {
        HStack(spacing: 4) {
            Text("\(value)").fontWeight(.semibold).monospacedDigit()
            Text(label).foregroundStyle(.secondary).lineLimit(1)
        }
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private var latest: some View {
        let rows = evolution.latest.list
        VStack(alignment: .leading, spacing: 8) {
            if rows.isEmpty {
                line("sparkles", "Lessons appear here as it learns from your corrections and its own repeated mistakes.")
            } else {
                ForEach(rows) { row in line(row.status.str == "active" ? "checkmark" : "flask", row.text.str) }
            }
        }
    }

    private func line(_ icon: String, _ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: icon).font(.system(size: 11)).foregroundStyle(.secondary).frame(width: 15).accessibilityHidden(true)
            Text(verbatim: text).font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct Sparkline: View {
    let values: [Double?]
    var width: CGFloat = 110
    var height: CGFloat = 34

    var body: some View {
        let points = plotted
        ZStack(alignment: .topLeading) {
            if points.count >= 2, let last = points.last {
                Path { path in
                    path.move(to: points[0])
                    for point in points.dropFirst() { path.addLine(to: point) }
                }
                .stroke(.tertiary, style: StrokeStyle(lineWidth: 2, lineCap: .round, lineJoin: .round))
                Circle()
                    .fill(.primary)
                    .overlay(Circle().strokeBorder(MC.surface, lineWidth: 2))
                    .frame(width: 10, height: 10)
                    .position(last)
            }
        }
        .frame(width: width, height: height)
        .accessibilityElement()
        .accessibilityLabel("Trend over \(values.count) days")
        .accessibilityHidden(points.count < 2)
    }

    private var plotted: [CGPoint] {
        let known = values.enumerated().compactMap { index, value in value.map { (index: index, value: $0) } }
        guard known.count >= 2 else { return [] }
        let steps = CGFloat(max(values.count - 1, 1))
        let low = known.map(\.value).min() ?? 0, high = known.map(\.value).max() ?? 0
        let span = high - low == 0 ? 1 : high - low
        return known.map { point in
            CGPoint(x: 2 + CGFloat(point.index) / steps * (width - 6),
                    y: height - 4 - CGFloat((point.value - low) / span) * (height - 8))
        }
    }
}
