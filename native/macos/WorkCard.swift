import SwiftUI

/// One step of a task: a tool call, in the words the agent gave it.
struct WorkStep: Identifiable, Equatable, Sendable {
    let id: String
    var label: String, status: String, tool: String, agent: String
    var ts: Double, seconds: Double

    init(_ row: JSON) {
        id = row["id"].str; label = row.label.str; status = row.status.str; tool = row.tool.optStr ?? row.name.str
        agent = row.agent.str; ts = row.ts.num; seconds = row.seconds.optNum ?? max(0, row.updated.num - row.ts.num)
    }
}

/// A task a conversation started, as `/api/work` describes it.
struct WorkTask: Identifiable, Equatable, Sendable {
    let id: String
    var thread: String, session: String, title: String, agent: String, agentName: String
    var others: [String]
    var phase: String, outcome: String
    var waitingKind: String, waitingText: String, approval: String?
    var started: Double, updated: Double
    var steps: [WorkStep]
    var checksDone: Int, checksTotal: Int
    var summary: String
    var live: Bool

    init(_ row: JSON) {
        id = row["id"].str; thread = row.thread.str; session = row.session.str; title = row.title.str
        agent = row.agent.str; agentName = row.agent_name.str
        others = row.others.list.map(\.name.str)
        phase = row.phase.str; outcome = row.outcome.str
        waitingKind = row.waiting_on.kind.str; waitingText = row.waiting_on.text.str; approval = row.waiting_on.approval.optStr
        started = row.started.num; updated = row.updated.num
        steps = row.steps.list.map(WorkStep.init)
        let checks = row.checks.list
        checksTotal = checks.count
        checksDone = checks.filter { $0.done.flag && !$0.how.str.isEmpty }.count
        summary = row.summary.str
        live = row.live.flag
    }

    /// What it is doing this moment, as a person would say it.
    var phaseLine: String {
        switch phase {
        case "thinking": return "Thinking"
        case "working": return steps.last.map { $0.status == "running" ? $0.label : "Working" } ?? "Working"
        case "fixing": return steps.last.map { "Fixing · \($0.label)" } ?? "Fixing"
        case "queued": return "Waiting for a free place"
        case "needs_you": return "Needs you"
        case "waiting":
            switch waitingKind {
            case "job": return "Running · \(waitingText)"
            case "watch": return "Watching · \(waitingText)"
            case "thread": return "Waiting on \(waitingText)"
            case "provider": return waitingText
            default: return "Waiting"
            }
        default:
            switch outcome {
            case "failed": return "Could not finish"
            case "stopped": return "Stopped"
            default: return "Finished"
            }
        }
    }
}

/// One task's card reads its own model, so a step arriving redraws that card and nothing else.
@Observable @MainActor
final class WorkModel: Identifiable {
    let id: String
    var task: WorkTask
    init(_ task: WorkTask) { id = task.id; self.task = task }
}

/// Where a task sits in a conversation: enough to place it, and nothing that changes as it runs.
struct WorkRef: Identifiable, Equatable, Sendable {
    let id: String, thread: String
    let started: Double
}

// MARK: The card

/// Work as it is being done: who has it, what they are on, and each step as it lands.
/// Finished, it folds to a line that opens again.
struct WorkCard: View {
    @Environment(Store.self) private var store
    @Environment(\.accessibilityReduceMotion) private var still
    let id: String
    /// How far in from the edge: under a message in a chat, flush on a page of its own.
    var indent: CGFloat = 34
    var width: CGFloat = 620
    @State private var opened: Bool?
    @State private var allSteps = false
    @State private var hovering = false
    @State private var steer = ""
    @State private var steered: String?
    @State private var sending = false

    private static let shown = 4

