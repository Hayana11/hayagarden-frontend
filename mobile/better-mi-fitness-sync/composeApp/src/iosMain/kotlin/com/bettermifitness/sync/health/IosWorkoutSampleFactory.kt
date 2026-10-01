package com.bettermifitness.sync.health

import com.bettermifitness.sync.data.api.WorkoutSession
import com.bettermifitness.sync.data.api.WorkoutTimedSample
import kotlinx.cinterop.ExperimentalForeignApi
import platform.Foundation.NSDate
import platform.Foundation.dateWithTimeIntervalSince1970
import platform.HealthKit.HKAuthorizationStatusSharingAuthorized
import platform.HealthKit.HKAuthorizationStatusSharingDenied
import platform.HealthKit.HKHealthStore
import platform.HealthKit.HKMetadataKeySyncIdentifier
import platform.HealthKit.HKMetadataKeySyncVersion
import platform.HealthKit.HKQuantity
import platform.HealthKit.HKQuantitySample
import platform.HealthKit.HKQuantityType
import platform.HealthKit.HKQuantityTypeIdentifierActiveEnergyBurned
import platform.HealthKit.HKQuantityTypeIdentifierCyclingCadence
import platform.HealthKit.HKQuantityTypeIdentifierCyclingPower
import platform.HealthKit.HKQuantityTypeIdentifierCyclingSpeed
import platform.HealthKit.HKQuantityTypeIdentifierDistanceCycling
import platform.HealthKit.HKQuantityTypeIdentifierDistanceSwimming
import platform.HealthKit.HKQuantityTypeIdentifierDistanceWalkingRunning
import platform.HealthKit.HKQuantityTypeIdentifierFlightsClimbed
import platform.HealthKit.HKQuantityTypeIdentifierHeartRate
import platform.HealthKit.HKQuantityTypeIdentifierHeartRateRecoveryOneMinute
import platform.HealthKit.HKQuantityTypeIdentifierRunningGroundContactTime
import platform.HealthKit.HKQuantityTypeIdentifierRunningPower
import platform.HealthKit.HKQuantityTypeIdentifierRunningSpeed
import platform.HealthKit.HKQuantityTypeIdentifierRunningStrideLength
import platform.HealthKit.HKQuantityTypeIdentifierRunningVerticalOscillation
import platform.HealthKit.HKQuantityTypeIdentifierStepCount
import platform.HealthKit.HKQuantityTypeIdentifierVO2Max
import platform.HealthKit.HKQuantityTypeIdentifierWalkingSpeed
import platform.HealthKit.HKQuantityTypeIdentifierWalkingStepLength
import platform.HealthKit.HKUnit

/**
 * Builds HealthKit quantity samples for a workout (SRP: sample construction only).
 * Does not save or associate — that is [IosWorkoutImporter].
 */
