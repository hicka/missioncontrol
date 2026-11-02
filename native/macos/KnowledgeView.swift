import AppKit
import SwiftUI

/// Another screen can open a tab by setting the `knowledge.tab` default to one of these
/// before going to `.knowledge`.
private enum KnowledgeTab: String, CaseIterable, Identifiable {
    case memory, skills, playbooks
    var id: String { rawValue }
    var title: String { rawValue.capitalized }
}

/// What the tabs last read. It outlives the screen, so coming back draws at once and the
/// read that follows only corrects what moved.
@Observable @MainActor
private final class KnowledgeModel {
    static let shared = KnowledgeModel()

    private var base = ""
    var query = ""
    private(set) var memories: [JSON] = []
    private(set) var memoriesLoaded = false
    private(set) var memoryError: String?
    /// The search `memories` answers, which trails `query` while the debounce waits.
    private(set) var answered = ""
    private(set) var playbooks: [JSON] = []
    private(set) var playbooksLoaded = false
    private(set) var playbookError: String?
    var autolearn: Bool?

    var search: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }

    private func adopt(_ api: API) {
        guard api.base != base else { return }
        base = api.base
        query = ""; answered = ""
        memories = []; memoriesLoaded = false; memoryError = nil
        playbooks = []; playbooksLoaded = false; playbookError = nil
        autolearn = nil
    }

    func loadMemories(_ api: API) async {
        adopt(api)
        let asked = search
        do {
            let rows = try await api.get("/api/memory?q=\(API.escape(asked))").memories.list
            if Task.isCancelled { return }
            let ordered = Array((asked.isEmpty ? rows.reversed() : rows).prefix(200))
            if ordered != memories { memories = ordered }
            answered = asked
            memoriesLoaded = true
            memoryError = nil
        } catch {
            if !Task.isCancelled { memoryError = error.localizedDescription }
        }
    }

    func saveMemory(_ id: String, text: String, api: API) async throws {
        let clean = text.trimmingCharacters(in: .whitespacesAndNewlines)
        _ = try await api.put("/api/memory/\(API.escape(id))", ["text": clean])
        if let index = memories.firstIndex(where: { $0.id == id }), case .object(var fields) = memories[index] {
            fields["text"] = .string(clean)
            memories[index] = .object(fields)
        }
    }

    func deleteMemory(_ id: String, api: API) async throws {
        memories.removeAll { $0.id == id }
        do { _ = try await api.delete("/api/memory/\(API.escape(id))") }
        catch { await loadMemories(api); throw error }
    }

    func loadPlaybooks(_ api: API) async {
        adopt(api)
        do {
            let rows = try await api.get("/api/playbooks").playbooks.list
            if Task.isCancelled { return }
            if rows != playbooks { playbooks = rows }
            playbooksLoaded = true
            playbookError = nil
        } catch {
            if !Task.isCancelled { playbookError = error.localizedDescription }
        }
    }

    func deletePlaybook(_ row: JSON, api: API) async throws {
        playbooks.removeAll { $0 == row }
        do { _ = try await api.post("/api/playbooks/delete", ["site": row.site.str, "title": row.title.str]) }
        catch { await loadPlaybooks(api); throw error }
    }

    /// The skills themselves come from the store; this read adds the switch that only
    /// this endpoint reports, and corrects the list if it has moved since.
    func loadSkills(_ api: API, into store: Store) async {
        adopt(api)
        guard let reply = try? await api.get("/api/skills"), !Task.isCancelled else { return }
        autolearn = reply.autolearn != .bool(false)
        let rows = reply.skills.list
        if rows != store.skills { store.skills = rows }
    }
}

/// Reads when a key moves, when the server changes and when the link comes back. The store
/// is only read in this view's body, so a tick redraws nothing else on the screen.
private struct Loader<Key: Equatable>: View {
    @Environment(Store.self) private var store
    let key: @MainActor (Store) -> Key
    let read: @MainActor (API, Store) async -> Void

    private struct Trigger: Equatable { let key: Key; let base: String?; let link: Bool }

