import SwiftUI

/// Add-ons the agents built: what each one is, what it may reach, the cards it draws and
/// what it has logged, with the owner's controls over it.
struct ModulesView: View {
    @Environment(Store.self) private var store

    var body: some View {
        Page(title: "Modules",
             subtitle: "Add-ons your agents build while Mission Control runs. Each runs on its own, sandboxed, with only the network, keys and files it was given.") {
            Button("Build one…") { store.focusHome(prefill: "Build me a module that ") }
        } content: {
            if !store.modulesSandboxed {
                Card(padding: 12) {
                    Label("This system has no sandbox for modules, so every version waits for your approval before it runs.",
                          systemImage: "exclamationmark.shield")
                        .font(.system(size: 12.5)).foregroundStyle(MC.warn)
                }
            }
            if store.modules.isEmpty {
                Card {
                    EmptyNote(title: "No modules yet",
                              detail: "Ask for anything that should keep going on its own or sit on your desktop: \"check our sites every five minutes and show me a card\", \"a card with today's open PRs\", \"a timer I can start from my desktop\".")
                }
            }
            ForEach(store.modules) { module in ModuleRow(module: module) }
        }
        .task { await store.loadModules() }
    }
}

private struct ModuleRow: View {
    @Environment(Store.self) private var store
    let module: JSON
    @State private var showingCode = false
    @State private var showingLog = false
    @State private var confirmRemove = false

    var body: some View {
        let slug = module.slug.str
        let status = module.status.str
        let cards = store.moduleCards.filter { $0.module.str == slug }
        Card(padding: 16) {
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(verbatim: module.name.str).font(.headline)
                    Pill(status: status)
                    Text(verbatim: "v\(module.version.str)").font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary)
                    Spacer()
                    if !module.author.str.isEmpty {
                        Text("built by \(store.name(module.author.str))").font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                }
                if !module.description.str.isEmpty {
                    Text(verbatim: module.description.str).font(.system(size: 12.5)).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                ModulePermissions(permissions: module.permissions)
                if !module.reason.str.isEmpty, status == "pending" || status == "stopped" {
                    Text(verbatim: "Waiting because \(module.reason.str).").font(.system(size: 12)).foregroundStyle(MC.warn)
                }
                if !module.error.str.isEmpty {
                    Text(verbatim: module.error.str)
                        .font(.system(size: 11.5, design: .monospaced))
                        .foregroundStyle(status == "failed" ? MC.danger : .secondary)
                        .textSelection(.enabled)
                        .lineLimit(8)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(8)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                }
                if !module.tools.list.isEmpty {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Tools for the agents").font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                        ForEach(module.tools.list, id: \.name.str) { tool in
                            Text("\(Text(verbatim: tool.name.str).font(.system(size: 12, design: .monospaced)))  \(Text(verbatim: tool.description.str).foregroundStyle(.secondary))")
                                .font(.system(size: 12)).lineLimit(2)
                        }
                    }
                }
                if !cards.isEmpty {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(alignment: .top, spacing: 12) {
                            ForEach(cards, id: \.id) { card in
                                VStack(alignment: .leading, spacing: 6) {
                                    ModuleCardView(card: card) { event in Task { await store.cardAction(card, event) } }
                                    Toggle("On the desktop", isOn: Binding(
                                        get: { card.desktop.flag },
                                        set: { on in Task { await store.setCardDesktop(card, on) } }))
                                        .toggleStyle(.checkbox).font(.system(size: 11.5))
                                }
                            }
                        }
                        .padding(.vertical, 2)
                    }
                }
                HStack(spacing: 8) {
                    if status == "pending" {
                        AsyncButton("Approve and run") { try await store.moduleAction(slug, "approve", version: module["hash"].str) }
                            .buttonStyle(.borderedProminent)
                    } else if status == "running" || status == "starting" {
                        AsyncButton("Stop") { try await store.moduleAction(slug, "stop") }
                        AsyncButton("Restart") { try await store.moduleAction(slug, "restart") }
                    } else {
                        AsyncButton("Start") { try await store.moduleAction(slug, "start") }
                    }
                    Button("Code") { showingCode = true }
                    Button("Log") { showingLog = true }
                    Spacer()
                    Button("Remove…", role: .destructive) { confirmRemove = true }
                }
                .controlSize(.small)
            }
        }
        .sheet(isPresented: $showingCode) { ModuleCodeSheet(slug: slug, approving: false) }
        .sheet(isPresented: $showingLog) { ModuleLogSheet(slug: slug) }
        .confirmationDialog("Remove \(module.name.str)?", isPresented: $confirmRemove) {
            Button("Remove", role: .destructive) { Task { try? await store.moduleAction(slug, "remove") } }
        } message: {
            Text("Its code, its data and its cards go. An agent can build it again.")
        }
    }
}

