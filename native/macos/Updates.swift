import AppKit
import Foundation
#if canImport(Sparkle)
import Sparkle
#endif

/// Staying current. A signed release is found, fetched and checked in the background, then
/// installed the next time nothing is running, so an update never cuts off an agent's work.
/// Builds without Sparkle, or without a feed configured, simply never update.
@MainActor
final class Updates: NSObject {
    /// True when no agent or task is running.
    var isIdle: () -> Bool = { false }
    /// Called just before the app is replaced.
    var prepareToInstall: () -> Void = {}
    private var waiting: Task<Void, Never>?
    #if canImport(Sparkle)
    private var controller: SPUStandardUpdaterController?
    #endif

    func start() {
        #if canImport(Sparkle)
        guard Bundle.main.object(forInfoDictionaryKey: "SUFeedURL") != nil,
              Bundle.main.object(forInfoDictionaryKey: "SUPublicEDKey") != nil else { return }
        controller = SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: self, userDriverDelegate: nil)
        #endif
    }

    /// False for a build from source, which follows its checkout instead.
    var available: Bool {
        #if canImport(Sparkle)
        return controller != nil
        #else
        return false
        #endif
    }

    func checkNow() {
        #if canImport(Sparkle)
        controller?.checkForUpdates(nil)
        #endif
    }

    /// Waits for a quiet moment, checking twice so a task starting just then is not cut off.
    fileprivate func installWhenIdle(_ install: @escaping () -> Void) {
        waiting?.cancel()
        waiting = Task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 60_000_000_000)
                guard isIdle() else { continue }
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                guard isIdle(), !Task.isCancelled else { continue }
                prepareToInstall()
                install()
                return
            }
        }
    }
}

#if canImport(Sparkle)
extension Updates: SPUUpdaterDelegate {
    /// An always-on app rarely quits, so waiting for that would mean never updating. Sparkle
    /// hands over this block to install now instead, and it is held until nothing is running.
    nonisolated func updater(_ updater: SPUUpdater, willInstallUpdateOnQuit item: SUAppcastItem, immediateInstallationBlock: @escaping () -> Void) -> Bool {
        let install = UnsafeSendable(immediateInstallationBlock)
        Task { @MainActor in self.installWhenIdle { install.value() } }
        return true
    }
}

private struct UnsafeSendable<Value>: @unchecked Sendable {
    let value: Value
    init(_ value: Value) { self.value = value }
}
#endif