    var body: some View {
        Color.clear.task(id: Trigger(key: key(store), base: store.api?.base, link: store.link)) {
            if let api = store.api { await read(api, store) }
        }
    }
}

private func notConnected() -> APIError { APIError(message: "Not connected to the runtime yet.") }

private struct TextBox: View {
    @Binding var text: String
    var monospaced = false
    var autofocus = false
    @FocusState private var focused: Bool

    var body: some View {
        TextEditor(text: $text)
            .font(monospaced ? .system(size: 12, design: .monospaced) : .system(size: 13))
            .autocorrectionDisabled(monospaced)
            .scrollContentBackground(.hidden)
            .focused($focused)
            .padding(6)
            .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 6, style: .continuous).strokeBorder(MC.line))
            .onAppear { if autofocus { focused = true } }
    }
}

private struct SearchField: NSViewRepresentable {
    @Binding var text: String
    let prompt: String
    let label: String

    func makeNSView(context: Context) -> NSSearchField {
        let field = NSSearchField()
        field.placeholderString = prompt
        field.setAccessibilityLabel(label)
        field.stringValue = text
        field.sendsSearchStringImmediately = true
        field.target = context.coordinator
        field.action = #selector(Coordinator.changed(_:))
        return field
    }

    func updateNSView(_ field: NSSearchField, context: Context) {
        context.coordinator.text = $text
        if field.stringValue != text { field.stringValue = text }
    }

    func makeCoordinator() -> Coordinator { Coordinator(text: $text) }

    final class Coordinator: NSObject {
        var text: Binding<String>
        init(text: Binding<String>) { self.text = text }
        @objc func changed(_ sender: NSSearchField) { text.wrappedValue = sender.stringValue }
    }
}

// MARK: The screen

struct KnowledgeView: View {
    @AppStorage("knowledge.tab") private var tab = KnowledgeTab.memory

    var body: some View {
        Page("Knowledge", subtitle: "What it knows and how it works: shared memory every task reads, procedures it saved as skills, and step-by-step playbooks for websites.") {
            Picker("Section", selection: $tab) {
                ForEach(KnowledgeTab.allCases) { Text($0.title).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()

            switch tab {
            case .memory: MemoryTab()
            case .skills: SkillsTab()
            case .playbooks: PlaybooksTab()
            }
        }
    }
}

// MARK: Memory

private struct MemoryTab: View {
    @State private var remembering = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                MemorySearch()
                Button { remembering = true } label: { Label("Remember something", systemImage: "plus") }
            }
            MemoryResults()
        }
        .background {
            Loader(key: { "\($0.ticks.memory)|\(KnowledgeModel.shared.query)" }, read: { api, _ in
                let model = KnowledgeModel.shared
                if model.memoriesLoaded, model.search != model.answered {
                    try? await Task.sleep(nanoseconds: 180_000_000)
                    if Task.isCancelled { return }
                }
                await model.loadMemories(api)
            })
        }
        .sheet(isPresented: $remembering) { RememberSheet() }
    }
}

/// On its own so a keystroke redraws only the field.
private struct MemorySearch: View {
    var body: some View {
        let model = KnowledgeModel.shared
        SearchField(text: Binding(get: { model.query }, set: { model.query = $0 }),
                    prompt: "Search facts, decisions, names", label: "Search memory")
            .frame(maxWidth: 360)
    }
}

private struct MemoryResults: View {
    @Environment(Store.self) private var store

