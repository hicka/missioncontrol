import AppKit
import Observation
import SwiftUI

private let openWorkerStatuses: Set<String> = ["queued", "working", "waiting", "blocked", "failed", "stalled"]
private let ownerChannels: Set<String> = ["gui", "imessage", "telegram", "whatsapp", "email"]
private let admissions = [("explicit", "Only when asked"), ("candidate", "Read and judge"), ("ignore", "Ignore")]

private func baseSource(_ source: String) -> String {
    source.split(separator: ":", maxSplits: 1).first.map(String.init) ?? ""
}

private func sourceIcon(_ source: String) -> String {
    switch baseSource(source) {
    case "github": return "arrow.triangle.branch"
    case "cron", "heartbeat", "brief", "watch": return "clock"
    case "imessage", "telegram", "whatsapp", "email", "gui", "worker", "whatsapp_link": return "person.2"
    case "fleet": return "globe"
    case "approval": return "checkmark.shield"
    default: return "bolt"
    }
}

private enum EventFilter: String, CaseIterable {
    case all, acted, quiet, waiting

    var name: String {
        switch self {
        case .all: return "All"
        case .acted: return "Acted on"
        case .quiet: return "Left alone"
        case .waiting: return "Waiting"
        }
    }

    func includes(_ event: JSON) -> Bool {
        let disposition = event.disposition.str
        switch self {
        case .all: return true
        case .acted: return ["routed", "passed", "answered"].contains(disposition)
        case .quiet: return ["ignored", "read", "logged"].contains(disposition)
        case .waiting: return ["new", "queued"].contains(disposition)
        }
    }
}

private enum ActivitySheet: Identifiable {
    case worker(String), event(JSON)
    var id: String {
        switch self {
        case .worker(let id): return "worker:\(id)"
        case .event(let event): return "event:\(event.id)"
        }
    }
}

// MARK: Reading

/// Reads for a stream of events. `afterEvent` runs inside `.task(id:)`, so the next event
/// cancels its wait and starts it again, and a burst ends in one read. A steady stream must
/// not hold the screen back for ever, so the wait stops growing after two seconds.
@MainActor
private final class ReadGate {
    private var reading = false
    private var again = false
    private var waitingSince: Date?

    func afterEvent(_ read: @escaping @MainActor () async -> Void) async {
        let now = Date()
        let since = waitingSince ?? now
        waitingSince = since
        let wait = min(0.35, max(0, 2 - now.timeIntervalSince(since)))
        try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
        if !Task.isCancelled { await self.now(read) }
    }

    func now(_ read: @escaping @MainActor () async -> Void) async {
        waitingSince = nil
        if reading { again = true; return }
        reading = true
        repeat {
            again = false
            // In a task of its own, so that an event cancels the wait and never a read
            // already under way, which under steady events would never finish.
            await Task { await read() }.value
        } while again
        reading = false
    }
}

/// Reads again on the kernel's events and when the socket comes back, for as long as it is
/// on screen. A view of its own, so a tick costs this view and not the whole screen.
private struct LiveReader: View {
    @Environment(Store.self) private var store
    let gate: ReadGate
    let read: @MainActor () async -> Void
    @State private var appeared = false

    var body: some View {
        Color.clear
            .task(id: store.ticks.kernel) {
                if appeared {
                    await gate.afterEvent(read)
                } else {
                    appeared = true
                    await gate.now(read)
                }
            }
            .onChange(of: store.link) { _, up in
                if up { Task { await gate.now(read) } }
            }
    }
}

private struct KernelStats: Equatable {
    var routerWord = "—", routerName = "", routerNote = ""
    var slots = "—"
    var total = 0, quiet = 0, passed = 0
}

private struct SourceEntry: Identifiable, Equatable {
    let id: String
    let admission: String
}

/// `/api/kernel`, field by field, so a section redraws only when its own rows moved. It
/// outlives the screen, so coming back draws at once and the fresh read follows.
@Observable @MainActor
private final class KernelModel {
    static let shared = KernelModel()

    var loaded = false
    var problem: String?
    var paused = false
    var stats = KernelStats()
    var sessions: [JSON] = []
    var events: [JSON] = []
    var policies: [JSON] = []
    var sources: [SourceEntry] = []
    var findings: [JSON] = []
    var outbox: [JSON] = []

