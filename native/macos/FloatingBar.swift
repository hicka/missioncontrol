import AppKit
import Carbon.HIToolbox
import Observation
import simd
import SwiftUI

// MARK: Global shortcut

/// A system-wide shortcut, with its release as well as its press, so holding it can mean
/// something. Carbon's hot keys are the one route that needs no Accessibility permission,
/// so the bar answers from the first launch.
final class HotKey {
    private static var actions: [UInt32: (Bool) -> Void] = [:]
    private static var installed = false
    private var reference: EventHotKeyRef?
    private let number: UInt32

    init?(keyCode: Int, modifiers: Int, number: UInt32 = 1, action: @escaping (_ pressed: Bool) -> Void) {
        self.number = number
        if !Self.installed {
            var specs = [EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)),
                         EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased))]
            InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
                var pressed = EventHotKeyID()
                GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID), nil,
                                  MemoryLayout<EventHotKeyID>.size, nil, &pressed)
                HotKey.actions[pressed.id]?(GetEventKind(event) == UInt32(kEventHotKeyPressed))
                return noErr
            }, 2, &specs, nil, nil)
            Self.installed = true
        }
        let identifier = EventHotKeyID(signature: OSType(0x4D43_4B59), id: number)
        guard RegisterEventHotKey(UInt32(keyCode), UInt32(modifiers), identifier, GetApplicationEventTarget(), 0, &reference) == noErr else { return nil }
        Self.actions[number] = action
    }

    deinit {
        if let reference { UnregisterEventHotKey(reference) }
        Self.actions[number] = nil
    }
}

// MARK: What the bar says

enum Pulse: Equatable { case offline, idle, thinking, streaming, working, attention, listening, speaking }

struct BarStatus: Equatable {
    var pulse: Pulse
    var title: String
    var detail = ""
    /// The name of the tool running, for its icon and how long it has been going.
    var tool: String?
    var since: Double?
}

extension Store {
    /// One line for what is happening now. What needs the owner comes before what is
    /// merely going on, and "Ask anything" never shows over open work.
    var barStatus: BarStatus {
        guard link else { return BarStatus(pulse: .offline, title: api == nil ? "Starting…" : "Reconnecting…") }
        if let lead, let thread = leadThread, let live = streams["\(lead.id)|\(thread)"], !live.text.isEmpty {
            return BarStatus(pulse: .streaming, title: "", detail: live.text)
        }
        if let first = inbox.first {
            return BarStatus(pulse: .attention, title: inbox.count == 1 ? "1 thing needs you" : "\(inbox.count) things need you",
                             detail: clip(first.title.str, 52))
        }
        if let lead, isBusy(lead.id) {
            if let tool = tools[lead.id] {
                let since = executions.first { $0.agent.str == lead.id && $0.status.str == "running" && $0.kind.str == "tool" }?.ts.optNum
                return BarStatus(pulse: .working, title: tool.replacingOccurrences(of: "_", with: " ").capitalized, tool: tool, since: since)
            }
            return BarStatus(pulse: .thinking, title: "Thinking…")
        }
        let working = workers.filter { $0.status.str == "working" }
        if let first = working.first {
            return BarStatus(pulse: .working, title: working.count == 1 ? clip(first.title.str, 44) : "\(working.count) tasks running",
                             detail: working.count == 1 ? name(first.profile.str) : clip(first.title.str, 44))
        }
        return BarStatus(pulse: .idle, title: "Ask anything")
    }
}

/// The end of a reply still being written, in one line, with no markdown showing through.
private func plainTail(_ text: String) -> String {
    let line = oneLine(text.split(separator: "\n").map { $0.hasPrefix("- ") || $0.hasPrefix("* ") ? String($0.dropFirst(2)) : String($0) }.filter { !$0.hasPrefix("```") }.joined(separator: " · "))
    return String(line.replacingOccurrences(of: "**", with: "").replacingOccurrences(of: "`", with: "").suffix(70))
}

/// A reply's opening lines, kept as lines, for reading at a glance. Code stays in the chat.
private func peekSummary(_ text: String) -> String {
    var lines: [String] = [], fenced = false
    for line in text.components(separatedBy: "\n") {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        if trimmed.hasPrefix("```") { fenced.toggle(); continue }
        if fenced || trimmed.isEmpty { continue }
        lines.append(trimmed.hasPrefix("- ") || trimmed.hasPrefix("* ") ? "• " + trimmed.dropFirst(2) : trimmed)
    }
    return String(lines.joined(separator: "\n").prefix(420))
}

private func toolIcon(_ tool: String) -> String {
    let name = tool.lowercased()
    if name.contains("browser") || name.contains("web") || name.contains("fetch") { return "globe" }
    if name.contains("shell") || name.contains("terminal") || name.contains("bash") || name.contains("run") || name.contains("code") { return "terminal" }
    if name.contains("read") || name.contains("write") || name.contains("edit") || name.contains("file") { return "doc.text" }
    if name.contains("search") || name.contains("recall") || name.contains("grep") { return "magnifyingglass" }
    if name.contains("dispatch") || name.contains("attach") { return "arrow.triangle.branch" }
    if name.contains("mail") { return "envelope" }
    return "gearshape.2"
}

// MARK: State

@Observable @MainActor
final class BarModel {
    enum Mode { case rest, open }
    var mode: Mode = .rest
    var draft = ""
    /// A reply that arrived while the bar was at rest. The engine sends one reply as several
    /// messages, a paragraph each, so this collects them.
    var peek: Peek?
    struct Peek: Equatable { let sender: String; var text: String }
    /// The held item being read in full before anyone can run it.
    var reviewing: JSON?
    var hovering = false
    /// The pointer is close, so an idle bar opens up to meet it.
    var near = false
    var focusField = false
    /// Content fades on its own timing, so the shape moves first and the words follow.
    var contentShown = true
    var solidFallback = false
    let voice = VoiceInput()
    let speaker = Speaker()
    /// The last thing was said out loud, so the reply is spoken too.
    @ObservationIgnored var answerAloud = false
    @ObservationIgnored var onCollapse: (() -> Void)?
    @ObservationIgnored var onExpand: (() -> Void)?
    @ObservationIgnored var onShapes: ((AuraView.Shapes) -> Void)?
    @ObservationIgnored var onPress: ((Bool) -> Void)?
    @ObservationIgnored var onDragChanged: (() -> Void)?
    @ObservationIgnored var onDragEnded: (() -> Void)?

