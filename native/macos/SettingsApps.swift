import AppKit
import SwiftUI

// MARK: Apple apps

@MainActor
struct SettingsApplePane: View {
    @Environment(Store.self) private var store
    private static let path = "/api/apple"
    private static let blurbs = [
        "notes": "search, read and create notes",
        "calendar": "see what's coming up, and add events",
        "reminders": "list, add and tick off reminders",
        "mail": "search and read your inbox, and open drafts. Never send",
        "messages": "read recent iMessage and SMS history. Read-only",
    ]

    @State private var data = SettingsCache.last(SettingsApplePane.path)
    @State private var problem: String?
    /// A switch moves at once and settles on the server's answer when it arrives.
    @State private var changing: [String: Bool] = [:]

    var body: some View {
        Form {
            Section {
                SettingsProblemRow(problem: problem)
                if data.isNull && problem == nil { Text("Checking…").foregroundStyle(.secondary) }
                ForEach(data.apps.list.map(\.str), id: \.self) { app in appRow(app) }
            } header: {
                SettingsHint("Your own Mac apps, one switch each. It reads widely and writes narrowly: it can add a note, an event or a reminder, and draft mail, never send it. Messages is read-only; texting still goes through the iMessage channel.")
            } footer: {
                if !data.isNull {
                    SettingsHint(data.remindctl.flag
                        ? "Reminders go through remindctl (EventKit): about 0.2s where AppleScript takes 44s."
                        : "Reminders will work but slowly. `brew install steipete/tap/remindctl` makes it ~200x faster.")
                }
            }
        }
        .formStyle(.grouped)
        .settingsLoad { await load() }
    }

    private func appRow(_ app: String) -> some View {
        let reachable = data.reachable[app].flag
        let why = data.reachable["\(app)_error"].str
        let refusal = why.isEmpty ? "macOS is refusing access" : "macOS is refusing access: \(clip(why, 120))"
        return Toggle(isOn: Binding(get: { changing[app] ?? data.connected[app].flag }, set: { set(app, $0) })) {
            Text(app.capitalized)
            Text(Self.blurbs[app] ?? "")
            if !reachable { Text(refusal).foregroundStyle(MC.warn) }
        }
        .toggleStyle(.switch)
    }

