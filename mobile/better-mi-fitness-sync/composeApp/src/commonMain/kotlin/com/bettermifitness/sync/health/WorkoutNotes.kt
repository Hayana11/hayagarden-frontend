package com.bettermifitness.sync.health

import com.bettermifitness.sync.data.api.WorkoutSession

/**
 * Human-readable workout summary for Health notes metadata.
 * Shared by HealthKit and Health Connect writers (SRP: presentation only).
 */
object WorkoutNotes {

    fun build(session: WorkoutSession): String? {
        val parts = mutableListOf<String>()
        session.avgPaceSecPerKm?.let { parts += "avg pace ${formatPace(it)}/km" }
        session.avgCadenceSpm?.let {
            val unit = when (WorkoutMetricFamilies.classify(session.activityType)) {
                WorkoutMetricFamily.CYCLING -> "rpm"
                else -> "spm"
            }
            parts += "cadence ${it.toInt()} $unit"
        }
        session.avgStrideCm?.let { parts += "stride ${it.toInt()} cm" }
            ?: session.strideMetersSeries.firstOrNull()?.let {
                parts += "stride ${(it.value * 100).toInt()} cm"
            }
        session.maxSpeedMps?.let {
            val kmh = (it * 3.6 * 10).toInt() / 10.0
            parts += "max $kmh km/h"
        }
        session.avgPowerWatts?.let { parts += "power ${it.toInt()} W" }
        session.avgGroundContactMs?.let { parts += "GCT ${it.toInt()} ms" }
        session.avgVerticalOscillationCm?.let {
            val tenths = ((it * 10).toInt()) / 10.0
            parts += "VO $tenths cm"
        }
        session.trainEffect?.let { parts += "TE $it" }
        session.trainLoad?.let { parts += "load ${it.toInt()}" }
        session.recoverMinutes?.let { parts += "recover ${it}m" }
        session.vo2Max?.let { parts += "VO2 $it" }
        val zones = listOfNotNull(
            session.hrZoneWarmupSec?.let { "WU${it}s" },
            session.hrZoneFatBurnSec?.let { "FB${it}s" },
            session.hrZoneAerobicSec?.let { "AE${it}s" },
            session.hrZoneAnaerobicSec?.let { "AN${it}s" },
            session.hrZoneExtremeSec?.let { "EX${it}s" },
        )
        if (zones.isNotEmpty()) parts += zones.joinToString("/")
        if (session.kmSplits.isNotEmpty()) parts += "${session.kmSplits.size} km splits"
        return parts.takeIf { it.isNotEmpty() }?.joinToString(" · ")
    }

    fun formatPace(secPerKm: Double): String {
        val s = secPerKm.toInt().coerceAtLeast(0)
        val min = s / 60
        val sec = s % 60
        return "$min:${sec.toString().padStart(2, '0')}"
    }
}
