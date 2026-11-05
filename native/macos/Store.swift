import Foundation
import Observation

struct Agent: Identifiable, Equatable, Sendable {
    let id: String, name: String, color: String, order: Int
    init(_ row: JSON) { id = row.id; name = row.name.str; color = row.color.str; order = row.order.int }
}

struct Message: Identifiable, Equatable, Sendable {
    var id: Int
    let ts: Double, thread: String, sender: String, kind: String, text: String
    let meta: JSON
    init(_ row: JSON) {
        id = row["id"].int; ts = row.ts.num; thread = row.thread.str; sender = row.sender.str
        kind = row.kind.str; text = row.text.str; meta = row.meta
    }
    /// Written here, shown at once, and replaced by the server's copy when it arrives.
    init(local id: Int, thread: String, sender: String, text: String, meta: JSON = .null) {
        self.id = id; ts = Date().timeIntervalSince1970; self.thread = thread; self.sender = sender
        kind = "chat"; self.text = text; self.meta = meta
    }
}

struct StreamText: Equatable { var agent: String; var thread: String; var text = ""; var thinking = "" }

/// Where the main window is looking.
enum Route: Hashable {
    case home, inbox, tasks, activity, schedule, knowledge, evolution, modules, runtime
    case chat(String)
    case settings(String)
}

/// One conversation, kept once read, so going back to it draws at once.
@Observable @MainActor
final class ThreadModel {
    let id: String
    var messages: [Message] = []
    var hasMore = false
    var loaded = false
    var loading = false
    /// The page on screen runs up to the present. Opened at a search hit it does not, and
    /// the server has no way to page forward, so the way back is reading the latest page.
    private(set) var atLatest = true
    /// A message arrived while the page on screen was in the past.
    private(set) var missedNew = false
    /// The message a search hit opened this page at, which sits at the foot of the page.
    private(set) var anchor: Int?
    /// Moves when the page on screen is replaced whole, so the list starts again at its foot.
    private(set) var generation = 0
    /// Moves when something asks for this thread to open at a message.
    private(set) var jumps = 0
    @ObservationIgnored private var jumpTarget: Int?
    @ObservationIgnored private var pageRequest = 0
    @ObservationIgnored private var nextLocal = -1

    init(id: String) { self.id = id }

    func load(_ api: API, limit: Int = 80) async {
        guard !loading else { return }
        loading = true; defer { loading = false }
        pageRequest += 1
        let request = pageRequest
        guard let page = try? await api.get("/api/thread/\(API.escape(id))?limit=\(limit)"), request == pageRequest else { return }
        let rows = page.messages.list.map(Message.init)
        let pending = messages.filter { local in local.id < 0 && !rows.contains { $0.sender == local.sender && $0.text == local.text } }
        messages = rows + pending
        hasMore = page.has_more.flag
        loaded = true
        if !atLatest {
            atLatest = true
            missedNew = false
            anchor = nil
            generation += 1
        }
    }

    /// The page that ends with one message, the way a search hit opens.
    func load(_ api: API, at messageID: Int, limit: Int = 80) async {
        loading = true
        pageRequest += 1
        let request = pageRequest
        defer { if request == pageRequest { loading = false } }
        guard let page = try? await api.get("/api/thread/\(API.escape(id))?limit=\(limit)&at_id=\(messageID)"), request == pageRequest else { return }
        messages = page.messages.list.map(Message.init)
        hasMore = page.has_more.flag
        loaded = true
        atLatest = page.at_latest.flag
        missedNew = false
        anchor = messageID
        generation += 1
    }

    func loadOlder(_ api: API) async {
        guard !loading, hasMore, let first = messages.first(where: { $0.id > 0 }) else { return }
        loading = true; defer { loading = false }
        let request = pageRequest
        guard let page = try? await api.get("/api/thread/\(API.escape(id))?limit=80&before_ts=\(first.ts)&before_id=\(first.id)"), request == pageRequest else { return }
        messages.insert(contentsOf: page.messages.list.map(Message.init), at: 0)
        hasMore = page.has_more.flag
    }

    func requestJump(to messageID: Int) {
        jumpTarget = messageID
        jumps += 1
    }

    func takeJump() -> Int? {
        defer { jumpTarget = nil }
        return jumpTarget
    }