    func expand() { onExpand?() }
    func collapse() { onCollapse?() }
}

// MARK: The window

/// Takes the keyboard without making the app active, as Spotlight does: whatever the
/// owner was working in stays the front app, and comes straight back on Escape.
final class BarPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

/// The first click acts, even with another app in front.
private final class BarHosting<Content: View>: NSHostingView<Content> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

/// Room around the bar for its light to spill into. Clicks here pass straight through to
/// whatever is underneath, because the window only takes the mouse over the shape itself.
private let barMargin: CGFloat = 52
private let holdToTalk: UInt64 = 280_000_000

@MainActor
final class BarController: NSObject, NSWindowDelegate {
    let bar = BarModel()
    private let store: Store
    private let panel: BarPanel
    private let aura: AuraView
    private let content: NSView
    private var hotKey: HotKey?
    private var keyMonitor: Any?
    private var shrink: Task<Void, Never>?
    private var peekTimer: Task<Void, Never>?
    private var holdTimer: Task<Void, Never>?
    private var heldToTalk = false
    private var openedAt = Date.distantPast
    private var lastShapes = AuraView.Shapes()
    /// Bottom centre of the bar, on screen. It grows upward from here.
    private var anchor: NSPoint
    private var dragStart: (mouse: NSPoint, anchor: NSPoint)?
    private var snapX: Spring?
    private var lastAnchor: NSPoint
    private var sinceSync: Float = 1
    private var lastPulse: Pulse = .offline
    private var lastWorking: Set<String> = []
    private var lastInbox = 0
    private var appliedLean = SIMD2<Float>(0, 0)

    var isVisible: Bool { panel.isVisible }