    @ObservationIgnored let gate = ReadGate()
    @ObservationIgnored private var base: String?

    func read(_ api: API?) async {
        guard let api else { return }
        if base != api.base {
            base = api.base
            loaded = false
            apply(.null)
        }
        do {
            let snapshot = try await api.get("/api/kernel")
            apply(snapshot)
            if !loaded { loaded = true }
            if problem != nil { problem = nil }
        } catch {
            problem = error.localizedDescription
        }
    }

    func reload(_ api: API?) async { await gate.now { await self.read(api) } }

    private func apply(_ kernel: JSON) {
        func assign<Value: Equatable>(_ path: ReferenceWritableKeyPath<KernelModel, Value>, _ value: Value) {
            if self[keyPath: path] != value { self[keyPath: path] = value }
        }
        assign(\.paused, kernel.paused.flag)
        assign(\.stats, Self.stats(kernel))
        assign(\.sessions, kernel.sessions.list)
        assign(\.events, kernel.events.list)
        assign(\.policies, kernel.policies.list)
        assign(\.sources, kernel.sources.dict
            .filter { !ownerChannels.contains($0.key) }
            .map { SourceEntry(id: $0.key, admission: $0.value.str) }
            .sorted { $0.id < $1.id })
        assign(\.findings, kernel.findings.list)
        assign(\.outbox, kernel.outbox.list)
    }

    private static func stats(_ kernel: JSON) -> KernelStats {
        guard !kernel.isNull else { return KernelStats() }
        let router = kernel.router, counts = kernel.events_24h
        var stats = KernelStats()
        stats.routerWord = router.alive.flag ? (router.state.str == "idle" ? "Listening" : "Deciding") : "Stopped"
        stats.routerName = router.name.str
        stats.routerNote = router.pending.int > 0 ? "\(router.pending.int) waiting" : "nothing waiting"
        stats.slots = "\(kernel.slots.used.int) of \(kernel.slots.limit.int)"
        stats.total = counts.dict.values.reduce(0) { $0 + $1.int }
        stats.quiet = counts.ignored.int + counts.read.int + counts.logged.int
        stats.passed = counts.passed.int
        return stats
    }

    /// Whether it took, so a rule being written is only cleared once it is saved.
    @discardableResult
    func policy(_ body: [String: Any], store: Store) async -> Bool {
        var saved = false
        if let api = store.api {
            do {
                _ = try await api.post("/api/kernel/policies", body)
                saved = true
            } catch {
                store.notify(error.localizedDescription, error: true)
            }
        } else {
            store.notify("Not connected to the runtime yet.", error: true)
        }
        await reload(store.api)
        return saved
    }
}

// MARK: The screen

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

/// What reached the agent, what became of it, and the rules it decides by.
struct ActivityView: View {
    @Environment(Store.self) private var store
    @State private var sheet: ActivitySheet?
    private let model = KernelModel.shared

    var body: some View {
        Page(title: "Activity", subtitle: "Everything that reached the agent, what became of it, and the rules it decides by.") {
            PauseButton(model: model)
        } content: {
            StatsSection(model: model)
            WorkingNow(model: model) { sheet = $0 }
            EventsSection(model: model) { sheet = $0 }
            PoliciesSection(model: model)
            SourcesSection(model: model)
            WatchdogSection(model: model) { sheet = $0 }
        }
        .background(LiveReader(gate: model.gate) { await model.read(store.api) })
        .task { await quietRefresh(store) }
        // Asked for from elsewhere, whether this screen was already open or not.
        .onChange(of: store.focusWorker, initial: true) { _, id in
            guard let id else { return }
            store.focusWorker = nil
            sheet = .worker(id)
        }
        .sheet(item: $sheet) { item in
            Group {
                switch item {
                case .worker(let id): WorkerSheet(id: id, model: model)
                case .event(let event): EventSheet(event: event)
                }
            }
            .environment(store)
        }
    }
}

private struct PauseButton: View {
    @Environment(Store.self) private var store
    let model: KernelModel

