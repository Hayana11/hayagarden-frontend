package com.bettermifitness.sync.sync

/**
 * Stable, user-facing diagnostic values. They intentionally differ from the
 * WorkManager Result object so the Settings page can explain what happened.
 */
object WorkerDiagnosticOutcome {
    const val SUCCESS = "success"
    const val PARTIAL = "partial"
    const val SKIPPED = "skipped"
    const val RETRY = "retry"
    const val FAILED = "failed"
}

enum class WorkerResultKind {
    SUCCESS,
    RETRY,
    FAILURE,
}

data class WorkerOutcomeMapping(
    val result: WorkerResultKind,
    val diagnosticOutcome: String,
    val error: String? = null,
)

fun SyncOutcome.toWorkerOutcomeMapping(): WorkerOutcomeMapping = when (this) {
    SyncOutcome.Success -> WorkerOutcomeMapping(
        result = WorkerResultKind.SUCCESS,
        diagnosticOutcome = WorkerDiagnosticOutcome.SUCCESS,
    )
    is SyncOutcome.PartialSuccess -> WorkerOutcomeMapping(
        result = WorkerResultKind.SUCCESS,
        diagnosticOutcome = WorkerDiagnosticOutcome.PARTIAL,
    )
    SyncOutcome.Skipped,
    SyncOutcome.NotLoggedIn,
    SyncOutcome.HealthUnavailable,
    SyncOutcome.AlreadyRunning,
    -> WorkerOutcomeMapping(
        result = WorkerResultKind.SUCCESS,
        diagnosticOutcome = WorkerDiagnosticOutcome.SKIPPED,
    )
    is SyncOutcome.Failed -> WorkerOutcomeMapping(
        result = if (shouldRetryBackground()) WorkerResultKind.RETRY else WorkerResultKind.FAILURE,
        diagnosticOutcome = if (shouldRetryBackground()) {
            WorkerDiagnosticOutcome.RETRY
        } else {
            WorkerDiagnosticOutcome.FAILED
        },
        error = message,
    )
}