/// What a module may reach, said plainly.
struct ModulePermissions: View {
    let permissions: JSON

    var body: some View {
        let keys = permissions.keys.list.map(\.str)
        let reads = permissions.read.list.map(\.str)
        HStack(spacing: 6) {
            chip(permissions.network.flag ? "Network" : "No network", "network", permissions.network.flag)
            chip(keys.isEmpty ? "No keys" : "Keys: " + keys.joined(separator: ", "), "key", !keys.isEmpty)
            if !reads.isEmpty { chip("Reads: " + reads.joined(separator: ", "), "doc.text.magnifyingglass", true) }
        }
    }

    private func chip(_ text: String, _ icon: String, _ granted: Bool) -> some View {
        Label(text, systemImage: icon)
            .font(.system(size: 11))
            .lineLimit(1).truncationMode(.middle)
            .foregroundStyle(granted ? .primary : .secondary)
            .padding(.horizontal, 8).padding(.vertical, 3)
            .background(Color.primary.opacity(granted ? 0.08 : 0.04), in: Capsule())
    }
}

/// Every file of a module, in full. Approving runs what is shown here, so nothing is folded
/// or cut short, and long lines wrap rather than scroll out of sight.
struct ModuleCodeSheet: View {
    @Environment(Store.self) private var store
    @Environment(\.dismiss) private var dismiss
    let slug: String
    let approving: Bool
    @State private var module: JSON = .null
    @State private var problem: String?

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(verbatim: module.name.str.isEmpty ? slug : module.name.str).font(.headline)
                    if !module.isNull {
                        Text(verbatim: "version \(module.version.str) · \(plural(module.files.list.count, "file"))")
                            .font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                }
                Spacer()
                if approving, module.status.str == "pending" {
                    AsyncButton("Approve and run") {
                        try await store.moduleAction(slug, "approve", version: module["hash"].str)
                        dismiss()
                    }
                    .buttonStyle(.borderedProminent)
                }
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            .padding(.horizontal, 16).padding(.vertical, 12)
            Divider()
            if let problem {
                EmptyNote(title: "Could not read the module", detail: problem).frame(maxHeight: .infinity)
            } else if module.isNull {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        ModulePermissions(permissions: module.permissions)
                        if !module.config.dict.isEmpty {
                            fileView("config", JSONText.pretty(module.config))
                        }
                        ForEach(module.files.list, id: \.path.str) { file in fileView(file.path.str, file.text.str) }
                    }
                    .padding(16)
                }
            }
        }
        .frame(minWidth: 640, idealWidth: 760, minHeight: 460, idealHeight: 640)
        .task { await load() }
    }

    private func fileView(_ name: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(verbatim: name).font(.system(size: 12, weight: .semibold, design: .monospaced))
            Text(verbatim: text)
                .font(.system(size: 11.5, design: .monospaced))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
                .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        }
    }

    private func load() async {
        guard let api = store.api else { problem = "Not connected to the runtime yet."; return }
        do { module = try await api.get("/api/modules/\(API.escape(slug))").module }
        catch { problem = error.localizedDescription }
    }
}

private struct ModuleLogSheet: View {
    @Environment(Store.self) private var store
    @Environment(\.dismiss) private var dismiss
    let slug: String
    @State private var lines: [JSON] = []

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Log").font(.headline)
                Spacer()
                Button("Refresh") { Task { await load() } }
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            .padding(.horizontal, 16).padding(.vertical, 12)
            Divider()
            if lines.isEmpty {
                EmptyNote(title: "Nothing logged since Mission Control started").frame(maxHeight: .infinity)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 3) {
                        ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                            Text(verbatim: "\(Date(timeIntervalSince1970: line.ts.num).formatted(date: .omitted, time: .standard))  \(line.text.str)")
                                .font(.system(size: 11.5, design: .monospaced))
                                .foregroundStyle(line.level.str == "error" ? MC.danger : line.level.str == "warn" ? MC.warn : .primary)
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(16)
                }
            }
        }
        .frame(minWidth: 620, idealWidth: 720, minHeight: 380, idealHeight: 540)
        .task { await load() }
    }

    private func load() async {
        guard let api = store.api, let reply = try? await api.get("/api/modules/\(API.escape(slug))") else { return }
        lines = reply.module.logs.list
    }
}

enum JSONText {
    static func pretty(_ value: JSON) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: value.raw, options: [.prettyPrinted, .sortedKeys]),
              let text = String(data: data, encoding: .utf8) else { return "" }
        return text
    }
}