    var body: some View {
        AsyncButton(action: toggle) { Text(model.paused ? "Paused: carry on" : "Pause") }
            .disabled(!model.loaded)
    }

    private func toggle() async throws {
        guard let api = store.api else { throw APIError(message: "Not connected to the runtime yet.") }
        _ = try await api.post(model.paused ? "/api/kernel/resume" : "/api/kernel/pause")
        await model.reload(api)
    }
}

private struct StatsSection: View {
    let model: KernelModel

    var body: some View {
        let stats = model.stats
        if let problem = model.problem, !model.loaded {
            Card { EmptyNote(title: "Could not read activity", detail: problem) }
        } else {
            HStack(alignment: .top, spacing: 10) {
                StatCard(value: stats.routerWord, label: stats.routerName, note: stats.routerNote)
                StatCard(value: stats.slots, label: "workers running")
                StatCard(value: "\(stats.total)", label: "events today", note: stats.total > 0 ? "\(stats.quiet) needed nothing" : "")
                StatCard(value: "\(stats.passed)", label: "went straight to a worker", note: "no decision spent")
            }
            .fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct StatCard: View {
    let value: String
    let label: String
    var note = ""

    var body: some View {
        Card(padding: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(value).font(.system(size: 20, weight: .semibold)).monospacedDigit().lineLimit(1).minimumScaleFactor(0.7)
                Text(label).font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(2)
                if !note.isEmpty { Text(note).font(.system(size: 11)).foregroundStyle(.tertiary).lineLimit(2) }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .accessibilityElement(children: .combine)
    }
}

/// A line with a relative time in it, kept true while the screen stays open.
private struct LiveLine: View {
    let text: () -> String
    var body: some View {
        TimelineView(.everyMinute) { _ in Text(text()) }
            .font(.system(size: 11.5))
            .foregroundStyle(.secondary)
            .lineLimit(2)
            .multilineTextAlignment(.leading)
    }
}

private struct RowButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View { Lit(configuration: configuration) }

    private struct Lit: View {
        let configuration: ButtonStyleConfiguration
        @State private var hovering = false
        var body: some View {
            configuration.label
                .contentShape(Rectangle())
                .background(Color.primary.opacity(configuration.isPressed ? 0.08 : hovering ? 0.04 : 0))
                .onHover { hovering = $0 }
        }
    }
}

private struct LoadingCard: View {
    var body: some View {
        Card {
            ProgressView().controlSize(.small).frame(maxWidth: .infinity).padding(18)
                .accessibilityLabel("Loading")
        }
    }
}

// MARK: Working now

private struct WorkingNow: View {
    @Environment(Store.self) private var store
    let model: KernelModel
    let open: (ActivitySheet) -> Void

    var body: some View {
        // The store already knows who is working, so this is never blank on the first visit.
        let rows = (model.loaded ? model.sessions : store.workers).filter { openWorkerStatuses.contains($0.status.str) }
        SectionHead("Working now")
        Card {
            if rows.isEmpty {
                EmptyNote(title: "Nothing running", detail: "Tasks you hand over show up here while they are worked on.")
            } else {
                LazyVStack(spacing: 0) {
                    ForEach(rows) { worker in
                        WorkerRow(worker: worker, open: open).equatable()
                        if worker.id != rows.last?.id { Divider() }
                    }
                }
            }
        }
    }
}

private struct WorkerRow: View, Equatable {
    @Environment(Store.self) private var store
    let worker: JSON
    let open: (ActivitySheet) -> Void

    nonisolated static func == (a: WorkerRow, b: WorkerRow) -> Bool { a.worker == b.worker }

    var body: some View {
        Button { open(.worker(worker.id)) } label: {
            HStack(spacing: 10) {
                AgentBadge(agent: store.agent(worker.profile.str), size: 28)
                VStack(alignment: .leading, spacing: 2) {
                    Text(worker.title.str.isEmpty ? "Task" : worker.title.str)
                        .font(.system(size: 13, weight: .medium))
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                    LiveLine { line }
                }
                Spacer(minLength: 8)
                Pill(status: worker.status.str)
            }
            .padding(.horizontal, 12).padding(.vertical, 9)
        }
        .buttonStyle(RowButtonStyle())
    }

    private var line: String {
        var parts = [store.name(worker.profile.str)]
        if worker.direct.flag { parts.append("direct line") }
        if worker.strength.str == "inferred" { parts.append("not asked for directly") }
        if worker.speculative.flag { parts.append("acting ahead") }
        parts.append(ago(worker.updated.num))
        if !worker.summary.str.isEmpty { parts.append(clip(worker.summary.str, 110)) }
        return parts.joined(separator: " · ")
    }
}

// MARK: Events

private struct EventsSection: View {
    let model: KernelModel
    let open: (ActivitySheet) -> Void
    @AppStorage("activityEventFilter") private var filter: EventFilter = .all

    var body: some View {
        let events = model.events
        let shown = Array(events.filter(filter.includes).prefix(40))
        SectionHead(title: "Events") {
            Picker("Show", selection: $filter) {
                ForEach(EventFilter.allCases, id: \.self) { option in
                    Text("\(option.name) \(events.filter(option.includes).count)").tag(option)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
        }
        if !model.loaded {
            LoadingCard()
        } else {
            Card {
                if shown.isEmpty {
                    EmptyNote(title: "Nothing here yet")
                } else {
                    LazyVStack(spacing: 0) {
                        ForEach(shown) { event in
                            EventRow(event: event, open: open).equatable()
                            if event.id != shown.last?.id { Divider() }
                        }
                    }
                }
            }
        }
    }
}

private struct EventRow: View, Equatable {
    let event: JSON
    let open: (ActivitySheet) -> Void

    nonisolated static func == (a: EventRow, b: EventRow) -> Bool { a.event == b.event }

    var body: some View {
        Button { open(.event(event)) } label: {
            HStack(spacing: 10) {
                Image(systemName: sourceIcon(event.source.str)).foregroundStyle(.secondary).frame(width: 22).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text(clip(event.text.str, 130))
                        .font(.system(size: 13, weight: .medium))
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                    LiveLine { line }
                }
                Spacer(minLength: 8)
                Pill(status: event.disposition.str)
            }
            .padding(.horizontal, 12).padding(.vertical, 9)
        }
        .buttonStyle(RowButtonStyle())
    }

    private var line: String {
        let actor = event.actor.str.isEmpty ? baseSource(event.source.str) : event.actor.str
        var parts = [event.from_owner.flag ? "You" : actor, event.thread.str, ago(event.ts.num)]
        if !event.detail.str.isEmpty { parts.append(clip(event.detail.str, 70)) }
        return parts.joined(separator: " · ")
    }
}

/// Opens on the text the list already has, and shows the stored record once it is read.
private struct EventSheet: View {
    @Environment(Store.self) private var store
    let event: JSON
    @State private var recalled: String?

    var body: some View {
        SheetFrame(title: "Event") {
            TextBlock(text: recalled ?? event.text.str, mono: true)
        } actions: {
            EmptyView()
        }
        .task {
            guard let api = store.api else { return }
            do {
                recalled = try await api.get("/api/kernel/recall/\(API.escape(event.id))").text.str
            } catch {
                if !Task.isCancelled { store.notify(error.localizedDescription, error: true) }
            }
        }
    }
}

// MARK: Rules

private struct PoliciesSection: View {
    let model: KernelModel

    var body: some View {
        SectionHead("What it acts on unprompted")
        Text("The agent answers you always. Everything else it reads against these rules, and otherwise stays quiet. Text from outside can never change them.")
            .font(.callout)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
        PolicyList(model: model)
        PolicyComposer(model: model)
    }
}

private struct PolicyList: View {
    let model: KernelModel

    var body: some View {
        let rows = model.policies
        if !model.loaded {
            LoadingCard()
        } else {
            Card {
                if rows.isEmpty {
                    EmptyNote(title: "No rules", detail: "It only acts on what you ask.")
                } else {
                    LazyVStack(spacing: 0) {
                        ForEach(rows) { policy in
                            PolicyRow(policy: policy, model: model).equatable()
                            if policy.id != rows.last?.id { Divider() }
                        }
                    }
                }
            }
        }
    }
}

private struct PolicyRow: View, Equatable {
    @Environment(Store.self) private var store
    let policy: JSON
    let model: KernelModel
    /// The switch moves at once; the next read confirms it, or moves it back.
    @State private var pending: Bool?

    nonisolated static func == (a: PolicyRow, b: PolicyRow) -> Bool { a.policy == b.policy }

    var body: some View {
        let on = pending ?? policy.enabled.flag
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(policy.text.str)
                    .font(.system(size: 13))
                    .foregroundStyle(on ? Color.primary : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                Text(policy.author.str == "system" ? "Starting rule" : "Your rule")
                    .font(.system(size: 11.5))
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            Text(on ? "On" : "Off").font(.system(size: 11.5)).foregroundStyle(.secondary).accessibilityHidden(true)
            Toggle("Use rule: \(clip(policy.text.str, 60))", isOn: Binding(get: { on }, set: setEnabled))
                .labelsHidden()
                .toggleStyle(.switch)
                .controlSize(.small)
            AsyncButton(action: remove) { Image(systemName: "xmark").font(.system(size: 11, weight: .semibold)) }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .help("Remove rule")
                .accessibilityLabel("Remove rule")
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
    }

    private func setEnabled(_ value: Bool) {
        pending = value
        Task {
            await model.policy(["action": value ? "enable" : "disable", "id": policy.id], store: store)
            pending = nil
        }
    }

    private func remove() async throws {
        await model.policy(["action": "remove", "id": policy.id], store: store)
    }
}

/// Its own view, so typing a rule redraws the field and not the rules above it.
private struct PolicyComposer: View {
    @Environment(Store.self) private var store
    let model: KernelModel
    @State private var text = ""
    @FocusState private var focused: Bool

    var body: some View {
        HStack(alignment: .bottom, spacing: 8) {
            TextField("Add a rule in plain words, like: when someone reports a bug in a repo, try to reproduce it quietly and only tell me if you find a simple fix.", text: $text, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(2...5)
                .focused($focused)
                .onSubmit { Task { try? await add() } }
                .onKeyPress(.return, phases: .down) { press in
                    guard press.modifiers.contains(.shift) else { return .ignored }
                    text += "\n"
                    return .handled
                }
            AsyncButton("Add rule", action: add)
        }
    }

    private func add() async throws {
        let rule = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !rule.isEmpty else { focused = true; return }
        // Cleared first so a second Return cannot add it twice, and put back if it failed.
        text = ""
        if await !model.policy(["action": "add", "text": rule], store: store) { text = rule }
    }
}

// MARK: Sources

private struct SourcesSection: View {
    let model: KernelModel

    var body: some View {
        let rows = model.sources
        SectionHead("Where events come from")
        if !model.loaded {
            LoadingCard()
        } else {
            Card {
                ForEach(rows) { source in
                    SourceRow(source: source, model: model)
                    if source.id != rows.last?.id { Divider() }
                }
            }
        }
    }
}

private struct SourceRow: View {
    @Environment(Store.self) private var store
    let source: SourceEntry
    let model: KernelModel
    @State private var pending: String?

    private var title: String {
        ["hook": "Webhooks", "fleet": "Your other machines", "github": "GitHub", "whatsapp_link": "Your WhatsApp (read-only)"][source.id] ?? source.id
    }

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: sourceIcon(source.id)).foregroundStyle(.secondary).frame(width: 22).accessibilityHidden(true)
            Text(title).font(.system(size: 13, weight: .medium)).lineLimit(1)
            Spacer(minLength: 8)
            Picker("How to treat \(title)", selection: Binding(get: { pending ?? source.admission }, set: choose)) {
                ForEach(admissions, id: \.0) { value, label in Text(label).tag(value) }
            }
            .labelsHidden()
            .fixedSize()
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }

    private func choose(_ value: String) {
        pending = value
        Task {
            await model.policy(["action": "source", "source": source.id, "admission": value], store: store)
            pending = nil
        }
    }
}

// MARK: Watchdog

private struct WatchdogSection: View {
    let model: KernelModel
    let open: (ActivitySheet) -> Void

    var body: some View {
        let stuck = model.outbox
        let findings = Array(model.findings.prefix(12))
        SectionHead("Watchdog")
        if !model.loaded {
            LoadingCard()
        } else {
            Card {
                if stuck.isEmpty && findings.isEmpty {
                    EmptyNote(title: "Nothing has needed stepping in", detail: "Ask it anything with /watchdog in any chat.")
                } else {
                    LazyVStack(spacing: 0) {
                        ForEach(stuck) { message in
                            StuckRow(message: message)
                            if message.id != stuck.last?.id || !findings.isEmpty { Divider() }
                        }
                        ForEach(findings) { finding in
                            FindingRow(finding: finding, open: open).equatable()
                            if finding.id != findings.last?.id { Divider() }
                        }
                    }
                }
            }
        }
    }
}

private struct StuckRow: View {
    let message: JSON

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "bolt").foregroundStyle(MC.warn).frame(width: 22).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text("A message to \(message.channel.str) has not gone out").font(.system(size: 13, weight: .medium))
                Text("\(plural(message.attempts.int, "try", "tries")) · \(message.error.str)")
                    .font(.system(size: 11.5))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            Spacer(minLength: 8)
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
    }
}

private struct FindingRow: View, Equatable {
    let finding: JSON
    let open: (ActivitySheet) -> Void

    nonisolated static func == (a: FindingRow, b: FindingRow) -> Bool { a.finding == b.finding }

    var body: some View {
        // Only workers have a detail to open.
        if finding.session.str.hasPrefix("w_") {
            Button { open(.worker(finding.session.str)) } label: { content }
                .buttonStyle(RowButtonStyle())
        } else {
            content
        }
    }

    private var content: some View {
        HStack(spacing: 10) {
            Image(systemName: "checkmark.shield").foregroundStyle(.secondary).frame(width: 22).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text("\(finding.verdict.str.replacingOccurrences(of: "_", with: " ")): \(finding.action.str.isEmpty ? "noted" : finding.action.str)")
                    .font(.system(size: 13, weight: .medium))
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                LiveLine { line }
            }
            Spacer(minLength: 8)
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
    }

    private var line: String {
        var parts = [finding.session.str, ago(finding.ts.num)]
        if !finding.reason.str.isEmpty { parts.append(clip(finding.reason.str, 110)) }
        return parts.joined(separator: " · ")
    }
}

// MARK: Worker detail

private struct WorkerStep: Identifiable, Equatable, Sendable {
    enum Kind: Equatable, Sendable { case call(String), result(failed: Bool), said }
    let id: Int
    let kind: Kind
    let text: String
}

/// One worker's record, shaped off the main thread: a transcript can run to 120 rows of
/// tool output, and only the last few steps are drawn.
private struct WorkerDetail: Equatable, Sendable {
    let session: JSON
    let reports: [JSON]
    let jobs: [JSON]
    let following: String
    let steps: [WorkerStep]

    init(_ reply: JSON) {
        session = reply.session
        reports = Array(reply.reports.list.prefix(6))
        jobs = Array(reply.jobs.list.prefix(6))
        following = reply.subscriptions.list.map { $0.thread.str }.joined(separator: " · ")
        steps = Self.steps(reply.transcript.list)
    }

    private static func steps(_ rows: [JSON]) -> [WorkerStep] {
        var steps: [WorkerStep] = []
        func add(_ kind: WorkerStep.Kind, _ text: String) { steps.append(WorkerStep(id: steps.count, kind: kind, text: text)) }
        for row in rows {
            let blocks: [JSON]
            if case .array(let list) = row.content { blocks = list } else { blocks = [.object(["type": .string("text"), "text": row.content])] }
            for block in blocks {
                switch block.type.str {
                case "tool_use":
                    add(.call(block.name.str.replacingOccurrences(of: "_", with: " ")), String(oneLine(compact(block.input)).prefix(180)))
                case "tool_result":
                    let content: String
                    if case .string(let string) = block.content { content = string } else { content = compact(block.content) }
                    add(.result(failed: block.is_error.flag), String(content.prefix(400)))
                case "text" where row.role.str == "assistant":
                    add(.said, String(block.text.str.prefix(600)))
                default:
                    break
                }
            }
        }
        return Array(steps.suffix(24))
    }

    private static func compact(_ value: JSON) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: value.raw, options: [.sortedKeys, .withoutEscapingSlashes, .fragmentsAllowed]) else { return value.str }
        return String(decoding: data, as: UTF8.self)
    }
}