    /// macOS is asked about each app in turn, which can take well over the usual request limit.
    private func load() async {
        do {
            data = try await SettingsCache.read(store.settingsAPI(), Self.path, long: true)
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func set(_ app: String, _ on: Bool) {
        changing[app] = on
        Task {
            do {
                _ = try await store.settingsAPI().put(Self.path, ["app": app, "enabled": on])
            } catch {
                store.notify(error.localizedDescription, error: true)
            }
            await load()
            changing[app] = nil
        }
    }
}

// MARK: Mail cleanup

private struct SweepForm: Equatable {
    var enabled = false, dryRun = true, minutes = "60", bulkAt = 0.7, keepAt = 0.35

    init(_ settings: JSON) {
        enabled = settings.enabled.flag
        dryRun = settings.dry_run.isNull ? true : settings.dry_run.flag
        minutes = settings.every_minutes.optStr ?? "60"
        bulkAt = settings.bulk_at.optNum ?? 0.7
        keepAt = settings.keep_at.optNum ?? 0.35
    }
}

/// The web has no page for this; it follows the server's own routes. Nothing here moves
/// mail until dry run is turned off, and that asks first.
@MainActor
struct SettingsMailPane: View {
    @Environment(Store.self) private var store
    private static let path = "/api/mail/sweep"

    @State private var data = SettingsCache.last(SettingsMailPane.path)
    @State private var laya = SettingsCache.last("/api/laya")
    @State private var form = SettingsDraft(reply: SettingsCache.last(SettingsMailPane.path)) { SweepForm($0.settings) }
    @State private var status = SettingsStatus.none
    @State private var runStatus = SettingsStatus.none
    @State private var problem: String?
    @State private var askingLive = false
    @State private var sender = ""
    @State private var restore = false
    @State private var senderStatus = SettingsStatus.none

    private var layaState: (status: String, label: String) {
        if laya.isNull { return ("off", "checking") }
        if !laya.enabled.flag { return ("off", "off") }
        if !laya.health.isNull { return ("running", "running") }
        return ("waiting", clip(laya.problem.optStr.flatMap { $0.isEmpty ? nil : $0 } ?? "not answering", 44))
    }

    var body: some View {
        Form {
            settingsSection
            runSection
            sendersSection
        }
        .formStyle(.grouped)
        .settingsLoad { await load() }
        .task(id: data.running.flag) { await pollWhileRunning() }
        .confirmationDialog("Move mail for real?", isPresented: $askingLive, titleVisibility: .visible) {
            Button("Turn dry run off") { form.value.dryRun = false }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Once this is saved, sweeps move mail from bulk senders to the Archive mailbox. It stays searchable and can be moved back.")
        }
    }

    private var settingsSection: some View {
        Section {
            SettingsProblemRow(problem: problem)
            SettingsSwitch(title: "Clean up the inbox", note: "Judged by the self-hosted Laya model from sender and subject alone, at no model cost. Flagged mail, mail you replied to and people you correspond with are never touched.", isOn: $form.value.enabled)
                .disabled(!form.ready)
            SettingsSwitch(title: "Dry run", note: "Report what would be archived and move nothing.", isOn: Binding(get: { form.value.dryRun }, set: { on in
                if on { form.value.dryRun = true } else { askingLive = true }
            }))
            .disabled(!form.ready)
            TextField("Every (minutes)", text: $form.value.minutes, prompt: Text("60"))
            slider("Bulk sender at", $form.value.bulkAt, 0.5...1)
            slider("Keep below", $form.value.keepAt, 0...1)
            LabeledContent("Laya") { Pill(status: layaState.status, label: layaState.label) }
            SettingsActions(status: status) {
                AsyncButton("Save") { await save() }.buttonStyle(.borderedProminent).disabled(!form.ready)
            }
        }
    }

    private func slider(_ title: String, _ value: Binding<Double>, _ range: ClosedRange<Double>) -> some View {
        LabeledContent(title) {
            HStack(spacing: 8) {
                Slider(value: value, in: range, step: 0.05).labelsHidden().frame(maxWidth: 180)
                Text(value.wrappedValue, format: .number.precision(.fractionLength(2))).monospacedDigit().foregroundStyle(.secondary)
            }
        }
    }

    private var runSection: some View {
        Section {
            if let summary { Text(summary).fixedSize(horizontal: false, vertical: true).textSelection(.enabled) }
            else if !data.isNull { Text("No sweep has run yet.").foregroundStyle(.secondary) }
            if !data.last.error.str.isEmpty { SettingsStatusLine(status: .bad("Problem: \(data.last.error.str)")) }
            LabeledContent("All time", value: "\(data.archived_total.int) archived, \(data.senders_judged.int) senders and \(data.messages_judged.int) messages judged")
            SettingsActions(status: data.running.flag ? .busy("Sweeping…") : runStatus) {
                AsyncButton("Run now") { await run() }.disabled(data.isNull || data.running.flag)
            }
        } header: {
            Text("Last sweep")
        }
    }

    private var summary: String? {
        let last = data.last
        guard !last.isNull else { return nil }
        let moved = last.dry_run.flag ? "would archive \(last.archive.int)" : "archived \(last.archived.int)"
        var parts = ["\(last.scanned.int) scanned", moved, "held \(last.held.int) as important", "kept \(last.kept.int)",
                     "\(last.protected.int) flagged or replied", "\(last.kept_by_rule.int) kept by rule",
                     "\(last.pending.int) not judged yet"]
        if last.dry_run.flag { parts.insert("Dry run, nothing moved", at: 0) }
        return "\(ago(last.ts.num)): " + parts.joined(separator: ", ") + "."
    }

    private var sendersSection: some View {
        Section {
            senderRow("Always keep", data.keep.list.map(\.str))
            senderRow("Always archive", data.always.list.map(\.str))
            TextField("Sender", text: $sender, prompt: Text("An address or a domain"))
                .autocorrectionDisabled()
            Toggle("Move back what was archived from them", isOn: $restore)
                .toggleStyle(.switch)
            SettingsActions(status: senderStatus) {
                AsyncButton("Always archive") { await setSender("always") }.disabled(sender.settingsTrimmed.isEmpty)
                AsyncButton("Always keep") { await setSender("keep") }.disabled(sender.settingsTrimmed.isEmpty)
            }
        } header: {
            Text("Senders")
        }
    }

    private func senderRow(_ title: String, _ rows: [String]) -> some View {
        LabeledContent(title, value: rows.isEmpty ? "None" : rows.joined(separator: ", "))
    }

    private func load() async {
        do {
            let api = try store.settingsAPI()
            async let sweep = SettingsCache.read(api, Self.path)
            async let model = SettingsCache.read(api, "/api/laya")
            apply(try await sweep)
            laya = try await model
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func apply(_ reply: JSON) {
        data = reply
        form.receive(SweepForm(reply.settings))
    }

    private func pollWhileRunning() async {
        guard data.running.flag else { return }
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            guard !Task.isCancelled, NSApp.isActive, let api = store.api else { continue }
            if let reply = try? await api.get(Self.path), !Task.isCancelled { apply(reply) }
        }
    }

    private func save() async {
        let minutes = min(max(Int(form.value.minutes.settingsTrimmed) ?? 60, 15), 1440)
        do {
            let reply = try await store.settingsAPI().put(Self.path, [
                "enabled": form.value.enabled, "dry_run": form.value.dryRun, "every_minutes": minutes,
                "bulk_at": form.value.bulkAt, "keep_at": form.value.keepAt,
            ])
            data = reply
            form.reset(to: SweepForm(reply.settings))
            status = .good("Saved")
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
    }

    private func run() async {
        do {
            let reply = try await store.settingsAPI().post("/api/mail/sweep/run", [:])
            runStatus = .good(reply.dry_run.flag ? "Dry run started." : "Sweep started.")
            await load()
        } catch {
            runStatus = .bad(error.localizedDescription)
        }
    }

    private func setSender(_ action: String) async {
        let keepingAndRestoring = action == "keep" && restore
        senderStatus = keepingAndRestoring ? .busy("Moving mail back…") : .busy("Saving…")
        do {
            let reply = try await store.settingsAPI().settingsLong("POST", "/api/mail/sweep/sender", [
                "sender": sender.settingsTrimmed, "action": action, "restore": keepingAndRestoring,
            ])
            sender = ""
            restore = false
            senderStatus = .good(keepingAndRestoring ? "Saved. \(plural(reply.restored.int, "message")) moved back." : "Saved")
            await load()
        } catch {
            senderStatus = .bad("Error: \(error.localizedDescription)")
        }
    }
}
