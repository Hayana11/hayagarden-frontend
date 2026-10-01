package com.bettermifitness.sync.data.preferences

import com.mifitness.miclient.auth.MiCredentials
import kotlinx.coroutines.flow.Flow

/** ISP: auth token + credentials used by sync policy. */
interface CredentialsPort {
    val token: Flow<String?>
    suspend fun loadCredentials(): MiCredentials?
    suspend fun saveCredentials(credentials: MiCredentials)

    /**
     * Epoch seconds of the last successful login or passport refresh, or null when unknown
     * (never signed in, or credentials stored by a build that predates this key).
     *
     * Used to roll the passport session forward before Xiaomi retires the current passToken.
     */
    suspend fun lastSessionRefreshEpochSeconds(): Long? = null

    /**
     * Stable device identity used across login, OTP, browser URL, and refresh.
     * Default is empty for fakes; the DataStore implementation generates once.
     */
    suspend fun loadDeviceId(): String = ""

    /** Same as [loadDeviceId] but generates and persists when missing. */
    suspend fun ensureDeviceId(): String = loadDeviceId()
}

/** ISP: sync settings + last-run outcome persistence. */
interface SyncPreferencesPort {
    val autoSync: Flow<Boolean>
    val enabledMetrics: Flow<Set<String>>
    val syncRangeDays: Flow<Int>
    suspend fun updateLastSync(timestamp: String)
    suspend fun updateLastBackgroundSync(timestamp: String)
    suspend fun updateLastSyncOutcome(status: String, message: String?)
    suspend fun updateLastBackgroundSyncOutcome(status: String, message: String?)
}

/**
 * Single hot snapshot of user sync preferences.
 * [ready] is false until the first DataStore emission lands; screens must hold a
 * skeleton until then instead of rendering zero-metric placeholders.
 */
data class UserPrefsSnapshot(
    val ready: Boolean = false,
    val enabledMetrics: Set<String> = emptySet(),
    val syncRangeDays: Int = 7,
    val autoSync: Boolean = false,
    val lastSyncTime: String? = null,
    val lastBackgroundSyncTime: String? = null,
)

/** ISP: ensure Mi API session is usable. */
interface SyncSessionPort {
    suspend fun ensureActive(): Boolean

    /**
     * Re-mints the session when it has not been refreshed recently.
     *
     * Xiaomi rotates the passToken on every `serviceLogin` and keeps the superseded one
     * alive only briefly, so an install that sits idle (serviceToken still valid, no 401,
     * therefore no lazy refresh) can wake up holding a passToken the server has retired.
     * Refreshing on a cadence keeps the rotation chain alive the way Mi Fitness does.
     *
     * No-op when fresh, not signed in, or when the refresh fails — the lazy 401 path is
     * still responsible for surfacing errors to the user.
     */
    suspend fun refreshSessionIfStale() = Unit
}
