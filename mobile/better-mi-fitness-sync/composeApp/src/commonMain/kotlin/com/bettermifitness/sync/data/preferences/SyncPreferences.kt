package com.bettermifitness.sync.data.preferences

import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.core.stringSetPreferencesKey
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn

/**
 * Process-wide scope for hot preference flows. Lives as long as the app;
 * cancelled only on process death (never in practice).
 */
private val prefsScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

/**
 * User sync settings and last-sync timestamps (SRP: not credentials).
 *
 * Owns the single hot [snapshot] flow: one DataStore subscription for the whole
 * process (started via [start]), so screens never replay the cold-load pop-in.
 * Per DataStore docs, expose the flow and share it; collectors only observe.
 */
class SyncPreferences(
    private val dataStore: DataStore<Preferences>,
) : SyncPreferencesPort {
    val lastSyncTime: Flow<String?> = dataStore.data.map { it[LAST_SYNC_KEY] }

    val lastBackgroundSyncTime: Flow<String?> = dataStore.data.map { it[LAST_BACKGROUND_SYNC_KEY] }

    /** Last run status code from [com.bettermifitness.sync.sync.SyncOutcome.toStatusCode]. */
    val lastSyncStatus: Flow<String?> = dataStore.data.map { it[LAST_SYNC_STATUS_KEY] }

    val lastSyncMessage: Flow<String?> = dataStore.data.map { it[LAST_SYNC_MESSAGE_KEY] }

    val lastBackgroundSyncStatus: Flow<String?> =
        dataStore.data.map { it[LAST_BACKGROUND_SYNC_STATUS_KEY] }

    val lastBackgroundSyncMessage: Flow<String?> =
        dataStore.data.map { it[LAST_BACKGROUND_SYNC_MESSAGE_KEY] }

    override val autoSync: Flow<Boolean> = dataStore.data.map {
        it[AUTO_SYNC_KEY]?.toBooleanStrictOrNull() ?: false
    }

    override val enabledMetrics: Flow<Set<String>> = dataStore.data.map {
        it[ENABLED_METRICS_KEY] ?: ALL_METRIC_KEYS
    }

    override val syncRangeDays: Flow<Int> = dataStore.data.map {
        it[SYNC_RANGE_DAYS_KEY] ?: 7
    }

    /**
     * Hot snapshot shared by all screens: single DataStore subscription for the
     * whole process. Started eagerly so the first emission lands before screens
     * draw; collectors created later observe the latest value immediately.
     * Sync/config writes still go through the suspend setters below.
     */
    val snapshot: StateFlow<UserPrefsSnapshot> = combine(
        enabledMetrics,
        syncRangeDays,
        autoSync,
        lastSyncTime,
        lastBackgroundSyncTime,
    ) { enabled, rangeDays, auto, lastSync, lastBg ->
        UserPrefsSnapshot(
            ready = true,
            enabledMetrics = enabled,
            syncRangeDays = rangeDays,
            autoSync = auto,
            lastSyncTime = lastSync,
            lastBackgroundSyncTime = lastBg,
        )
    }.stateIn(
        scope = prefsScope,
        started = SharingStarted.Eagerly,
        initialValue = UserPrefsSnapshot(),
    )

    override suspend fun updateLastSync(timestamp: String) {
        dataStore.edit { it[LAST_SYNC_KEY] = timestamp }
    }

    override suspend fun updateLastBackgroundSync(timestamp: String) {
        dataStore.edit { it[LAST_BACKGROUND_SYNC_KEY] = timestamp }
    }

    override suspend fun updateLastSyncOutcome(status: String, message: String?) {
        dataStore.edit { prefs ->
            prefs[LAST_SYNC_STATUS_KEY] = status
            if (message.isNullOrBlank()) {
                prefs.remove(LAST_SYNC_MESSAGE_KEY)
            } else {
                prefs[LAST_SYNC_MESSAGE_KEY] = message.take(400)
            }
        }
    }

    override suspend fun updateLastBackgroundSyncOutcome(status: String, message: String?) {
        dataStore.edit { prefs ->
            prefs[LAST_BACKGROUND_SYNC_STATUS_KEY] = status
            if (message.isNullOrBlank()) {
                prefs.remove(LAST_BACKGROUND_SYNC_MESSAGE_KEY)
            } else {
                prefs[LAST_BACKGROUND_SYNC_MESSAGE_KEY] = message.take(400)
            }
        }
    }

    suspend fun setAutoSync(enabled: Boolean) {
        dataStore.edit { it[AUTO_SYNC_KEY] = enabled.toString() }
    }

    suspend fun setMetricEnabled(key: String, enabled: Boolean) {
        dataStore.edit { preferences ->
            val current = preferences[ENABLED_METRICS_KEY] ?: ALL_METRIC_KEYS
            preferences[ENABLED_METRICS_KEY] =
                if (enabled) current + key else current - key
        }
    }

    suspend fun setSyncRangeDays(days: Int) {
        dataStore.edit { it[SYNC_RANGE_DAYS_KEY] = days }
    }

    companion object {
        val ALL_METRIC_KEYS = setOf(
            "heart_rate",
            "resting_heart_rate",
            "sleep",
            "hrv",
            "steps",
            "distance",
            "active_calories",
            "spo2",
            "weight",
            "workouts",
            "blood_pressure",
            "temperature",
            "vo2_max",
        )

        private val LAST_SYNC_KEY = stringPreferencesKey("last_sync_time")
        private val LAST_BACKGROUND_SYNC_KEY = stringPreferencesKey("last_background_sync_time")
        private val LAST_SYNC_STATUS_KEY = stringPreferencesKey("last_sync_status")
        private val LAST_SYNC_MESSAGE_KEY = stringPreferencesKey("last_sync_message")
        private val LAST_BACKGROUND_SYNC_STATUS_KEY =
            stringPreferencesKey("last_background_sync_status")
        private val LAST_BACKGROUND_SYNC_MESSAGE_KEY =
            stringPreferencesKey("last_background_sync_message")
        private val AUTO_SYNC_KEY = stringPreferencesKey("auto_sync")
        private val ENABLED_METRICS_KEY = stringSetPreferencesKey("enabled_metrics")
        private val SYNC_RANGE_DAYS_KEY = intPreferencesKey("sync_range_days")
    }
}
