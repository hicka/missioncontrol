import SwiftUI

/// The main window: the sections and chats on the left, the one in view on the right.
struct ShellView: View {
    @Environment(Store.self) private var store
    let runtime: RuntimeModel
    /// The settings read that found no model ready, while the welcome is up.
    @State private var welcome: JSON?

    var body: some View {
        NavigationSplitView {
            Sidebar()
                .navigationSplitViewColumnWidth(min: 200, ideal: 228, max: 300)
        } detail: {
            detail
                .frame(minWidth: 560, maxWidth: .infinity, maxHeight: .infinity)
                .background(Color(nsColor: .windowBackgroundColor))
        }
        .overlay(alignment: .bottom) { ToastStack() }
        .overlay { CommandPaletteHost() }
        .onChange(of: store.route, initial: true) { _, route in runtime.managerVisible = route == .runtime }
        .sheet(isPresented: Binding(get: { welcome != nil }, set: { if !$0 { welcome = nil } })) {
            if let welcome {
                WelcomeView(full: welcome) { WelcomeGate.skipped = true; self.welcome = nil }.environment(store)
            }
        }
        .task(id: store.live) { await checkWelcome() }
    }

    /// Once the runtime has answered, a missing model brings up the welcome, as the web does.
    private func checkWelcome() async {
        guard store.live, welcome == nil, !WelcomeGate.skipped, let api = store.api,
              let full = try? await SettingsCache.read(api, SettingsCache.full), !full.llm.ready.flag else { return }
        welcome = full
    }

    @ViewBuilder private var detail: some View {
        switch store.route {
        case .home: HomeView()
        case .inbox: InboxView()
        case .tasks: TasksView()
        case .activity: ActivityView()
        case .schedule: ScheduleView()
        case .knowledge: KnowledgeView()
        case .evolution: EvolutionView()
        case .modules: ModulesView()
        case .runtime: RuntimeView(model: runtime)
        case .chat(let id): ChatView(threadID: id).id(id)
        case .settings(let tab): SettingsView(initialTab: tab)
        }
    }
}

private struct Sidebar: View {
    @Environment(Store.self) private var store
    @AppStorage("sidebar.more") private var moreOpen = false

    private var inMore: Bool { [.activity, .schedule, .knowledge, .evolution, .modules].contains(store.route) }
    private var modulesNeedYou: Int { store.modules.filter { ["failed", "pending"].contains($0.status.str) }.count }

    var body: some View {
        @Bindable var store = store
        List(selection: Binding<Route?>(get: { store.route }, set: { if let route = $0 { store.route = route; if case .chat(let id) = route { store.unread.remove(id) } } })) {
            // What is used every day stays in view. The rest is one click further, and
            // opens by itself when something in it wants attention.
            Section {
                row("Home", "house", .home)
                row("Inbox", "tray", .inbox, badge: store.inbox.count, tone: MC.warn)
                row("Tasks", "checklist", .tasks, badge: store.openMissions.count)
            }
            Section("Chats") {
                ForEach(store.threadRows) { thread in ThreadRow(thread: thread).tag(Route.chat(thread.id)) }
            }
            Section("More", isExpanded: Binding(get: { moreOpen || inMore || modulesNeedYou > 0 }, set: { moreOpen = $0 })) {
                row("Activity", "bolt", .activity, badge: store.workingCount)
                row("Schedule", "clock", .schedule, badge: store.activeWatchCount)
                row("Knowledge", "book", .knowledge)
                row("Evolution", "chart.line.uptrend.xyaxis", .evolution, badge: store.evolution.trial.int)
                row("Modules", "puzzlepiece.extension", .modules, badge: modulesNeedYou, tone: MC.warn)
            }
        }
        .listStyle(.sidebar)
        .safeAreaInset(edge: .bottom) { footer }
    }

    private func row(_ title: String, _ icon: String, _ route: Route, badge: Int = 0, tone: Color = .secondary) -> some View {
        Label(title, systemImage: icon)
            .badge(badge > 0 ? Text("\(badge)").foregroundColor(tone) : nil)
            .tag(route)
    }

    private var footer: some View {
        HStack(spacing: 8) {
            Circle().fill(store.link ? MC.good : MC.warn).frame(width: 7, height: 7)
            Text(store.link ? (store.user.name.optStr ?? "Connected") : "Reconnecting…").font(.system(size: 12)).lineLimit(1)
            Spacer()
            Button { store.paletteOpen = true } label: { Image(systemName: "magnifyingglass") }.buttonStyle(.plain).help("Search and commands (⌘K)").accessibilityLabel("Search and commands")
            Button { store.route = .runtime } label: { Image(systemName: "cpu") }.buttonStyle(.plain).help("Runtime Manager").accessibilityLabel("Runtime Manager")
            Button { store.route = .settings("llm") } label: { Image(systemName: "gearshape") }.buttonStyle(.plain).help("Settings").accessibilityLabel("Settings")
        }
        .foregroundStyle(.secondary)
        .padding(.horizontal, 14).padding(.vertical, 10)
    }
}

private struct ThreadRow: View {
    @Environment(Store.self) private var store
    let thread: JSON

    var body: some View {
        let agentID = thread.agent.str
        HStack(spacing: 8) {
            if thread.kind.str == "dm" { AgentBadge(agent: store.agent(agentID), size: 24) } else { Image(systemName: "person.3").frame(width: 24) }
            VStack(alignment: .leading, spacing: 0) {
                Text(thread.name.str).font(.system(size: 13)).lineLimit(1)
                if store.isBusy(agentID) { Text(verbatim: store.doing(agentID)).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1) }
            }
            Spacer()
            // Busy shows in the presence itself, so only unread needs a mark of its own.
            if store.unread.contains(thread.id) { Circle().fill(Color.accentColor).frame(width: 7, height: 7).accessibilityLabel("Unread") }
        }
    }
}

private struct ToastStack: View {
    @Environment(Store.self) private var store
    var body: some View {
        VStack(spacing: 6) {
            ForEach(store.toasts) { toast in
                Text(toast.text).font(.callout).lineLimit(3)
                    .padding(.horizontal, 14).padding(.vertical, 9)
                    .glassBackground(cornerRadius: 12)
                    .foregroundStyle(toast.error ? MC.danger : .primary)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .animation(.spring(response: 0.3, dampingFraction: 0.85), value: store.toasts)
        .padding(.bottom, 18)
    }
}