    var body: some View {
        if let model = store.workModels[id] {
            let task = model.task
            let open = opened ?? task.live
            VStack(alignment: .leading, spacing: 0) {
                head(task, open: open)
                if open {
                    VStack(alignment: .leading, spacing: 0) {
                        steps(task)
                        foot(task)
                    }
                    .transition(.opacity.combined(with: .move(edge: .top)))
                }
            }
            .padding(.horizontal, 14).padding(.vertical, 11)
            .frame(maxWidth: width, alignment: .leading)
            .background(Color.primary.opacity(task.live ? 0.045 : 0.03), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .overlay { LiveEdge(active: task.live && !still, tint: tint(task), needsYou: task.phase == "needs_you") }
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            .onHover { hovering = $0 }
            .animation(.spring(response: 0.38, dampingFraction: 0.86), value: open)
            .animation(.spring(response: 0.38, dampingFraction: 0.86), value: task.steps.count)
            .animation(.easeInOut(duration: 0.25), value: task.phase)
            .padding(.leading, indent).padding(.top, 12)
            .accessibilityElement(children: .contain)
            .accessibilityLabel("\(task.agentName), \(task.title), \(task.phaseLine)")
        }
    }

    private func tint(_ task: WorkTask) -> Color {
        switch task.phase {
        case "needs_you": return MC.warn
        case "fixing": return MC.warn
        case "done": return task.outcome == "finished" ? MC.good : (task.outcome == "failed" ? MC.danger : .secondary)
        default: return .accentColor
        }
    }

    private func head(_ task: WorkTask, open: Bool) -> some View {
        HStack(alignment: .center, spacing: 10) {
            // While it runs, the one doing it; once over, how it ended.
            if task.live { AgentBadge(agent: store.agent(task.agent), size: 30) }
            else { PhaseMark(phase: task.phase, outcome: task.outcome, tint: tint(task)) }
            VStack(alignment: .leading, spacing: 2) {
                Text(verbatim: task.title).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                HStack(spacing: 5) {
                    Text(verbatim: task.agentName).foregroundStyle(.secondary)
                    Text(verbatim: "·").foregroundStyle(.tertiary)
                    if task.live {
                        Shimmer(text: task.phaseLine, active: !still && task.phase != "needs_you", tint: tint(task))
                    } else {
                        Text(verbatim: doneLine(task)).foregroundStyle(.secondary)
                    }
                }
                .font(.system(size: 11.5))
                .lineLimit(1)
            }
            Spacer(minLength: 8)
            if task.live {
                if hovering {
                    Button { Task { await store.stopWorker(task.session) } } label: {
                        Image(systemName: "stop.circle").font(.system(size: 14))
                    }
                    .buttonStyle(.plain).foregroundStyle(.secondary).help("Stop this task")
                    .accessibilityLabel("Stop this task")
                    .transition(.opacity)
                }
                Elapsed(since: task.started)
            }
            Button { opened = !open } label: {
                Image(systemName: "chevron.down")
                    .font(.system(size: 9, weight: .bold))
                    .rotationEffect(.degrees(open ? 0 : -90))
                    .foregroundStyle(.tertiary)
                    .frame(width: 18, height: 18)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(open ? "Hide steps" : "Show steps")
        }
        .contentShape(Rectangle())
        .onTapGesture { opened = !open }
    }

    private func doneLine(_ task: WorkTask) -> String {
        let took = max(0, task.updated - task.started)
        let time = took < 90 ? "\(Int(took.rounded()))s" : "\(Int((took / 60).rounded()))m"
        return "\(task.phaseLine) · \(plural(task.steps.count, "step")) · \(time)"
    }

    @ViewBuilder private func steps(_ task: WorkTask) -> some View {
        let hidden = allSteps ? 0 : max(0, task.steps.count - Self.shown)
        let visible = Array(task.steps.dropFirst(hidden))
        VStack(alignment: .leading, spacing: 0) {
            if hidden > 0 {
                Button { allSteps = true } label: {
                    HStack(spacing: 9) {
                        Image(systemName: "ellipsis").font(.system(size: 9, weight: .bold)).frame(width: 14)
                        Text(verbatim: "\(plural(hidden, "earlier step"))").font(.system(size: 11.5))
                    }
                    .foregroundStyle(.tertiary)
                    .padding(.vertical, 3)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
            ForEach(visible) { step in
                StepRow(step: step, last: step.id == visible.last?.id, still: still)
                    .transition(.asymmetric(insertion: .opacity.combined(with: .offset(y: 8)), removal: .opacity))
            }
            if task.phase == "thinking" {
                ThinkingRow(still: still).transition(.opacity)
            }
        }
        .padding(.top, visible.isEmpty && task.phase != "thinking" ? 0 : 10)
    }

    /// A word to whoever has the task, while it runs. It reads it at its next step.
    @ViewBuilder private func steering(_ task: WorkTask) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Rectangle().fill(MC.line.opacity(0.5)).frame(height: 0.5)
            HStack(spacing: 8) {
                Image(systemName: "arrow.turn.down.right").font(.system(size: 10, weight: .semibold)).foregroundStyle(.tertiary)
                TextField("", text: $steer, prompt: Text(task.phase == "needs_you" ? "Answer \(task.agentName)" : "Tell \(task.agentName) to change course").foregroundColor(.secondary.opacity(0.7)))
                    .textFieldStyle(.plain)
                    .font(.system(size: 12.5))
                    .onSubmit { send(task) }
                    .disabled(sending)
                    .accessibilityLabel("Message for \(task.agentName) about this task")
                if sending { Spinner(size: 10) }
                else if !steer.trimmingCharacters(in: .whitespaces).isEmpty {
                    Button { send(task) } label: { Image(systemName: "arrow.up.circle.fill").font(.system(size: 16)) }
                        .buttonStyle(.plain).foregroundStyle(Color.accentColor).accessibilityLabel("Send")
                        .transition(.scale(scale: 0.6).combined(with: .opacity))
                }
            }
            if let steered {
                Text(verbatim: "Sent. \(task.agentName) reads it at its next step: “\(clip(steered, 70))”")
                    .font(.system(size: 11)).foregroundStyle(.tertiary)
                    .transition(.opacity)
            }
        }
        .padding(.top, 10)
        .animation(.easeInOut(duration: 0.2), value: steered)
        .animation(.spring(response: 0.25, dampingFraction: 0.7), value: steer.isEmpty)
    }

    private func send(_ task: WorkTask) {
        let text = steer.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !sending else { return }
        sending = true
        Task {
            let ok = await store.steer(task.session, text)
            sending = false
            if ok { steered = text; steer = "" }
        }
    }

    @ViewBuilder private func foot(_ task: WorkTask) -> some View {
        if task.phase == "needs_you" {
            VStack(alignment: .leading, spacing: 8) {
                Text(verbatim: task.waitingText)
                    .font(.system(size: 12.5))
                    .fixedSize(horizontal: false, vertical: true)
                if task.approval != nil {
                    Button("Review in Inbox") { store.go(.inbox) }.controlSize(.small)
                }
            }
            .padding(.top, 10)
        }
        if task.checksTotal > 0 {
            HStack(spacing: 6) {
                ForEach(0..<task.checksTotal, id: \.self) { index in
                    Capsule()
                        .fill(index < task.checksDone ? MC.good : Color.primary.opacity(0.14))
                        .frame(width: 18, height: 3)
                }
                Text(verbatim: "\(task.checksDone) of \(task.checksTotal) confirmed")
                    .font(.system(size: 11)).foregroundStyle(.tertiary)
            }
            .padding(.top, 10)
            .animation(.easeInOut(duration: 0.3), value: task.checksDone)
        }
        if task.live { steering(task) }
    }
}

// MARK: Pieces

/// A step: its mark, its words, and how long it took once done.
private struct StepRow: View {
    let step: WorkStep
    let last: Bool
    let still: Bool

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 9) {
            mark.frame(width: 14, height: 14).alignmentGuide(.firstTextBaseline) { $0[.bottom] - 2 }
            Text(verbatim: step.label)
                .font(.system(size: 12.5, design: step.tool == "terminal" && looksLikeCommand ? .monospaced : .default))
                .foregroundStyle(step.status == "running" ? .primary : .secondary)
                .lineLimit(1)
                .truncationMode(.tail)
            Spacer(minLength: 6)
            if step.status != "running", step.seconds >= 1 {
                Text(verbatim: step.seconds < 90 ? "\(Int(step.seconds.rounded()))s" : "\(Int((step.seconds / 60).rounded()))m")
                    .font(.system(size: 11)).monospacedDigit().foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 3.5)
        .help(step.label)
    }

    /// A note reads as words. A bare command reads as a command.
    private var looksLikeCommand: Bool {
        step.label.contains("/") || step.label.contains(" -") || step.label.contains("=")
    }

    @ViewBuilder private var mark: some View {
        switch step.status {
        case "running":
            if still { Circle().strokeBorder(Color.accentColor, lineWidth: 1.5).frame(width: 11, height: 11) } else { Spinner() }
        case "completed":
            DrawnCheck().foregroundStyle(MC.good)
        case "failed":
            Image(systemName: "xmark").font(.system(size: 8.5, weight: .bold)).foregroundStyle(MC.danger)
        default:
            Circle().fill(Color.primary.opacity(0.25)).frame(width: 5, height: 5)
        }
    }
}

/// A tick that draws itself once, when the step lands.
private struct DrawnCheck: View {
    @State private var drawn: CGFloat = 0
    var body: some View {
        CheckShape()
            .trim(from: 0, to: drawn)
            .stroke(style: StrokeStyle(lineWidth: 1.8, lineCap: .round, lineJoin: .round))
            .frame(width: 10, height: 8)
            .onAppear { withAnimation(.easeOut(duration: 0.28)) { drawn = 1 } }
    }
}

private struct CheckShape: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: rect.minX, y: rect.midY + rect.height * 0.05))
        path.addLine(to: CGPoint(x: rect.minX + rect.width * 0.36, y: rect.maxY))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.minY))
        return path
    }
}

