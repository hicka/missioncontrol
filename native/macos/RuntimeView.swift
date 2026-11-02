import SwiftUI

private let accent = Color(red: 0.36, green: 0.94, blue: 0.81)
private let panel = Color(red: 0.065, green: 0.085, blue: 0.105)

struct RuntimeView: View {
    @ObservedObject var model: RuntimeModel
    @State private var section = "Services"
    @State private var filter = ""

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 20) {
                VStack(alignment: .leading, spacing: 7) {
                    Text("MISSION CONTROL / NATIVE HOST").font(.system(size: 10, weight: .semibold, design: .monospaced)).tracking(2).foregroundColor(accent)
                    Text("Runtime manager").font(.system(size: 30, weight: .semibold))
                    Text("Every service, harness and process. One place to stay in control.").foregroundStyle(.secondary)
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 12) {
                    HStack(spacing: 7) { Circle().fill(model.status == "Running" ? accent : .orange).frame(width: 6, height: 6); Text(model.status).font(.system(.caption, design: .monospaced)) }
                    HStack {
                        if model.isRunning {
                            Button("Restart runtime") { model.stop(restart: true) }
                            Button("Stop runtime") { model.stop() }
                        } else { Button("Start runtime") { model.crashCount = 0; model.start() }.tint(accent) }
                        Button("Open workspace ↗") { model.onShowWorkspace?() }.disabled(model.snapshot == nil)
                    }.disabled(model.busy)
                }
            }.padding(28)
            if !model.error.isEmpty {
                HStack { Image(systemName: "exclamationmark.triangle"); Text(model.error).textSelection(.enabled); Spacer(); Button("Logs") { model.showLogs() }; Button { model.error = "" } label: { Image(systemName: "xmark") } }
                    .font(.callout).foregroundColor(.orange).padding(12).background(Color.orange.opacity(0.08)).padding(.horizontal, 28).padding(.bottom, 16)
            }
            HStack(spacing: 14) {
                metric("SERVICES ONLINE", "\(model.snapshot?.services.filter { $0.status == "running" }.count ?? 0)", "of \(model.snapshot?.services.count ?? 0) configured")
                metric("PROCESS INVENTORY", "\(model.processes.count)", "\(model.processes.filter { !$0.owned }.count) outside this runtime")
                metric("HARNESS", "\(model.snapshot?.harness.agents.count ?? 0)", "agents · contracts enforced")
                metric("MEMORY FOOTPRINT", String(format: "%.0f MB", model.processes.reduce(0) { $0 + $1.memory_mb }), "detected processes · resident")
            }.padding(.horizontal, 28)
            HStack {
                Picker("View", selection: $section) { ForEach(["Services", "Processes", "Harness", "Background & permissions", "Tasks"], id: \.self) { Text($0) } }.pickerStyle(.segmented).frame(maxWidth: 770)
                Spacer()
                if model.busy { ProgressView().controlSize(.small) }
                Button { Task { await model.refresh() } } label: { Image(systemName: "arrow.clockwise") }.help("Refresh inventory")
            }.padding(.horizontal, 28).padding(.vertical, 20)
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if section == "Services" { services }
                    if section == "Processes" { processes }
                    if section == "Harness" { harness }
                    if section == "Background & permissions" { permissions }
                    if section == "Tasks" { tasks }
                }.padding(.horizontal, 28).padding(.bottom, 24).frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack {
                Image(systemName: "externaldrive"); Text(model.dataHome).lineLimit(1).truncationMode(.middle)
                Spacer(); Text("HOST \(ProcessInfo.processInfo.processIdentifier)")
                if let pid = model.engine?.processIdentifier, model.isRunning { Text("· RUNTIME \(pid)") }
                if let pid = model.inventoryPID { Text("· INVENTORY \(pid)") }
                if let updated = model.lastUpdated { Text("· \(updated.formatted(date: .omitted, time: .standard))") }
            }.font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary).padding(14).background(panel)
        }.background(Color(red: 0.035, green: 0.047, blue: 0.06)).preferredColorScheme(.dark)
    }

    private func metric(_ title: String, _ value: String, _ caption: String) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(title).font(.system(size: 10, weight: .medium, design: .monospaced)).tracking(1).foregroundStyle(.secondary)
            Text(value).font(.system(size: 25, weight: .medium, design: .monospaced)).contentTransition(.numericText())
            Text(caption).font(.caption).foregroundStyle(.secondary)
        }.frame(maxWidth: .infinity, alignment: .leading).padding(18).background(panel).clipShape(RoundedRectangle(cornerRadius: 10))
    }
    private func status(_ value: String) -> some View {
        Text(value.uppercased()).font(.system(size: 10, weight: .semibold, design: .monospaced)).foregroundColor(["running", "idle", "enforced", "enabled"].contains(value) ? accent : .secondary).padding(.horizontal, 9).padding(.vertical, 5).background(Color.white.opacity(0.045)).clipShape(Capsule())
    }
    private func title(_ text: String, _ detail: String) -> some View {
        VStack(alignment: .leading, spacing: 5) { Text(text).font(.headline); Text(detail).font(.callout).foregroundStyle(.secondary) }.padding(.bottom, 3)
    }
    private var services: some View {
        Group {
            title("Managed services", "Service switches persist across restarts. Stopping the agent engine also stops its dependent services and active tool processes.")
            if let services = model.snapshot?.services {
                ForEach(services) { service in
                    HStack(spacing: 16) {
                        Image(systemName: service.id.contains("channel") ? "antenna.radiowaves.left.and.right" : "cpu").foregroundColor(accent).frame(width: 22)
                        VStack(alignment: .leading, spacing: 5) {
                            Text(service.name).fontWeight(.medium)
                            Text(service.description).font(.caption).foregroundStyle(.secondary)
                            if !service.error.isEmpty { Text(service.error).font(.caption).foregroundColor(.orange).textSelection(.enabled) }
                        }
                        Spacer(); Text("\(service.tasks) tasks").font(.caption).foregroundStyle(.secondary); status(service.status)
                        if !service.settings_tab.isEmpty { Button("Configure") { model.configure(service.settings_tab) }.disabled(model.snapshot == nil) }
                        Button("Restart") { model.serviceAction(service, "restart") }.disabled(!service.enabled)
                        Toggle("Enable \(service.name)", isOn: Binding(get: { service.enabled }, set: { model.serviceAction(service, $0 ? "start" : "stop") })).labelsHidden().toggleStyle(.switch).controlSize(.small)
                    }.padding(16).background(panel).clipShape(RoundedRectangle(cornerRadius: 8)).disabled(model.busy)
                }
            } else { empty("Start the runtime to configure its services.", "Process and login-service inventories remain available while the runtime is stopped.") }
        }
    }
    private var processes: some View {
        Group {
            title("Live process inventory", "Includes the native host, runtime, child tools, tagged detached tools and detected older instances. Stop the owning service to prevent a process from being recreated.")
            TextField("Filter by process, service or PID", text: $filter).textFieldStyle(.roundedBorder).frame(maxWidth: 400)
            ForEach(model.processes.filter { filter.isEmpty || "\($0.name) \($0.service) \($0.pid)".localizedCaseInsensitiveContains(filter) }) { process in
                HStack(spacing: 18) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(process.name).fontWeight(.medium)
                        Text("\(process.service)\(process.agent.map { " · " + $0 } ?? "") · parent \(process.parent_pid)").font(.caption).foregroundStyle(.secondary)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                    status(process.owned ? "managed" : "outside runtime")
                    Text("PID \(process.pid)").frame(width: 90, alignment: .trailing)
                    Text(String(format: "%.1f MB", process.memory_mb)).frame(width: 80, alignment: .trailing)
                    Button("Stop") { model.stopProcess(process) }.disabled(!process.can_stop || model.busy)
                }.font(.system(size: 12, design: .monospaced)).padding(14).background(panel).clipShape(RoundedRectangle(cornerRadius: 8))
            }
            if model.processes.isEmpty { empty("No processes detected yet.", "Inventory refreshes independently from the chat runtime.") }
            title("Existing launch services", "These can start older copies outside this app. Disable a legacy login service here before moving its data folder into the native host.")
            ForEach(model.loginServices) { service in
                HStack {
                    VStack(alignment: .leading, spacing: 5) { Text(service.label).fontWeight(.medium); Text(service.path).font(.caption).foregroundStyle(.secondary).textSelection(.enabled); if !service.data_home.isEmpty { Text(service.data_home).font(.caption).foregroundColor(accent) } }
                    Spacer(); status(service.loaded ? "loaded" : "unloaded")
                    if service.can_manage {
                        Button(service.loaded ? "Disable & stop" : "Enable") { model.loginAction(service, service.loaded ? "disable" : "enable") }
                        Button("Restart") { model.loginAction(service, "restart") }.disabled(!service.loaded)
                    } else { Text("System service").font(.caption).foregroundStyle(.secondary) }
                }.padding(16).background(panel).clipShape(RoundedRectangle(cornerRadius: 8)).disabled(model.busy)
            }
            if model.loginServices.isEmpty { Text("No legacy Mission Control launch services found.").foregroundStyle(.secondary) }
        }
    }
    private var harness: some View {
        Group {
            title("Agent harness", "Live state, queue depth and tool budgets. Skill inputs, permissions, deadlines and evidence are enforced by the runtime.")
            ForEach(model.snapshot?.harness.agents ?? []) { agent in
                VStack(alignment: .leading, spacing: 14) {
                    HStack { Text(agent.name).font(.title3).fontWeight(.medium); Text(agent.id).font(.system(.caption, design: .monospaced)).foregroundStyle(.secondary); Spacer(); status(agent.state) }
                    HStack(spacing: 30) { Label("\(agent.queued) queued", systemImage: "tray.full"); Label("\(agent.max_tool_rounds) rounds", systemImage: "arrow.triangle.branch"); Label("\(agent.max_tool_calls) tool calls", systemImage: "wrench.and.screwdriver") }.font(.callout).foregroundStyle(.secondary)
                    Text("Skills: " + (agent.skills.map { $0.isEmpty ? "None enabled" : $0.joined(separator: ", ") } ?? "All available")).font(.caption)
                    if let tools = agent.tools { Text("Tools: " + (tools.isEmpty ? "None enabled" : tools.joined(separator: ", "))).font(.caption).foregroundStyle(.secondary) }
                    if let run = agent.skill_run { Text("Active skill run · " + run).font(.system(.caption, design: .monospaced)).foregroundColor(accent) }
                }.padding(20).background(panel).clipShape(RoundedRectangle(cornerRadius: 10))
            }
            Button("Open skills & memory settings ↗") { model.configure("memory") }.disabled(model.snapshot == nil)
        }
    }
    private var permissions: some View {
        Group {
            title("Background operation", "The menu bar host owns the runtime. Closing the window can keep it working; Quit Mission Control stops its services and tools.")
            VStack(alignment: .leading, spacing: 18) {
                Toggle("Keep running in the menu bar when the window closes", isOn: $model.keepRunning)
                Toggle("Recover from a runtime crash (up to three attempts)", isOn: $model.restartOnFailure)
                HStack { VStack(alignment: .leading, spacing: 5) { Text("Launch at login"); Text(model.loginStatus).font(.caption).foregroundStyle(.secondary) }; Spacer(); Button("Login Item settings") { model.openLoginSettings() }; Toggle("Launch at login", isOn: Binding(get: { model.loginStatus == "Enabled" || model.loginStatus == "Awaiting macOS approval" }, set: model.setLogin)).labelsHidden() }
            }.toggleStyle(.switch).padding(20).background(panel).clipShape(RoundedRectangle(cornerRadius: 10))
            title("macOS access", "Request only the capabilities you use. macOS owns permission approval; changes appear here after approval in System Settings.")
            permissionRow("Microphone", "Voice conversations", model.microphone, "Request access", model.requestMicrophone)
            permissionRow("Accessibility", "Control applications on your behalf", model.accessibility ? "Allowed" : "Not allowed", "Request access", model.requestAccessibility)
            permissionRow("Screen recording", "Read the screen for computer tasks", model.screenCapture ? "Allowed" : "Not allowed", "Request access", model.requestScreen)
            permissionRow("Full Disk Access", "Read protected files and Messages when enabled", "Managed by macOS", "Open settings") { model.openPrivacy("Privacy_AllFiles") }
            permissionRow("Automation", "Apple Events for connected applications", "Requested per application", "Open settings") { model.openPrivacy("Privacy_Automation") }
            title("Runtime location", "Stop the runtime before changing its port or data folder. Selecting an existing folder preserves its settings, memory and conversation history.")
            HStack { Text(model.dataHome).font(.system(.caption, design: .monospaced)).textSelection(.enabled); Spacer(); Button("Reveal") { model.revealData() }; Button("Choose folder…") { model.chooseData() }.disabled(model.isRunning) }.padding(16).background(panel).clipShape(RoundedRectangle(cornerRadius: 8))
            HStack { Text("Local app port"); TextField("8899", text: $model.runtimePort).frame(width: 80).textFieldStyle(.roundedBorder).onSubmit { model.savePort() }.disabled(model.isRunning); Button("Save") { model.savePort() }.disabled(model.isRunning); Spacer(); Button("Open runtime log") { model.showLogs() } }.padding(16).background(panel).clipShape(RoundedRectangle(cornerRadius: 8))
        }
    }
    private func permissionRow(_ name: String, _ detail: String, _ state: String, _ action: String, _ handler: @escaping () -> Void) -> some View {
        HStack { VStack(alignment: .leading, spacing: 5) { Text(name).fontWeight(.medium); Text(detail).font(.caption).foregroundStyle(.secondary) }; Spacer(); Text(state).font(.caption).foregroundStyle(.secondary); Button(action, action: handler) }.padding(16).background(panel).clipShape(RoundedRectangle(cornerRadius: 8))
    }
    private var tasks: some View {
        Group {
            title("Background tasks", "Named service tasks and transport tasks from the live event loop. Stop their owning service or the runtime to stop them.")
            ForEach(model.snapshot?.tasks ?? []) { task in taskRow(task) }
            title("Recently finished", "Completed, cancelled and failed tasks from this runtime session.")
            ForEach(model.snapshot?.recent_tasks ?? []) { task in taskRow(task) }
        }
    }
    private func taskRow(_ task: MCTask) -> some View {
        HStack { VStack(alignment: .leading, spacing: 5) { Text(task.label).font(.system(.callout, design: .monospaced)); Text(task.service + (task.agent.map { " · " + $0 } ?? "")).font(.caption).foregroundStyle(.secondary) }; Spacer(); status(task.status) }.padding(14).background(panel).clipShape(RoundedRectangle(cornerRadius: 8))
    }
    private func empty(_ title: String, _ detail: String) -> some View {
        VStack(alignment: .leading, spacing: 8) { Text(title).font(.title3); Text(detail).foregroundStyle(.secondary) }.frame(maxWidth: .infinity, alignment: .leading).padding(30).background(panel).clipShape(RoundedRectangle(cornerRadius: 10))
    }
}
