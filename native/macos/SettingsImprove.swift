import SwiftUI

private struct DiffSheet: Identifiable {
    let id: String, title: String
    var diff: String
}

/// Mission Control can change its own code. These switches decide what lands on its own
/// and what waits for the owner.
@MainActor
struct SettingsImprovePane: View {
    @Environment(Store.self) private var store
    private static let path = "/api/improvements"

    @State private var data = SettingsCache.last(SettingsImprovePane.path)
    @State private var problem: String?
    @State private var failure: String?
    @State private var changing: [String: Bool] = [:]
    @State private var diff: DiffSheet?

    private var settings: JSON { data.settings }
    private var enabled: Bool { settings.enabled.flag }
    private var repairing: Bool { enabled && settings.autofix.flag }

    private var lastCheck: String {
        if !repairing { return "Self-repair is off." }
        let last = data.last_check
        return last.ts.num > 0 ? "Last check \(settingsClock(last.ts.num)): \(last.result.str)" : "Checks every 10 minutes."
    }

    var body: some View {
        Form {
            switches
            Section {
                SettingsProblemRow(problem: problem)
                if let blocked = data.blocked.optStr { SettingsStatusLine(status: .bad("Cannot change code right now: \(blocked)")) }
                if let failure { SettingsStatusLine(status: .bad(failure)) }
                SettingsActions(status: .plain(lastCheck)) {
                    AsyncButton("Check now") { await check() }.disabled(!repairing)
                }
            }
            issues
            Section {
                let rows = data.improvements.list
                if !data.isNull && rows.isEmpty { Text("No changes yet.").foregroundStyle(.secondary) }
                ForEach(rows) { row in
                    ImprovementRow(row: row, autoApply: settings.auto_apply.flag) { await showDiff(row) } decide: { action in await decide(row, action) }
                }
            } header: {
                Text("Changes")
            } footer: {
                if !data.protected.list.isEmpty {
                    SettingsHint("Off limits to agents and to self-repair, always: \(data.protected.list.map(\.str).joined(separator: ", ")). A change touching any of those is refused before you ever see it.")
                }
            }
        }
        .formStyle(.grouped)
        .settingsLoad(on: \.improvements) { await load() }
        .sheet(item: $diff) { sheet in
            VStack(alignment: .leading, spacing: 12) {
                Text(sheet.title).font(.headline)
                SettingsTextView(label: "Diff", text: .constant(sheet.diff), editable: false)
                    .frame(height: 480)
                    .overlay(RoundedRectangle(cornerRadius: 6, style: .continuous).strokeBorder(MC.line))
                HStack {
                    Spacer()
                    Button("Close", role: .cancel) { diff = nil }.keyboardShortcut(.cancelAction)
                }
            }
            .padding(18)
            .frame(width: 760)
        }
    }

    private var switches: some View {
        Section {
            improveSwitch("enabled", "Let it propose changes", "A task that hits a real limitation in the app can put a tested branch in front of you. Proposals always wait for your approval.")
            // Each switch only means something once the one above it is on.
            improveSwitch("autofix", "Find and fix its own problems", "Watches its turns for problems that are the app's fault, like replies cut off at the output limit or turns that crash the same way, and has Claude Code write a fix with a test.")
                .disabled(!enabled)
            improveSwitch("auto_apply", "Land fixes that pass the tests", "Merges its own green fixes and restarts once no agent is mid-turn. If the app does not come back, the previous code is put back automatically.")
                .disabled(!repairing)
            improveSwitch("push", "Push landed changes to GitHub", "Pushes once the app is back up on the change. Never forced: if GitHub has commits this Mac lacks, the push is left to you.")
        } header: {
            SettingsHint("Mission Control can change its own code. Every change is written and tested on its own branch, never in the running code. These switches decide what lands on its own and what waits for you.")
        }
    }

    private func improveSwitch(_ key: String, _ title: String, _ note: String) -> some View {
        SettingsSwitch(title: title, note: note, isOn: Binding(get: { changing[key] ?? settings[key].flag }, set: { set(key, $0) }))
    }

    @ViewBuilder private var issues: some View {
        let rows = data.issues.list
        if !rows.isEmpty || repairing {
            Section {
                if rows.isEmpty { Text("No problem has repeated on the code running now.").foregroundStyle(.secondary) }
                ForEach(rows, id: \.self) { row in IssueRow(row: row) }
            } header: {
                Text("Problems it has noticed")
            }
        }
    }

