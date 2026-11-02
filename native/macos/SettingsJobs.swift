import SwiftUI

/// The jobs and held commands come from what the store already holds, so they are on
/// screen the moment the pane opens. Nothing can be approved until the store has read the
/// server, because before that it is showing a snapshot from disk.
@MainActor
struct SettingsJobsPane: View {
    @Environment(Store.self) private var store
    /// The store's copy keeps only the newest rows of every status, so an old watch that
    /// is still running can be missing from it. It fills the pane until this arrives.
    @State private var watches: [JSON]?

    var body: some View {
        Form {
            HeartbeatSection()
            BriefSection()
            HeldCommandsSection()
            WatchesSection(fetched: watches) { await loadWatches() }
            ScheduledJobsSection()
        }
        .formStyle(.grouped)
        .task { store.refreshSoon(after: 0) }
        .task(id: store.watches) { await loadWatches() }
    }

    private func loadWatches() async {
        guard let api = store.api, let reply = try? await api.get("/api/watches") else { return }
        if !Task.isCancelled { watches = reply.watches.list }
    }
}

// MARK: Heartbeat and brief

private struct HeartbeatForm: Equatable {
    var enabled = false, agent = "", minutes = "30", checklist = ""
    init(_ reply: JSON) {
        enabled = reply.enabled.flag
        agent = reply.agent.str
        minutes = reply.every_minutes.optStr ?? "30"
        checklist = reply.checklist.str
    }
}

@MainActor
private struct HeartbeatSection: View {
    @Environment(Store.self) private var store
    private static let path = "/api/heartbeat"
    @State private var form = SettingsDraft(reply: SettingsCache.last(HeartbeatSection.path), HeartbeatForm.init)
    @State private var status = SettingsStatus.none
    @State private var problem: String?

    var body: some View {
        Section {
            SettingsProblemRow(problem: problem)
            // Loading hangs off one row, because a modifier on a section applies to each row in it.
            SettingsSwitch(title: "Heartbeat", note: "A proactive pulse: the agent reviews your checklist and only speaks up when something needs attention.", isOn: $form.value.enabled)
                .disabled(!form.ready)
                .settingsLoad { await load() }
            SettingsAgentPicker(title: "Profile", selection: $form.value.agent)
            TextField("Every (minutes)", text: $form.value.minutes, prompt: Text("30"))
            VStack(alignment: .leading, spacing: 5) {
                Text("Checklist")
                SettingsTextBox(label: "Checklist", text: $form.value.checklist, height: 120)
            }
            SettingsActions(status: status) {
                AsyncButton("Save") { await save() }.buttonStyle(.borderedProminent).disabled(!form.ready)
            }
        }
    }

    private func load() async {
        do {
            form.receive(HeartbeatForm(try await SettingsCache.read(store.settingsAPI(), Self.path)))
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func save() async {
        let minutes = min(max(Int(form.value.minutes.settingsTrimmed) ?? 30, 5), 1440)
        do {
            _ = try await store.settingsAPI().put(Self.path, [
                "enabled": form.value.enabled, "agent": store.settingsAgent(form.value.agent),
                "every_minutes": minutes, "checklist": form.value.checklist,
            ])
            var saved = form.value
            saved.minutes = String(minutes)
            form.reset(to: saved)
            status = .good("Saved")
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
    }
}

private struct BriefForm: Equatable {
    var enabled = false, agent = "", at = "08:00"
    init(_ reply: JSON) {
        enabled = reply.enabled.flag
        agent = reply.agent.str
        at = reply.at.optStr ?? "08:00"
    }
}

@MainActor
private struct BriefSection: View {
    @Environment(Store.self) private var store
    private static let path = "/api/settings/brief"
    @State private var form = SettingsDraft(reply: SettingsCache.last(BriefSection.path), BriefForm.init)
    @State private var status = SettingsStatus.none

    /// The server keeps the time as text, HH:MM.
    private var time: Binding<Date> {
        Binding(get: {
            let parts = form.value.at.split(separator: ":").compactMap { Int($0) }
            let hour = parts.first ?? 8, minute = parts.count > 1 ? parts[1] : 0
            return Calendar.current.date(bySettingHour: hour, minute: minute, second: 0, of: Date()) ?? Date()
        }, set: { date in
            let parts = Calendar.current.dateComponents([.hour, .minute], from: date)
            form.value.at = String(format: "%02d:%02d", parts.hour ?? 8, parts.minute ?? 0)
        })
    }

    var body: some View {
        Section {
            SettingsSwitch(title: "Daily brief", note: "A proactive morning digest: overnight GitHub activity, messages, job outcomes, and anything waiting on you, sent to your chat and phone.", isOn: $form.value.enabled)
                .disabled(!form.ready)
                .settingsLoad { await load() }
            SettingsAgentPicker(title: "Profile", selection: $form.value.agent)
            DatePicker("At", selection: time, displayedComponents: .hourAndMinute)
            SettingsActions(status: status) {
                AsyncButton("Save") { await save() }.buttonStyle(.borderedProminent).disabled(!form.ready)
            }
        }
    }

    private func load() async {
        guard let api = store.api, let reply = try? await SettingsCache.read(api, Self.path) else { return }
        form.receive(BriefForm(reply))
    }

    private func save() async {
        do {
            let reply = try await store.settingsAPI().put(Self.path, [
                "enabled": form.value.enabled, "agent": store.settingsAgent(form.value.agent), "at": form.value.at,
            ])
            form.reset(to: BriefForm(reply))
            status = .good("Saved")
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
    }
}

// MARK: Held commands

@MainActor
private struct HeldCommandsSection: View {
    @Environment(Store.self) private var store

    private var held: [JSON] { store.inbox.filter { ["command", "tool"].contains($0.kind.str) } }

    var body: some View {
        if !held.isEmpty {
            Section {
                ForEach(held) { row in HeldCommandRow(row: row) }
            } header: {
                SettingsHint("Held for approval. Approving runs the command exactly as written.")
            }
        }
    }
}

/// The command is shown whole and never shortened, since approving runs exactly this.
@MainActor
private struct HeldCommandRow: View {
    @Environment(Store.self) private var store
    let row: JSON

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(row.kind.str == "tool" ? "Tool call held for you" : "Command held for you").font(.headline)
                Spacer()
                Pill(status: "pending", label: "needs approval")
            }
            Text("\(store.name(row.agent.str)) · in \(row.detail.str)")
                .font(.caption)
                .foregroundStyle(.secondary)
            SettingsCode(row.title.str)
            if !row.reason.str.isEmpty {
                Text(row.reason.str).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            PriorDecisionNote(prior: row.prior)
            HStack(spacing: 8) {
                AsyncButton("Run it") {
                    try await store.decide(approval: row.id, approve: true)
                }
                .buttonStyle(.borderedProminent)
                .disabled(!store.live)
                AsyncButton("Decline") {
                    try await store.decide(approval: row.id, approve: false)
                }
                .disabled(!store.live)
            }
        }
        .padding(.vertical, 2)
    }
}

/// Three near-identical held commands are impossible to tell apart, so this says which one
/// was already answered and what moved since.
private struct PriorDecisionNote: View {
    let prior: JSON

    var body: some View {
        if !prior.isNull {
            let verb = prior.status.str == "denied" ? "declined" : "approved"
            let when = settingsClock(prior.ts.num)
            if prior.identical.flag {
                Text("Identical to the command you \(verb) at \(when). Approving runs it again.")
                    .font(.caption)
                    .foregroundStyle(MC.warn)
            } else {
                DisclosureGroup("Differs from the command you \(verb) at \(when)") {
                    let lines = prior.diff.list.map(\.str)
                    if lines.isEmpty {
                        Text("Only whitespace changed.").font(.caption).foregroundStyle(.secondary)
                    }
                    ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                        SettingsCode(line).foregroundStyle(line.hasPrefix("+") ? MC.good : MC.danger)
                    }
                }
                .font(.caption)
            }
        }
    }
}

// MARK: Watches

@MainActor
private struct WatchesSection: View {
    @Environment(Store.self) private var store
    let fetched: [JSON]?
    let reload: () async -> Void