    init(store: Store) {
        self.store = store
        panel = BarPanel(contentRect: NSRect(x: 0, y: 0, width: 420, height: 200),
                         styleMask: [.borderless, .nonactivatingPanel, .fullSizeContentView], backing: .buffered, defer: false)
        aura = AuraView(frame: NSRect(x: 0, y: 0, width: 420, height: 200))
        let saved = UserDefaults.standard
        if saved.object(forKey: "barAnchorX") != nil {
            anchor = NSPoint(x: saved.double(forKey: "barAnchorX"), y: saved.double(forKey: "barAnchorY"))
        } else {
            let screen = (NSScreen.main ?? NSScreen.screens[0]).visibleFrame
            anchor = NSPoint(x: screen.midX, y: screen.minY + 14)
        }
        lastAnchor = anchor
        let hosting = BarHosting(rootView: BarRoot(bar: bar).environment(store))
        hosting.sizingOptions = []
        hosting.wantsLayer = true
        content = hosting
        super.init()
        bar.solidFallback = !aura.available
        aura.onUnavailable = { [weak self] in self?.bar.solidFallback = true }
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = false
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = false
        panel.animationBehavior = .none
        panel.isReleasedWhenClosed = false
        panel.ignoresMouseEvents = true
        panel.delegate = self
        let container = NSView(frame: NSRect(x: 0, y: 0, width: 420, height: 200))
        for view in [aura, hosting] as [NSView] {
            view.frame = container.bounds
            view.autoresizingMask = [.width, .height]
            container.addSubview(view)
        }
        panel.contentView = container
        aura.onTick = { [weak self] dt in self?.tick(dt) }
        bar.onShapes = { [weak self] shapes in self?.shapesChanged(shapes) }
        bar.onExpand = { [weak self] in self?.open() }
        bar.onCollapse = { [weak self] in self?.close() }
        bar.onPress = { [weak self] pressed in self?.aura.setPressed(pressed) }
        bar.onDragChanged = { [weak self] in self?.dragChanged() }
        bar.onDragEnded = { [weak self] in self?.dragEnded() }
        if !CommandLine.arguments.contains("--quiet-launch") {
            hotKey = HotKey(keyCode: kVK_Space, modifiers: optionKey) { [weak self] pressed in
                MainActor.assumeIsolated { self?.hotKeyChanged(pressed) }
            }
        }
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self, event.keyCode == UInt16(kVK_Escape), self.panel.isKeyWindow else { return event }
            self.escape()
            return nil
        }
        store.onMessage = { [weak self] message in self?.received(message) }
        store.onDelta = { [weak self] in self?.aura.token() }
        bar.voice.onFinish = { [weak self] text in self?.heard(text) }
        NotificationCenter.default.addObserver(forName: NSWindow.didChangeOcclusionStateNotification, object: panel, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.occlusionChanged() }
        }
    }

    private func occlusionChanged() { aura.setPaused(!panel.occlusionState.contains(.visible)) }

    func show() {
        UserDefaults.standard.set(false, forKey: "barHidden")
        place(panel.frame.size)
        panel.orderFrontRegardless()
        aura.setPaused(false)
    }

    func hide() {
        UserDefaults.standard.set(true, forKey: "barHidden")
        close()
        panel.orderOut(nil)
        aura.setPaused(true)
    }

    func toggleVisible() { if panel.isVisible { hide() } else { show() } }

    // MARK: The shortcut: a tap opens the bar, holding it talks

    private func hotKeyChanged(_ pressed: Bool) {
        if pressed {
            holdTimer = Task {
                try? await Task.sleep(nanoseconds: holdToTalk)
                guard !Task.isCancelled else { return }
                heldToTalk = true
                haptic()
                if !panel.isVisible { show() }
                bar.voice.start()
            }
        } else {
            holdTimer?.cancel()
            if heldToTalk { heldToTalk = false; bar.voice.finish() } else { toggleOpen() }
        }
    }

    func toggleOpen() { if bar.mode == .open { close() } else { open() } }

    func open() {
        guard bar.mode != .open else { return }
        openedAt = Date()
        bar.peek = nil
        peekTimer?.cancel()
        bar.speaker.stop()
        bar.contentShown = false
        bar.mode = .open
        aura.keepLively = true
        aura.ripple(SIMD3(0.55, 0.6, 1), strength: 0.8, speed: 420, life: 0.8)
        haptic()
        panel.makeKeyAndOrderFront(nil)
        bar.focusField = true
        withAnimation(.easeOut(duration: 0.24).delay(0.09)) { bar.contentShown = true }
        if let id = store.leadThread, let api = store.api {
            let model = store.thread(id)
            if !model.loaded { Task { await model.load(api) } }
        }
    }

    func close() {
        guard bar.mode == .open else { return }
        bar.reviewing = nil
        bar.focusField = false
        if bar.voice.listening { bar.voice.cancel() }
        withAnimation(.easeOut(duration: 0.09)) { bar.contentShown = false }
        aura.keepLively = false
        Task {
            // The words leave first, then the shape closes around nothing.
            try? await Task.sleep(nanoseconds: 80_000_000)
            bar.mode = .rest
            withAnimation(.easeOut(duration: 0.22).delay(0.14)) { bar.contentShown = true }
            // A key panel that only resigns keeps the keyboard; ordering it out and back
            // returns it to the app the owner was in.
            panel.orderOut(nil)
            panel.orderFrontRegardless()
        }
    }

    private func escape() {
        if bar.reviewing != nil { bar.reviewing = nil } else if bar.voice.listening { bar.voice.cancel() } else { close() }
    }

    private func haptic() { NSHapticFeedbackManager.defaultPerformer.perform(.alignment, performanceTime: .now) }

    private func heard(_ text: String) {
        let spoken = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !spoken.isEmpty, let thread = store.leadThread else { return }
        bar.draft = ""
        bar.answerAloud = true
        aura.ripple(SIMD3(0.7, 0.55, 1))
        Task { await store.send(spoken, to: thread, voice: true) }
    }

    private func received(_ message: Message) {
        guard message.thread == store.leadThread, message.sender != "user", message.kind == "chat" else { return }
        if bar.answerAloud, let api = store.api { bar.speaker.speak(message.text, api: api) }
        guard bar.mode == .rest, panel.isVisible else { return }
        if let current = bar.peek, current.sender == message.sender { bar.peek?.text = current.text + "\n\n" + message.text }
        else {
            bar.contentShown = true
            bar.peek = BarModel.Peek(sender: message.sender, text: message.text)
            aura.ripple(SIMD3(0.6, 0.65, 1), strength: 0.7, speed: 300)
            haptic()
        }
        peekTimer?.cancel()
        peekTimer = Task {
            // Long enough to read; held for as long as the pointer stays on it.
            try? await Task.sleep(nanoseconds: 10_000_000_000)
            while bar.hovering, !Task.isCancelled { try? await Task.sleep(nanoseconds: 500_000_000) }
            if !Task.isCancelled { bar.peek = nil }
        }
    }

    // MARK: Every frame

    private func tick(_ dt: Float) {
        // The pointer, read rather than monitored: no event tap, and nothing to ask permission for.
        let local = pointInView(NSEvent.mouseLocation)
        aura.pointerMoved(to: local)
        let inside = aura.contains(local)
        if panel.ignoresMouseEvents == inside { panel.ignoresMouseEvents = !inside }
        if bar.near ? aura.pointerDistance > 96 : aura.pointerDistance < 58 { bar.near.toggle() }
        if bar.hovering != inside { bar.hovering = inside }

        // The text rides on the same lean as the shape, without SwiftUI having to know.
        if aura.lean != appliedLean {
            appliedLean = aura.lean
            CATransaction.begin()
            CATransaction.setDisableActions(true)
            content.layer?.transform = CATransform3DMakeTranslation(CGFloat(aura.lean.x), CGFloat(-aura.lean.y), 0)
            CATransaction.commit()
        }

        if var spring = snapX {
            spring.step(dt)
            anchor.x = CGFloat(spring.value)
            place(panel.frame.size)
            snapX = spring.moving ? spring : nil
            if snapX == nil { saveAnchor() }
        }
        let speed = 1 / max(dt, 0.001)
        aura.stretch(vx: Float(anchor.x - lastAnchor.x) * speed, vy: Float(anchor.y - lastAnchor.y) * speed)
        lastAnchor = anchor

        aura.level = Float(bar.voice.listening ? bar.voice.level : bar.speaker.level)
        sinceSync += dt
        if sinceSync >= 0.1 { sinceSync = 0; syncFromStore() }
    }

    /// Ten times a second is plenty for what the agent is doing; the words themselves
    /// arrive through store.onDelta the moment they do.
    private func syncFromStore() {
        var status = store.barStatus
        if bar.voice.listening { status.pulse = .listening } else if bar.speaker.speaking { status.pulse = .speaking }
        aura.pulseState = status.pulse
        if status.pulse != lastPulse {
            if status.pulse == .attention { aura.nudge(); aura.ripple(SIMD3(1, 0.7, 0.25)); haptic() }
            if status.pulse == .listening { aura.ripple(SIMD3(0.5, 0.6, 1), strength: 0.8) }
            if status.pulse == .thinking && lastPulse == .idle { aura.ripple(SIMD3(0.5, 0.5, 1), strength: 0.7, speed: 320) }
            if [.thinking, .streaming, .working].contains(lastPulse), status.pulse == .idle { aura.ripple(SIMD3(0.3, 0.95, 0.55), strength: 0.9, speed: 280) }
            lastPulse = status.pulse
        }
        if store.inbox.count > lastInbox, lastPulse == .attention, lastInbox > 0 { aura.nudge() }
        lastInbox = store.inbox.count
        let working = store.workers.filter { $0.status.str == "working" }
        let ids = Set(working.map(\.id))
        if ids != lastWorking {
            lastWorking = ids
            aura.setSatellites(working.map { (id: $0.id, tint: tintVector(hex: store.agent($0.profile.str)?.color ?? "")) })
        }
    }

    private func pointInView(_ screen: NSPoint) -> CGPoint {
        let frame = panel.frame
        return CGPoint(x: screen.x - frame.minX, y: frame.height - (screen.y - frame.minY))
    }

    // MARK: Fitting the window to the content
    //
    // The window grows at once, ahead of the shape, and shrinks only after it has settled.

    private func shapesChanged(_ shapes: AuraView.Shapes) {
        guard shapes != lastShapes, let rect = shapes.bar else { return }
        lastShapes = shapes
        aura.update(shapes, cornerRadius: bar.mode == .open ? 28 : rect.height / 2)
        let rects = [rect, shapes.peek].compactMap { $0 }
        let halfWidth = rects.map { max(abs($0.minX), abs($0.maxX)) }.max() ?? 0
        let top = rects.map(\.minY).min() ?? 0
        let target = NSSize(width: ceil(halfWidth * 2 + barMargin * 2), height: ceil(-top + barMargin))
        shrink?.cancel()
        let current = panel.frame.size
        if target.width > current.width || target.height > current.height {
            place(NSSize(width: max(target.width, current.width), height: max(target.height, current.height)))
        }
        shrink = Task {
            try? await Task.sleep(nanoseconds: 800_000_000)
            if !Task.isCancelled { place(target) }
        }
    }

    private func place(_ size: NSSize) {
        var point = anchor
        if let screen = (NSScreen.screens.first { $0.frame.contains(anchor) } ?? NSScreen.main)?.visibleFrame {
            point.x = min(max(point.x, screen.minX + 60), screen.maxX - 60)
            point.y = min(max(point.y, screen.minY + 6), screen.maxY - size.height + barMargin)
        }
        panel.setFrame(NSRect(x: point.x - size.width / 2, y: point.y - barMargin, width: size.width, height: size.height), display: false)
    }

    // MARK: Dragging: it follows with some weight, and snaps to centre when it is close

    private func dragChanged() {
        let mouse = NSEvent.mouseLocation
        if dragStart == nil { dragStart = (mouse, anchor); snapX = nil }
        guard let start = dragStart else { return }
        anchor = NSPoint(x: start.anchor.x + mouse.x - start.mouse.x, y: start.anchor.y + mouse.y - start.mouse.y)
        place(panel.frame.size)
    }

    private func dragEnded() {
        dragStart = nil
        guard let screen = panel.screen?.visibleFrame else { saveAnchor(); return }
        if abs(anchor.x - screen.midX) < 90 {
            var spring = Spring(Float(anchor.x), response: 0.4, damping: 0.68)
            spring.target = Float(screen.midX)
            snapX = spring
            haptic()
        } else { saveAnchor() }
    }

    private func saveAnchor() {
        UserDefaults.standard.set(anchor.x, forKey: "barAnchorX")
        UserDefaults.standard.set(anchor.y, forKey: "barAnchorY")
    }

    func windowDidResignKey(_ notification: Notification) {
        // Clicking away dismisses it, unless that would lose what is being written or read.
        guard Date().timeIntervalSince(openedAt) > 0.4 else { return }
        if bar.draft.isEmpty, bar.reviewing == nil, !bar.voice.listening { close() }
    }
}

