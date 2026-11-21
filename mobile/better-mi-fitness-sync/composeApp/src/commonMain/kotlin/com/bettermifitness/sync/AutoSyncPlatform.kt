package com.bettermifitness.sync

enum class BatteryOptimizationStatus {
    EXEMPT,
    NOT_EXEMPT,
    UNAVAILABLE,
}

/**
 * iOS bridges for system background refresh and status. Android also exposes
 * the WorkManager and battery diagnostics used by Settings.
 */
expect object AutoSyncPlatform {
    fun scheduleBackgroundRefresh()
    fun cancelBackgroundRefresh()
    fun backgroundRefreshStatusLabel(): String
    fun supportsBatteryOptimization(): Boolean
    fun batteryOptimizationStatus(): BatteryOptimizationStatus
    fun requestBatteryOptimizationExemption(): Boolean
    suspend fun currentBackgroundWorkState(): String
    fun supportsOpportunisticRefreshTest(): Boolean
    fun runOpportunisticRefreshTest(onDone: (String) -> Unit)
    fun supportsShortcutsHelp(): Boolean
}

/** A platform read must never make the settings screen crash. */
fun safeBatteryOptimizationStatus(
    read: () -> BatteryOptimizationStatus,
): BatteryOptimizationStatus = try {
    read()
} catch (_: Exception) {
    BatteryOptimizationStatus.UNAVAILABLE
}
