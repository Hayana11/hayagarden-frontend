package com.bettermifitness.sync.health

import com.bettermifitness.sync.data.api.WorkoutSession
import com.bettermifitness.sync.data.api.WorkoutTimedSample
import kotlin.math.abs

/**
 * Resolves form metric series for health writers without inventing power/GCT/VO.
 * Prefer series already filled by [WorkoutRunningMetrics.enrich]; fall back to summaries.
 */
object WorkoutFormSeries {

    fun speed(session: WorkoutSession): List<WorkoutTimedSample> {
        if (session.speedSeries.size >= 2) return session.speedSeries
        val mps = session.maxSpeedMps?.takeIf { it > 0.3 }
            ?: session.avgPaceSecPerKm?.takeIf { it in 60.0..3600.0 }?.let { 1000.0 / it }
            ?: return emptyList()
        return WorkoutRunningMetrics.flatSeries(session.startTime, session.endTime, mps)
    }

    fun strideMeters(session: WorkoutSession): List<WorkoutTimedSample> {
        if (session.strideMetersSeries.size >= 2) return session.strideMetersSeries
        val cm = session.avgStrideCm?.takeIf { it in 20.0..250.0 } ?: return emptyList()
        return WorkoutRunningMetrics.flatSeries(session.startTime, session.endTime, cm / 100.0)
    }

    fun cadence(session: WorkoutSession): List<WorkoutTimedSample> {
        if (session.cadenceSeries.size >= 2) return session.cadenceSeries
        val spm = session.avgCadenceSpm?.takeIf { it in 40.0..250.0 } ?: return emptyList()
        return WorkoutRunningMetrics.flatSeries(session.startTime, session.endTime, spm)
    }

    /**
     * Heart rate ~1 minute after workout end, for HK HeartRateRecoveryOneMinute.
     */
    fun recoverOneMinuteBpm(session: WorkoutSession): Double? {
        val series = session.recoverHeartRateSeries
        if (series.isEmpty()) return null
        val target = session.endTime + 60
        val nearest = series.minByOrNull { abs(it.timeSec - target) } ?: return null
        if (abs(nearest.timeSec - target) > 90) return null
        return nearest.value.takeIf { it in 30.0..250.0 }
    }

    fun contentVersion(session: WorkoutSession, vararg extra: Any?): Long =
        HealthRecordIds.version(
            session.startTime,
            session.endTime,
            session.activityType,
            session.distanceMeters,
            session.caloriesKcal,
            session.route.size,
            session.heartRateSeries.size,
            session.avgPaceSecPerKm,
            session.avgCadenceSpm,
            session.trainLoad,
            session.speedSeries.size,
            session.cadenceSeries.size,
            session.strideMetersSeries.size,
            session.powerWattsSeries.size,
            session.groundContactMsSeries.size,
            session.verticalOscillationCmSeries.size,
            *extra,
        )
}