// MARK: The content, on top of the renderer

private struct ShapesKey: PreferenceKey {
    static let defaultValue: [String: CGRect] = [:]
    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) { value.merge(nextValue()) { $1 } }
}

private struct RootSizeKey: EnvironmentKey { static let defaultValue: CGSize = .zero }
private extension EnvironmentValues {
    var barRootSize: CGSize { get { self[RootSizeKey.self] } set { self[RootSizeKey.self] = newValue } }
}

/// Where a part of the bar is, relative to the bottom centre of the window.
private struct Report: ViewModifier {
    let key: String
    @Environment(\.barRootSize) private var root
    func body(content: Content) -> some View {
        content.background(GeometryReader { proxy in
            let frame = proxy.frame(in: .named("aura"))
            Color.clear.preference(key: ShapesKey.self, value: [key: CGRect(x: frame.minX - root.width / 2, y: frame.minY - root.height, width: frame.width, height: frame.height)])
        })
    }
}

struct BarRoot: View {
    @Environment(Store.self) private var store
    @Bindable var bar: BarModel

    var body: some View {
        GeometryReader { root in
            VStack(spacing: 12) {
                if let peek = bar.peek, bar.mode == .rest { PeekCard(peek: peek, bar: bar).modifier(Report(key: "peek")) }
                Group {
                    if bar.mode == .open { OpenBar(bar: bar, status: status) } else { RestBar(bar: bar, status: status) }
                }
                .background { if bar.solidFallback { RoundedRectangle(cornerRadius: bar.mode == .open ? 28 : 22, style: .continuous).fill(Color(white: 0.08)) } }
                .modifier(Report(key: "bar"))
            }
            .fixedSize()
            .frame(width: root.size.width, height: max(root.size.height - barMargin, 0), alignment: .bottom)
            .environment(\.barRootSize, root.size)
        }
        .coordinateSpace(name: "aura")
        .environment(\.colorScheme, .dark)
        .onPreferenceChange(ShapesKey.self) { frames in
            MainActor.assumeIsolated { bar.onShapes?(AuraView.Shapes(bar: frames["bar"], peek: frames["peek"], orb: frames["orb"])) }
        }
    }

    private var status: BarStatus {
        if bar.voice.listening { return BarStatus(pulse: .listening, title: bar.voice.transcript.isEmpty ? "Listening…" : "", detail: bar.voice.transcript) }
        if bar.speaker.speaking { return BarStatus(pulse: .speaking, title: "Speaking…") }
        return store.barStatus
    }
}

