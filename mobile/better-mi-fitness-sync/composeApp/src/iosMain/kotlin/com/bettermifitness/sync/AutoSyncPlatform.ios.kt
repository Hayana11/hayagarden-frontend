package com.bettermifitness.sync

actual object AutoSyncPlatform {
    actual fun scheduleBackgroundRefresh() {
        AutoSyncBridge.invokeSchedule()
    }

    actual fun cancelBackgroundRefresh() {
        AutoSyncBridge.invokeCancel()
    }

    actual fun backgroundRefreshStatusLabel(): String {
        return AutoSyncBridge.invokeStatus()
    }

    actual fun supportsBatteryOptimization(): Boolean = false

    actual fun batteryOptimizationStatus(): BatteryOptimizationStatus =
        BatteryOptimizationStatus.UNAVAILABLE

    actual fun requestBatteryOptimizationExemption(): Boolean = false

    actual suspend fun currentBackgroundWorkState(): String = "UNAVAILABLE"

    actual fun supportsOpportunisticRefreshTest(): Boolean = false

    actual fun runOpportunisticRefreshTest(onDone: (String) -> Unit) {
        onDone("skipped")
    }

    actual fun supportsShortcutsHelp(): Boolean = true
}

object AutoSyncBridge {
    private var onSchedule: (() -> Unit)? = null
    private var onCancel: (() -> Unit)? = null
    private var statusProvider: (() -> String)? = null
    private var supportsDebugTest: (() -> Boolean)? = null

    fun setHandlers(
        onSchedule: (() -> Unit)?,
        onCancel: (() -> Unit)?,
        statusProvider: (() -> String)?,
        supportsDebugTest: (() -> Boolean)?,
    ) {
        this.onSchedule = onSchedule
        this.onCancel = onCancel
        this.statusProvider = statusProvider
        this.supportsDebugTest = supportsDebugTest
    }

    fun invokeSchedule() {
        onSchedule?.invoke()
    }

    fun invokeCancel() {
        onCancel?.invoke()
    }

    fun invokeStatus(): String {
        return statusProvider?.invoke() ?: "Unknown"
    }

    fun invokeSupportsDebugTest(): Boolean {
        return supportsDebugTest?.invoke() ?: false
    }
}
