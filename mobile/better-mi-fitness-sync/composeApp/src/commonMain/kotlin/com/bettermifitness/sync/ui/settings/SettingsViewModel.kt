package com.bettermifitness.sync.ui.settings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.bettermifitness.sync.AutoSyncPlatform
import com.bettermifitness.sync.BatteryOptimizationStatus
import com.bettermifitness.sync.data.MiSessionManager
import com.bettermifitness.sync.data.preferences.SyncPreferences
import com.bettermifitness.sync.data.preferences.TokenStore
import com.bettermifitness.sync.data.preferences.UserPrefsSnapshot
import com.bettermifitness.sync.health.HealthAvailability
import com.bettermifitness.sync.health.HealthPermissionRequester
import com.bettermifitness.sync.health.HealthReadiness
import com.bettermifitness.sync.i18n.L10n
import com.bettermifitness.sync.sync.WorkerDiagnosticOutcome
import com.bettermifitness.sync.util.RelativeTime
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

data class SettingsUiState(
    val enabledMetrics: Set<String> = emptySet(),
    val prefsReady: Boolean = false,
    val rangeDays: Int = 7,
    val autoSync: Boolean = false,
    val lastBackgroundSyncLabel: String = L10n.text(L10n.homeNever),
    val lastBackgroundStatusTitle: String = L10n.text(L10n.outcomeNotSynced),
    val lastBackgroundDetail: String = L10n.text(L10n.outcomeIdleDetail),
    val lastBackgroundIsError: Boolean = false,
    val lastSyncLabel: String = L10n.text(L10n.homeNever),
    val lastSyncStatusTitle: String = L10n.text(L10n.outcomeNotSynced),
    val lastSyncDetail: String = L10n.text(L10n.outcomeIdleDetail),
    val lastSyncIsError: Boolean = false,
    val lastSyncIsWarning: Boolean = false,
    val showBatteryOptimization: Boolean = false,
    val batteryOptimizationStatus: BatteryOptimizationStatus =
        BatteryOptimizationStatus.UNAVAILABLE,
    val workerStateLabel: String = L10n.text(L10n.settingsWorkerStateUnknown),
    val lastWorkerStartedLabel: String = L10n.text(L10n.homeNever),
    val lastWorkerFinishedLabel: String = L10n.text(L10n.homeNever),
    val lastWorkerOutcomeLabel: String = L10n.text(L10n.settingsWorkerUnknown),
    val lastWorkerError: String? = null,
    val canTestBgRefresh: Boolean = false,
    val bgTestStatus: String? = null,
    val bgTestRunning: Boolean = false,
    val showShortcutsHelp: Boolean = false,
    val showBackgroundRefreshDetails: Boolean = true,
    val healthServiceName: String = "",
    val healthStatusTitle: String = "",
    val healthStatusDetail: String = "",
    val healthNeedsAction: Boolean = false,
    val loggedOut: Boolean = false,
)

