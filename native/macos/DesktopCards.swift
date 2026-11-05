import AppKit
import SwiftUI

/// Module cards on the desktop: one borderless panel each, at the desktop's own level by
/// default, behind every window and above the icons, the way desktop widgets sit. Each
/// can be dragged anywhere and stays where it was put, and can be kept above windows.
@MainActor
final class DesktopCards {
    private let store: Store
    private var panels: [String: CardPanel] = [:]
    private static let hiddenKey = "desktopCardsHidden"

    var hidden: Bool {
        get { UserDefaults.standard.bool(forKey: Self.hiddenKey) }
        set { UserDefaults.standard.set(newValue, forKey: Self.hiddenKey); sync() }
    }

    init(store: Store) {
        self.store = store
        observe()
        NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.panels.values.forEach { $0.keepOnScreen() } }
        }
    }

    private func observe() {
        withObservationTracking {
            _ = store.moduleCards
            _ = store.link
        } onChange: { [weak self] in
            Task { @MainActor in
                self?.sync()
                self?.observe()
            }
        }
        sync()
    }

    /// The cards the owner wants on the desktop, and no others.
    func sync() {
        let wanted = hidden ? [] : store.moduleCards.filter { $0.desktop.flag }
        let keys = Set(wanted.map(CardPanel.key))
        for (key, panel) in panels where !keys.contains(key) {
            panel.orderOut(nil)
            panels[key] = nil
        }
        for card in wanted {
            let key = CardPanel.key(card)
            if panels[key] == nil {
                let panel = CardPanel(key: key, store: store)
                panel.onFirstFit = { [weak self] panel in self?.settle(panel) }
                panels[key] = panel
                panel.orderFront(nil)
            }
        }
    }

    /// A new card goes below any card it would cover, and starts a new column to the right
    /// when it reaches the bottom of the screen. Its size is only known once it has drawn.
    private func settle(_ panel: CardPanel) {
        guard let visible = (panel.screen ?? NSScreen.main)?.visibleFrame else { return }
        let others = panels.values.filter { $0 !== panel && $0.isVisible && $0.isPlaced }.map(\.frame)
        var frame = panel.frame
        for _ in 0..<40 {
            guard let hit = others.first(where: { $0.insetBy(dx: -8, dy: -8).intersects(frame) }) else { break }
            frame.origin.y = hit.minY - 16 - frame.height
            if frame.minY < visible.minY {
                frame.origin.x += frame.width + 16
                frame.origin.y = visible.maxY - 24 - frame.height
            }
        }
        panel.setFrameOrigin(frame.origin)
        panel.keepOnScreen()
        panel.remember()
    }
}

/// One card's window.
final class CardPanel: NSPanel {
    let cardKey: String
    private var floats: Bool {
        didSet { UserDefaults.standard.set(floats, forKey: "cardFloats.\(cardKey)"); applyLevel() }
    }
    private var hosting: CardHosting<AnyView>!
    private weak var store: Store?
    /// Called once it knows its size, for a card that has no place saved yet.
    var onFirstFit: ((CardPanel) -> Void)?
    private var placed = false
    var isPlaced: Bool { placed }

    static func key(_ card: JSON) -> String { "\(card.module.str)/\(card.id)" }

    override var canBecomeKey: Bool { true }   // its fields take typing
    override var canBecomeMain: Bool { false }