/// A small turning arc. Driven by the clock, so it costs nothing once it leaves the screen.
struct Spinner: View {
    var size: CGFloat = 11
    var tint: Color = .accentColor
    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0)) { context in
            let turn = context.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 0.9) / 0.9
            Circle()
                .trim(from: 0.08, to: 0.72)
                .stroke(tint, style: StrokeStyle(lineWidth: 1.6, lineCap: .round))
                .rotationEffect(.degrees(turn * 360))
                .frame(width: size, height: size)
        }
        .accessibilityHidden(true)
    }
}

/// The mark at the head of a card: turning while it works, a slow pulse while it waits,
/// and the result once there is one.
private struct PhaseMark: View {
    @Environment(\.accessibilityReduceMotion) private var still
    let phase: String, outcome: String
    let tint: Color

    var body: some View {
        ZStack {
            Circle().fill(tint.opacity(0.14))
            switch phase {
            case "done":
                Image(systemName: outcome == "finished" ? "checkmark" : (outcome == "failed" ? "exclamationmark" : "stop.fill"))
                    .font(.system(size: 10, weight: .bold)).foregroundStyle(tint)
                    .transition(.scale(scale: 0.4).combined(with: .opacity))
            case "needs_you":
                Image(systemName: "hand.raised.fill").font(.system(size: 10)).foregroundStyle(tint)
            case "waiting", "queued":
                if still { Image(systemName: "clock").font(.system(size: 10, weight: .semibold)).foregroundStyle(tint) } else { Breathing(tint: tint) }
            default:
                if still { Circle().fill(tint).frame(width: 7, height: 7) } else { Spinner(size: 13, tint: tint) }
            }
        }
        .frame(width: 26, height: 26)
        .animation(.spring(response: 0.35, dampingFraction: 0.7), value: phase)
        .accessibilityHidden(true)
    }
}

