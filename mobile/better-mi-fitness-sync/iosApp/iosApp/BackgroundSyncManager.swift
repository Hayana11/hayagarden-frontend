import BackgroundTasks
import ComposeApp
import UIKit

/// Schedules and runs opportunistic background auto-sync.
///
/// Two scheduler contracts cover days one slot is skipped (both OS-gated):
/// - BGAppRefreshTask: short (~30s) refresh.
/// - BGProcessingTask: longer (minutes) deferred work, network-required.
/// The Kotlin side only syncs the **last 1 day** so the work fits either budget.
/// Full-range sync is for:
/// - Manual "Sync Now"
/// - Foreground auto-sync when the app is opened
/// - Shortcuts / App Intent (`SyncBetterMiFitnessIntent`)
///
/// Simulator: tasks won't fire alone — use:
///   e -l objc -- (void)[[BGTaskScheduler sharedScheduler] _simulateLaunchForTaskWithIdentifier:@"com.bettermifitness.sync.refresh"]
///   e -l objc -- (void)[[BGTaskScheduler sharedScheduler] _simulateLaunchForTaskWithIdentifier:@"com.bettermifitness.sync.processing"]
enum BackgroundSyncManager {
    static let taskIdentifier = "com.bettermifitness.sync.refresh"
    static let processingIdentifier = "com.bettermifitness.sync.processing"

    /// Finishing assertion so an in-flight foreground sync gets ~30s grace
    /// when the app backgrounds instead of stopping mid-write.
    private static var finishingTask: UIBackgroundTaskIdentifier = .invalid

    /// Registers the task handlers and wires Kotlin → Swift scheduling bridges.
    /// Must be called during app launch.
    static func register() {
        BGTaskScheduler.shared.register(
            forTaskWithIdentifier: taskIdentifier,
            using: nil
        ) { task in
            handle(task: task as! BGAppRefreshTask)
        }
        BGTaskScheduler.shared.register(
            forTaskWithIdentifier: processingIdentifier,
            using: nil
        ) { task in
            handleProcessing(task: task as! BGProcessingTask)
        }

        AutoSyncBridge.shared.setHandlers(
            onSchedule: { schedule() },
            onCancel: { cancel() },
            statusProvider: { backgroundRefreshStatusLabel() },
            supportsDebugTest: { KotlinBoolean(bool: isDebugRefreshEnabled) }
        )
    }

    /// Debug / Simulator only — never shown or callable in App Store Release.
    static var isDebugRefreshEnabled: Bool {
        #if DEBUG
        return true
        #elseif targetEnvironment(simulator)
        return true
        #else
        return false
        #endif
    }