class SettingsViewModel(
    private val syncPreferences: SyncPreferences,
    private val healthAvailability: HealthAvailability,
    private val healthPermissions: HealthPermissionRequester,
    private val tokenStore: TokenStore,
    private val session: MiSessionManager,
) : ViewModel() {
    private val prefsSnapshot: StateFlow<UserPrefsSnapshot> = syncPreferences.snapshot
    private val showShortcutsHelp = AutoSyncPlatform.supportsShortcutsHelp()

    private val localState = MutableStateFlow(
        LocalSettingsState(
            canTestBgRefresh = AutoSyncPlatform.supportsOpportunisticRefreshTest(),
            showBatteryOptimization = safeSupportsBatteryOptimization(),
            batteryOptimizationStatus = safeBatteryOptimizationStatus(),
        ),
    )
    private val healthState = MutableStateFlow(
        HealthReadiness(
            available = true,
            permissionsGranted = true,
            serviceName = healthAvailability.healthServiceName(),
            hint = null,
        ),
    )

    private val lastSyncBundle = combine(
        syncPreferences.lastSyncTime,
        syncPreferences.lastSyncStatus,
        syncPreferences.lastSyncMessage,
    ) { t, s, m -> Triple(t, s, m) }

    private val lastBgBundle = combine(
        syncPreferences.lastBackgroundSyncTime,
        syncPreferences.lastBackgroundSyncStatus,
        syncPreferences.lastBackgroundSyncMessage,
    ) { t, s, m -> Triple(t, s, m) }

    private val workerDiagnosticsBundle = combine(
        syncPreferences.lastWorkerStartedAt,
        syncPreferences.lastWorkerFinishedAt,
        syncPreferences.lastWorkerOutcome,
        syncPreferences.lastWorkerError,
    ) { started, finished, outcome, error ->
        WorkerDiagnosticsBundle(started, finished, outcome, error)
    }

    private val configBundle = prefsSnapshot.map { snap ->
        ConfigBundle(snap.enabledMetrics, snap.syncRangeDays, snap.autoSync, snap.ready)
    }

    private val persistedBundle = combine(
        lastSyncBundle,
        lastBgBundle,
        workerDiagnosticsBundle,
    ) { lastSync, lastBg, worker ->
        PersistedBundle(lastSync, lastBg, worker)
    }

    val uiState: StateFlow<SettingsUiState> = combine(
        configBundle,
        persistedBundle,
        localState,
        healthState,
    ) { config, persisted, local, health ->
        SettingsUiState(
            enabledMetrics = config.enabledMetrics,
            prefsReady = config.ready,
            rangeDays = config.rangeDays,
            autoSync = config.autoSync,
            lastBackgroundSyncLabel = RelativeTime.format(persisted.lastBg.first),
            lastBackgroundStatusTitle = com.bettermifitness.sync.sync.SyncOutcomeLabels.title(
                persisted.lastBg.second,
            ),
            lastBackgroundDetail = com.bettermifitness.sync.sync.SyncOutcomeLabels.detail(
                persisted.lastBg.second,
                persisted.lastBg.third,
            ),
            lastBackgroundIsError = com.bettermifitness.sync.sync.SyncOutcomeLabels.isError(
                persisted.lastBg.second,
            ),
            lastSyncLabel = RelativeTime.format(persisted.lastSync.first),
            lastSyncStatusTitle = com.bettermifitness.sync.sync.SyncOutcomeLabels.title(
                persisted.lastSync.second,
            ),
            lastSyncDetail = com.bettermifitness.sync.sync.SyncOutcomeLabels.detail(
                persisted.lastSync.second,
                persisted.lastSync.third,
            ),
            lastSyncIsError = com.bettermifitness.sync.sync.SyncOutcomeLabels.isError(
                persisted.lastSync.second,
            ),
            lastSyncIsWarning = com.bettermifitness.sync.sync.SyncOutcomeLabels.isWarning(
                persisted.lastSync.second,
            ),
            showBatteryOptimization = local.showBatteryOptimization,
            batteryOptimizationStatus = local.batteryOptimizationStatus,
            workerStateLabel = local.workerStateLabel,
            lastWorkerStartedLabel = RelativeTime.format(persisted.worker.started),
            lastWorkerFinishedLabel = RelativeTime.format(persisted.worker.finished),
            lastWorkerOutcomeLabel = workerOutcomeLabel(persisted.worker.outcome),
            lastWorkerError = persisted.worker.error,
            canTestBgRefresh = local.canTestBgRefresh,
            bgTestStatus = local.bgTestStatus,
            bgTestRunning = local.bgTestRunning,
            showShortcutsHelp = showShortcutsHelp,
            healthServiceName = health.serviceName,
            healthStatusTitle = health.statusTitle,
            healthStatusDetail = health.statusDetail,
            healthNeedsAction = !health.isReady,
            loggedOut = local.loggedOut,
        )
    }.stateIn(
        scope = viewModelScope,
        started = SharingStarted.Eagerly,
        initialValue = SettingsUiState(
            showBatteryOptimization = localState.value.showBatteryOptimization,
            batteryOptimizationStatus = localState.value.batteryOptimizationStatus,
            canTestBgRefresh = localState.value.canTestBgRefresh,
            showShortcutsHelp = showShortcutsHelp,
            healthServiceName = healthAvailability.healthServiceName(),
        ),
    )

    init {
        refreshHealth()
        refreshBackgroundDiagnostics()
    }

    fun refreshHealth() {
        viewModelScope.launch {
            try {
                healthState.value = healthAvailability.readiness()
            } catch (_: Exception) {
                healthState.value = HealthReadiness(
                    available = false,
                    permissionsGranted = false,
                    serviceName = healthAvailability.healthServiceName(),
                    hint = L10n.text(L10n.healthStatusCheckFailed),
                )
            }
        }
    }

    fun refreshBackgroundDiagnostics() {
        viewModelScope.launch {
            val batteryStatus = safeBatteryOptimizationStatus()
            val workState = try {
                AutoSyncPlatform.currentBackgroundWorkState()
            } catch (_: Exception) {
                "UNKNOWN"
            }
            localState.update {
                it.copy(
                    showBatteryOptimization = safeSupportsBatteryOptimization(),
                    batteryOptimizationStatus = batteryStatus,
                    workerStateLabel = workerStateLabel(workState),
                )
            }
        }
    }

    fun requestBatteryOptimizationExemption() {
        viewModelScope.launch {
            try {
                AutoSyncPlatform.requestBatteryOptimizationExemption()
            } catch (_: Exception) {
                // Unsupported OEM / policy — keep the status safe and readable.
            }
            refreshBackgroundDiagnostics()
        }
    }

    fun openHealthService() {
        viewModelScope.launch {
            try {
                healthPermissions.requestPermissions()
            } catch (_: Exception) {
                // Denied / failed — open Settings or Health Connect below.
            }
            if (!healthAvailability.hasWritePermissions()) {
                healthAvailability.openHealthService()
            }
            refreshHealth()
        }
    }

    fun setMetricEnabled(key: String, enabled: Boolean) {
        viewModelScope.launch { syncPreferences.setMetricEnabled(key, enabled) }
    }

    fun setSyncRangeDays(days: Int) {
        viewModelScope.launch { syncPreferences.setSyncRangeDays(days) }
    }

    fun setAutoSync(enabled: Boolean) {
        viewModelScope.launch {
            syncPreferences.setAutoSync(enabled)
            if (enabled) AutoSyncPlatform.scheduleBackgroundRefresh()
            else AutoSyncPlatform.cancelBackgroundRefresh()
            refreshBackgroundDiagnostics()
        }
    }

    fun runBackgroundRefreshTest() {
        if (localState.value.bgTestRunning) return
        if (!uiState.value.autoSync) return
        localState.update {
            it.copy(bgTestRunning = true, bgTestStatus = L10n.text(L10n.settingsRunningRefresh))
        }
        AutoSyncPlatform.runOpportunisticRefreshTest { status ->
            viewModelScope.launch {
                localState.update {
                    it.copy(
                        bgTestRunning = false,
                        bgTestStatus = mapBgTestStatus(status),
                    )
                }
            }
        }
    }

    fun logout() {
        viewModelScope.launch {
            tokenStore.clear()
            session.clear()
            localState.update { it.copy(loggedOut = true) }
        }
    }

    fun consumeLoggedOut() {
        localState.update { it.copy(loggedOut = false) }
    }

    private fun safeSupportsBatteryOptimization(): Boolean = try {
        AutoSyncPlatform.supportsBatteryOptimization()
    } catch (_: Exception) {
        false
    }

    private fun safeBatteryOptimizationStatus(): BatteryOptimizationStatus = try {
        AutoSyncPlatform.batteryOptimizationStatus()
    } catch (_: Exception) {
        BatteryOptimizationStatus.UNAVAILABLE
    }

    private fun workerOutcomeLabel(outcome: String?): String = when (outcome) {
        WorkerDiagnosticOutcome.SUCCESS -> L10n.text(L10n.outcomeUpToDate)
        WorkerDiagnosticOutcome.PARTIAL -> L10n.text(L10n.outcomeAlmostDone)
        WorkerDiagnosticOutcome.SKIPPED -> L10n.text(L10n.outcomeNothingToDo)
        WorkerDiagnosticOutcome.RETRY -> L10n.text(L10n.settingsWorkerRetrying)
        WorkerDiagnosticOutcome.FAILED -> L10n.text(L10n.outcomeCouldNotFinish)
        else -> L10n.text(L10n.settingsWorkerUnknown)
    }

    private fun workerStateLabel(state: String): String = when (state) {
        "ENQUEUED" -> L10n.text(L10n.settingsWorkerStateEnqueued)
        "RUNNING" -> L10n.text(L10n.settingsWorkerStateRunning)
        "BLOCKED" -> L10n.text(L10n.settingsWorkerStateBlocked)
        "SUCCEEDED" -> L10n.text(L10n.settingsWorkerStateSucceeded)
        "FAILED" -> L10n.text(L10n.settingsWorkerStateFailed)
        "CANCELLED" -> L10n.text(L10n.settingsWorkerStateCancelled)
        "NOT_FOUND" -> L10n.text(L10n.settingsWorkerStateNotScheduled)
        else -> L10n.text(L10n.settingsWorkerStateUnknown)
    }

    private fun mapBgTestStatus(status: String): String = when (status) {
        "success" -> L10n.text(L10n.settingsTestOk)
        "partial_success" -> L10n.text(L10n.settingsPartialOk)
        "skipped" -> L10n.text(L10n.settingsSkipped)
        "not_logged_in" -> L10n.text(L10n.settingsNotSignedIn)
        "cancelled" -> L10n.text(L10n.settingsCancelled)
        else -> L10n.textFmt(L10n.settingsFailedStatus, status)
    }

    private data class ConfigBundle(
        val enabledMetrics: Set<String>,
        val rangeDays: Int,
        val autoSync: Boolean,
        val ready: Boolean,
    )

    private data class WorkerDiagnosticsBundle(
        val started: String?,
        val finished: String?,
        val outcome: String?,
        val error: String?,
    )

    private data class PersistedBundle(
        val lastSync: Triple<String?, String?, String?>,
        val lastBg: Triple<String?, String?, String?>,
        val worker: WorkerDiagnosticsBundle,
    )

    private data class LocalSettingsState(
        val canTestBgRefresh: Boolean = false,
        val bgTestStatus: String? = null,
        val bgTestRunning: Boolean = false,
        val showBatteryOptimization: Boolean = false,
        val batteryOptimizationStatus: BatteryOptimizationStatus =
            BatteryOptimizationStatus.UNAVAILABLE,
        val workerStateLabel: String = L10n.text(L10n.settingsWorkerStateUnknown),
        val loggedOut: Boolean = false,
    )
}
