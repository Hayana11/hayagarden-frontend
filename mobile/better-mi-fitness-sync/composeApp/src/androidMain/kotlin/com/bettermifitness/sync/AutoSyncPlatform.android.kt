package com.bettermifitness.sync

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import com.bettermifitness.sync.i18n.L10n
import com.bettermifitness.sync.sync.MiSyncWorker
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

actual object AutoSyncPlatform {
    private lateinit var appContext: Context

    fun init(context: Context) {
        appContext = context.applicationContext
    }

    private fun requireContext(): Context {
        check(::appContext.isInitialized) {
            "AutoSyncPlatform.init(context) must be called from Application.onCreate"
        }
        return appContext
    }

    actual fun scheduleBackgroundRefresh() {
        val ctx = requireContext()
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()
        // Android WorkManager minimum periodic interval is 15 minutes.
        // Timing is opportunistic; Doze / app standby may delay execution.
        val request = PeriodicWorkRequestBuilder<MiSyncWorker>(15, TimeUnit.MINUTES)
            .setConstraints(constraints)
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.MINUTES)
            .build()
        WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(
            MiSyncWorker.UNIQUE_WORK_NAME,
            ExistingPeriodicWorkPolicy.UPDATE,
            request,
        )
    }

    actual fun cancelBackgroundRefresh() {
        if (!::appContext.isInitialized) return
        WorkManager.getInstance(appContext).cancelUniqueWork(MiSyncWorker.UNIQUE_WORK_NAME)
    }

    actual fun backgroundRefreshStatusLabel(): String {
        return if (isDebuggable(requireContext()) || isAndroidEmulator()) {
            L10n.text(L10n.backgroundAndroidStatus)
        } else {
            ""
        }
    }

    actual fun supportsBatteryOptimization(): Boolean = true

    actual fun batteryOptimizationStatus(): BatteryOptimizationStatus {
        if (!supportsBatteryOptimization()) return BatteryOptimizationStatus.UNAVAILABLE
        return safeBatteryOptimizationStatus {
            val powerManager = requireContext().getSystemService(PowerManager::class.java)
                ?: return@safeBatteryOptimizationStatus BatteryOptimizationStatus.UNAVAILABLE
            if (powerManager.isIgnoringBatteryOptimizations(requireContext().packageName)) {
                BatteryOptimizationStatus.EXEMPT
            } else {
                BatteryOptimizationStatus.NOT_EXEMPT
            }
        }
    }

    actual fun requestBatteryOptimizationExemption(): Boolean {
        if (batteryOptimizationStatus() != BatteryOptimizationStatus.NOT_EXEMPT) {
            return false
        }
        return try {
            val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                data = Uri.parse("package:${requireContext().packageName}")
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            requireContext().startActivity(intent)
            true
        } catch (_: SecurityException) {
            false
        } catch (_: ActivityNotFoundException) {
            false
        } catch (_: IllegalArgumentException) {
            false
        }
    }

    actual suspend fun currentBackgroundWorkState(): String = withContext(Dispatchers.IO) {
        try {
            val infos = WorkManager.getInstance(requireContext())
                .getWorkInfosForUniqueWork(MiSyncWorker.UNIQUE_WORK_NAME)
                .get()
            infos.firstOrNull()?.state?.name ?: "NOT_FOUND"
        } catch (_: Exception) {
            "UNKNOWN"
        }
    }

    actual fun supportsOpportunisticRefreshTest(): Boolean = false

    actual fun runOpportunisticRefreshTest(onDone: (String) -> Unit) {
        onDone("skipped")
    }

    actual fun supportsShortcutsHelp(): Boolean = false

    private fun isDebuggable(context: Context): Boolean =
        context.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0

    private fun isAndroidEmulator(): Boolean =
        Build.FINGERPRINT.startsWith("generic") ||
            Build.FINGERPRINT.startsWith("unknown") ||
            Build.MODEL.contains("google_sdk", ignoreCase = true) ||
            Build.MODEL.contains("emulator", ignoreCase = true) ||
            Build.MODEL.contains("android sdk built for", ignoreCase = true) ||
            Build.MANUFACTURER.contains("genymotion", ignoreCase = true) ||
            (Build.BRAND.startsWith("generic") && Build.DEVICE.startsWith("generic")) ||
            Build.HARDWARE.contains("goldfish", ignoreCase = true) ||
            Build.HARDWARE.contains("ranchu", ignoreCase = true)
}