    private var rows: [JSON] { fetched ?? store.watches.filter { $0.status.str == "active" } }

    var body: some View {
        if !rows.isEmpty {
            Section {
                ForEach(rows) { watch in row(watch) }
            } header: {
                SettingsHint("Active watches, polling automatically with no approval.")
            }
        }
    }

    private func row(_ watch: JSON) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(watch.name.str).font(.headline)
                Spacer()
                Pill(status: "active", label: "\(watch.checks.int)/\(watch.max_checks.int) checks")
            }
            Text("\(store.name(watch.agent.str)) · every \(watch.interval.int)s · no approval, auto-stops")
                .font(.caption)
                .foregroundStyle(.secondary)
            SettingsCode(watch.poll.str)
            AsyncButton("Stop watching") {
                _ = try await store.settingsAPI().post("/api/watches/\(API.escape(watch.id))/cancel")
                store.refreshSoon(after: 0.05)
                await reload()
            }
        }
        .padding(.vertical, 2)
    }
}

// MARK: Jobs

@MainActor
private struct ScheduledJobsSection: View {
    @Environment(Store.self) private var store

    var body: some View {
        Section {
            if store.jobs.isEmpty {
                Text("No scheduled jobs yet. Ask it to schedule something, e.g. “check the error logs every morning at 9”.")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(store.jobs) { job in JobRow(job: job) }
        } header: {
            SettingsHint("The agent can schedule recurring or one-off work, but nothing runs until you approve it here.")
        }
    }
}

/// An agent writes every field of a job it schedules. The check it runs before each fire
/// is shown whole, since approving runs it.
@MainActor
private struct JobRow: View {
    @Environment(Store.self) private var store
    let job: JSON

    private var meta: String {
        var parts = [store.name(job.agent.str), job.schedule.str, "next: \(next)"]
        if job.max_runs.int > 0 { parts.append("\(job.runs.int)/\(job.max_runs.int) checks") }
        return parts.joined(separator: " · ")
    }

    private var next: String {
        guard job.next_run.num > 0 else { return "-" }
        return Date(timeIntervalSince1970: job.next_run.num)
            .formatted(Date.FormatStyle().month(.abbreviated).day().hour().minute())
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(job.name.str).font(.headline)
                Spacer()
                Pill(status: job.status.str)
            }
            Text(meta).font(.caption).foregroundStyle(.secondary)
            Text(job.task.str).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            if !job.monitor_cmd.str.isEmpty {
                Text("Check before each run:").font(.caption).foregroundStyle(.secondary)
                SettingsCode(job.monitor_cmd.str)
            }
            if !job.note.str.isEmpty {
                Text("Notepad: \(job.note.str)").font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            }
            HStack(spacing: 8) {
                switch job.status.str {
                case "pending": action("Approve", "approve", prominent: true).disabled(!store.live)
                case "active": action("Pause", "pause")
                case "paused": action("Resume", "resume", prominent: true)
                default: EmptyView()
                }
                action("Delete", "delete")
            }
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder private func action(_ title: String, _ name: String, prominent: Bool = false) -> some View {
        let button = AsyncButton(title) {
            _ = try await store.settingsAPI().post("/api/cron/\(API.escape(job.id))/\(name)")
            store.refreshSoon(after: 0.05)
        }
        if prominent { button.buttonStyle(.borderedProminent) } else { button }
    }
}
