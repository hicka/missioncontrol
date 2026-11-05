import AppKit
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let model = RuntimeModel()
    let store = Store()
    let updates = Updates()
    var bar: BarController!
    var cards: DesktopCards!
    var cardsItem: NSMenuItem!
    var window: NSWindow!
    var statusItem: NSStatusItem!
    var runtimeItem: NSMenuItem!
    var teamItem: NSMenuItem!
    var barItem: NSMenuItem!
    var startItem: NSMenuItem!
    var stopItem: NSMenuItem!
    var restartItem: NSMenuItem!
    var activity: NSObjectProtocol?

    func applicationDidFinishLaunching(_ notification: Notification) {
        AppTheme.apply()
        buildMenus(); buildWindow(); buildStatusItem()
        bar = BarController(store: store)
        cards = DesktopCards(store: store)
        cardsItem?.title = cards.hidden ? "Show Desktop Cards" : "Hide Desktop Cards"
        bar.bar.voice.onError = { [weak self] text in self?.store.notify(text, error: true) }
        store.onShowWindow = { [weak self] in self?.showWindow() }
        model.onReady = { [weak self] url, token in self?.store.connect(API(base: url.absoluteString, token: token)) }
        model.onShowWorkspace = { [weak self] in self?.store.go(.home) }
        model.onOpenSettings = { [weak self] tab in self?.store.go(.settings(tab)) }
        model.onNativeState = { [weak self] in self?.updateStatus() }
        updates.isIdle = { [weak self] in
            guard let self else { return false }
            return self.store.workingCount == 0 && !self.store.agents.contains { self.store.isBusy($0.id) }
        }
        updates.prepareToInstall = { [weak self] in self?.model.prepareForUpdate() }
        updates.start()
        store.onCheckForUpdates = { [weak self] in self?.updates.checkNow() }
        model.begin()
        if !UserDefaults.standard.bool(forKey: "barHidden") && !CommandLine.arguments.contains("--no-bar") { bar.show() }
        // A login item begins with the bar and the menu bar; normal launches show the window.
        let loginLaunch = NSAppleEventManager.shared().currentAppleEvent?.paramDescriptor(forKeyword: keyAEPropData)?.enumCodeValue == keyAELaunchedAsLogInItem
        if CommandLine.arguments.contains("--quiet-launch") { window.orderFrontRegardless() }
        else if !loginLaunch { showWindow() } else { NSApp.setActivationPolicy(.accessory) }
    }

    func buildMenus() {
        let main = NSMenu()
        let appMenu = NSMenu(), appItem = NSMenuItem()
        appMenu.addItem(withTitle: "About Mission Control", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(item("Check for Updates…", #selector(updatesAction)))
        appMenu.addItem(.separator())
        appMenu.addItem(item("Settings…", #selector(settingsAction), ","))
        appMenu.addItem(item("Runtime Manager", #selector(managerAction), "0"))
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Mission Control", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(item("Quit Mission Control", #selector(quitAction), "q"))
        appItem.submenu = appMenu; main.addItem(appItem)
        let file = NSMenu(title: "File"), fileItem = NSMenuItem()
        file.addItem(item("Open Mission Control", #selector(workspaceAction), "n"))
        file.addItem(withTitle: "Close window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        fileItem.submenu = file; main.addItem(fileItem)
        let edit = NSMenu(title: "Edit"), editItem = NSMenuItem()
        for (title, action, key) in [("Undo", Selector(("undo:")), "z"), ("Redo", Selector(("redo:")), "Z"), ("Cut", #selector(NSText.cut(_:)), "x"), ("Copy", #selector(NSText.copy(_:)), "c"), ("Paste", #selector(NSText.paste(_:)), "v"), ("Select All", #selector(NSText.selectAll(_:)), "a")] { edit.addItem(withTitle: title, action: action, keyEquivalent: key) }
        editItem.submenu = edit; main.addItem(editItem)
        let view = NSMenu(title: "View"), viewItem = NSMenuItem()
        for (index, entry) in [("Home", Route.home), ("Inbox", .inbox), ("Tasks", .tasks), ("Activity", .activity), ("Schedule", .schedule), ("Knowledge", .knowledge), ("Evolution", .evolution), ("Modules", .modules)].enumerated() {
            let menuItem = item(entry.0, #selector(routeAction(_:)), "\(index + 1)")
            menuItem.representedObject = entry.1
            view.addItem(menuItem)
        }
        view.addItem(.separator())
        view.addItem(item("Search and Commands…", #selector(paletteAction), "k"))
        view.addItem(item("Show Floating Bar", #selector(barAction), "b"))
        cardsItem = item("Hide Desktop Cards", #selector(cardsAction))
        view.addItem(cardsItem)
        viewItem.submenu = view; main.addItem(viewItem)
        let windows = NSMenu(title: "Window"), windowItem = NSMenuItem()
        windows.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windows.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        windowItem.submenu = windows; main.addItem(windowItem); NSApp.windowsMenu = windows
        NSApp.mainMenu = main
    }
    func item(_ title: String, _ action: Selector, _ key: String = "") -> NSMenuItem { let item = NSMenuItem(title: title, action: action, keyEquivalent: key); item.target = self; return item }

    func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1240, height: 820), styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView], backing: .buffered, defer: false)
        window.title = "Mission Control"; window.minSize = NSSize(width: 860, height: 560); window.delegate = self
        window.titlebarAppearsTransparent = true; window.isReleasedWhenClosed = false
        window.contentViewController = NSHostingController(rootView: ShellView(runtime: model).environment(store))
        window.setContentSize(NSSize(width: 1240, height: 820))
        window.center()
        window.setFrameAutosaveName("MissionControlMainWindow")
    }

    func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "waveform.path.ecg", accessibilityDescription: "Mission Control")
        statusItem.button?.image?.isTemplate = true
        let menu = NSMenu()
        runtimeItem = NSMenuItem(title: "Runtime: starting", action: nil, keyEquivalent: ""); menu.addItem(runtimeItem)
        teamItem = NSMenuItem(title: "Waiting for the runtime", action: nil, keyEquivalent: ""); menu.addItem(teamItem)
        menu.addItem(.separator()); menu.addItem(item("Open Mission Control", #selector(workspaceAction)))
        barItem = item("Hide Floating Bar", #selector(barAction)); menu.addItem(barItem)
        menu.addItem(item("Runtime Manager…", #selector(managerAction)))
        menu.addItem(item("Settings…", #selector(settingsAction)))
        menu.addItem(item("Check for Updates…", #selector(updatesAction)))
        menu.addItem(.separator())
        startItem = item("Start runtime", #selector(startAction)); menu.addItem(startItem)
        stopItem = item("Stop runtime", #selector(stopAction)); menu.addItem(stopItem)
        restartItem = item("Restart runtime", #selector(restartAction)); menu.addItem(restartItem)
        menu.addItem(item("Open runtime log", #selector(logsAction)))
        menu.addItem(.separator()); menu.addItem(item("Quit Mission Control", #selector(quitAction)))
        statusItem.menu = menu
    }

    func updateStatus() {
        let active = store.agents.filter { store.isBusy($0.id) }.count + store.workingCount
        runtimeItem?.title = "Runtime: \(model.status.lowercased())"
        teamItem?.title = active > 0 ? "\(active) working" : (store.inbox.isEmpty ? "All quiet" : plural(store.inbox.count, "thing needs you", "things need you"))
        barItem?.title = bar?.isVisible == true ? "Hide Floating Bar" : "Show Floating Bar"
        // A runtime this app attached to belongs to whatever started it.
        for control in [startItem, stopItem, restartItem] { control?.isHidden = model.attached }
        statusItem?.button?.image = NSImage(systemSymbolName: active > 0 ? "waveform.path.ecg.rectangle.fill" : "waveform.path.ecg", accessibilityDescription: "Mission Control · \(model.status)")
        statusItem?.button?.image?.isTemplate = true
        if active > 0 && activity == nil { activity = ProcessInfo.processInfo.beginActivity(options: [.userInitiatedAllowingIdleSystemSleep], reason: "Mission Control agents are working") }
        if active == 0, let current = activity { ProcessInfo.processInfo.endActivity(current); activity = nil }
    }

    func showWindow() {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    @objc func workspaceAction() { showWindow() }
    @objc func routeAction(_ sender: NSMenuItem) { if let route = sender.representedObject as? Route { store.go(route) } }
    @objc func managerAction() { store.go(.runtime) }
    @objc func settingsAction() { store.go(.settings("llm")) }
    @objc func paletteAction() {
        // From the floating bar or the menu bar it opens over the window, rather than closing
        // one left open behind them.
        store.paletteOpen = window.isKeyWindow ? !store.paletteOpen : true
        showWindow()
    }
    @objc func barAction() { bar.toggleVisible(); updateStatus() }
    @objc func cardsAction() {
        cards.hidden.toggle()
        cardsItem.title = cards.hidden ? "Show Desktop Cards" : "Hide Desktop Cards"
    }
    @objc func updatesAction() { if updates.available { updates.checkNow() } else { store.go(.settings("updates")) } }
    @objc func startAction() { model.crashCount = 0; model.start() }
    @objc func stopAction() { model.stop() }
    @objc func restartAction() { model.stop(restart: true) }
    @objc func logsAction() { model.showLogs() }
    @objc func quitAction() { NSApp.terminate(nil) }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if model.keepRunning {
            sender.orderOut(nil)
            // With the window closed it lives in the bar and the menu bar, not the Dock.
            NSApp.setActivationPolicy(.accessory)
        } else { NSApp.terminate(nil) }
        return false
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        statusItem.button?.toolTip = "Stopping Mission Control services…"
        model.quit { NSApp.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }
}

@main
struct MissionControlApplication {
    @MainActor static func main() {
        let application = NSApplication.shared
        let delegate = AppDelegate()
        application.delegate = delegate
        application.setActivationPolicy(.regular)
        withExtendedLifetime(delegate) { application.run() }
    }
}