private struct WorkerSheet: View {
    @Environment(Store.self) private var store
    @Environment(\.dismiss) private var dismiss
    let id: String
    let model: KernelModel
    @State private var detail: WorkerDetail?
    @State private var gate = ReadGate()

    /// What is already known, drawn while the full record is read.
    private var known: JSON {
        detail?.session ?? model.sessions.first { $0.id == id } ?? store.workers.first { $0.id == id } ?? .null
    }

    var body: some View {
        let session = known
        SheetFrame(title: "\(store.name(session.profile.str)): \(session.title.str.isEmpty ? "Task" : session.title.str)") {
            if !session.status.str.isEmpty { Pill(status: session.status.str) }
            if let detail {
                WorkerRecord(detail: detail)
            } else {
                ProgressView().controlSize(.small).frame(maxWidth: .infinity).padding(18).accessibilityLabel("Loading")
            }
        } actions: {
            if !session.isNull, session.status.str != "ended" {
                Divider()
                StepIn(id: id, reload: { await gate.now(read) }, act: act)
            }
        }
        .background(LiveReader(gate: gate, read: read))
    }

    private func read() async {
        guard let api = store.api else { return }
        do {
            let reply = try await api.get("/api/kernel/sessions/\(API.escape(id))")
            let next = await Task.detached(priority: .userInitiated) { WorkerDetail(reply) }.value
            if next != detail { detail = next }
        } catch {
            // Once the record is on screen, one failed refresh is not worth interrupting for.
            if detail == nil {
                store.notify(error.localizedDescription, error: true)
                dismiss()
            }
        }
    }

