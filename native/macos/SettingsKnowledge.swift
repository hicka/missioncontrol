import SwiftUI

/// What the agent believes, in one place. These lists can run to hundreds of rows, so they
/// are native lists rather than forms, and each tab reads only its own endpoint.
@MainActor
struct SettingsKnowledgePane: View {
    private enum Tab: String { case memory, skills, playbooks }

    let section: String
    @State private var tab: Tab

    init(section: String) {
        self.section = section
        _tab = State(initialValue: Tab(rawValue: section) ?? .memory)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SettingsHint("What it believes, in one place. Fix a wrong memory, delete a stale one, rewrite a skill; it sees the change on its next turn.")
                .padding(.horizontal, 24)
                .padding(.top, 6)
            Picker("Show", selection: $tab) {
                Text("Memory").tag(Tab.memory)
                Text("Skills").tag(Tab.skills)
                Text("Web playbooks").tag(Tab.playbooks)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(.horizontal, 24)
            switch tab {
            case .memory: MemoryList()
            case .skills: SkillList()
            case .playbooks: PlaybookList()
            }
        }
        .onChange(of: section) { _, next in if let wanted = Tab(rawValue: next) { tab = wanted } }
    }
}

// MARK: Memory

@MainActor
private struct MemoryList: View {
    @Environment(Store.self) private var store
    private static let everything = "/api/memory?q="

    @State private var rows = Array(SettingsCache.last(MemoryList.everything).memories.list.reversed())
    @State private var loaded = !SettingsCache.last(MemoryList.everything).isNull
    @State private var query = ""
    @State private var search: Task<Void, Never>?
    @State private var editing: JSON?
    @State private var problem: String?

    var body: some View {
        VStack(spacing: 8) {
            TextField("Search memory", text: $query, prompt: Text("Search memory"))
                .textFieldStyle(.roundedBorder)
                .autocorrectionDisabled()
                .padding(.horizontal, 24)
            SettingsProblemRow(problem: problem)
            List {
                if loaded && rows.isEmpty {
                    Text(query.settingsTrimmed.isEmpty ? "No memories yet." : "Nothing matches.").foregroundStyle(.secondary)
                }
                ForEach(rows) { row in
                    MemoryRow(row: row) { editing = row } delete: { Task { await delete(row) } }
                }
            }
            .listStyle(.inset)
        }
        .settingsLoad(on: \.memory) { await load() }
        .onChange(of: query) {
            search?.cancel()
            search = Task {
                try? await Task.sleep(nanoseconds: 250_000_000)
                if !Task.isCancelled { await load() }
            }
        }
        .sheet(item: $editing) { row in
            MemoryEditor(row: row) { changed in
                editing = nil
                if changed { Task { await load() } }
            }
        }
    }

    private func load() async {
        let term = query.settingsTrimmed
        do {
            let api = try store.settingsAPI()
            // Only the plain list is kept for next time; a search result would not be it.
            let reply = term.isEmpty ? try await SettingsCache.read(api, Self.everything)
                                     : try await api.get("/api/memory?q=\(API.escape(term))")
            guard term == query.settingsTrimmed, !Task.isCancelled else { return }
            rows = term.isEmpty ? Array(reply.memories.list.reversed()) : reply.memories.list
            loaded = true
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func delete(_ row: JSON) async {
        rows.removeAll { $0.id == row.id }
        do {
            _ = try await store.settingsAPI().delete("/api/memory/\(API.escape(row.id))")
        } catch {
            store.notify(error.localizedDescription, error: true)
            await load()
        }
    }
}

@MainActor
private struct MemoryRow: View {
    @Environment(Store.self) private var store
    let row: JSON
    let edit: () -> Void
    let delete: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                SettingsKindLabel(text: row.kind.optStr ?? "fact")
                Text("\(store.name(row.author.str)) · \(ago(row.ts.num))").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Edit", action: edit).buttonStyle(.borderless)
                Button("Delete", role: .destructive, action: delete).buttonStyle(.borderless)
            }
            Text(row.text.str).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
        }
        .padding(.vertical, 3)
    }
}

private struct SettingsKindLabel: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.system(size: 11, weight: .medium))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 7)
            .padding(.vertical, 1)
            .background(Color.primary.opacity(0.07), in: RoundedRectangle(cornerRadius: 5, style: .continuous))
    }
}

@MainActor
private struct MemoryEditor: View {
    @Environment(Store.self) private var store
    let row: JSON
    let finish: (Bool) -> Void
    @State private var text: String
    @State private var status = SettingsStatus.none

    init(row: JSON, finish: @escaping (Bool) -> Void) {
        self.row = row
        self.finish = finish
        _text = State(initialValue: row.text.str)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Edit memory").font(.headline)
            SettingsTextBox(label: "Memory", text: $text, height: 220, mono: false)
            SettingsActions(status: status) {
                Button("Cancel", role: .cancel) { finish(false) }.keyboardShortcut(.cancelAction)
                AsyncButton("Save") { await save() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(text.settingsTrimmed.isEmpty)
            }
        }
        .padding(18)
        .frame(width: 520)
    }

    private func save() async {
        do {
            _ = try await store.settingsAPI().put("/api/memory/\(API.escape(row.id))", ["text": text])
            finish(true)
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
    }
}

// MARK: Skills

private struct SkillSheet: Identifiable {
    let slug: String, name: String
    var id: String { slug }
}

@MainActor
private struct SkillList: View {
    @Environment(Store.self) private var store
    private static let path = "/api/skills"