/// Where the renderer draws the orb. It is the one part with no SwiftUI in it at all.
private struct OrbSlot: View {
    var size: CGFloat = 24
    var body: some View { Color.clear.frame(width: size, height: size).modifier(Report(key: "orb")).accessibilityHidden(true) }
}

/// At rest. Idle, it folds down to just the orb, and opens up as the pointer comes near.
private struct RestBar: View {
    @Environment(Store.self) private var store
    let bar: BarModel
    let status: BarStatus
    @State private var pressing = false
    @State private var dragged = false

    private var compact: Bool { status.pulse == .idle && !bar.near && bar.peek == nil }

    var body: some View {
        HStack(spacing: 10) {
            OrbSlot()
            if !compact { line.transition(.opacity.combined(with: .scale(scale: 0.92, anchor: .leading))) }
        }
        .padding(.leading, compact ? 10 : 10).padding(.trailing, compact ? 10 : 16)
        .frame(height: 44)
        .opacity(bar.contentShown ? 1 : 0)
        .animation(.spring(response: 0.3, dampingFraction: 0.86), value: compact)
        .contentShape(Rectangle())
        .gesture(DragGesture(minimumDistance: 0, coordinateSpace: .global)
            .onChanged { value in
                if !pressing { pressing = true; bar.onPress?(true) }
                if hypot(value.translation.width, value.translation.height) > 4 { dragged = true }
                if dragged { bar.onDragChanged?() }
            }
            .onEnded { _ in
                pressing = false
                bar.onPress?(false)
                if dragged { bar.onDragEnded?() } else { bar.expand() }
                dragged = false
            })
        .contextMenu {
            Button("Open Mission Control") { store.go(store.route) }
            Button("Open Inbox") { store.go(.inbox) }
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isButton)
        .accessibilityLabel(status.pulse == .streaming ? "Mission Control: replying" : "Mission Control: \(status.title)")
        .accessibilityAction { bar.expand() }
    }

    @ViewBuilder private var line: some View {
        if status.pulse == .streaming || (status.pulse == .listening && !status.detail.isEmpty) {
            // The reply as it is written, running past in one line.
            Text(plainTail(status.detail))
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(.white)
                .lineLimit(1)
                .fixedSize()
                .frame(width: 330, alignment: .trailing)
                .clipped()
                .mask(LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black, location: 0.12), .init(color: .black, location: 1)], startPoint: .leading, endPoint: .trailing))
        } else {
            HStack(spacing: 8) {
                if let tool = status.tool {
                    Image(systemName: toolIcon(tool)).font(.system(size: 12, weight: .medium)).foregroundStyle(.white.opacity(0.85))
                        .symbolEffect(.pulse, options: .repeating)
                        .contentTransition(.symbolEffect(.replace))
                }
                VStack(alignment: .leading, spacing: 0) {
                    if status.pulse == .thinking {
                        ShimmerText(text: status.title)
                    } else {
                        Text(status.title).font(.system(size: 13, weight: .medium)).foregroundStyle(.white).lineLimit(1)
                    }
                    if !status.detail.isEmpty { Text(status.detail).font(.system(size: 11)).foregroundStyle(.white.opacity(0.6)).lineLimit(1) }
                }
                .id(status.title)
                .transition(.blurReplace)
                if let since = status.since {
                    Text(Date(timeIntervalSince1970: since), style: .timer).font(.system(size: 11).monospacedDigit()).foregroundStyle(.white.opacity(0.55))
                }
                if status.pulse == .idle { Text("⌥Space").font(.system(size: 11)).foregroundStyle(.white.opacity(0.4)) }
            }
            .animation(.spring(response: 0.35, dampingFraction: 0.86), value: status.title)
        }
    }
}

/// Light passing across the words, for as long as it is thinking.
private struct ShimmerText: View {
    let text: String
    // Off a clock that stops with the view. A repeating SwiftUI animation here outlives the
    // view and keeps the whole bar laying out at full rate until the app quits.
    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30)) { timeline in
            let phase = CGFloat(timeline.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 1.5) / 1.5)
            Text(text).font(.system(size: 13, weight: .medium)).lineLimit(1)
                .foregroundStyle(LinearGradient(colors: [.white.opacity(0.45), .white, .white.opacity(0.45)],
                                                startPoint: UnitPoint(x: phase * 3 - 1.5, y: 0.5), endPoint: UnitPoint(x: phase * 3 - 0.5, y: 0.5)))
        }
    }
}

/// A reply arriving while the bar is at rest, which buds off the bar like a droplet.
private struct PeekCard: View {
    @Environment(Store.self) private var store
    let peek: BarModel.Peek
    let bar: BarModel
    @State private var shown = false

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text(store.name(peek.sender)).font(.system(size: 11, weight: .semibold)).foregroundStyle(.white.opacity(0.6))
                Spacer()
                Button { bar.peek = nil } label: { Image(systemName: "xmark").font(.system(size: 9, weight: .bold)) }
                    .buttonStyle(.plain).foregroundStyle(.white.opacity(0.6)).accessibilityLabel("Dismiss")
            }
            Text(Markdown.inline(peekSummary(peek.text))).font(.system(size: 13)).foregroundStyle(.white).lineLimit(6).fixedSize(horizontal: false, vertical: true)
        }
        .padding(14)
        .frame(width: 420, alignment: .leading)
        .opacity(shown ? 1 : 0)
        .blur(radius: shown ? 0 : 6)
        .contentShape(Rectangle())
        .onTapGesture { bar.expand() }
        .accessibilityAction(named: "Open") { bar.expand() }
        .onAppear { withAnimation(.easeOut(duration: 0.28).delay(0.16)) { shown = true } }
    }
}

/// Open: what needs the owner, what is running, the conversation, and somewhere to write.
private struct OpenBar: View {
    @Environment(Store.self) private var store
    @Bindable var bar: BarModel
    let status: BarStatus
    @FocusState private var fieldFocused: Bool