    private func act(_ action: String) async throws {
        guard let api = store.api else { throw APIError(message: "Not connected to the runtime yet.") }
        _ = try await api.post("/api/kernel/sessions/\(API.escape(id))/\(action)")
        store.notify(action == "stop" ? "Stopped" : "Restarted")
        dismiss()
        store.refreshSoon()
        await model.reload(api)
    }
}

private struct WorkerRecord: View {
    let detail: WorkerDetail

    var body: some View {
        let session = detail.session
        if !session.brief.str.isEmpty {
            DetailBlock(title: "Router's note") { TextBlock(text: session.brief.str) }
        }
        if !session.summary.str.isEmpty {
            DetailBlock(title: "Where it stands") { TextBlock(text: session.summary.str) }
        }
        if !detail.reports.isEmpty {
            DetailBlock(title: "Reports") {
                Card {
                    ForEach(detail.reports) { report in
                        ReportRow(report: report)
                        if report.id != detail.reports.last?.id { Divider() }
                    }
                }
            }
        }
        if !detail.jobs.isEmpty {
            DetailBlock(title: "Background jobs") {
                Card {
                    ForEach(detail.jobs) { job in
                        JobRow(job: job)
                        if job.id != detail.jobs.last?.id { Divider() }
                    }
                }
            }
        }
        if !detail.following.isEmpty {
            DetailBlock(title: "Following") {
                Text(detail.following).font(.system(size: 12)).foregroundStyle(.secondary).textSelection(.enabled)
            }
        }
        DetailBlock(title: "Latest steps") {
            if detail.steps.isEmpty {
                Text("Nothing yet.").font(.system(size: 12)).foregroundStyle(.secondary)
            } else {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(detail.steps) { StepView(step: $0) }
                }
            }
        }
        Text(meta(session)).font(.system(size: 12)).foregroundStyle(.secondary).textSelection(.enabled)
    }

    private func meta(_ session: JSON) -> String {
        var parts = [session.id]
        if !session.mission.str.isEmpty { parts.append("task \(session.mission.str)") }
        parts.append(session.strength.str == "inferred" ? "started from an event, so outward commands wait for you" : "started by you")
        return parts.joined(separator: " · ")
    }
}