private struct Breathing: View {
    let tint: Color
    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 20.0)) { context in
            let wave = (sin(context.date.timeIntervalSinceReferenceDate * 2.2) + 1) / 2
            Circle().fill(tint.opacity(0.45 + 0.55 * wave)).frame(width: 6 + 3 * wave, height: 6 + 3 * wave)
        }
    }
}

/// Words with a band of light passing through them, for something still under way.
struct Shimmer: View {
    let text: String
    var active = true
    var tint: Color = .accentColor

    var body: some View {
        if active {
            TimelineView(.animation(minimumInterval: 1.0 / 30.0)) { context in
                let at = context.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 1.9) / 1.9
                Text(verbatim: text)
                    .foregroundStyle(.secondary)
                    .overlay {
                        GeometryReader { area in
                            LinearGradient(colors: [.clear, tint.opacity(0.95), .clear], startPoint: .leading, endPoint: .trailing)
                                .frame(width: max(40, area.size.width * 0.45))
                                .offset(x: -area.size.width * 0.45 + (area.size.width * 1.45) * at)
                        }
                        .mask(Text(verbatim: text))
                        .allowsHitTesting(false)
                    }
            }
        } else {
            Text(verbatim: text).foregroundStyle(tint == .accentColor ? Color.secondary : tint)
        }
    }
}