    var body: some View {
        let model = KnowledgeModel.shared
        let rows = model.memories
        if !model.memoriesLoaded {
            if let error = model.memoryError {
                Card { EmptyNote(title: "Could not read memory", detail: error) }
            } else {
                Text("Loading…").font(.callout).foregroundStyle(.secondary)
            }
        } else if rows.isEmpty {
            Card { EmptyNote(title: model.answered.isEmpty ? "Nothing saved yet." : "Nothing matches.") }
        } else {
            if let error = model.memoryError {
                Text("Could not refresh: \(error)").font(.caption).foregroundStyle(MC.danger)
            }
            Card {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(rows) { memory in
                        if memory.id != rows.first?.id { Divider() }
                        MemoryRow(memory: memory)
                    }
                }
            }
            if model.answered.isEmpty, store.counts.memories.int > rows.count {
                Text("Showing the newest \(rows.count) of \(store.counts.memories.int). Search to reach the rest.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

private struct MemoryRow: View {
    @Environment(Store.self) private var store
    let memory: JSON
    @State private var draft: String?
    @State private var confirming = false

    private var author: String {
        let name = store.name(memory.author.str)
        return name.isEmpty ? memory.author.str : name
    }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 6) {
                    Text(memory.kind.str.isEmpty ? "fact" : memory.kind.str)
                        .font(.system(size: 11, weight: .medium))
                        .padding(.horizontal, 7).padding(.vertical, 1.5)
                        .background(Color.primary.opacity(0.07), in: Capsule())
                    Text("\(author) · \(ago(memory.ts.num))")
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                if draft == nil {
                    Text(memory.text.str)
                        .font(.system(size: 13))
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    editor
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            if draft == nil {
                HStack(spacing: 10) {
                    Button { draft = memory.text.str } label: { Image(systemName: "pencil") }
                        .help("Edit").accessibilityLabel("Edit")
                    Button { confirming = true } label: { Image(systemName: "xmark").foregroundStyle(MC.danger) }
                        .help("Delete").accessibilityLabel("Delete")
                }
                .buttonStyle(.borderless)
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 11)
        .confirmationDialog("Delete this memory?", isPresented: $confirming, titleVisibility: .visible) {
            Button("Delete", role: .destructive) { delete() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(clip(memory.text.str, 200))
        }
    }

    private var editor: some View {
        let text = Binding(get: { draft ?? "" }, set: { draft = $0 })
        return VStack(alignment: .leading, spacing: 8) {
            TextBox(text: text, autofocus: true).frame(minHeight: 70, maxHeight: 200)
            HStack(spacing: 8) {
                AsyncButton(action: {
                    guard let api = store.api else { throw notConnected() }
                    try await KnowledgeModel.shared.saveMemory(memory.id, text: text.wrappedValue, api: api)
                    draft = nil
                }) { Text("Save") }
                .buttonStyle(.borderedProminent)
                .disabled(text.wrappedValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                Button("Cancel") { draft = nil }
            }
            .controlSize(.small)
        }
    }

    private func delete() {
        Task {
            guard let api = store.api else { store.notify(notConnected().message, error: true); return }
            do { try await KnowledgeModel.shared.deleteMemory(memory.id, api: api) }
            catch { store.notify(error.localizedDescription, error: true) }
        }
    }
}

private struct RememberSheet: View {
    @Environment(Store.self) private var store
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var problem: String?
    @State private var saving = false

    private var clean: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Remember something").font(.headline)
            Text("Saved to shared memory, where every task can read it.").font(.callout).foregroundStyle(.secondary)
            TextBox(text: $text, autofocus: true).frame(height: 120).accessibilityLabel("What to remember")
            if let problem { Text(problem).font(.callout).foregroundStyle(MC.danger) }
            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Remember") { Task { await save() } }
                    .keyboardShortcut(.return, modifiers: .command)
                    .buttonStyle(.borderedProminent)
                    .disabled(clean.isEmpty || saving)
            }
        }
        .padding(20)
        .frame(width: 460)
    }

    private func save() async {
        guard let api = store.api else { problem = notConnected().message; return }
        saving = true; defer { saving = false }
        do {
            _ = try await api.post("/api/command", ["text": "/remember \(clean)"])
            store.notify("Remembered.")
            store.refreshSoon(after: 0.05)
            dismiss()
        } catch {
            problem = error.localizedDescription
        }
    }
}

// MARK: Skills

private enum SkillSheet: Identifiable {
    case edit(String), run(String)
    var id: String {
        switch self {
        case .edit(let slug): return "edit:\(slug)"
        case .run(let slug): return "run:\(slug)"
        }
    }
}

@MainActor private func deleteSkill(_ slug: String, store: Store) async throws {
    guard let api = store.api else { throw notConnected() }
    store.skills.removeAll { $0.slug.str == slug }
    defer { store.refreshSoon(after: 0.05) }
    _ = try await api.delete("/api/skills/\(API.escape(slug))")
}

private struct SkillsTab: View {
    @Environment(Store.self) private var store
    @State private var sheet: SkillSheet?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            AutolearnSwitch()
            if store.skills.isEmpty {
                Card { EmptyNote(title: "No skills yet", detail: "When a task works out a procedure worth keeping, it is saved here.") }
            } else {
                Card {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(store.skills, id: \.slug.str) { skill in
                            if skill.slug != store.skills.first?.slug { Divider() }
                            SkillRow(skill: skill) { sheet = $0 }
                        }
                    }
                }
            }
        }
        .background {
            Loader(key: { $0.ticks.skills }, read: { api, store in
                await KnowledgeModel.shared.loadSkills(api, into: store)
            })
        }
        .sheet(item: $sheet) { sheet in
            switch sheet {
            case .edit(let slug): SkillEditorSheet(slug: slug)
            case .run(let slug): SkillRunSheet(slug: slug)
            }
        }
    }
}

