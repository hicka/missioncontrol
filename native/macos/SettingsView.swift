import SwiftUI

/// The web keeps these on eight tabs, and one of those tabs holds eight cards. Here each
/// has a pane of its own, so opening one reads only its own endpoint.
enum SettingsPane: String, CaseIterable, Identifiable {
    case llm, profiles, voice, usage
    case browser, channels, whatsapp, apple, mail
    case jobs, memory, improve, updates
    case github, hooks, mcp, importer = "import", fleet, remote, ports

    var id: String { rawValue }

    var title: String {
        switch self {
        case .llm: return "General"
        case .profiles: return "Profiles"
        case .voice: return "Voice"
        case .usage: return "Usage and budget"
        case .browser: return "Websites"
        case .channels: return "Channels"
        case .whatsapp: return "WhatsApp"
        case .apple: return "Apple apps"
        case .mail: return "Mail cleanup"
        case .jobs: return "Jobs"
        case .memory: return "Knowledge"
        case .improve: return "Improve"
        case .updates: return "Updates"
        case .github: return "GitHub"
        case .hooks: return "Webhooks"
        case .mcp: return "MCP servers"
        case .importer: return "Import"
        case .fleet: return "Machines"
        case .remote: return "Mobile access"
        case .ports: return "Ports"
        }
    }

    var icon: String {
        switch self {
        case .llm: return "gearshape"
        case .profiles: return "person.crop.circle"
        case .voice: return "waveform"
        case .usage: return "gauge.with.dots.needle.50percent"
        case .browser: return "globe"
        case .channels: return "bubble.left.and.bubble.right"
        case .whatsapp: return "phone.bubble"
        case .apple: return "app.badge"
        case .mail: return "envelope"
        case .jobs: return "clock"
        case .memory: return "book"
        case .improve: return "wrench.and.screwdriver"
        case .updates: return "arrow.down.circle"
        case .github: return "arrow.triangle.branch"
        case .hooks: return "link"
        case .mcp: return "puzzlepiece.extension"
        case .importer: return "square.and.arrow.down"
        case .fleet: return "desktopcomputer"
        case .remote: return "iphone"
        case .ports: return "network"
        }
    }

    static let groups: [(title: String, panes: [SettingsPane])] = [
        ("Agent", [.llm, .profiles, .voice, .usage]),
        ("Reach", [.browser, .channels, .whatsapp, .apple, .mail]),
        ("Work", [.jobs, .memory, .improve, .updates]),
        ("Integrations", [.github, .hooks, .mcp, .importer, .fleet, .remote, .ports]),
    ]

    /// A tab name from anywhere in the app: the web's names, the server's `settings_tab`
    /// values, or `pane:detail`, as in `profiles:new`, `profiles:<id>` and `memory:skills`.
    static func parse(_ tab: String) -> (pane: SettingsPane, detail: String) {
        let parts = tab.split(separator: ":", maxSplits: 1).map(String.init)
        let name = parts.first ?? ""
        let detail = parts.count > 1 ? parts[1] : ""
        if let pane = SettingsPane(rawValue: name) { return (pane, detail) }
        switch name {
        case "budget": return (.usage, "")
        case "websites": return (.browser, "")
        case "devices", "phones": return (.channels, "")
        case "agents", "agent", "profile": return (.profiles, detail)
        case "knowledge": return (.memory, detail)
        case "skills", "playbooks": return (.memory, name)
        case "heartbeat", "brief": return (.jobs, "")
        case "integrations": return (.github, "")
        case "webhooks": return (.hooks, "")
        case "machines", "tailscale": return (.fleet, "")
        case "mobile": return (.remote, "")
        default: return (.llm, "")
        }
    }
}

struct SettingsView: View {
    let initialTab: String
    @Environment(Store.self) private var store
    @State private var pane: SettingsPane
    @State private var detail: String

    init(initialTab: String) {
        self.initialTab = initialTab
        let target = SettingsPane.parse(initialTab)
        _pane = State(initialValue: target.pane)
        _detail = State(initialValue: target.detail)
    }

    var body: some View {
        HStack(spacing: 0) {
            SettingsSidebar(pane: $pane)
                .frame(width: 196)
            Divider()
            VStack(alignment: .leading, spacing: 0) {
                Text(pane.title)
                    .font(.system(size: 20, weight: .semibold))
                    .padding(.horizontal, 24)
                    .padding(.top, 16)
                    .padding(.bottom, 2)
                    .accessibilityAddTraits(.isHeader)
                SettingsPaneBody(pane: pane, detail: detail)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .onChange(of: initialTab, initial: true) { _, tab in
            // The learning engine has a page of its own; the Runtime Manager still asks
            // for it as a settings tab.
            if tab == "evolution" { store.go(.evolution); return }
            let target = SettingsPane.parse(tab)
            pane = target.pane
            detail = target.detail
        }
    }
}

/// The badges are read here so that a new inbox item redraws this list and not the pane
/// beside it, which may have a half-typed form in it.
private struct SettingsSidebar: View {
    @Environment(Store.self) private var store
    @Binding var pane: SettingsPane

    var body: some View {
        List(selection: Binding<SettingsPane?>(get: { pane }, set: { if let next = $0 { pane = next } })) {
            ForEach(SettingsPane.groups, id: \.title) { group in
                Section(group.title) {
                    ForEach(group.panes) { row($0) }
                }
            }
        }
        .listStyle(.sidebar)
    }

    private func row(_ item: SettingsPane) -> some View {
        let count = waiting(on: item)
        return Label(item.title, systemImage: item.icon)
            .badge(count)
            .accessibilityValue(count > 0 ? "\(count) waiting" : "")
            .tag(item)
    }

    private func waiting(on item: SettingsPane) -> Int {
        switch item {
        case .jobs:
            let jobs = store.jobs.filter { $0.status.str == "pending" }.count
            return jobs + store.inbox.filter { ["command", "tool"].contains($0.kind.str) }.count
        case .improve: return store.inbox.filter { $0.kind.str == "proposal" }.count
        case .browser: return store.inbox.filter { $0.kind.str == "signin" }.count
        default: return 0
        }
    }
}

private struct SettingsPaneBody: View {
    let pane: SettingsPane
    let detail: String

    var body: some View {
        switch pane {
        case .llm: SettingsGeneralPane()
        case .profiles: SettingsProfilesPane(open: detail)
        case .voice: SettingsVoicePane()
        case .usage: SettingsUsagePane()
        case .browser: SettingsBrowserPane()
        case .channels: SettingsChannelsPane()
        case .whatsapp: SettingsWhatsAppPane()
        case .apple: SettingsApplePane()
        case .mail: SettingsMailPane()
        case .jobs: SettingsJobsPane()
        case .memory: SettingsKnowledgePane(section: detail)
        case .improve: SettingsImprovePane()
        case .updates: SettingsUpdatesPane()
        case .github: SettingsGitHubPane()
        case .hooks: SettingsHooksPane()
        case .mcp: SettingsMCPPane()
        case .importer: SettingsImportPane()
        case .fleet: SettingsFleetPane()
        case .remote: SettingsRemotePane()
        case .ports: SettingsPortsPane()
        }
    }
}