    func appendLocal(sender: String, text: String, meta: JSON = .null) -> Int {
        let id = nextLocal; nextLocal -= 1
        messages.append(Message(local: id, thread: self.id, sender: sender, text: text, meta: meta))
        return id
    }
    func removeLocal(_ id: Int) { messages.removeAll { $0.id == id } }

    func receive(_ message: Message) {
        // A page opened at a search hit is a view of the past. A new message must not graft
        // itself onto the end of it, so it waits behind the Latest button.
        guard atLatest else { missedNew = true; return }
        if messages.contains(where: { $0.id == message.id }) { return }
        if let pending = messages.firstIndex(where: { $0.id < 0 && $0.sender == message.sender && $0.text == message.text }) {
            messages[pending] = message
        } else {
            messages.append(message)
        }
    }
}

/// Everything the windows draw. Each field is observed on its own and only assigned when
/// it really changed, so a refresh redraws the rows that moved and nothing else.
@Observable @MainActor
final class Store {
    private(set) var api: API?
    /// The event socket is open.
    var link = false
    /// Read from the server since connecting. Until then what is on screen came from the
    /// disk snapshot: fine to look at, but nothing on it may be decided.
    var live = false
    var route: Route = .home

    var user: JSON = .null
    var agents: [Agent] = []
    var threadRows: [JSON] = []
    var unread: Set<String> = []

    var missions: [JSON] = []
    var executions: [JSON] = []
    var watches: [JSON] = []
    var skills: [JSON] = []
    var activity: [JSON] = []
    var counts: JSON = .null
    var inbox: [JSON] = []
    var evolution: JSON = .null
    var jobs: [JSON] = []
    var workers: [JSON] = []
    var agentInfo: JSON = .null

    /// Add-ons the agents built, and the cards they draw, which the desktop shows.
    var modules: [JSON] = []
    var moduleCards: [JSON] = []
    var modulesSandboxed = true

    /// Held commands the conversation draws as cards, by id, as the server last said.
    var approvalCards: [String: JSON] = [:]

    var states: [String: String] = [:]
    var tools: [String: String] = [:]
    var streams: [String: StreamText] = [:]
    var typing: Set<String> = []

    /// Screens that read their own endpoints reload when these move: `.task(id: store.ticks.memory)`.
    var ticks = Ticks()
    struct Ticks: Equatable { var kernel = 0, evolution = 0, memory = 0, skills = 0, settings = 0, improvements = 0, browser = 0 }

    var toasts: [Toast] = []
    struct Toast: Identifiable, Equatable { let id = UUID(); let text: String; let error: Bool }
    /// A worker to show when the Activity screen opens.
    var focusWorker: String?
    /// Search and commands, over whichever screen is up.
    var paletteOpen = false
    /// Another screen asking Home's composer to take focus, with text to start from if any.
    /// Home clears it once done, so it also works when Home is not on screen yet.
    var homeComposer: HomeComposerRequest?
    struct HomeComposerRequest: Equatable { let id = UUID(); var text: String? }

    @ObservationIgnored private var threadCache: [String: ThreadModel] = [:]
    @ObservationIgnored private let stream = EventStream()
    @ObservationIgnored private var refreshTask: Task<Void, Never>?
    @ObservationIgnored private var refreshing = false
    @ObservationIgnored private var again = false
    @ObservationIgnored private var lastSnapshot = Date.distantPast
    /// A message reached the conversation; the floating bar shows replies from this.
    @ObservationIgnored var onMessage: ((Message) -> Void)?
    /// A word of the lead's reply arrived; the floating bar reacts to each one.
    @ObservationIgnored var onDelta: (() -> Void)?
    /// Ask the signed app's updater to look now.
    @ObservationIgnored var onCheckForUpdates: (() -> Void)?
    /// Bring the main window forward.
    @ObservationIgnored var onShowWindow: (() -> Void)?

    // MARK: Connecting

    func connect(_ api: API) {
        if self.api?.base == api.base, link { return }
        self.api = api
        live = false
        loadSnapshot(for: api)
        stream.start(api, onLink: { [weak self] up in self?.linkChanged(up) }, onEvent: { [weak self] event in self?.handle(event) })
    }

    func disconnect() { stream.stop(); link = false; live = false; api = nil }

    private func linkChanged(_ up: Bool) {
        link = up
        guard up else { streams = [:]; typing = []; return }
        // Whatever happened while the socket was down arrives as a fresh read.
        Task {
            await refreshState()
            await refresh()
            await loadModules()
            for model in threadCache.values where model.loaded && model.atLatest { if let api { await model.load(api) } }
        }
    }