private struct AutolearnSwitch: View {
    @Environment(Store.self) private var store

    var body: some View {
        let model = KnowledgeModel.shared
        Toggle("Learn skills automatically after long pieces of work", isOn: Binding(
            get: { model.autolearn ?? true },
            set: { change($0) }
        ))
        .toggleStyle(.switch)
        .controlSize(.small)
        .disabled(model.autolearn == nil)
    }

    private func change(_ on: Bool) {
        let model = KnowledgeModel.shared
        let before = model.autolearn
        model.autolearn = on
        Task {
            do {
                guard let api = store.api else { throw notConnected() }
                _ = try await api.put("/api/skills-autolearn", ["autolearn": on])
            } catch {
                model.autolearn = before
                store.notify(error.localizedDescription, error: true)
            }
        }
    }
}

private struct SkillRow: View {
    @Environment(Store.self) private var store
    let skill: JSON
    let open: (SkillSheet) -> Void
    @State private var confirming = false

    var body: some View {
        let slug = skill.slug.str
        let valid = skill.valid.flag
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text(skill.name.str).font(.system(size: 13, weight: .semibold))
                Text(skill.description.str.isEmpty ? "A saved procedure." : skill.description.str)
                    .font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if !skill.avoid.str.isEmpty {
                    Text("Not for: \(skill.avoid.str)")
                        .font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack(spacing: 8) {
                    Pill(status: valid ? "completed" : "failed", label: valid ? "Valid" : "Needs attention")
                    Text(skill.uses.int > 0 ? "used \(skill.uses.int)×" : "not used yet")
                        .font(.caption).foregroundStyle(.secondary)
                }
                .padding(.top, 2)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            HStack(spacing: 6) {
                Button("Open") { open(.edit(slug)) }
                Button("Run") { open(.run(slug)) }.disabled(!valid)
            }
            .controlSize(.small)
        }
        .padding(.horizontal, 14).padding(.vertical, 11)
        .contentShape(Rectangle())
        .contextMenu {
            Button("Open") { open(.edit(slug)) }
            Button("Run") { open(.run(slug)) }.disabled(!valid)
            Divider()
            Button("Delete…", role: .destructive) { confirming = true }
        }
        .confirmationDialog("Delete the skill “\(skill.name.str)”?", isPresented: $confirming, titleVisibility: .visible) {
            Button("Delete", role: .destructive) {
                Task {
                    do { try await deleteSkill(slug, store: store) }
                    catch { store.notify(error.localizedDescription, error: true) }
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Its procedure is removed for good.")
        }
    }
}

private func prettyJSON(_ value: JSON) -> String {
    guard let data = try? JSONSerialization.data(withJSONObject: value.raw, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes, .fragmentsAllowed]) else { return "" }
    return String(decoding: data, as: UTF8.self)
}

private struct SkillEditorSheet: View {
    @Environment(Store.self) private var store
    @Environment(\.dismiss) private var dismiss
    let slug: String
    @State private var content = ""
    @State private var saved = ""
    @State private var loaded = false
    @State private var saving = false
    @State private var status: (text: String, failed: Bool)?
    @State private var confirming = false

