package com.bettermifitness.sync.sync

data class WorkerDiagnosticsRecord(
    val lastWorkerStartedAt: String? = null,
    val lastWorkerFinishedAt: String? = null,
    val lastWorkerOutcome: String? = null,
    val lastWorkerError: String? = null,
)

fun WorkerDiagnosticsRecord.recordStarted(timestamp: String): WorkerDiagnosticsRecord =
    copy(lastWorkerStartedAt = timestamp)

fun WorkerDiagnosticsRecord.recordFinished(
    timestamp: String,
    outcome: String,
    error: String?,
): WorkerDiagnosticsRecord = copy(
    lastWorkerFinishedAt = timestamp,
    lastWorkerOutcome = outcome,
    lastWorkerError = error,
)
