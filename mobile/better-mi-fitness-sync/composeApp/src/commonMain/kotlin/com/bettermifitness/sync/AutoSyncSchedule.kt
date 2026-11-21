package com.bettermifitness.sync

import com.bettermifitness.sync.data.preferences.SyncPreferences
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import org.koin.mp.KoinPlatform

/**
 * Restores or clears the platform background schedule based on the Auto-sync preference.
 * Call after Koin starts (Android [Application], iOS launch).
 */
object AutoSyncSchedule {
    private val scope = CoroutineScope(Dispatchers.Default + SupervisorJob())

    fun restore() {
        scope.launch {
            AutoSyncScheduleRestorer(
                autoSyncEnabled = {
                    KoinPlatform.getKoin().get<SyncPreferences>().autoSync.first()
                },
                schedule = AutoSyncPlatform::scheduleBackgroundRefresh,
                cancel = AutoSyncPlatform::cancelBackgroundRefresh,
            ).restore()
        }
    }

    fun rescheduleIfEnabled() {
        restore()
    }
}

/**
 * Small policy seam for deterministic tests. The Android implementation still
 * owns the unique WorkManager request and UPDATE policy.
 */
class AutoSyncScheduleRestorer(
    private val autoSyncEnabled: suspend () -> Boolean,
    private val schedule: () -> Unit,
    private val cancel: () -> Unit,
) {
    suspend fun restore() {
        val enabled = try {
            autoSyncEnabled()
        } catch (_: Exception) {
            false
        }
        if (enabled) schedule() else cancel()
    }
}