    private var skill: JSON { store.skills.first { $0.slug.str == slug } ?? .null }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Text(skill.name.str.isEmpty ? slug : skill.name.str).font(.headline)
                Pill(status: skill.valid.flag ? "completed" : "failed", label: skill.valid.flag ? "Valid" : "Needs attention")
                Spacer()
            }
            if !skill.error.str.isEmpty {
                Text(skill.error.str).font(.callout).foregroundStyle(MC.danger).fixedSize(horizontal: false, vertical: true)
            }
            if loaded {
                TextBox(text: $content, monospaced: true).frame(minHeight: 240, maxHeight: .infinity).accessibilityLabel("Skill file")
            } else {
                Text("Loading…").font(.callout).foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            DisclosureGroup("Contract") {
                ScrollView {
                    Text(prettyJSON(skill.contract))
                        .font(.system(size: 12, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.vertical, 4)
                }
                .frame(maxHeight: 150)
            }
            footer
        }
        .padding(20)
        .frame(minWidth: 620, idealWidth: 720, minHeight: 500, idealHeight: 640)
        .task(id: slug) { await load() }
        .confirmationDialog("Delete the skill “\(skill.name.str.isEmpty ? slug : skill.name.str)”?", isPresented: $confirming, titleVisibility: .visible) {
            Button("Delete", role: .destructive) { delete() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Its procedure is removed for good.")
        }
    }

    private var footer: some View {
        HStack(spacing: 8) {
            Button("Delete…", role: .destructive) { confirming = true }
            if let status {
                Text(status.text)
                    .font(.callout)
                    .foregroundStyle(status.failed ? MC.danger : .secondary)
                    .lineLimit(3)
            }
            Spacer()
            Button("Close", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
            Button("Save") { Task { await save() } }
                .keyboardShortcut("s", modifiers: .command)
                .buttonStyle(.borderedProminent)
                .disabled(!loaded || saving || content == saved)
        }
    }

    private func load() async {
        guard let api = store.api else { status = (notConnected().message, true); return }
        do {
            let text = try await api.get("/api/skills/\(API.escape(slug))").content.str
            if Task.isCancelled { return }
            content = text; saved = text; loaded = true
        } catch {
            if !Task.isCancelled { status = (error.localizedDescription, true) }
        }
    }

    private func save() async {
        guard let api = store.api else { status = (notConnected().message, true); return }
        saving = true; defer { saving = false }
        let text = content
        do {
            _ = try await api.put("/api/skills/\(API.escape(slug))", ["content": text])
            saved = text
            status = ("Saved", false)
        } catch {
            status = ("Error: \(error.localizedDescription)", true)
        }
    }

    private func delete() {
        Task {
            do { try await deleteSkill(slug, store: store); dismiss() }
            catch { status = (error.localizedDescription, true) }
        }
    }
}

private struct SkillRunSheet: View {
    @Environment(Store.self) private var store
    @Environment(\.dismiss) private var dismiss
    let slug: String
    @State private var inputs = ""
    @State private var problem: String?
    @State private var starting = false

    private var skill: JSON { store.skills.first { $0.slug.str == slug } ?? .null }

    var body: some View {
        let contract = skill.contract
        VStack(alignment: .leading, spacing: 10) {
            Text("Run \(skill.name.str.isEmpty ? slug : skill.name.str)").font(.headline)
            if !skill.description.str.isEmpty {
                Text(skill.description.str).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Text("\(contract.max_tool_calls.str) tool calls · \(contract.timeout_seconds.str)s · version \(contract.version.str)")
                .font(.caption).foregroundStyle(.secondary)
            Text("Inputs").font(.subheadline.weight(.semibold)).padding(.top, 4)
            TextBox(text: $inputs, monospaced: true, autofocus: true).frame(minHeight: 150).accessibilityLabel("Inputs as JSON")
            if let problem {
                Text(problem).font(.callout).foregroundStyle(MC.danger).fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
                Button { Task { await start() } } label: { Label("Start", systemImage: "play.fill") }
                    .keyboardShortcut(.return, modifiers: .command)
                    .buttonStyle(.borderedProminent)
                    .disabled(starting)
            }
        }
        .padding(20)
        .frame(minWidth: 480, idealWidth: 520, minHeight: 340)
        .onAppear { inputs = sample() }
    }

    /// A value for each input the contract names, so the form starts out valid wherever it can.
    private func sample() -> String {
        var values: [String: JSON] = [:]
        for (name, spec) in skill.contract.inputs.properties.dict {
            if !spec["default"].isNull {
                values[name] = spec["default"]
            } else if !spec.examples[0].isNull {
                values[name] = spec.examples[0]
            } else {
                switch spec.type.str {
                case "number", "integer": values[name] = .number(1)
                case "boolean": values[name] = .bool(false)
                case "array": values[name] = .array([])
                case "object": values[name] = .object([:])
                default: values[name] = .string("")
                }
            }
        }
        return prettyJSON(.object(values))
    }

    private func start() async {
        guard let api = store.api else { problem = notConnected().message; return }
        starting = true; defer { starting = false }
        problem = nil
        guard let parsed = try? JSONSerialization.jsonObject(with: Data(inputs.utf8), options: [.fragmentsAllowed]),
              let compact = try? JSONSerialization.data(withJSONObject: parsed, options: [.fragmentsAllowed, .withoutEscapingSlashes]) else {
            problem = "The inputs are not valid JSON."
            return
        }
        do {
            let reply = try await api.post("/api/command", ["text": "/skill \(slug) \(String(decoding: compact, as: UTF8.self))"])
            dismiss()
            store.go(.tasks)
            store.notify("Skill started as task \(reply.mission.id).")
            store.refreshSoon(after: 0.05)
        } catch {
            problem = error.localizedDescription
        }
    }
}

// MARK: Playbooks

private struct PlaybooksTab: View {
    var body: some View {
        PlaybookResults()
            .background {
                Loader(key: { $0.ticks.memory }, read: { api, _ in await KnowledgeModel.shared.loadPlaybooks(api) })
            }
    }
}

private struct PlaybookResults: View {
    var body: some View {
        let model = KnowledgeModel.shared
        let rows = model.playbooks
        if !model.playbooksLoaded {
            if let error = model.playbookError {
                Card { EmptyNote(title: "Could not read playbooks", detail: error) }
            } else {
                Text("Loading…").font(.callout).foregroundStyle(.secondary)
            }
        } else if rows.isEmpty {
            Card { EmptyNote(title: "No playbooks yet", detail: "A task saves one after it works out how to do something on a website.") }
        } else {
            if let error = model.playbookError {
                Text("Could not refresh: \(error)").font(.caption).foregroundStyle(MC.danger)
            }
            Card {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(rows, id: \.self) { row in
                        if row != rows.first { Divider() }
                        PlaybookRow(row: row)
                    }
                }
            }
        }
    }
}

private struct PlaybookRow: View {
    @Environment(Store.self) private var store
    let row: JSON
    @State private var confirming = false

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "globe").foregroundStyle(.secondary).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(row.title.str).font(.system(size: 13, weight: .medium))
                Text("\(row.site.str) · \(store.name(row.author.str)) · \(ago(row.ts.num))")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Button("Delete", role: .destructive) { confirming = true }
                .controlSize(.small)
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .confirmationDialog("Delete the playbook “\(row.title.str)”?", isPresented: $confirming, titleVisibility: .visible) {
            Button("Delete", role: .destructive) {
                Task {
                    do {
                        guard let api = store.api else { throw notConnected() }
                        try await KnowledgeModel.shared.deletePlaybook(row, api: api)
                    } catch {
                        store.notify(error.localizedDescription, error: true)
                    }
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Its steps for \(row.site.str) are removed for good.")
        }
    }
}
