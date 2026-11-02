import AppKit
import SwiftUI

private let resumableStatuses: Set<String> = ["waiting", "failed", "cancelled"]

private enum TaskFilter: String, CaseIterable {
    case active, waiting, done, all

    var name: String {
        switch self {
        case .active: return "In progress"
        case .waiting: return "Waiting"
        case .done: return "Finished"
        case .all: return "All"
        }
    }

    func includes(_ mission: JSON) -> Bool {
        switch self {
        case .active: return ["running", "queued"].contains(mission.status.str)
        case .waiting: return mission.status.str == "waiting"
        case .done: return ["completed", "failed", "cancelled"].contains(mission.status.str)
        case .all: return true
        }
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

/// Every objective handed over. Drawn from the store, so it is there the moment it opens.
struct TasksView: View {
    @Environment(Store.self) private var store
    @AppStorage("tasksFilter") private var filter: TaskFilter = .active
    @State private var opened: JSON?

    var body: some View {
        Page(title: "Tasks", subtitle: "Every objective you have handed over, what it runs as, and where it stands. Tasks survive restarts and wait for you to resume them.") {
            Button { store.focusHome() } label: { Label("New task", systemImage: "plus") }
                .buttonStyle(.borderedProminent)
        } content: {
            Picker("Show", selection: $filter) {
                ForEach(TaskFilter.allCases, id: \.self) { option in
                    Text("\(option.name) \(store.missions.filter(option.includes).count)").tag(option)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
            TaskList(rows: store.missions.filter(filter.includes), filter: filter) { opened = $0 }
        }
        .task { await quietRefresh(store) }
        .sheet(item: $opened) { mission in
            MissionSheet(initial: mission).environment(store)
        }
    }
}

private struct TaskList: View {
    let rows: [JSON]
    let filter: TaskFilter
    let open: (JSON) -> Void

    var body: some View {
        Card {
            if rows.isEmpty {
                EmptyNote(title: filter == .done ? "Nothing finished yet" : "No tasks here",
                          detail: "Describe an outcome on Home and a worker takes it on.")
            } else {
                LazyVStack(spacing: 0) {
                    ForEach(rows) { mission in
                        TaskRow(mission: mission, open: open).equatable()
                        if mission.id != rows.last?.id { Divider() }
                    }
                }
            }
        }
    }
}

private struct TaskRow: View, Equatable {
    @Environment(Store.self) private var store
    let mission: JSON
    let open: (JSON) -> Void
    @State private var hovering = false

    nonisolated static func == (a: TaskRow, b: TaskRow) -> Bool { a.mission == b.mission }

    var body: some View {
        HStack(spacing: 8) {
            Button { open(mission) } label: {
                HStack(spacing: 10) {
                    AgentBadge(agent: store.agent(mission.agent.str))
                    VStack(alignment: .leading, spacing: 2) {
                        Text(mission.objective.str)
                            .font(.system(size: 13, weight: .medium))
                            .lineLimit(2)
                            .multilineTextAlignment(.leading)
                        LiveLine { "\(store.name(mission.agent.str)) · updated \(ago(mission.updated.num))" }
                    }
                    Spacer(minLength: 8)
                    Pill(status: mission.status.str)
                }
                .padding(.leading, 12).padding(.vertical, 9)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if resumableStatuses.contains(mission.status.str) {
                AsyncButton("Resume") { try await resume(mission.id, store: store) }
                    .controlSize(.small)
            }
        }
        .padding(.trailing, 12)
        .background(Color.primary.opacity(hovering ? 0.04 : 0))
        .onHover { hovering = $0 }
    }
}

/// The reply says only that it started, so the list is read again before the button lets go.
@MainActor
private func resume(_ id: String, store: Store) async throws {
    // A task on the disk snapshot may already be running, and resuming starts it again.
    guard let api = store.api, store.live else { throw APIError(message: "Still reading the current state. Try again in a moment.") }
    _ = try await api.post("/api/command", ["text": "/resume \(id)"])
    store.notify("Resumed.")
    await store.refresh()
}

/// A line with a relative time in it, kept true while the screen stays open.
private struct LiveLine: View {
    let text: () -> String
    var body: some View {
        TimelineView(.everyMinute) { _ in Text(text()) }
            .font(.system(size: 11.5))
            .foregroundStyle(.secondary)
            .lineLimit(1)
    }
}

// MARK: Detail

/// One sheet for a task and the steps under it. A step opens in place, with a way back.
private struct MissionSheet: View {
    let initial: JSON
    @State private var execution: JSON?

    var body: some View {
        if let execution {
            ExecutionPane(initial: execution) { self.execution = nil }
        } else {
            MissionPane(initial: initial) { execution = $0 }
        }
    }
}

private struct MissionPane: View {
    @Environment(Store.self) private var store
    @Environment(\.dismiss) private var dismiss
    let initial: JSON
    let openStep: (JSON) -> Void

    private var current: JSON { store.missions.first { $0.id == initial.id } ?? initial }

    var body: some View {
        let mission = current
        let steps = store.executions.filter { $0.mission.str == mission.id }
        let worker = store.workers.first { $0.mission.str == mission.id }
        SheetFrame(title: "Task") {
            Pill(status: mission.status.str)
            DetailBlock(title: "Objective") { TextBlock(text: mission.objective.str) }
            DetailBlock(title: "Latest result or checkpoint") {
                if mission.result.str.isEmpty {
                    TextBlock(text: "No result has been reported yet.")
                } else {
                    MarkdownView(text: mission.result.str)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                        .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                }
            }
            if !steps.isEmpty {
                DetailBlock(title: "Steps recorded") {
                    Card {
                        ForEach(steps.prefix(12)) { step in
                            StepRow(step: step) { openStep(step) }
                            if step.id != steps.prefix(12).last?.id { Divider() }
                        }
                    }
                }
            }
            Text("\(store.name(mission.agent.str)) · task \(mission.id) · started \(Date(timeIntervalSince1970: mission.ts.num).formatted(date: .abbreviated, time: .shortened))")
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
        } actions: {
            Divider()
            HStack(spacing: 8) {
                Spacer()
                if resumableStatuses.contains(mission.status.str) {
                    AsyncButton("Resume") {
                        try await resume(mission.id, store: store)
                        dismiss()
                    }
                }
                Button("Open conversation") {
                    dismiss()
                    store.go(.chat(mission.thread.str))
                }
                .keyboardShortcut(worker == nil ? KeyboardShortcut.defaultAction : nil)
                if let worker {
                    Button("Open worker") {
                        dismiss()
                        // Activity shows the worker in a sheet of its own, which cannot go
                        // up until this one has finished closing.
                        Task {
                            try? await Task.sleep(nanoseconds: 250_000_000)
                            store.focusWorker = worker.id
                            store.go(.activity)
                        }
                    }
                    .keyboardShortcut(.defaultAction)
                }
            }
            .padding(14)
        }
    }
}

private struct StepRow: View {
    @Environment(Store.self) private var store
    let step: JSON
    let open: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: open) {
            HStack(spacing: 10) {
                Image(systemName: "bolt").foregroundStyle(.secondary).frame(width: 18).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text(step.name.str.replacingOccurrences(of: "_", with: " "))
                        .font(.system(size: 13, weight: .medium))
                        .lineLimit(1)
                    LiveLine { "\(store.name(step.agent.str)) · \(ago(step.ts.num))" }
                }
                Spacer(minLength: 8)
                Pill(status: step.status.str)
            }
            .padding(.horizontal, 12).padding(.vertical, 8)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .background(Color.primary.opacity(hovering ? 0.04 : 0))
        .onHover { hovering = $0 }
    }
}

private struct ExecutionPane: View {
    @Environment(Store.self) private var store
    let initial: JSON
    let back: () -> Void
    @State private var inputs = ""

    private var current: JSON { store.executions.first { $0.id == initial.id } ?? initial }

    var body: some View {
        let record = current
        let detail = record.detail
        SheetFrame(title: record.name.str.replacingOccurrences(of: "_", with: " "), back: back) {
            Pill(status: record.status.str)
            Text(summary(record))
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
            DetailBlock(title: "Inputs") { TextBlock(text: inputs, mono: true) }
            DetailBlock(title: "Result") { TextBlock(text: result(record), mono: true) }
        } actions: {
            EmptyView()
        }
        .task(id: detail) {
            // Inputs can hold a whole file, so formatting them stays off the main thread.
            inputs = await Task.detached(priority: .userInitiated) { prettyJSON(detail) }.value
        }
    }

    private func summary(_ record: JSON) -> String {
        var parts = [store.name(record.agent.str), record.kind.str,
                     Date(timeIntervalSince1970: record.ts.num).formatted(date: .omitted, time: .standard),
                     String(format: "%.1fs", max(0, record.updated.num - record.ts.num))]
        if !record.mission.str.isEmpty { parts.append("task \(record.mission.str)") }
        return parts.joined(separator: " · ")
    }

    private func result(_ record: JSON) -> String {
        if !record.result.str.isEmpty { return record.result.str }
        return record.status.str == "running" ? "Running…" : "No output recorded."
    }
}

private func prettyJSON(_ value: JSON) -> String {
    guard let data = try? JSONSerialization.data(withJSONObject: value.raw, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes, .fragmentsAllowed]) else { return value.str }
    return String(decoding: data, as: UTF8.self)
}

// MARK: Sheet parts

private struct SheetFrame<Content: View, Actions: View>: View {
    @Environment(\.dismiss) private var dismiss
    let title: String
    var back: (() -> Void)?
    @ViewBuilder var content: Content
    @ViewBuilder var actions: Actions

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                if let back {
                    Button(action: back) { Label("Back", systemImage: "chevron.left") }
                }
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
