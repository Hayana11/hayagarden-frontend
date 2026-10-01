package com.bettermifitness.sync.data

import com.bettermifitness.sync.data.api.MiDirectApi
import com.bettermifitness.sync.data.preferences.CredentialsPort
import com.bettermifitness.sync.data.preferences.SyncSessionPort
import com.mifitness.miclient.api.MiDataClient
import com.mifitness.miclient.auth.MiAuth
import com.mifitness.miclient.auth.MiAuthException
import com.mifitness.miclient.auth.MiCredentials
import kotlin.time.Clock
import kotlin.time.Duration
import kotlin.time.Duration.Companion.days
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/**
 * Holds the current Mi session (authenticated API client).
 * Can re-mint serviceToken via [refreshSession] using the stored passToken (APK force-refresh).
 */
class MiSessionManager(
    private val credentialsStore: CredentialsPort,
    private val miAuth: MiAuth,
) : SyncSessionPort {
    private var dataClient: MiDataClient? = null
    private var directApi: MiDirectApi? = null
    private val refreshMutex = Mutex()

    /** The active API client. Throws if not logged in. */
    val api: MiDirectApi
        get() = directApi ?: throw IllegalStateException("Not logged in — call activate() first")

    val isActive: Boolean get() = directApi != null

    /** Activates the session with fresh credentials (called after login). */
    fun activate(credentials: MiCredentials) {
        dataClient?.close()
        dataClient = MiDataClient(credentials)
        directApi = MiDirectApi(dataClient!!)
    }

    /** Clears the session (called on logout). */
    fun clear() {
        dataClient?.close()
        dataClient = null
        directApi = null
    }

    /**
     * Ensures an in-memory session exists from persisted credentials.
     * @return false if there are no saved credentials
     */
    override suspend fun ensureActive(): Boolean {
        if (isActive) return true
        val creds = credentialsStore.loadCredentials() ?: return false
        if (creds.serviceToken.isBlank() || creds.userId.isBlank() || creds.ssecurity.isBlank()) {
            return false
        }
        activate(creds)
        return true
    }

    /**
     * Rebuilds the in-memory client from persisted credentials (e.g. after region change).
     * @return false if not signed in
     */
    suspend fun reloadFromStore(): Boolean {
        val creds = credentialsStore.loadCredentials() ?: return false
        activate(creds)
        return true
    }

    /**
     * Uses passToken to obtain a new serviceToken, persists it, and re-activates the client.
     * Serialized so concurrent metric failures do not stampede passport (APK update lock).
     */
    suspend fun refreshSessionDetailed(): SessionRefreshResult = refreshMutex.withLock {
        val current = credentialsStore.loadCredentials()
            ?: return@withLock SessionRefreshResult.NeedsReLogin("No saved credentials")
        if (current.passToken.isBlank()) {
            return@withLock SessionRefreshResult.NeedsReLogin(
                "No passToken saved — sign in again to stay connected across days",
            )
        }
        if (current.deviceId.isBlank()) {
            return@withLock SessionRefreshResult.NeedsReLogin(
                "No deviceId saved — sign in again",
            )
        }

        return@withLock try {
            val refreshed = miAuth.refreshWithPassToken(current)
            credentialsStore.saveCredentials(refreshed)
            val effective = credentialsStore.loadCredentials() ?: refreshed
            activate(effective)
            SessionRefreshResult.Success
        } catch (e: MiAuthException) {
            when (e.kind) {
                MiAuthException.Kind.MissingPassToken,
                MiAuthException.Kind.MissingDeviceId,
                MiAuthException.Kind.InvalidCredential,
                -> SessionRefreshResult.NeedsReLogin(e.message ?: "Sign in again")
                MiAuthException.Kind.NeedsVerification ->
                    SessionRefreshResult.NeedsVerification(
                        reason = e.message ?: "Xiaomi requires re-verification",
                        notificationUrl = e.notificationUrl,
                    )
                MiAuthException.Kind.StsFailed,
                MiAuthException.Kind.Generic,
                -> SessionRefreshResult.TransientFailure(e.message ?: "Session refresh failed")
            }
        } catch (e: Exception) {
            SessionRefreshResult.TransientFailure(
                e.message?.takeIf { it.isNotBlank() } ?: (e::class.simpleName ?: "Refresh error"),
            )
        }
    }

    /**
     * @return true if refresh succeeded
     */
    suspend fun refreshSession(): Boolean = refreshSessionDetailed().isSuccess

    /**
     * Keeps the passToken rotation chain alive on installs that rarely hit a 401.
     *
     * Xiaomi hands back a rotated passToken on every `serviceLogin` and retires the previous
     * one shortly after. Lazy 401-only refresh is enough while the app syncs often, but a
     * long-valid serviceToken means no refresh happens at all — and by the time one is needed
     * the stored passToken can already be dead (code 70016 → forced re-login).
     *
     * Runs at most once per [PROACTIVE_REFRESH_AFTER]. Failures are swallowed on purpose:
     * this is opportunistic, and the lazy path still reports real problems to the user.
     */
    override suspend fun refreshSessionIfStale() {
        val stale = isSessionStale(
            lastRefreshEpochSeconds = credentialsStore.lastSessionRefreshEpochSeconds(),
            nowEpochSeconds = Clock.System.now().epochSeconds,
        )
        if (!stale) return

        val current = credentialsStore.loadCredentials() ?: return
        if (current.passToken.isBlank() || current.deviceId.isBlank()) return

        refreshSessionDetailed()
    }

    companion object {
        /**
         * How long a session may go unrefreshed before we roll it forward.
         * Well inside Xiaomi's retirement window for a superseded passToken.
         */
        val PROACTIVE_REFRESH_AFTER: Duration = 1.days

        /**
         * Whether the passport session should be re-minted.
         *
         * A missing stamp counts as stale: credentials were stored by a build without the
         * key, so the rotation chain has never been exercised and should start now.
         * A stamp in the future means the device clock moved backwards — refresh instead of
         * waiting out a bogus interval that could outlive the passToken.
         */
        fun isSessionStale(
            lastRefreshEpochSeconds: Long?,
            nowEpochSeconds: Long,
            maxAge: Duration = PROACTIVE_REFRESH_AFTER,
        ): Boolean {
            val age = lastRefreshEpochSeconds?.let { nowEpochSeconds - it } ?: return true
            return age < 0 || age >= maxAge.inWholeSeconds
        }
    }
}