private struct ReportRow: View {
    let report: JSON

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(clip(report.summary.str, 220)).font(.system(size: 13)).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                if !report.question.str.isEmpty {
                    Text("Asks: \(report.question.str)").font(.system(size: 11.5)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                LiveLine { ago(report.ts.num) }
            }
            Spacer(minLength: 8)
            Pill(status: report.status.str)
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }
}

private struct JobRow: View {
    let job: JSON

    var body: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(job.tool.str).font(.system(size: 13, weight: .medium)).lineLimit(1)
                Text(job.label.str).font(.system(size: 11.5)).foregroundStyle(.secondary).lineLimit(2)
            }
            Spacer(minLength: 8)
            Pill(status: job.status.str)
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }
}

private struct StepView: View {
    let step: WorkerStep
    private var shape: RoundedRectangle { RoundedRectangle(cornerRadius: 8, style: .continuous) }

    var body: some View {
        Group {
            switch step.kind {
            case .call(let name):
                (Text(name).fontWeight(.semibold) + Text("  ") + Text(step.text).font(.system(size: 11.5, design: .monospaced)).foregroundColor(.secondary))
                    .padding(.horizontal, 9).padding(.vertical, 6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.primary.opacity(0.05), in: shape)
            case .result(let failed):
                Text(failed ? "Error: \(step.text)" : step.text)
                    .foregroundStyle(failed ? MC.danger : Color.secondary)
                    .padding(.horizontal, 9).padding(.vertical, 6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .overlay(shape.strokeBorder(MC.line, lineWidth: 0.5))
            case .said:
                Text(step.text)
                    .padding(.horizontal, 9).padding(.vertical, 6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.primary.opacity(0.05), in: shape)
            }
        }
        .font(.system(size: 12.5))
        .fixedSize(horizontal: false, vertical: true)
        .textSelection(.enabled)
    }
}

/// Its own view, so a live refresh of the record never disturbs what is being typed.
private struct StepIn: View {
    @Environment(Store.self) private var store
    let id: String
    let reload: () async -> Void
    let act: (String) async throws -> Void
    @State private var text = ""
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Step in").font(.system(size: 12, weight: .semibold)).foregroundStyle(.secondary)
            TextField("Tell this worker something directly. It reads it at its next step.", text: $text, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(2...5)
                .focused($focused)
                .onSubmit {
                    Task {
                        do { try await send() } catch { store.notify(error.localizedDescription, error: true) }
                    }
                }
                .onKeyPress(.return, phases: .down) { press in
                    guard press.modifiers.contains(.shift) else { return .ignored }
                    text += "\n"
                    return .handled
                }
            HStack(spacing: 8) {
                Spacer()
                AsyncButton(action: { try await act("stop") }) { Text("Stop").foregroundStyle(MC.danger) }
                AsyncButton("Restart") { try await act("restart") }
                AsyncButton("Send", action: send).keyboardShortcut(.defaultAction)
            }
        }
        .padding(14)
    }

