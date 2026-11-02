import AppKit
import SwiftUI

private let staleMessage = "Still reading the current state. Try again in a moment."

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

/// Recurring jobs and watches, both straight from the store. The two lists move on their
/// own events, so each section observes only its own rows.
struct ScheduleView: View {
    @Environment(Store.self) private var store

    var body: some View {
        Page(title: "Schedule", subtitle: "Recurring jobs run only after you approve them. Watches follow something already in flight and stop themselves when it resolves.") {
            Button { store.go(.settings("jobs")) } label: { Label("Heartbeat and daily brief", systemImage: "gearshape") }
        } content: {
            JobsSection()
            WatchesSection()
        }
        .task { await quietRefresh(store) }
    }
}

// MARK: Jobs

private struct JobsSection: View {
    @Environment(Store.self) private var store

    var body: some View {
        let jobs = store.jobs
        SectionHead("Scheduled jobs", count: jobs.isEmpty ? nil : jobs.count)
        Card {
            if jobs.isEmpty {
                EmptyNote(title: "No scheduled jobs", detail: "Ask it to check something every morning, and approve it here.")
            } else {
                LazyVStack(spacing: 0) {
                    ForEach(jobs) { job in
                        JobRow(job: job).equatable()
                        if job.id != jobs.last?.id { Divider() }
                    }
                }
            }
        }
    }
}

private struct JobRow: View, Equatable {
    @Environment(Store.self) private var store
    let job: JSON

    nonisolated static func == (a: JobRow, b: JobRow) -> Bool { a.job == b.job }

    var body: some View {
        let status = job.status.str
        HStack(alignment: .top, spacing: 10) {
            AgentBadge(agent: store.agent(job.agent.str))
            VStack(alignment: .leading, spacing: 5) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(job.name.str).font(.system(size: 13, weight: .medium)).lineLimit(2)
                    Spacer(minLength: 8)
                    Pill(status: status)
                }
                Text(summary).font(.system(size: 11.5)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                // What approving will run is shown whole. Once it is running, two lines are enough.
                Text(job.task.str)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .lineLimit(status == "pending" ? nil : 2)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                if !job.monitor_cmd.str.isEmpty {
                    Text("Check before each run:").font(.system(size: 11.5)).foregroundStyle(.secondary).padding(.top, 2)
                    Text(job.monitor_cmd.str)
                        .font(.system(size: 11.5, design: .monospaced))
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(8)
                        .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                }
                if !job.note.str.isEmpty {
                    Text("Notes: \(job.note.str)").font(.system(size: 11.5)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                actions(status).padding(.top, 3)
            }
        }
        .padding(12)
    }

    private func actions(_ status: String) -> some View {
        HStack(spacing: 6) {
            if status == "pending" {
                AsyncButton("Approve") { try await run("approve") }.buttonStyle(.borderedProminent)
            }
            if status == "active" {
                AsyncButton("Pause") { try await run("pause") }
            }
            if status == "paused" {
                AsyncButton("Resume") { try await run("resume") }
            }
            AsyncButton(action: { try await run("delete") }) { Text("Delete").foregroundStyle(MC.danger) }
        }
        .controlSize(.small)
    }

    private var summary: String {
        var parts = [store.name(job.agent.str), job.schedule.str, "next \(nextRun(job.next_run.num))"]
        if job.max_runs.int > 0 { parts.append("\(job.runs.int)/\(job.max_runs.int) runs") }
        return parts.joined(separator: " · ")
    }

    /// The reply carries the whole list, so the row changes without waiting for another read.
    private func run(_ action: String) async throws {
        guard let api = store.api, store.live else { throw APIError(message: staleMessage) }
        let reply = try await api.post("/api/cron/\(API.escape(job.id))/\(action)")
        let rows = reply.jobs.list
        if rows != store.jobs { store.jobs = rows }
        // A pending job is also an inbox item, and those counts come from the full read.
        store.refreshSoon(after: 0.05)
    }
}

private func nextRun(_ ts: Double) -> String {
    guard ts > 0 else { return "not scheduled" }
    return Date(timeIntervalSince1970: ts).formatted(.dateTime.weekday(.abbreviated).hour().minute())
}

// MARK: Watches

private struct WatchesSection: View {
    @Environment(Store.self) private var store

    var body: some View {
        let watches = ordered(store.watches)
        let active = watches.filter { $0.status.str == "active" }.count
        SectionHead("Watches", count: active > 0 ? active : nil)
        Card {
            if watches.isEmpty {
                EmptyNote(title: "No watches", detail: "A task arms one to follow a CI run, a deploy or a status page.")
            } else {
                LazyVStack(spacing: 0) {
                    ForEach(watches) { watch in
                        WatchRow(watch: watch).equatable()
                        if watch.id != watches.last?.id { Divider() }
                    }
                }
            }
        }
    }

    private func ordered(_ rows: [JSON]) -> [JSON] {
        let sorted = rows.sorted { a, b in
            let first = a.status.str == "active", second = b.status.str == "active"
            return first != second ? first : a.ts.num > b.ts.num
        }
        return Array(sorted.prefix(24))
    }
}

private struct WatchRow: View, Equatable {
    @Environment(Store.self) private var store
    let watch: JSON

    nonisolated static func == (a: WatchRow, b: WatchRow) -> Bool { a.watch == b.watch }

    var body: some View {
        let status = watch.status.str
        HStack(alignment: .top, spacing: 10) {
            AgentBadge(agent: store.agent(watch.agent.str))
            VStack(alignment: .leading, spacing: 5) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(watch.name.str).font(.system(size: 13, weight: .medium)).lineLimit(2)
                    Spacer(minLength: 8)
                    Pill(status: status)
                }
                Text(summary).font(.system(size: 11.5)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                ProgressView(value: min(1, watch.checks.num / max(1, watch.max_checks.num)))
                    .progressViewStyle(.linear)
                    .controlSize(.small)
                    .accessibilityLabel("Checks used")
                if !watch.last_out.str.isEmpty {
                    OutputBlock(text: String(watch.last_out.str.prefix(1200))).padding(.top, 3)
                }
                if status == "active" {
                    AsyncButton(action: { try await cancel() }) { Text("Stop watching").foregroundStyle(MC.danger) }
                        .controlSize(.small)
                        .padding(.top, 3)
                }
            }
        }
        .padding(12)
    }

    private var summary: String {
        var parts = [store.name(watch.agent.str), "\(watch.checks.int)/\(watch.max_checks.int) checks", "every \(watch.interval.int)s"]
        if !watch.goal.str.isEmpty { parts.append("waiting for: \(watch.goal.str)") }
        return parts.joined(separator: " · ")
    }

    private func cancel() async throws {
        guard let api = store.api, store.live else { throw APIError(message: staleMessage) }
        _ = try await api.post("/api/watches/\(API.escape(watch.id))/cancel")
        await store.refresh()
    }
}

/// A watch's last output, in a window of its own so a long one does not push the list down.
/// The height is set from the text, because a scroll view inside a scroll view has no size
/// of its own to fall back on.
private struct OutputBlock: View {
    let text: String

    private var height: CGFloat {
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false).reduce(0) { $0 + max(1, ($1.count + 79) / 80) }
        return min(120, CGFloat(lines) * 15 + 16)
    }

    var body: some View {
        ScrollView {
            Text(text)
                .font(.system(size: 11.5, design: .monospaced))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(8)
        }
        .frame(height: height)
        .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}