    // MARK: Reading

    var lead: Agent? { agents.first }
    var leadThread: String? { lead.map { "dm:\($0.id)" } }
    func agent(_ id: String) -> Agent? { agents.first { $0.id == id } }
    func name(_ id: String) -> String {
        if let agent = agent(id) { return agent.name }
        return ["*": "Everywhere", "selfimprove": "Self-repair", "user": user.name.str, "control": "Control"][id] ?? (id.isEmpty ? "Someone" : id)
    }
    var openMissions: [JSON] { missions.filter { ["running", "queued", "waiting"].contains($0.status.str) } }
    var workingCount: Int { workers.filter { $0.status.str == "working" }.count }
    var activeWatchCount: Int { watches.filter { $0.status.str == "active" }.count }
    func isBusy(_ agentID: String) -> Bool { (states[agentID] ?? "idle") != "idle" }

    func thread(_ id: String) -> ThreadModel {
        if let model = threadCache[id] { return model }
        let model = ThreadModel(id: id)
        threadCache[id] = model
        return model
    }

    func refreshState() async {
        guard let api, let (data, state) = try? await api.getRaw("/api/state") else { return }
        applyState(state)
        saveSnapshot("state", data, for: api)
    }

    private func applyState(_ state: JSON) {
        let next = state.agents.list.map(Agent.init).sorted { $0.order < $1.order }
        if next != agents { agents = next }
        if state.user != user { user = state.user }
        if state.threads.list != threadRows { threadRows = state.threads.list }
    }

    /// The projection every workspace screen draws from.
    func refresh() async {
        guard let api else { return }
        if refreshing { again = true; return }
        refreshing = true
        defer {
            refreshing = false
            if again { again = false; refreshSoon() }
        }
        guard let (data, control) = try? await api.getRaw("/api/control") else { return }
        applyControl(control)
        live = true
        if Date().timeIntervalSince(lastSnapshot) > 30 {
            lastSnapshot = Date()
            saveSnapshot("control", data, for: api)
        }
    }

    private func applyControl(_ control: JSON) {
        func set(_ path: ReferenceWritableKeyPath<Store, [JSON]>, _ value: JSON) {
            let rows = value.list
            if self[keyPath: path] != rows { self[keyPath: path] = rows }
        }
        set(\.missions, control.missions); set(\.executions, control.executions); set(\.watches, control.watches)
        set(\.skills, control.skills); set(\.activity, control.activity); set(\.inbox, control.inbox)
        set(\.jobs, control.jobs); set(\.workers, control.workers)
        if counts != control.counts { counts = control.counts }
        if evolution != control.evolution { evolution = control.evolution }
        if agentInfo != control.agents { agentInfo = control.agents }
        var nextStates = states, nextTools: [String: String] = [:]
        for (id, info) in control.agents.dict {
            nextStates[id] = info.state.str
            if let tool = info.tool.optStr { nextTools[id] = tool }
        }
        if nextStates != states { states = nextStates }
        if nextTools != tools { tools = nextTools }
    }

    func refreshSoon(after delay: Double = 0.45) {
        refreshTask?.cancel()
        refreshTask = Task {
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            if !Task.isCancelled { await refresh() }
        }
    }

    // MARK: Live events

