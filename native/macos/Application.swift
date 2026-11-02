import AppKit
import SwiftUI
import WebKit
import Combine

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, NSToolbarDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, WKDownloadDelegate {
    let model = RuntimeModel()
    var window: NSWindow!
    var web: WKWebView!
    var manager: NSHostingView<RuntimeView>!
    var statusItem: NSStatusItem!
    var switcher: NSSegmentedControl!
    var statusLabel: NSTextField!
    var runtimeItem: NSMenuItem!
    var teamItem: NSMenuItem!
    var showingManager = true
    var loadedURL: URL?
    var pendingSettings: String?
    var downloads: [WKDownload] = []
    var activity: NSObjectProtocol?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.appearance = NSAppearance(named: .darkAqua)
        buildMenus(); buildWindow(); buildStatusItem()
        model.onReady = { [weak self] url in self?.loadWorkspace(url) }
        model.onShowWorkspace = { [weak self] in self?.showWorkspace() }
        model.onOpenSettings = { [weak self] tab in self?.openSettings(tab) }
        model.onNativeState = { [weak self] in self?.updateStatus() }
        model.begin()
        // A login item begins in the menu bar; normal launches reveal the window.
        let loginLaunch = NSAppleEventManager.shared().currentAppleEvent?.paramDescriptor(forKeyword: keyAEPropData)?.enumCodeValue == keyAELaunchedAsLogInItem
        if !loginLaunch { showWindow() }
    }

    func buildMenus() {
        let main = NSMenu()
        let appMenu = NSMenu(), appItem = NSMenuItem()
        appMenu.addItem(withTitle: "About Mission Control", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
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
        for (title, action, key) in [("Undo", Selector(("undo:")), "z"), ("Cut", #selector(NSText.cut(_:)), "x"), ("Copy", #selector(NSText.copy(_:)), "c"), ("Paste", #selector(NSText.paste(_:)), "v"), ("Select All", #selector(NSText.selectAll(_:)), "a")] { edit.addItem(withTitle: title, action: action, keyEquivalent: key) }
        editItem.submenu = edit; main.addItem(editItem)
        let view = NSMenu(title: "View"), viewItem = NSMenuItem()
        view.addItem(item("Telemetry & chat", #selector(workspaceAction), "1"))
        view.addItem(item("Runtime Manager", #selector(managerAction), "0"))
        view.addItem(item("Reload workspace", #selector(reloadAction), "r"))
        viewItem.submenu = view; main.addItem(viewItem)
        let windows = NSMenu(title: "Window"), windowItem = NSMenuItem()
        windows.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windows.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        windowItem.submenu = windows; main.addItem(windowItem); NSApp.windowsMenu = windows
        NSApp.mainMenu = main
    }
    func item(_ title: String, _ action: Selector, _ key: String = "") -> NSMenuItem { let item = NSMenuItem(title: title, action: action, keyEquivalent: key); item.target = self; return item }

    func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1480, height: 960), styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView], backing: .buffered, defer: false)
        window.title = "Mission Control"; window.minSize = NSSize(width: 1040, height: 700); window.delegate = self
        window.setFrameAutosaveName("MissionControlMainWindow"); window.center(); window.isReleasedWhenClosed = false
        window.titlebarAppearsTransparent = true; window.toolbarStyle = .unified
        let toolbar = NSToolbar(identifier: "MissionControlToolbar"); toolbar.delegate = self; toolbar.displayMode = .iconOnly
        window.toolbar = toolbar
        let config = WKWebViewConfiguration(); config.websiteDataStore = .default()
        config.preferences.javaScriptCanOpenWindowsAutomatically = true
        config.userContentController.add(self, name: "missionControl")
        web = WKWebView(frame: .zero, configuration: config); web.navigationDelegate = self; web.uiDelegate = self
        web.setValue(false, forKey: "drawsBackground")
        manager = NSHostingView(rootView: RuntimeView(model: model))
        let content = NSView(); window.contentView = content
        for view in [web!, manager!] as [NSView] {
            view.translatesAutoresizingMaskIntoConstraints = false; content.addSubview(view)
            NSLayoutConstraint.activate([view.topAnchor.constraint(equalTo: content.safeAreaLayoutGuide.topAnchor), view.leadingAnchor.constraint(equalTo: content.leadingAnchor), view.trailingAnchor.constraint(equalTo: content.trailingAnchor), view.bottomAnchor.constraint(equalTo: content.bottomAnchor)])
        }
        web.isHidden = true
    }

    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] { [.init("views"), .flexibleSpace, .init("status")] }
    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] { toolbarAllowedItemIdentifiers(toolbar) }
    func toolbar(_ toolbar: NSToolbar, itemForItemIdentifier identifier: NSToolbarItem.Identifier, willBeInsertedIntoToolbar flag: Bool) -> NSToolbarItem? {
        let item = NSToolbarItem(itemIdentifier: identifier)
        if identifier.rawValue == "views" {
            switcher = NSSegmentedControl(labels: ["Telemetry & chat", "Runtime Manager"], trackingMode: .selectOne, target: self, action: #selector(changeView))
            switcher.selectedSegment = 1; item.view = switcher
        } else if identifier.rawValue == "status" {
            statusLabel = NSTextField(labelWithString: "◌ Starting runtime"); statusLabel.font = .monospacedSystemFont(ofSize: 11, weight: .medium); statusLabel.textColor = .secondaryLabelColor
            item.view = statusLabel
        } else { return nil }
        return item
    }

    func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "waveform.path.ecg", accessibilityDescription: "Mission Control")
        statusItem.button?.image?.isTemplate = true
        let menu = NSMenu()
        runtimeItem = NSMenuItem(title: "Runtime: starting", action: nil, keyEquivalent: ""); menu.addItem(runtimeItem)
        teamItem = NSMenuItem(title: "Waiting for telemetry", action: nil, keyEquivalent: ""); menu.addItem(teamItem)
        menu.addItem(.separator()); menu.addItem(item("Open Mission Control", #selector(workspaceAction)))
        menu.addItem(item("Runtime Manager…", #selector(managerAction)))
        menu.addItem(item("Settings…", #selector(settingsAction)))
        menu.addItem(.separator()); menu.addItem(item("Start runtime", #selector(startAction)))
        menu.addItem(item("Stop runtime", #selector(stopAction))); menu.addItem(item("Restart runtime", #selector(restartAction)))
        menu.addItem(item("Open runtime log", #selector(logsAction)))
        menu.addItem(.separator()); menu.addItem(item("Quit Mission Control", #selector(quitAction)))
        statusItem.menu = menu
    }
    func updateStatus() {
        let agents = model.snapshot?.harness.agents ?? []
        let active = agents.filter { !["idle", "error", "offline"].contains($0.state) }.count
        runtimeItem?.title = "Runtime: \(model.status.lowercased())"
        teamItem?.title = "\(active) working · \(model.processes.count) processes"
        statusLabel?.stringValue = "\(model.status == "Running" ? "●" : "◌") \(model.status) · \(active) active"
        statusLabel?.sizeToFit()
        statusItem?.button?.image = NSImage(systemSymbolName: active > 0 ? "waveform.path.ecg.rectangle.fill" : "waveform.path.ecg", accessibilityDescription: "Mission Control · \(model.status)")
        statusItem?.button?.image?.isTemplate = true
        if active > 0 && activity == nil { activity = ProcessInfo.processInfo.beginActivity(options: [.userInitiatedAllowingIdleSystemSleep], reason: "Mission Control agents are working") }
        if active == 0, let current = activity { ProcessInfo.processInfo.endActivity(current); activity = nil }
        if model.snapshot == nil && !model.isRunning { setView(manager: true) }
    }

    func loadWorkspace(_ url: URL) {
        loadedURL = url
        web.load(URLRequest(url: url))
        if !CommandLine.arguments.contains("--show-manager") { setView(manager: false) }
    }
    func trusted(_ url: URL?) -> Bool { guard let url, let base = loadedURL else { return false }; return url.scheme == "http" && url.host == "127.0.0.1" && url.port == base.port }
    func setView(manager show: Bool) { showingManager = show; manager.isHidden = !show; web.isHidden = show; switcher?.selectedSegment = show ? 1 : 0; if !show { window.makeFirstResponder(web) } }
    func showWindow() { NSApp.activate(ignoringOtherApps: true); window.makeKeyAndOrderFront(nil) }
    func showWorkspace() { setView(manager: model.snapshot == nil); showWindow() }
    func openSettings(_ tab: String) {
        guard model.snapshot != nil else { setView(manager: true); showWindow(); return }
        showWorkspace()
        let encoded = (try? String(data: JSONSerialization.data(withJSONObject: [tab]), encoding: .utf8)) ?? "[\"llm\"]"
        web.evaluateJavaScript("window.openSettings && openSettings(\(encoded)[0])")
    }
    @objc func changeView() { setView(manager: switcher.selectedSegment == 1 || model.snapshot == nil) }
    @objc func workspaceAction() { showWorkspace() }
    @objc func managerAction() { setView(manager: true); showWindow() }
    @objc func settingsAction() { openSettings("llm") }
    @objc func reloadAction() { web.reload() }
    @objc func startAction() { model.crashCount = 0; model.start() }
    @objc func stopAction() { model.stop() }
    @objc func restartAction() { model.stop(restart: true) }
    @objc func logsAction() { model.showLogs() }
    @objc func quitAction() { NSApp.terminate(nil) }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if model.keepRunning { sender.orderOut(nil) } else { NSApp.terminate(nil) }
        return false
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        statusItem.button?.toolTip = "Stopping Mission Control services…"
        model.quit { NSApp.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame, trusted(message.frameInfo.request.url), let body = message.body as? [String: String] else { return }
        if body["action"] == "runtime" { managerAction() }
    }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if trusted(url) || url.scheme == "blob" {
            if navigationAction.shouldPerformDownload { decisionHandler(.download) } else { decisionHandler(.allow) }
        } else {
            if ["https", "http", "mailto"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
            decisionHandler(.cancel)
        }
    }
    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let disposition = (navigationResponse.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        decisionHandler(navigationResponse.canShowMIMEType && !disposition.lowercased().contains("attachment") ? .allow : .download)
    }
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url, ["http", "https"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
        return nil
    }
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        guard trusted(frame.request.url) else { completionHandler(nil); return }
        let panel = NSOpenPanel(); panel.allowsMultipleSelection = parameters.allowsMultipleSelection; panel.canChooseDirectories = parameters.allowsDirectories
        panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.urls : nil) }
    }
    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin, initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType, decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(trusted(frame.request.url) && type == .microphone ? .prompt : .deny)
    }
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        guard trusted(frame.request.url) else { completionHandler(); return }
        let alert = NSAlert(); alert.messageText = "Mission Control"; alert.informativeText = message; alert.beginSheetModal(for: window) { _ in completionHandler() }
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        guard trusted(frame.request.url) else { completionHandler(false); return }
        let alert = NSAlert(); alert.messageText = "Mission Control"; alert.informativeText = message; alert.addButton(withTitle: "Continue"); alert.addButton(withTitle: "Cancel"); alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }
    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { downloads.append(download); download.delegate = self }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { downloads.append(download); download.delegate = self }
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel(); panel.nameFieldStringValue = URL(fileURLWithPath: suggestedFilename).lastPathComponent
        panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.url : nil) }
    }
    func downloadDidFinish(_ download: WKDownload) { downloads.removeAll { $0 === download } }
    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) { downloads.removeAll { $0 === download }; model.error = "Download: " + error.localizedDescription }
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
