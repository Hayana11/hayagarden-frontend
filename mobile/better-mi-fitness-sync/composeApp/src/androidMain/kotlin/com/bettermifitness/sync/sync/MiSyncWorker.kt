package com.bettermifitness.sync.sync

import android.content.Context
import android.util.Log
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.bettermifitness.sync.data.preferences.SyncPreferences
import com.bettermifitness.sync.di.initKoin
import kotlin.time.Clock
import org.koin.core.context.GlobalContext
import org.koin.mp.KoinPlatform

class MiSyncWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result {
        val startedAt = Clock.System.now().toString()
        val diagnostics = loadDiagnostics()
        recordStartedSafely(diagnostics, startedAt)

        return try {
            ensureKoin()
            val coordinator = KoinPlatform.getKoin().get<SyncCoordinator>()
            val outcome = coordinator.run(
                rangeDaysOverride = 1,
                requireAutoSync = true,
                requestHealthPermissions = false,
                recordAsBackground = true,
                resetProgress = false,
                userInitiated = false,
            )
            Log.i(TAG, "background sync finished: $outcome")
            val mapping = outcome.toWorkerOutcomeMapping()
            val result = when (mapping.result) {
                WorkerResultKind.SUCCESS -> Result.success()
                WorkerResultKind.RETRY -> Result.retry()
                WorkerResultKind.FAILURE -> Result.failure()
            }
            recordFinishedSafely(diagnostics, mapping.diagnosticOutcome, mapping.error)
            result
        } catch (e: Exception) {
            Log.e(TAG, "background sync error", e)
            recordFinishedSafely(
                diagnostics = diagnostics,
                outcome = WorkerDiagnosticOutcome.RETRY,
                error = e.message,
            )
            Result.retry()
        }
    }

    private suspend fun loadDiagnostics(): SyncPreferences? {
        return try {
            ensureKoin()
            KoinPlatform.getKoin().get<SyncPreferences>()
        } catch (e: Exception) {
            Log.w(TAG, "worker diagnostics unavailable", e)
            null
        }
    }

    private suspend fun recordStartedSafely(
        diagnostics: SyncPreferences?,
        timestamp: String,
    ) {
        if (diagnostics == null) return
        try {
            diagnostics.recordWorkerStarted(timestamp)
        } catch (e: Exception) {
            Log.w(TAG, "worker start diagnostic unavailable", e)
        }
    }

    private suspend fun recordFinishedSafely(
        diagnostics: SyncPreferences?,
        outcome: String,
        error: String?,
    ) {
        if (diagnostics == null) return
        try {
            diagnostics.recordWorkerFinished(
                timestamp = Clock.System.now().toString(),
                outcome = outcome,
                error = error,
            )
        } catch (e: Exception) {
            Log.w(TAG, "worker finish diagnostic unavailable", e)
        }
    }

    private fun ensureKoin() {
        if (GlobalContext.getOrNull() == null) {
            com.bettermifitness.sync.di.provideAndroidContext(applicationContext)
            initKoin()
        }
    }

    companion object {
        private const val TAG = "MiSyncWorker"
        const val UNIQUE_WORK_NAME = "mi_fitness_auto_sync"
    }
}