    private func load() async {
        do {
            data = try await SettingsCache.read(store.settingsAPI(), Self.path)
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func set(_ key: String, _ on: Bool) {
        changing[key] = on
        Task {
            do {
                _ = try await store.settingsAPI().put("/api/improvements-enabled", [key: on])
            } catch {
                store.notify(error.localizedDescription, error: true)
            }
            await load()
            changing[key] = nil
        }
    }

    private func check() async {
        do {
            _ = try await store.settingsAPI().post("/api/improvements/check")
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            await load()
        } catch {
            failure = error.localizedDescription
        }
    }

    private func showDiff(_ row: JSON) async {
        do {
            let reply = try await store.settingsAPI().get("/api/improvements/\(API.escape(row.id))/diff")
            diff = DiffSheet(id: row.id, title: reply.title.optStr ?? row.title.str, diff: reply.diff.optStr.flatMap { $0.isEmpty ? nil : $0 } ?? "(empty)")
        } catch {
            failure = error.localizedDescription
        }
    }

    private func decide(_ row: JSON, _ action: String) async {
        failure = nil
        do {
            _ = try await store.settingsAPI().settingsLong("POST", "/api/improvements/\(API.escape(row.id))/\(action)")
            await load()
            store.refreshSoon(after: 0.1)
        } catch is URLError where action == "approve" {
            // Landing restarts the server, which may go before it finishes answering.
            store.notify("Restarting onto the change.")
        } catch {
            failure = error.localizedDescription
        }
    }
}

private struct IssueRow: View {
    @Environment(Store.self) private var store
    let row: JSON
    private static let states = ["open": "not fixed yet", "fixing": "fixing now", "pending": "fix ready", "gave_up": "needs you"]

    private var meta: String {
        let who = row.agents.dict.sorted { $0.key < $1.key }.map { "\(store.name($0.key)) \($0.value.int)" }.joined(separator: ", ")
        var parts = ["\(row.count.int) times since \(settingsClock(row.first.num))", who]
        if row.attempts.int > 0 { parts.append("\(plural(row.attempts.int, "fix", "fixes")) tried") }
        return parts.filter { !$0.isEmpty }.joined(separator: " · ")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Pill(status: row.state.str == "open" || row.state.str == "gave_up" ? "pending" : "active", label: Self.states[row.state.str] ?? row.state.str)
                Text(row.title.str).fontWeight(.medium).fixedSize(horizontal: false, vertical: true)
            }
            Text(meta).font(.caption).foregroundStyle(.secondary)
            DisclosureGroup("Evidence") { SettingsCode(row.evidence.str) }.font(.caption)
        }
    }
}

@MainActor
private struct ImprovementRow: View {
    @Environment(Store.self) private var store
    let row: JSON
    let autoApply: Bool
    let showDiff: () async -> Void
    let decide: (String) async -> Void

    private static let statuses = [
        "fixing": "fixing", "pending": "waiting on you", "applied": "landed", "pushed": "landed and pushed",
        "discarded": "discarded", "rolled_back": "rolled back", "failed": "failed", "rejected": "rejected",
    ]

    private var isAuto: Bool { row.agent.str == "selfimprove" }
    private var isPending: Bool { row.status.str == "pending" }
    private var statusWord: String {
        if isAuto && isPending && autoApply { return "lands when nothing is running" }
        return Self.statuses[row.status.str] ?? row.status.str
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Pill(status: row.status.str, label: statusWord)
                Text(row.title.str).fontWeight(.medium).fixedSize(horizontal: false, vertical: true)
            }
            Text("\(isAuto ? "self-repair" : store.name(row.agent.str)) · \(plural(row.stat.files.list.count, "file")) · +\(row.stat.added.int)/-\(row.stat.removed.int)")
                .font(.caption)
                .foregroundStyle(.secondary)
            if !row.note.str.isEmpty {
                Text(row.note.str).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
            HStack(spacing: 8) {
                if !["fixing", "discarded"].contains(row.status.str) {
                    AsyncButton("Diff") { await showDiff() }
                }
                if isPending {
                    SettingsConfirmButton(title: isAuto && autoApply ? "Land now" : "Approve", question: "Approve & restart?",
                                          detail: "\(row.title.str)\n\nThis merges the change and restarts Mission Control.",
                                          confirm: "Approve & restart", destructive: false) {
                        await decide("approve")
                    }
                    Button("Reject", role: .destructive) { Task { await decide("reject") } }
                }
            }
        }
        .padding(.vertical, 2)
    }
}
