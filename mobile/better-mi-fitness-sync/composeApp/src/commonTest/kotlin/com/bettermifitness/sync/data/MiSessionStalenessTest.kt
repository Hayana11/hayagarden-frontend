package com.bettermifitness.sync.data

import com.bettermifitness.sync.data.MiSessionManager.Companion.PROACTIVE_REFRESH_AFTER
import com.bettermifitness.sync.data.MiSessionManager.Companion.isSessionStale
import kotlin.test.Test
import kotlin.test.assertFalse
import kotlin.test.assertTrue
import kotlin.time.Duration.Companion.days
import kotlin.time.Duration.Companion.hours

/**
 * Policy for proactive passport refresh.
 *
 * Xiaomi rotates passToken on every `serviceLogin` and retires the superseded one, so a
 * session that is never refreshed (long-lived serviceToken → no 401 → no lazy refresh)
 * can wake up holding a dead token. These cases guard the cadence decision.
 */
class MiSessionStalenessTest {

    private val now = 1_800_000_000L

    @Test
    fun freshSession_isNotStale() {
        assertFalse(isSessionStale(now - 1.hours.inWholeSeconds, now))
        assertFalse(isSessionStale(now - 23.hours.inWholeSeconds, now))
        assertFalse(isSessionStale(now, now))
    }

    @Test
    fun atAndBeyondThreshold_isStale() {
        assertTrue(isSessionStale(now - PROACTIVE_REFRESH_AFTER.inWholeSeconds, now))
        assertTrue(isSessionStale(now - 5.days.inWholeSeconds, now))
    }

    @Test
    fun missingStamp_isStale() {
        // Upgrade from a build that never recorded refresh time: start the rotation chain.
        assertTrue(isSessionStale(null, now))
    }

    @Test
    fun stampInFuture_isStale() {
        // Device clock moved backwards; do not wait out a bogus interval.
        assertTrue(isSessionStale(now + 30.days.inWholeSeconds, now))
    }

    @Test
    fun customMaxAgeIsHonoured() {
        val lastRefresh = now - 2.hours.inWholeSeconds
        assertFalse(isSessionStale(lastRefresh, now, maxAge = 6.hours))
        assertTrue(isSessionStale(lastRefresh, now, maxAge = 1.hours))
    }

    @Test
    fun defaultThresholdIsOneDay() {
        assertTrue(PROACTIVE_REFRESH_AFTER == 1.days)
    }
}