    /// Asks iOS to schedule the next opportunistic refresh (earliest ~15 min)
    /// plus a deferred processing run (network-required, no charger needed).
    /// iOS treats earliestBeginDate as a floor, not a guarantee.
    static func schedule() {
        BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: taskIdentifier)
        BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: processingIdentifier)
        let refresh = BGAppRefreshTaskRequest(identifier: taskIdentifier)
        refresh.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        do {
            try BGTaskScheduler.shared.submit(refresh)
            print("[BGSync] scheduled \(taskIdentifier)")
        } catch {
            print("[BGSync] schedule failed: \(error)")
        }
        let processing = BGProcessingTaskRequest(identifier: processingIdentifier)
        processing.requiresNetworkConnectivity = true
        processing.requiresExternalPower = false
        do {
            try BGTaskScheduler.shared.submit(processing)
            print("[BGSync] scheduled \(processingIdentifier)")
        } catch {
            print("[BGSync] schedule processing failed: \(error)")
        }
        #if DEBUG
        BGTaskScheduler.shared.getPendingTaskRequests { requests in
            print("[BGSync] pending: \(requests.map(\.identifier))")
        }
        #endif
    }

    /// Cancels pending requests (e.g. auto-sync turned off).
    static func cancel() {
        BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: taskIdentifier)
        BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: processingIdentifier)
        print("[BGSync] cancelled \(taskIdentifier) + \(processingIdentifier)")
    }

    /// Debug/simulator-only diagnostic. The OS permission does not indicate this app's Auto-sync state.
    static func backgroundRefreshStatusLabel() -> String {
        guard isDebugRefreshEnabled else { return "" }

        #if targetEnvironment(simulator)
        return L10n.backgroundSimulator
        #else
        switch UIApplication.shared.backgroundRefreshStatus {
        case .available:
            return L10n.backgroundOn
        case .denied:
            return L10n.backgroundOff
        case .restricted:
            return L10n.backgroundRestricted
        @unknown default:
            return L10n.backgroundUnknown
        }
        #endif
    }

    /// Runs the same 1-day path as BGAppRefresh (Debug / Simulator only).
    /// URL: `bettermifitness://debug/bg-refresh`
    static func handleDebugURL(_ url: URL) {
        guard isDebugRefreshEnabled else { return }
        guard url.scheme == "bettermifitness" else { return }
        let host = url.host?.lowercased() ?? ""
        let path = url.path.lowercased()
        guard host == "debug", path.contains("bg-refresh") else { return }
        print("[BGSync] debug URL trigger — running opportunistic 1-day sync")
        BackgroundSync.shared.runOpportunisticBackgroundSync { status in
            let code = (status as? String) ?? "?"
            print("[BGSync] debug opportunistic finished status=\(code)")
        }
    }

    /// Begins the finishing assertion; call from scenePhase.background so an
    /// in-flight sync can complete instead of stopping mid-write.
    /// Wired in iOSApp; kept here so all BGTask-adjacent code stays together.
    static func beginFinishing() {
        guard finishingTask == .invalid else { return }
        finishingTask = UIApplication.shared.beginBackgroundTask(withName: "finish-sync") {
            endFinishing()
        }
    }

    /// Ends the finishing assertion; call when the app returns to foreground.
    static func endFinishing() {
        guard finishingTask != .invalid else { return }
        UIApplication.shared.endBackgroundTask(finishingTask)
        finishingTask = .invalid
    }

    private static func handle(task: BGAppRefreshTask) {
        // Line up the next run only if Auto-sync is still enabled.
        AutoSyncSchedule.shared.rescheduleIfEnabled()

        var finished = false
        func finish(success: Bool) {
            guard !finished else { return }
            finished = true
            task.setTaskCompleted(success: success)
        }

        task.expirationHandler = {
            print("[BGSync] task expired — cancelling in-flight sync")
            BackgroundSync.shared.cancel()
            finish(success: false)
        }

        // Last 1 day only — see BackgroundSync.runOpportunisticBackgroundSync
        BackgroundSync.shared.runOpportunisticBackgroundSync { status in
            let code = (status as? String) ?? BackgroundSync.shared.STATUS_FAILED
            // success or quiet skip both count as a completed task for the OS
            let ok = code == BackgroundSync.shared.STATUS_SUCCESS
                || code == BackgroundSync.shared.STATUS_SKIPPED
            print("[BGSync] opportunistic sync finished status=\(code)")
            finish(success: ok)
        }
    }

    private static func handleProcessing(task: BGProcessingTask) {
        // Line up the next run only if Auto-sync is still enabled.
        AutoSyncSchedule.shared.rescheduleIfEnabled()

        var finished = false
        func finish(success: Bool) {
            guard !finished else { return }
            finished = true
            task.setTaskCompleted(success: success)
        }

        task.expirationHandler = {
            print("[BGSync] processing task expired — cancelling in-flight sync")
            BackgroundSync.shared.cancel()
            finish(success: false)
        }

        // Same 1-day path as BGAppRefresh; processing just gets minutes of budget.
        BackgroundSync.shared.runOpportunisticBackgroundSync { status in
            let code = (status as? String) ?? BackgroundSync.shared.STATUS_FAILED
            let ok = code == BackgroundSync.shared.STATUS_SUCCESS
                || code == BackgroundSync.shared.STATUS_SKIPPED
            print("[BGSync] processing sync finished status=\(code)")
            finish(success: ok)
        }
    }
}