    @State private var rows = SettingsCache.last(SkillList.path).skills.list
    @State private var autolearn = SettingsDraft(reply: SettingsCache.last(SkillList.path)) { $0.autolearn.isNull ? true : $0.autolearn.flag }
    @State private var loaded = !SettingsCache.last(SkillList.path).isNull
    @State private var opened: SkillSheet?
    @State private var problem: String?

    var body: some View {
        List {
            SettingsProblemRow(problem: problem)
            Toggle("Learn skills automatically after long pieces of work", isOn: Binding(get: { autolearn.value }, set: { setAutolearn($0) }))
                .toggleStyle(.switch)
                .disabled(!autolearn.ready)
            if loaded && rows.isEmpty { Text("No skills saved yet.").foregroundStyle(.secondary) }
            ForEach(rows, id: \.self) { row in skillRow(row) }
        }
        .listStyle(.inset)
        .settingsLoad(on: \.skills) { await load() }
        .sheet(item: $opened) { skill in
            SkillEditor(skill: skill) { opened = nil }
        }
    }

    private func skillRow(_ row: JSON) -> some View {
        HStack(alignment: .top, spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text(row.name.str).fontWeight(.medium)
                if !row.description.str.isEmpty {
                    Text(row.description.str).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                if !row.avoid.str.isEmpty {
                    Text("Not for: \(row.avoid.str)").font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                if !row.error.str.isEmpty {
                    Text("Problem: \(row.error.str)").font(.caption).foregroundStyle(MC.danger).fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer()
            Text(row.uses.int > 0 ? "\(row.uses.int)×" : "unused")
                .font(.caption)
                .foregroundStyle(.secondary)
                .monospacedDigit()
                .help("Times the agent has read this skill")
            Button("Open") { opened = SkillSheet(slug: row.slug.str, name: row.name.str) }.buttonStyle(.borderless)
            Button("Delete", role: .destructive) { Task { await delete(row) } }.buttonStyle(.borderless)
        }
        .padding(.vertical, 3)
    }

    private func load() async {
        do {
            let reply = try await SettingsCache.read(store.settingsAPI(), Self.path)
            rows = reply.skills.list
            autolearn.receive(reply.autolearn.isNull ? true : reply.autolearn.flag)
            loaded = true
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func setAutolearn(_ on: Bool) {
        autolearn.value = on
        Task {
            do {
                let reply = try await store.settingsAPI().put("/api/skills-autolearn", ["autolearn": on])
                autolearn.reset(to: reply.autolearn.flag)
            } catch {
                store.notify(error.localizedDescription, error: true)
                await load()
            }
        }
    }

    private func delete(_ row: JSON) async {
        rows.removeAll { $0.slug == row.slug }
        do {
            _ = try await store.settingsAPI().delete("/api/skills/\(API.escape(row.slug.str))")
        } catch {
            store.notify(error.localizedDescription, error: true)
            await load()
        }
    }
}

@MainActor
private struct SkillEditor: View {
    @Environment(Store.self) private var store
    let skill: SkillSheet
    let close: () -> Void
    @State private var text = ""
    @State private var loaded = false
    @State private var status = SettingsStatus.none

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(skill.name).font(.headline)
            SettingsTextBox(label: "Skill", text: $text, height: 420)
            SettingsActions(status: status) {
                Button("Close", role: .cancel, action: close).keyboardShortcut(.cancelAction)
                AsyncButton("Save") { await save() }
                    .buttonStyle(.borderedProminent)
                    .disabled(!loaded)
            }
        }
        .padding(18)
        .frame(width: 680)
        .task { await load() }
    }

    private func load() async {
        do {
            text = try await store.settingsAPI().get("/api/skills/\(API.escape(skill.slug))").content.str
            loaded = true
        } catch {
            if !Task.isCancelled { status = .bad("Error: \(error.localizedDescription)") }
        }
    }

    private func save() async {
        do {
            _ = try await store.settingsAPI().put("/api/skills/\(API.escape(skill.slug))", ["content": text])
            status = .good("Saved")
        } catch {
            status = .bad("Error: \(error.localizedDescription)")
        }
    }
}

// MARK: Playbooks

@MainActor
private struct PlaybookList: View {
    @Environment(Store.self) private var store
    private static let path = "/api/playbooks"
    @State private var rows = SettingsCache.last(PlaybookList.path).playbooks.list
    @State private var loaded = !SettingsCache.last(PlaybookList.path).isNull
    @State private var problem: String?

    var body: some View {
        List {
            SettingsProblemRow(problem: problem)
            Text("Site-by-site steps the browser learned.").font(.callout).foregroundStyle(.secondary)
            if loaded && rows.isEmpty { Text("No playbooks yet.").foregroundStyle(.secondary) }
            ForEach(rows, id: \.self) { row in
                HStack(spacing: 8) {
                    SettingsKindLabel(text: row.site.str)
                    Text(row.title.str)
                    Spacer()
                    Text("\(store.name(row.author.str)) · \(ago(row.ts.num))").font(.caption).foregroundStyle(.secondary)
                    Button("Delete", role: .destructive) { Task { await delete(row) } }.buttonStyle(.borderless)
                }
                .padding(.vertical, 2)
            }
        }
        .listStyle(.inset)
        .settingsLoad { await load() }
    }

    private func load() async {
        do {
            rows = try await SettingsCache.read(store.settingsAPI(), Self.path).playbooks.list
            loaded = true
            problem = nil
        } catch {
            if !Task.isCancelled { problem = error.localizedDescription }
        }
    }

    private func delete(_ row: JSON) async {
        rows.removeAll { $0 == row }
        do {
            _ = try await store.settingsAPI().post("/api/playbooks/delete", ["site": row.site.str, "title": row.title.str])
        } catch {
            store.notify(error.localizedDescription, error: true)
            await load()
        }
    }
}
