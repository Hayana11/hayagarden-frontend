package com.bettermifitness.sync

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlinx.coroutines.runBlocking

class AutoSyncScheduleTest {
    @Test
    fun autoSyncOn_schedulesPeriodicWork() = runBlocking {
        var scheduled = 0
        var cancelled = 0
        AutoSyncScheduleRestorer({ true }, { scheduled++ }, { cancelled++ }).restore()
        assertEquals(1, scheduled)
        assertEquals(0, cancelled)
    }

    @Test
    fun autoSyncOff_cancelsPeriodicWork() = runBlocking {
        var scheduled = 0
        var cancelled = 0
        AutoSyncScheduleRestorer({ false }, { scheduled++ }, { cancelled++ }).restore()
        assertEquals(0, scheduled)
        assertEquals(1, cancelled)
    }

    @Test
    fun repeatedRestore_reusesTheSameSchedulerHook() = runBlocking {
        var scheduled = 0
        val restorer = AutoSyncScheduleRestorer({ true }, { scheduled++ }, {})
        repeat(3) { restorer.restore() }
        // Android uses enqueueUniquePeriodicWork with UPDATE, so this hook
        // updates one unique task instead of creating duplicate workers.
        assertEquals(3, scheduled)
    }

    @Test
    fun preferenceReadFailure_failsClosedAndCancels() = runBlocking {
        var cancelled = 0
        AutoSyncScheduleRestorer({ error("DataStore unavailable") }, {}, { cancelled++ }).restore()
        assertEquals(1, cancelled)
    }
}