@OptIn(ExperimentalForeignApi::class)
class IosWorkoutSampleFactory(
    private val healthStore: HKHealthStore,
) {

    fun allSamples(session: WorkoutSession, version: Long): List<HKQuantitySample> =
        coreSamples(session, version) + formSamples(session, version)

    fun coreSamples(session: WorkoutSession, version: Long): List<HKQuantitySample> {
        val out = ArrayList<HKQuantitySample>()
        val idPrefix = HealthRecordIds.workout(session.startTime)
        val start = epochDate(session.startTime)
        val end = epochDate(session.endTime)
        val family = WorkoutMetricFamilies.classify(session.activityType)
        val bpm = HKUnit.unitFromString("count/min")

        session.caloriesKcal?.takeIf { it > 0 }?.let { cals ->
            authorizedSample(
                typeId = typeId(HKQuantityTypeIdentifierActiveEnergyBurned, "HKQuantityTypeIdentifierActiveEnergyBurned"),
                unitStr = "kcal",
                value = cals,
                start = start,
                end = end,
                syncId = "$idPrefix:v3:energy",
                version = version,
                requireAuthorized = true,
            )?.let { out += it }
        }

        session.distanceMeters?.takeIf { it > 0 }?.let { dist ->
            val distType = distanceTypeId(family)
            authorizedSample(
                typeId = distType,
                unitStr = "m",
                value = dist,
                start = start,
                end = end,
                syncId = "$idPrefix:v3:dist",
                version = version,
                requireAuthorized = true,
            )?.let { out += it }
        }

        val hrType = HKQuantityType.quantityTypeForIdentifier(HKQuantityTypeIdentifierHeartRate)
        if (hrType != null) {
            for (s in session.heartRateSeries) {
                val date = epochDate(s.timeSec)
                out += HKQuantitySample.quantitySampleWithType(
                    quantityType = hrType,
                    quantity = HKQuantity.quantityWithUnit(bpm, s.value),
                    startDate = date,
                    endDate = date,
                    metadata = syncMeta("$idPrefix:v3:hr:${s.timeSec}", version),
                )
            }
        }

        if (family.usesSteps()) {
            val stepType = HKQuantityType.quantityTypeForIdentifier(HKQuantityTypeIdentifierStepCount)
            val steps = session.totalSteps ?: 0
            if (stepType != null && steps > 0) {
                out += HKQuantitySample.quantitySampleWithType(
                    quantityType = stepType,
                    quantity = HKQuantity.quantityWithUnit(HKUnit.unitFromString("count"), steps.toDouble()),
                    startDate = start,
                    endDate = end,
                    metadata = syncMeta("$idPrefix:v3:steps", version),
                )
            }
        }
        return out
    }

    fun formSamples(session: WorkoutSession, version: Long): List<HKQuantitySample> {
        val out = ArrayList<HKQuantitySample>()
        val idPrefix = HealthRecordIds.workout(session.startTime)
        val start = epochDate(session.startTime)
        val end = epochDate(session.endTime)
        val family = WorkoutMetricFamilies.classify(session.activityType)
        val speed = WorkoutFormSeries.speed(session)
        val stride = WorkoutFormSeries.strideMeters(session)
        val cadence = WorkoutFormSeries.cadence(session)

        when (family) {
            WorkoutMetricFamily.RUNNING -> {
                out += series(
                    typeId(HKQuantityTypeIdentifierRunningSpeed, "HKQuantityTypeIdentifierRunningSpeed"),
                    "m/s", speed, "$idPrefix:v3:rspeed", version,
                )
                out += series(
                    typeId(HKQuantityTypeIdentifierRunningStrideLength, "HKQuantityTypeIdentifierRunningStrideLength"),
                    "m", stride, "$idPrefix:v3:stride", version,
                )
                out += series(
                    typeId(HKQuantityTypeIdentifierRunningPower, "HKQuantityTypeIdentifierRunningPower"),
                    "W", session.powerWattsSeries, "$idPrefix:v3:rpow", version,
                )
                out += series(
                    typeId(HKQuantityTypeIdentifierRunningGroundContactTime, "HKQuantityTypeIdentifierRunningGroundContactTime"),
                    "ms", session.groundContactMsSeries, "$idPrefix:v3:gct", version,
                )
                out += series(
                    typeId(HKQuantityTypeIdentifierRunningVerticalOscillation, "HKQuantityTypeIdentifierRunningVerticalOscillation"),
                    "cm", session.verticalOscillationCmSeries, "$idPrefix:v3:vo", version,
                )
            }
            WorkoutMetricFamily.WALKING -> {
                out += series(
                    typeId(HKQuantityTypeIdentifierWalkingSpeed, "HKQuantityTypeIdentifierWalkingSpeed"),
                    "m/s", speed, "$idPrefix:v3:wspeed", version,
                )
                out += series(
                    typeId(HKQuantityTypeIdentifierWalkingStepLength, "HKQuantityTypeIdentifierWalkingStepLength"),
                    "m", stride, "$idPrefix:v3:wstep", version,
                )
            }
            WorkoutMetricFamily.CYCLING -> {
                out += series(
                    typeId(HKQuantityTypeIdentifierCyclingCadence, "HKQuantityTypeIdentifierCyclingCadence"),
                    "count/min", cadence, "$idPrefix:v3:ccad", version,
                )
                out += series(
                    typeId(HKQuantityTypeIdentifierCyclingSpeed, "HKQuantityTypeIdentifierCyclingSpeed"),
                    "m/s", speed, "$idPrefix:v3:cspd", version,
                )
                out += series(
                    typeId(HKQuantityTypeIdentifierCyclingPower, "HKQuantityTypeIdentifierCyclingPower"),
                    "W", session.powerWattsSeries, "$idPrefix:v3:cpow", version,
                )
            }
            WorkoutMetricFamily.SWIMMING, WorkoutMetricFamily.OTHER -> Unit
        }

        session.vo2Max?.takeIf { it in 10.0..100.0 }?.let { vo2 ->
            authorizedSample(
                typeId = typeId(HKQuantityTypeIdentifierVO2Max, "HKQuantityTypeIdentifierVO2Max"),
                unitStr = "ml/(kg*min)",
                value = vo2,
                start = end,
                end = end,
                syncId = "$idPrefix:v3:vo2",
                version = version,
                requireAuthorized = true,
            )?.let { out += it }
        }

        WorkoutFormSeries.recoverOneMinuteBpm(session)?.let { bpm1 ->
            val t1 = epochDate(session.endTime + 60)
            authorizedSample(
                typeId = typeId(
                    HKQuantityTypeIdentifierHeartRateRecoveryOneMinute,
                    "HKQuantityTypeIdentifierHeartRateRecoveryOneMinute",
                ),
                unitStr = "count/min",
                value = bpm1,
                start = t1,
                end = t1,
                syncId = "$idPrefix:v3:hrr1",
                version = version,
                requireAuthorized = true,
            )?.let { out += it }
        }

        session.elevationGainM?.takeIf { it >= 3.0 }?.let { gain ->
            val flights = (gain / 3.0).toInt().coerceAtLeast(1)
            authorizedSample(
                typeId = typeId(HKQuantityTypeIdentifierFlightsClimbed, "HKQuantityTypeIdentifierFlightsClimbed"),
                unitStr = "count",
                value = flights.toDouble(),
                start = start,
                end = end,
                syncId = "$idPrefix:v3:flights",
                version = version,
                requireAuthorized = true,
            )?.let { out += it }
        }
        return out
    }

    fun recoverHeartRateSamples(session: WorkoutSession, version: Long): List<HKQuantitySample> {
        val hrType = HKQuantityType.quantityTypeForIdentifier(HKQuantityTypeIdentifierHeartRate)
            ?: return emptyList()
        if (session.recoverHeartRateSeries.isEmpty()) return emptyList()
        val bpm = HKUnit.unitFromString("count/min")
        val idPrefix = HealthRecordIds.workout(session.startTime)
        return session.recoverHeartRateSeries.map { s ->
            val date = epochDate(s.timeSec)
            HKQuantitySample.quantitySampleWithType(
                quantityType = hrType,
                quantity = HKQuantity.quantityWithUnit(bpm, s.value),
                startDate = date,
                endDate = date,
                metadata = syncMeta("$idPrefix:rhr:${s.timeSec}", version),
            )
        }
    }

    fun workoutMetadata(session: WorkoutSession, version: Long): Map<Any?, Any?> {
        val meta = mutableMapOf<Any?, Any?>(
            HKMetadataKeySyncIdentifier to HealthRecordIds.workout(session.startTime),
            HKMetadataKeySyncVersion to version,
        )
        session.avgHeartRateBpm?.let { meta["MiAvgHeartRate"] = it }
        session.maxHeartRateBpm?.let { meta["MiMaxHeartRate"] = it }
        session.minHeartRateBpm?.let { meta["MiMinHeartRate"] = it }
        WorkoutNotes.build(session)?.let { meta["MiFitnessNotes"] = it }
        return meta
    }

    private fun series(
        typeId: String,
        unitStr: String,
        samples: List<WorkoutTimedSample>,
        idPrefix: String,
        version: Long,
    ): List<HKQuantitySample> {
        if (samples.isEmpty()) return emptyList()
        val type = HKQuantityType.quantityTypeForIdentifier(typeId) ?: return emptyList()
        if (healthStore.authorizationStatusForType(type) == HKAuthorizationStatusSharingDenied) {
            return emptyList()
        }
        val unit = HKUnit.unitFromString(unitStr)
        return samples.map { s ->
            val date = epochDate(s.timeSec)
            HKQuantitySample.quantitySampleWithType(
                quantityType = type,
                quantity = HKQuantity.quantityWithUnit(unit, s.value),
                startDate = date,
                endDate = date,
                metadata = syncMeta("$idPrefix:${s.timeSec}", version),
            )
        }
    }

    private fun authorizedSample(
        typeId: String,
        unitStr: String,
        value: Double,
        start: NSDate,
        end: NSDate,
        syncId: String,
        version: Long,
        requireAuthorized: Boolean,
    ): HKQuantitySample? {
        val type = HKQuantityType.quantityTypeForIdentifier(typeId) ?: return null
        val status = healthStore.authorizationStatusForType(type)
        if (requireAuthorized && status != HKAuthorizationStatusSharingAuthorized) return null
        if (status == HKAuthorizationStatusSharingDenied) return null
        val unit = HKUnit.unitFromString(unitStr)
        return HKQuantitySample.quantitySampleWithType(
            quantityType = type,
            quantity = HKQuantity.quantityWithUnit(unit, value),
            startDate = start,
            endDate = end,
            metadata = syncMeta(syncId, version),
        )
    }

    private fun distanceTypeId(family: WorkoutMetricFamily): String = when (family) {
        WorkoutMetricFamily.CYCLING ->
            typeId(HKQuantityTypeIdentifierDistanceCycling, "HKQuantityTypeIdentifierDistanceCycling")
        WorkoutMetricFamily.SWIMMING ->
            typeId(HKQuantityTypeIdentifierDistanceSwimming, "HKQuantityTypeIdentifierDistanceSwimming")
        else ->
            typeId(HKQuantityTypeIdentifierDistanceWalkingRunning, "HKQuantityTypeIdentifierDistanceWalkingRunning")
    }

    private fun typeId(constant: String?, fallback: String): String = constant ?: fallback

    private fun syncMeta(syncId: String, version: Long): Map<Any?, Any?> =
        mapOf(
            HKMetadataKeySyncIdentifier to syncId,
            HKMetadataKeySyncVersion to version,
        )

    private fun epochDate(sec: Long): NSDate =
        NSDate.dateWithTimeIntervalSince1970(sec.toDouble())
}

private fun WorkoutMetricFamily.usesSteps(): Boolean =
    this == WorkoutMetricFamily.RUNNING ||
        this == WorkoutMetricFamily.WALKING ||
        this == WorkoutMetricFamily.OTHER
