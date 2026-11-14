package com.bettermifitness.sync.sync

import kotlin.test.Test
import kotlin.test.assertEquals

class WorkerDiagnosticsTest {
    @Test
    fun resultMapping_preservesSuccessPartialSkippedRetryAndFailure() {
        assertEquals(
            WorkerOutcomeMapping(WorkerResultKind.SUCCESS, WorkerDiagnosticOutcome.SUCCESS),
            SyncOutcome.Success.toWorkerOutcomeMapping(),
        )
        assertEquals(
            WorkerOutcomeMapping(WorkerResultKind.SUCCESS, WorkerDiagnosticOutcome.PARTIAL),
            SyncOutcome.PartialSuccess(1, 1, "one failed").toWorkerOutcomeMapping(),
        )
        assertEquals(
            WorkerOutcomeMapping(WorkerResultKind.SUCCESS, WorkerDiagnosticOutcome.SKIPPED),
            SyncOutcome.Skipped.toWorkerOutcomeMapping(),
        )
        assertEquals(
            WorkerOutcomeMapping(WorkerResultKind.RETRY, WorkerDiagnosticOutcome.RETRY, "timeout"),
            SyncOutcome.Failed("timeout", retryable = true).toWorkerOutcomeMapping(),
        )
        assertEquals(
            WorkerOutcomeMapping(WorkerResultKind.FAILURE, WorkerDiagnosticOutcome.FAILED, "auth"),
            SyncOutcome.Failed("auth", retryable = false).toWorkerOutcomeMapping(),
        )
    }

    @Test
    fun diagnosticsRecord_tracksStartedAndFinishedStates() {
        var record = WorkerDiagnosticsRecord()
        record = record.recordStarted("2026-10-04T01:00:00Z")
        assertEquals("2026-10-04T01:00:00Z", record.lastWorkerStartedAt)
        assertEquals(null, record.lastWorkerFinishedAt)

        record = record.recordFinished(
            timestamp = "2026-10-04T01:00:03Z",
            outcome = WorkerDiagnosticOutcome.SUCCESS,
            error = null,
        )
        assertEquals("2026-10-04T01:00:03Z", record.lastWorkerFinishedAt)
        assertEquals(WorkerDiagnosticOutcome.SUCCESS, record.lastWorkerOutcome)
        assertEquals(null, record.lastWorkerError)

        record = record.recordFinished(
            timestamp = "2026-10-04T01:01:03Z",
            outcome = WorkerDiagnosticOutcome.RETRY,
            error = "network timeout",
        )
        assertEquals(WorkerDiagnosticOutcome.RETRY, record.lastWorkerOutcome)
        assertEquals("network timeout", record.lastWorkerError)
    }
}