    var body: some View {
        VStack(spacing: 0) {
            Group {
                if let item = bar.reviewing {
                    ReviewPane(item: item, bar: bar)
                } else {
                    if let away = AwayLine.since(store) { AwayLine(since: away, bar: bar); divider }
                    if !store.inbox.isEmpty { NeedsStrip(bar: bar); divider }
                    if !openWorkers.isEmpty { TaskStrip(workers: openWorkers); divider }
                    if let id = store.leadThread { Conversation(model: store.thread(id), threadID: id) }
                }
            }
            .opacity(bar.contentShown ? 1 : 0)
            .blur(radius: bar.contentShown ? 0 : 8)
            .offset(y: bar.contentShown ? 0 : 10)
            composer
        }
        .frame(width: 600)
        .onChange(of: bar.focusField, initial: true) { _, focus in if focus { fieldFocused = true; bar.focusField = false } }
    }

    private var divider: some View { Rectangle().fill(.white.opacity(0.08)).frame(height: 0.5) }
    private var openWorkers: [JSON] { store.workers.filter { ["working", "waiting", "blocked"].contains($0.status.str) } }

    private var composer: some View {
        HStack(alignment: .bottom, spacing: 10) {
            OrbSlot(size: 26).padding(.bottom, 2)
            TextField("", text: bar.voice.listening ? .constant(bar.voice.transcript) : $bar.draft,
                      prompt: Text(bar.voice.listening ? "Listening…" : "Ask anything, or hand over a task").foregroundColor(.white.opacity(0.42)), axis: .vertical)
                .textFieldStyle(.plain)
                .font(.system(size: 15))
                .foregroundStyle(.white)
                .tint(.white)
                .lineLimit(1...6)
                .focused($fieldFocused)
                .padding(.vertical, 6)
                .onSubmit(submit)
                .onKeyPress(.return, phases: .down) { press in
                    guard press.modifiers.contains(.shift) else { return .ignored }
                    bar.draft += "\n"
                    return .handled
                }
            Button { bar.voice.toggle() } label: {
                Image(systemName: bar.voice.listening ? "waveform" : "mic").font(.system(size: 14, weight: .medium)).frame(width: 30, height: 30)
                    .foregroundStyle(bar.voice.listening ? .white : .white.opacity(0.6))
                    .symbolEffect(.variableColor.iterative, isActive: bar.voice.listening)
            }
            .buttonStyle(.plain).help(bar.voice.listening ? "Stop listening" : "Speak (or hold ⌥Space)").accessibilityLabel(bar.voice.listening ? "Stop listening" : "Speak")
            if let lead = store.lead, bar.draft.isEmpty, store.isBusy(lead.id) {
                Button { Task { await store.stopAgent(lead.id) } } label: {
                    Image(systemName: "stop.circle.fill").font(.system(size: 24)).foregroundStyle(.white.opacity(0.75))
                }.buttonStyle(.plain).help("Stop").accessibilityLabel("Stop")
            } else {
                Button(action: submit) {
                    Image(systemName: "arrow.up.circle.fill").font(.system(size: 24))
                        .foregroundStyle(.white.opacity(bar.draft.isEmpty ? 0.2 : 0.95))
                }.buttonStyle(.plain).disabled(bar.draft.isEmpty).accessibilityLabel("Send")
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 9)
    }

    private func submit() {
        let text = bar.draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, let thread = store.leadThread else { return }
        bar.draft = ""
        bar.answerAloud = false
        Task { await store.send(text, to: thread) }
    }
}

private func kindInfo(_ kind: String) -> (icon: String, label: String) {
    switch kind {
    case "command": return ("exclamationmark.shield", "Command held for you")
    case "tool": return ("exclamationmark.shield", "Tool call held for you")
    case "job": return ("clock", "Recurring job wants to start")
    case "proposal": return ("arrow.triangle.branch", "Proposed change to Mission Control")
    case "signin": return ("globe", "Website sign-in")
    case "lesson": return ("flask", "Lesson needs a decision")
    case "question": return ("questionmark.bubble", "A worker needs your answer")
    default: return ("tray", kind)
    }
}

/// After a long absence, what finished meanwhile, in one line. It goes once the owner says
/// something, because from then on the conversation itself is the record.
private struct AwayLine: View {
    @Environment(Store.self) private var store
    let since: Double
    let bar: BarModel
    private static let absence: Double = 3 * 3600

    /// When the owner last spoke, if that was long enough ago and tasks ended since.
    static func since(_ store: Store) -> Double? {
        guard let thread = store.leadThread,
              let last = store.thread(thread).messages.last(where: { $0.sender == "user" })?.ts,
              Date().timeIntervalSince1970 - last >= absence else { return nil }
        return store.missions.contains { $0.updated.num > last && ["completed", "failed"].contains($0.status.str) } ? last : nil
    }

    var body: some View {
        let ended = store.missions.filter { $0.updated.num > since }
        let finished = ended.filter { $0.status.str == "completed" }.count
        let failed = ended.filter { $0.status.str == "failed" }.count
        Button { store.go(.tasks); bar.collapse() } label: {
            HStack(spacing: 8) {
                Image(systemName: "clock").foregroundStyle(.white.opacity(0.55)).accessibilityHidden(true)
                Text("Since \(TimeDivider.label(since).replacingOccurrences(of: "Today ", with: ""))").foregroundStyle(.white.opacity(0.62))
                if finished > 0 { Text("·").foregroundStyle(.white.opacity(0.4)); Text("\(finished) finished").foregroundStyle(.white) }
                if failed > 0 { Text("·").foregroundStyle(.white.opacity(0.4)); Text("\(failed) failed").foregroundStyle(MC.danger) }
                Spacer()
                Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold)).foregroundStyle(.white.opacity(0.35)).accessibilityHidden(true)
            }
            .font(.system(size: 12))
            .padding(.horizontal, 18).padding(.vertical, 9)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Since you were last here: \(finished) finished, \(failed) failed. Open Tasks.")
    }
}