    private func handle(_ event: JSON) {
        switch event.type.str {
        case "stream":
            let key = "\(event.agent.str)|\(event.thread.str)"
            if event.reset.flag { streams[key] = nil; return }
            var entry = streams[key] ?? StreamText(agent: event.agent.str, thread: event.thread.str)
            entry.thinking += event.thinking.str
            entry.text += event.text.str
            streams[key] = entry
            if event.thread.str == leadThread { onDelta?() }
        case "message":
            receive(Message(event.msg))
        case "typing":
            let key = "\(event.agent.str)|\(event.thread.str)"
            if event.on.flag { typing.insert(key) } else { typing.remove(key); streams[key] = nil }
        case "execution":
            let record = event.execution
            if let index = executions.firstIndex(where: { $0.id == record.id }) { executions[index] = record }
            else { executions = Array(([record] + executions).prefix(120)) }
            if record.kind.str == "tool" {
                if record.status.str == "running" { tools[record.agent.str] = record.name.str }
                else if tools[record.agent.str] == record.name.str { tools[record.agent.str] = nil }
            }
        case "agent_state":
            states[event.agent.str] = event.state.str
            if event.state.str == "idle" { tools[event.agent.str] = nil }
        case "user":
            user = event.user
        case "agent_added", "agent_updated", "agent_removed", "settings":
            ticks.settings += 1
            Task { await refreshState() }
            refreshSoon()
        case "session_updated":
            ticks.kernel += 1
            refreshSoon()
        case "trigger", "watchdog", "kernel_updated":
            ticks.kernel += 1
        case "mission_updated":
            ticks.kernel += 1
            refreshSoon()
        case "evolution_updated":
            ticks.evolution += 1
            refreshSoon()
        case "memory_updated":
            ticks.memory += 1
            refreshSoon()
        case "skills_updated":
            ticks.skills += 1
            refreshSoon()
        case "improvements_updated":
            ticks.improvements += 1
            refreshSoon()
        case "browser_auth_updated", "browser_updated":
            ticks.browser += 1
            refreshSoon()
        case "approvals_updated":
            refreshSoon()
            let shown = Array(approvalCards.keys)
            Task { for id in shown { await loadApproval(id) } }
        case "watch_updated", "cron_updated":
            refreshSoon()
        case "modules_updated", "module_cards":
            Task { await loadModules() }
        case "module_card":
            if event.removed.flag {
                moduleCards.removeAll { $0.module.str == event.module.str && $0.id == event["id"].str }
            } else if !event.card.isNull {
                let card = event.card
                if let index = moduleCards.firstIndex(where: { $0.module.str == card.module.str && $0.id == card.id }) {
                    if moduleCards[index] != card { moduleCards[index] = card }
                } else {
                    moduleCards.append(card)
                }
            }
        default:
            break
        }
    }

    private func receive(_ message: Message) {
        if message.kind != "tool" {
            for key in streams.keys where key.hasPrefix("\(message.sender)|") { streams[key] = nil }
        }
        // A message between two profiles shows in each one's chat.
        var targets = [message.thread]
        if message.kind == "a2a" { targets = ["dm:\(message.sender)", "dm:\(message.meta.to.str)"] }
        for id in targets {
            if let model = threadCache[id] { model.receive(message) }
            if message.sender != "user", route != .chat(id), threadRows.contains(where: { $0.id == id }) { unread.insert(id) }
        }
        onMessage?(message)
    }

    // MARK: Doing

    /// Into a conversation. The message is on screen before the server has answered.
    /// A slash command is posted back by the server in its own words, along with its reply,
    /// and those arrive as messages, so the copy written here goes.
    @discardableResult
    func send(_ text: String, to threadID: String, voice: Bool = false, attachments: [JSON] = []) async -> JSON? {
        guard let api else { notify("Not connected to the runtime yet.", error: true); return nil }
        let model = thread(threadID)
        // Writing from a view of the past comes back to the present, or the message would
        // land at the end of a page it does not belong to.
        if !model.atLatest { await model.load(api) }
        // The server stores a message with no words under this text, and the copy written
        // here is matched to the server's by its text.
        let shown = text.isEmpty && !attachments.isEmpty ? "(sent a file)" : text
        let meta: JSON = attachments.isEmpty ? .null : .object(["attachments": .array(attachments)])
        let local = model.appendLocal(sender: "user", text: shown, meta: meta)
        do {
            var body: [String: Any] = ["thread": threadID, "text": text]
            if voice { body["voice"] = true }
            if !attachments.isEmpty { body["attachments"] = attachments.map(\.raw) }
            let result = try await api.post("/api/send", body)
            if !result.command.isNull || !result.reply.isNull { model.removeLocal(local) }
            return result
        } catch {
            model.removeLocal(local)
            notify(error.localizedDescription, error: true)
            return nil
        }
    }

    func stopAgent(_ id: String) async {
        do { _ = try await api?.post("/api/agents/\(API.escape(id))/stop") }
        catch { notify(error.localizedDescription, error: true) }
    }

    func stopWorker(_ id: String) async {
        do { _ = try await api?.post("/api/kernel/sessions/\(API.escape(id))/stop"); refreshSoon(after: 0.1) }
        catch { notify(error.localizedDescription, error: true) }
    }

    /// Approve or decline one held command. Approving runs it exactly as written.
    func decide(approval id: String, approve: Bool) async throws {
        guard let api, live else { throw APIError(message: "Still reading the current state. Try again in a moment.") }
        do {
            _ = try await api.post("/api/approvals/\(API.escape(id))/\(approve ? "approve" : "deny")")
        } catch let error as URLError where error.code == .timedOut && approve {
            // The server answers when the command finishes. A long one outlasts the request
            // but is still running, and its output goes to the task.
            notify("Still running. The task will have the output.")
        }
        refreshSoon(after: 0.05)
        await loadApproval(id)
    }

