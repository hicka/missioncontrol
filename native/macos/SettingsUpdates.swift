import SwiftUI

/// Staying current. The signed app handles itself; a source checkout follows GitHub by the
/// rules in server/selfupdate.py, and only when it has been told to.
@MainActor
struct SettingsUpdatesPane: View {
    @Environment(Store.self) private var store
    private static let path = "/api/updates"

    @State private var data = SettingsCache.last(SettingsUpdatesPane.path)
    @State private var problem: String?

    private var version: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "" }
    private var isApp: Bool { data.state.str == "app" }

    var body: some View {
        Form {
            Section {
                SettingsProblemRow(problem: problem)
                LabeledContent("This app", value: version.isEmpty ? "Built from source" : version)
                if isApp || data.state.str.isEmpty {
                    SettingsHint("New releases install themselves, when nothing is running. The database is copied first, and the app reopens by itself.")
                }
                if !data.note.str.isEmpty { LabeledContent("Status", value: data.note.str) }
                SettingsActions {
                    AsyncButton("Check now") { try await check(install: false) }
                    if data.state.str == "available" {
                        AsyncButton("Update now") { try await check(install: true) }.buttonStyle(.borderedProminent)
                    }
                }
            }
            if !isApp, !data.isNull {
                Section {
                    Toggle("Update this checkout automatically", isOn: Binding(get: { data.auto.flag }, set: { value in save(["auto": value]) }))
                    Picker("Follow", selection: Binding(get: { data.channel.optStr ?? "release" }, set: { value in save(["channel": value]) })) {
                        Text("Tagged releases").tag("release")
                        Text("Every push to master").tag("master")
                    }
                } header: {
                    Text("Source checkout")
                } footer: {
                    SettingsHint("Only ever a fast-forward of a clean master, while nothing is running, behind a watchdog that puts the old version back if the new one does not come up. Following master runs every change as soon as it is pushed, including ones self-repair made on its own.")
                }
            }
        }
        .formStyle(.grouped)
        .settingsLoad(on: \.settings) { await load() }
    }

    private func load() async {
        guard let api = store.api else { return }
        do {
            data = try await SettingsCache.read(api, Self.path)
            problem = nil
        } catch { problem = error.localizedDescription }
    }

    private func check(install: Bool) async throws {
        store.onCheckForUpdates?()
        guard let api = store.api else { return }
        data = try await api.post(Self.path + "/check" + (install ? "?install=true" : ""))
    }

    private func save(_ change: [String: Any]) {
        Task {
            do {
                _ = try await store.api?.put(Self.path, change)
                await load()
            } catch { store.notify(error.localizedDescription, error: true) }
        }
    }
}