private struct NeedsStrip: View {
    @Environment(Store.self) private var store
    let bar: BarModel

    var body: some View {
        VStack(spacing: 2) {
            ForEach(store.inbox.prefix(3)) { item in
                Button {
                    if ["command", "tool"].contains(item.kind.str) { bar.reviewing = item } else { store.go(.inbox); bar.collapse() }
                } label: {
                    HStack(spacing: 10) {
                        Image(systemName: kindInfo(item.kind.str).icon).foregroundStyle(MC.warn).frame(width: 18)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(kindInfo(item.kind.str).label).font(.system(size: 11)).foregroundStyle(.white.opacity(0.55))
                            Text(clip(item.title.str, 74)).font(.system(size: 12.5, weight: .medium)).foregroundStyle(.white).lineLimit(1)
                        }
                        Spacer()
                        Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold)).foregroundStyle(.white.opacity(0.35))
                    }
                    .padding(.horizontal, 10).padding(.vertical, 6)
                    .contentShape(Rectangle())
                }.buttonStyle(.plain)
            }
            if store.inbox.count > 3 {
                Button("and \(store.inbox.count - 3) more in the Inbox") { store.go(.inbox); bar.collapse() }
                    .buttonStyle(.plain).font(.system(size: 11)).foregroundStyle(.white.opacity(0.55)).padding(.vertical, 3)
            }
        }
        .padding(8)
    }
}

/// A held command is approved here only after it has been shown in full, with what changed
/// since a near-identical one was answered. Whatever is approved runs exactly as shown.
private struct ReviewPane: View {
    @Environment(Store.self) private var store
    let item: JSON
    let bar: BarModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Button { bar.reviewing = nil } label: { Label("Back", systemImage: "chevron.left").font(.system(size: 12)) }.buttonStyle(.plain).foregroundStyle(.white.opacity(0.6))
                Spacer()
                Text(ago(item.ts.num)).font(.system(size: 11)).foregroundStyle(.white.opacity(0.55))
            }
            Text("\(store.name(item.agent.str)) wants to run this in \(item.detail.str). Running it runs exactly what is shown.")
                .font(.system(size: 12)).foregroundStyle(.white.opacity(0.65)).fixedSize(horizontal: false, vertical: true)
            ScrollView {
                Text(item.title.str).font(.system(size: 12, design: .monospaced)).foregroundStyle(.white).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(11)
            }
            .frame(maxHeight: 240)
            .background(.white.opacity(0.07), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            if !item.reason.str.isEmpty { Text(item.reason.str).font(.system(size: 12)).foregroundStyle(.white.opacity(0.65)).fixedSize(horizontal: false, vertical: true) }
            if let prior = priorNote { Text(prior).font(.system(size: 12)).foregroundStyle(MC.warn).fixedSize(horizontal: false, vertical: true) }
            HStack {
                Spacer()
                AsyncButton("Decline") { try await store.decide(approval: item.id, approve: false); bar.reviewing = nil }
                AsyncButton(item.kind.str == "tool" ? "Approve" : "Run it") { try await store.decide(approval: item.id, approve: true); bar.reviewing = nil }
                    .buttonStyle(.borderedProminent)
            }
            .disabled(!store.live)
        }
        .padding(16)
    }

    private var priorNote: String? {
        let prior = item.prior
        guard !prior.isNull else { return nil }
        let verb = prior.status.str == "denied" ? "declined" : "approved"
        let time = Date(timeIntervalSince1970: prior.ts.num).formatted(date: .omitted, time: .shortened)
        if prior.identical.flag { return "Identical to the command you \(verb) at \(time). Running it runs it again." }
        let lines = prior.diff.list.map(\.str).joined(separator: "\n")
        return "Differs from the command you \(verb) at \(time):\n\(lines.isEmpty ? "only whitespace changed" : lines)"
    }
}

private struct TaskStrip: View {
    @Environment(Store.self) private var store
    let workers: [JSON]

    var body: some View {
        VStack(spacing: 1) {
            ForEach(workers.prefix(3)) { worker in
                VStack(alignment: .leading, spacing: 4) {
                    row(worker)
                    StepTrail(steps: steps(of: worker)).padding(.leading, 28)
                }
                .padding(.horizontal, 10).padding(.vertical, 5)
            }
            if workers.count > 3 {
                Button("and \(workers.count - 3) more") { store.go(.activity) }
                    .buttonStyle(.plain).font(.system(size: 11)).foregroundStyle(.white.opacity(0.55)).padding(.vertical, 2)
            }
        }
        .padding(8)
    }

    /// The task's tool steps so far, oldest first.
    private func steps(of worker: JSON) -> [JSON] {
        let mission = worker.mission.str
        guard !mission.isEmpty else { return [] }
        return store.executions.filter { $0.mission.str == mission && $0.agent.str == worker.profile.str }.sorted { $0.ts.num < $1.ts.num }
    }

    private func row(_ worker: JSON) -> some View {
        HStack(spacing: 10) {
            AgentBadge(agent: store.agent(worker.profile.str), size: 18)
            Button {
                store.focusWorker = worker.id
                store.go(.activity)
            } label: {
                Text(clip(worker.title.str.isEmpty ? "Task" : worker.title.str, 58)).font(.system(size: 12.5)).foregroundStyle(.white).lineLimit(1)
            }.buttonStyle(.plain)
            Spacer()
            Text(ago(worker.updated.num)).font(.system(size: 11)).foregroundStyle(.white.opacity(0.4))
            Pill(status: worker.status.str)
            if worker.status.str == "working" {
                Button { Task { await store.stopWorker(worker.id) } } label: { Image(systemName: "stop.fill").font(.system(size: 9)) }
                    .buttonStyle(.plain).foregroundStyle(.white.opacity(0.6)).help("Stop this task").accessibilityLabel("Stop this task")
            }
        }
    }
}