    /// One held command as it stands now. False when the server could not say, such as
    /// one too old to know the request at all.
    @discardableResult
    func loadApproval(_ id: String) async -> Bool {
        guard let api, let reply = try? await api.get("/api/approvals/\(API.escape(id))"), !reply.approval.isNull else { return false }
        if approvalCards[id] != reply.approval { approvalCards[id] = reply.approval }
        return true
    }

    // MARK: Modules

    func loadModules() async {
        guard let api, let reply = try? await api.get("/api/modules") else { return }
        let rows = reply.modules.list, cards = reply.cards.list
        if rows != modules { modules = rows }
        if cards != moduleCards { moduleCards = cards }
        modulesSandboxed = reply.sandboxed.flag
    }

    /// The owner's own controls. Approving sends back the version that was shown, and the
    /// server refuses it if the code has changed since.
    func moduleAction(_ slug: String, _ action: String, version: String = "") async throws {
        guard let api, live else { throw APIError(message: "Still reading the current state. Try again in a moment.") }
        _ = try await api.post("/api/modules/\(API.escape(slug))/\(action)", version.isEmpty ? [:] : ["version": version])
        await loadModules()
        refreshSoon(after: 0.05)
    }

    /// A button, toggle or field on a module's card.
    func cardAction(_ card: JSON, _ event: CardEvent) async {
        guard let api else { notify("Not connected to the runtime yet.", error: true); return }
        do {
            _ = try await api.post("/api/modules/\(API.escape(card.module.str))/cards/\(API.escape(card.id))/action",
                                   ["action": event.action, "value": event.value.raw, "values": event.values.mapValues(\.raw)])
        } catch { notify(error.localizedDescription, error: true) }
    }

    func setCardDesktop(_ card: JSON, _ desktop: Bool) async {
        guard let api else { return }
        do {
            _ = try await api.put("/api/modules/\(API.escape(card.module.str))/cards/\(API.escape(card.id))", ["desktop": desktop])
            if let index = moduleCards.firstIndex(where: { $0.module.str == card.module.str && $0.id == card.id }),
               case .object(var row) = moduleCards[index] {
                row["desktop"] = .bool(desktop)
                moduleCards[index] = .object(row)
            }
        } catch { notify(error.localizedDescription, error: true) }
    }

    func notify(_ text: String, error: Bool = false) {
        let toast = Toast(text: text, error: error)
        toasts = Array((toasts + [toast]).suffix(3))
        Task {
            try? await Task.sleep(nanoseconds: error ? 9_000_000_000 : 5_500_000_000)
            toasts.removeAll { $0.id == toast.id }
        }
    }

    func go(_ route: Route) {
        self.route = route
        if case .chat(let id) = route { unread.remove(id) }
        onShowWindow?()
    }

    /// A conversation, opened at one message, the way a search hit does.
    func open(_ threadID: String, at messageID: Int) {
        thread(threadID).requestJump(to: messageID)
        go(.chat(threadID))
    }

    /// Home, with its composer ready to type in, and text to start from if there is any.
    func focusHome(prefill text: String? = nil) {
        homeComposer = HomeComposerRequest(text: text)
        go(.home)
    }

    // MARK: Snapshot on disk: the last state, drawn at launch before the runtime is up

    private func snapshotFolder(for api: API) -> URL {
        let name = api.base.replacingOccurrences(of: "[^A-Za-z0-9]+", with: "_", options: .regularExpression)
        return FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("com.missioncontrol.native/\(name)")
    }

    private func saveSnapshot(_ name: String, _ data: Data, for api: API) {
        let folder = snapshotFolder(for: api)
        Task.detached(priority: .utility) {
            try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            let file = folder.appendingPathComponent(name + ".json")
            try? data.write(to: file, options: .atomic)
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        }
    }

    private func loadSnapshot(for api: API) {
        let folder = snapshotFolder(for: api)
        if let data = try? Data(contentsOf: folder.appendingPathComponent("state.json")) { applyState(JSON(data: data)) }
        if let data = try? Data(contentsOf: folder.appendingPathComponent("control.json")) { applyControl(JSON(data: data)) }
    }
}