    @MainActor init(key: String, store: Store) {
        cardKey = key
        self.store = store
        floats = UserDefaults.standard.bool(forKey: "cardFloats.\(key)")
        super.init(contentRect: NSRect(x: 0, y: 0, width: 320, height: 160),
                   styleMask: [.borderless, .nonactivatingPanel, .fullSizeContentView], backing: .buffered, defer: false)
        isFloatingPanel = false
        hidesOnDeactivate = false
        // Its buttons answer the first click only in a key window. A panel that does not
        // activate the app takes the keyboard without taking the owner away from their app.
        becomesKeyOnlyIfNeeded = false
        backgroundColor = .clear
        isOpaque = false
        hasShadow = true
        animationBehavior = .none
        isReleasedWhenClosed = false
        // The system cannot move this window, since SwiftUI takes the mouse first: sendEvent
        // below moves it instead.
        isMovableByWindowBackground = false
        collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenNone]
        applyLevel()
        let root = DesktopCardRoot(key: key, onSize: { [weak self] size in self?.fit(size) })
            .environment(store)
        hosting = CardHosting(rootView: AnyView(root))
        hosting.sizingOptions = []
        contentView = hosting
        place()
    }

    private func applyLevel() {
        level = floats ? .floating : NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.desktopIconWindow)) + 1)
    }

    /// Where it was left, or the top left of the screen for a new one, which then moves
    /// down past the cards already there once it knows its size. Not the top right: that is
    /// where the Finder puts the desktop's own icons.
    private func place() {
        let saved = UserDefaults.standard
        // Places saved before cards could be dragged were all on top of the desktop icons.
        saved.removeObject(forKey: "cardTopLeft.\(cardKey)")
        if let point = saved.array(forKey: Self.placeKey(cardKey)) as? [Double], point.count == 2 {
            placed = true
            setFrameTopLeftPoint(NSPoint(x: point[0], y: point[1]))
        } else {
            let screen = (NSScreen.main ?? NSScreen.screens[0]).visibleFrame
            setFrameTopLeftPoint(NSPoint(x: screen.minX + 24, y: screen.maxY - 24))
        }
        keepOnScreen()
    }

    static func placeKey(_ key: String) -> String { "cardPlace.\(key)" }

    /// The card grows and shrinks downward from its top edge, as its contents change.
    private func fit(_ size: CGSize) {
        guard size.width > 1, size.height > 1 else { return }
        let top = frame.maxY
        let rounded = NSSize(width: ceil(size.width), height: ceil(size.height))
        if frame.size != rounded { setFrame(NSRect(x: frame.minX, y: top - rounded.height, width: rounded.width, height: rounded.height), display: true) }
        if !placed {
            placed = true
            onFirstFit?(self)
        }
    }

    func remember() {
        UserDefaults.standard.set([Double(frame.minX), Double(frame.maxY)], forKey: Self.placeKey(cardKey))
    }

    func keepOnScreen() {
        guard let screen = screen ?? NSScreen.main else { return }
        let visible = screen.visibleFrame
        var origin = frame.origin
        origin.x = min(max(origin.x, visible.minX), visible.maxX - frame.width)
        origin.y = min(max(origin.y, visible.minY), visible.maxY - frame.height)
        if origin != frame.origin { setFrameOrigin(origin) }
    }

    /// Where the pointer is on screen. The window moves under it during a drag, so a position
    /// taken from the event itself would lag a step behind and shake. A test stands in here.
    var pointer: () -> NSPoint = { NSEvent.mouseLocation }
    private var press: (pointer: NSPoint, origin: NSPoint)?
    private var dragging = false
    private static let dragAfter: CGFloat = 4

    /// Pressing anywhere on the card and moving drags it, whatever is under the pointer: the
    /// card is mostly text and controls, and SwiftUI would otherwise keep the mouse for them.
    /// A press that does not move is an ordinary click. Typing in a field is left alone.
    override func sendEvent(_ event: NSEvent) {
        switch event.type {
        case .leftMouseDown:
            super.sendEvent(event)
            press = firstResponder is NSText ? nil : (pointer(), frame.origin)
            dragging = false
            return
        case .leftMouseDragged:
            guard let press else { break }
            let now = pointer()
            if !dragging, hypot(now.x - press.pointer.x, now.y - press.pointer.y) >= Self.dragAfter {
                dragging = true
                // What the press began on lets go of it, away from it, so a button does not fire.
                if let away = NSEvent.mouseEvent(with: .leftMouseUp, location: NSPoint(x: -10_000, y: -10_000),
                                                 modifierFlags: event.modifierFlags, timestamp: event.timestamp,
                                                 windowNumber: windowNumber, context: nil, eventNumber: event.eventNumber,
                                                 clickCount: 1, pressure: 0) {
                    super.sendEvent(away)
                }
            }
            if dragging {
                setFrameOrigin(NSPoint(x: press.origin.x + now.x - press.pointer.x, y: press.origin.y + now.y - press.pointer.y))
                return
            }
        case .leftMouseUp:
            press = nil
            if dragging {
                dragging = false
                keepOnScreen()
                remember()
                return
            }
        case .rightMouseDown:
            if let view = contentView { NSMenu.popUpContextMenu(cardMenu(), with: event, for: view); return }
        default:
            break
        }
        super.sendEvent(event)
    }

    @MainActor func cardMenu() -> NSMenu {
        let menu = NSMenu()
        menu.addItem(ClosureItem(floats ? "Keep on the Desktop" : "Keep Above Windows") { [weak self] in self?.floats.toggle() })
        guard let store, let card = store.moduleCards.first(where: { CardPanel.key($0) == cardKey }) else { return menu }
        menu.addItem(ClosureItem("Hide from the Desktop") { Task { await store.setCardDesktop(card, false) } })
        menu.addItem(.separator())
        menu.addItem(ClosureItem("Restart \(card.module_name.str)") { Task { try? await store.moduleAction(card.module.str, "restart") } })
        menu.addItem(ClosureItem("Open in Mission Control") { store.go(.modules) })
        return menu
    }
}

private final class ClosureItem: NSMenuItem {
    private let run: () -> Void
    init(_ title: String, _ run: @escaping () -> Void) {
        self.run = run
        super.init(title: title, action: #selector(fire), keyEquivalent: "")
        target = self
    }
    required init(coder: NSCoder) { fatalError("not used") }
    @objc private func fire() { run() }
}

/// The first click acts, even with another app in front.
final class CardHosting<Content: View>: NSHostingView<Content> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

private struct DesktopCardRoot: View {
    @Environment(Store.self) private var store
    let key: String
    let onSize: (CGSize) -> Void

    var body: some View {
        Group {
            if let card = store.moduleCards.first(where: { CardPanel.key($0) == key }) {
                ModuleCardView(card: card, onDesktop: true) { event in Task { await store.cardAction(card, event) } }
            }
        }
        .padding(6)   // room for the edge's light and the shadow
        .fixedSize()
        .onGeometryChange(for: CGSize.self) { $0.size } action: { onSize($0) }
    }
}