/// A task's steps as a row of marks, with the one running now named beside them, so how
/// far along it is and whether anything failed shows without opening it.
private struct StepTrail: View {
    let steps: [JSON]
    private static let shown = 24

    var body: some View {
        if !steps.isEmpty {
            let recent = steps.suffix(Self.shown)
            let failed = steps.filter { $0.status.str == "failed" }.count
            HStack(spacing: 8) {
                HStack(spacing: 2) {
                    ForEach(recent) { step in
                        RoundedRectangle(cornerRadius: 1.5, style: .continuous).fill(tone(step.status.str)).frame(width: 5, height: 5)
                    }
                }
                .accessibilityElement()
                .accessibilityLabel(failed > 0 ? "\(plural(steps.count, "step")), \(failed) failed" : plural(steps.count, "step"))
                if let now = steps.last(where: { $0.status.str == "running" }) {
                    let input = now.detail.arguments
                    let what = input.command.optStr ?? input.path.optStr ?? input.url.optStr ?? input.query.optStr ?? ""
                    Text(clip(oneLine([now.name.str, what].filter { !$0.isEmpty }.joined(separator: " · ")), 56))
                        .font(.system(size: 10.5, design: .monospaced)).foregroundStyle(.white.opacity(0.55)).lineLimit(1)
                } else {
                    Text(plural(steps.count, "step")).font(.system(size: 10.5)).foregroundStyle(.white.opacity(0.5))
                }
            }
        }
    }

    private func tone(_ status: String) -> Color {
        switch status {
        case "failed": return MC.danger
        case "running": return .white.opacity(0.9)
        default: return .white.opacity(0.32)
        }
    }
}

private struct Conversation: View {
    @Environment(Store.self) private var store
    let model: ThreadModel
    let threadID: String

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 11) {
                ForEach(BarLine.lines(model.messages.suffix(24))) { line in
                    view(line).transition(.asymmetric(insertion: .opacity.combined(with: .offset(y: 10)), removal: .opacity))
                }
                LiveText(threadID: threadID)
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .animation(.spring(response: 0.36, dampingFraction: 0.82), value: model.messages.count)
        }
        .defaultScrollAnchor(.bottom)
        .frame(height: 320)
        .mask(LinearGradient(stops: [.init(color: .clear, location: 0), .init(color: .black, location: 0.07), .init(color: .black, location: 0.96), .init(color: .clear, location: 1)], startPoint: .top, endPoint: .bottom))
    }

    @ViewBuilder private func view(_ line: BarLine) -> some View {
        switch line.kind {
        case .divider(let ts): TimeDivider(ts: ts)
        case .tools(let steps): ToolRun(steps: steps)
        case .message(let message): row(message)
        }
    }

    @ViewBuilder private func row(_ message: Message) -> some View {
        if let approval = message.meta.approval.optStr {
            ApprovalCard(id: approval, fallback: message.text)
        } else if message.sender == "user" {
            HStack {
                Spacer(minLength: 60)
                Text(message.text).font(.system(size: 13)).foregroundStyle(.white).textSelection(.enabled)
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .background(.white.opacity(0.11), in: RoundedRectangle(cornerRadius: 15, style: .continuous))
            }
        } else {
            MarkdownView(text: message.text).foregroundStyle(.white)
        }
    }
}

/// What the conversation draws, in order: messages, the tools used between them folded
/// into one line, and the time wherever a long gap starts a new sitting.
private struct BarLine: Identifiable {
    enum Kind { case message(Message), tools([Message]), divider(Double) }
    let id: String
    let kind: Kind

    static func lines(_ messages: ArraySlice<Message>) -> [BarLine] {
        var out: [BarLine] = [], previous: Double?
        for message in messages {
            if let previous, TimeDivider.separates(previous, message.ts) {
                out.append(BarLine(id: "d\(message.id)", kind: .divider(message.ts)))
            }
            previous = message.ts
            if message.kind == "tool" {
                if case .tools(let steps)? = out.last?.kind {
                    out[out.count - 1] = BarLine(id: out[out.count - 1].id, kind: .tools(steps + [message]))
                } else {
                    out.append(BarLine(id: "t\(message.id)", kind: .tools([message])))
                }
            } else {
                out.append(BarLine(id: "m\(message.id)", kind: .message(message)))
            }
        }
        return out
    }
}

/// The tools a reply used, as one quiet line that opens into the list.
private struct ToolRun: View {
    let steps: [Message]
    @State private var open = false

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Button { if steps.count > 1 { open.toggle() } } label: {
                HStack(spacing: 6) {
                    Image(systemName: "gearshape").accessibilityHidden(true)
                    Text(steps.count == 1 ? clip(steps[0].text, 90) : "Used \(steps.count) tools · \(clip(steps[steps.count - 1].text, 60))").lineLimit(1)
                    if steps.count > 1 {
                        Image(systemName: "chevron.right").font(.system(size: 8, weight: .semibold)).rotationEffect(.degrees(open ? 90 : 0)).accessibilityHidden(true)
                    }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(steps.count == 1 ? steps[0].text : "Used \(steps.count) tools")
            if open {
                ForEach(steps) { step in
                    Text(clip(step.text, 96)).lineLimit(1).padding(.leading, 18)
                }
            }
        }
        .font(.system(size: 11))
        .foregroundStyle(.secondary)
    }
}

/// The reply as it is written, the newest words still arriving out of the dark. Its own
/// view, so each new word redraws this and nothing else.
struct LiveText: View {
    @Environment(Store.self) private var store
    let threadID: String

    var body: some View {
        let live = store.streams.values.filter { $0.thread == threadID }.sorted { $0.agent < $1.agent }
        ForEach(live, id: \.agent) { entry in
            if !entry.text.isEmpty {
                MarkdownView(text: entry.text, ink: true).foregroundStyle(.white)
            } else if !entry.thinking.isEmpty {
                Text(clip(entry.thinking, 200)).font(.system(size: 12)).foregroundStyle(.white.opacity(0.42)).lineLimit(3)
            }
        }
    }
}