/// Three dots that rise in turn: the model is reading or deciding, and has not said yet.
struct ThinkingDots: View {
    var tint: Color = .secondary
    var size: CGFloat = 5
    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0)) { context in
            let time = context.date.timeIntervalSinceReferenceDate
            HStack(spacing: size * 0.6) {
                ForEach(0..<3, id: \.self) { index in
                    let wave = max(0, sin(time * 5.2 - Double(index) * 0.75))
                    Circle().fill(tint.opacity(0.35 + 0.65 * wave)).frame(width: size, height: size).offset(y: -size * 0.5 * wave)
                }
            }
        }
        .frame(height: 12)
        .accessibilityHidden(true)
    }
}

private struct ThinkingRow: View {
    let still: Bool
    var body: some View {
        HStack(spacing: 9) {
            Group { if still { Circle().fill(Color.secondary).frame(width: 5, height: 5) } else { ThinkingDots(size: 3.4) } }
                .frame(width: 14, alignment: .center)
            Text("Thinking").font(.system(size: 12.5)).foregroundStyle(.tertiary)
        }
        .padding(.vertical, 3.5)
    }
}

/// How long it has been at it, counted by the second.
private struct Elapsed: View {
    let since: Double
    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            let seconds = max(0, Int(context.date.timeIntervalSince1970 - since))
            Text(verbatim: seconds < 60 ? "\(seconds)s" : String(format: "%d:%02d", seconds / 60, seconds % 60))
                .font(.system(size: 11)).monospacedDigit().foregroundStyle(.tertiary)
                .contentTransition(.numericText())
        }
    }
}

/// The card's outline. While work is under way a soft light travels round it, and stops
/// drawing the moment it is not.
private struct LiveEdge: View {
    let active: Bool
    let tint: Color
    let needsYou: Bool

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: 14, style: .continuous)
        if active && !needsYou {
            TimelineView(.animation(minimumInterval: 1.0 / 30.0)) { context in
                let turn = context.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 3.2) / 3.2
                shape.strokeBorder(
                    AngularGradient(stops: [.init(color: tint.opacity(0.0), location: 0), .init(color: tint.opacity(0.55), location: 0.12),
                                            .init(color: tint.opacity(0.0), location: 0.3), .init(color: tint.opacity(0.0), location: 1)],
                                    center: .center, angle: .degrees(turn * 360)),
                    lineWidth: 1.2)
                    .background(shape.strokeBorder(MC.line.opacity(0.5), lineWidth: 0.5))
            }
            .allowsHitTesting(false)
        } else {
            shape.strokeBorder(needsYou ? tint.opacity(0.55) : MC.line.opacity(0.5), lineWidth: needsYou ? 1 : 0.5)
                .allowsHitTesting(false)
        }
    }
}