    private func send() async throws {
        let message = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !message.isEmpty else { focused = true; return }
        guard let api = store.api else { throw APIError(message: "Not connected to the runtime yet.") }
        // Return can reach both the field and the default button. Clearing first means the
        // second one finds nothing to send, and the words come back if the send failed.
        text = ""
        do {
            _ = try await api.post("/api/kernel/sessions/\(API.escape(id))/message", ["text": message])
        } catch {
            text = message
            throw error
        }
        store.notify("Sent")
        await reload()
    }
}

// MARK: Sheet parts

private struct SheetFrame<Content: View, Actions: View>: View {
    @Environment(\.dismiss) private var dismiss
    let title: String
    @ViewBuilder var content: Content
    @ViewBuilder var actions: Actions

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Text(title).font(.headline).lineLimit(2)
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            .padding(.horizontal, 16).padding(.vertical, 12)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 14) { content }
                    .padding(18)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            actions
        }
        .frame(minWidth: 540, idealWidth: 640, maxWidth: 900, minHeight: 380, idealHeight: 640, maxHeight: 900)
    }
}

private struct DetailBlock<Content: View>: View {
    let title: String
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.system(size: 12, weight: .semibold)).foregroundStyle(.secondary)
            content
        }
    }
}

private struct TextBlock: View {
    let text: String
    var mono = false
    var body: some View {
        Text(text)
            .font(mono ? .system(size: 12, design: .monospaced) : .system(size: 13))
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(10)
            .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}
